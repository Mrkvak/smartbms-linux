#!/usr/bin/env python3
"""
123\\SmartBMS Linux client.

Talks to a 123\\SmartBMS lithium battery BMS over Bluetooth Low Energy, the same
way the official Android app (123SmartBMS v3.6.7) does. Lets you view live
values and read/write the configuration from a Linux computer.

Protocol was reverse engineered from the app's .NET assemblies. It is a plain
ASCII line protocol carried over a BLE UART:

  * Every command is ASCII terminated with '\\r'.
  * The device echoes the command, then replies.
  * Config reads:   send "V@" / "S1@" / "S2@" / "S3@" / "SRY0@" / "SRY1@"
                    reply is underscore separated hex fields ending in '\\r',
                    last field is an 8-bit checksum.
  * Authorize:      send "PW<pin>!"   -> "OK\\r" or "NA\\r"
  * Config write:   send "<hexvalue>-<address>!"  -> "OK\\r" / "KO\\r"
  * Live stream:    send "E!" to start, "D!" to stop. Device then streams
                    records (U/V/T/C/E/M/H/B), each terminated by ' ' or '\\r'.
  * Keep-alive:     send "$" roughly every 1.6 s while streaming.

Two BLE hardware variants exist and are auto-detected:
  * Raytac  (Gen3, newer) -> Nordic UART Service
  * BlueRadios (Gen2)     -> proprietary UART service

Usage:
  python smartbms.py scan
  python smartbms.py monitor [--pin 0000] [--address AA:BB:..]
  python smartbms.py read    [--pin 0000] [--json]
  python smartbms.py set <param> <value> [--pin 0000]
  python smartbms.py set --list
  python smartbms.py raw "<command>" [--pin 0000]

Requires: bleak  (pip install bleak)
"""
import argparse
import asyncio
import json
import sys
import time

from bleak import BleakClient, BleakScanner

# --------------------------------------------------------------------------
# BLE identifiers (from 123Connection assembly)
# --------------------------------------------------------------------------

# Raytac / Gen3 -> Nordic UART Service
RAYTAC = {
    "service": "6e400001-b5a3-f393-e0a9-e50e24dcca9e",
    "rx":      "6e400002-b5a3-f393-e0a9-e50e24dcca9e",  # write to device
    "tx":      "6e400003-b5a3-f393-e0a9-e50e24dcca9e",  # notify from device
}
# BlueRadios BRSP module (used by both Gen2 and some Gen3 units)
BLUERADIOS = {
    "service": "da2b84f1-6279-48de-bdc0-afbea0226079",
    "rx":      "bf03260c-7205-4c25-af43-93b1c299d159",  # write to device
    "tx":      "18cda784-4bd3-4370-85bb-bfed91ec86af",  # notify (data from device)
    "mode":    "a87988b9-694c-479c-900e-95dfa6c00a24",  # write mode (1 = Data)
    "info":    "99564a02-dc01-4d3c-b04e-3bb1ef0571b2",  # read during init
    "rts":     "fdd6b4d3-046d-4330-bdec-1fd0c90cb43b",  # notify: flow control (device->host)
    "cts":     "0a1934f5-24b8-4f13-9842-37bb167c6aff",  # host->device flow control
}
# BlueRadios module must be switched into "Data" mode for UART passthrough.
BLUERADIOS_MODE_DATA = 1

# BLE manufacturer company identifiers used by the BMS advertisements
COMPANY_IDS = {133: "BlueRadios", 1674: "Raytac", 2330: "Albertronic"}

DEVICE_TYPES = {
    -1: "Unknown", 0: "123\\TUNE", 1: "123\\TUNE+", 2: "123\\SmartBMS",
    3: "123\\OFFGRID", 4: "123\\SmartBMS Gen3 (BlueRadios)",
    5: "123\\TUNE+ (Raytac)", 6: "123\\SmartBMS Gen3 (Raytac)",
}

# --------------------------------------------------------------------------
# Scaling constants (from BMS class)
# --------------------------------------------------------------------------
VOLT_STEP = 0.005            # cell / battery voltage
VOLT_HYST_STEP = 0.01        # voltage hysteresis
VOLT_EXTREME_STEP = 0.02
WATT_STEP = 10               # peak power
WATTHOUR_STEP = 100          # capacity
PAUSE_DELAY_STEP = 2         # seconds
RESTART_DELAY_STEP = 5       # seconds
SYNC_TAIL_STEP = 0.2         # amps
POWER_STEP_GEN3 = 0.05       # current, gen3 (amps per count)
POWER_STEP_GEN2 = 0.125      # current, gen2

SENSOR_TYPES = {-1: "Unknown", 0: "125A/5A", 1: "250A/10A",
                2: "500A/20A", 11: "1000A/40A"}
CHEMISTRIES = {-1: "Unknown", 0: "Other", 1: "LFP", 2: "LTO", 3: "NMC", 4: "NCA"}
RELAY_FUNCTIONS = {-1: "Unknown", 1: "AllowedToCharge", 2: "AllowedToDischarge",
                   3: "MainRelay", 4: "Prealarm"}
RELAY_FORCE_POS = {-1: "Unknown", 0: "Inactive", 1: "KeepOff", 2: "KeepOn"}


# --------------------------------------------------------------------------
# Hex field parsing (mirrors the Hex helper class)
# --------------------------------------------------------------------------
def parse_int(hexstr, xvalue=0):
    """Parse a signed hex field. 'X...' means 'not available' -> xvalue."""
    if not hexstr:
        return xvalue
    if hexstr[0] == 'X':
        return xvalue
    sign = 1
    if hexstr[0] in '+-':
        sign = -1 if hexstr[0] == '-' else 1
        hexstr = hexstr[1:]
    try:
        return int(hexstr, 16) * sign
    except ValueError:
        return 0


def is_na(hexstr):
    return (not hexstr) or hexstr[0] == 'X'


def parse_voltage(hexstr):
    return parse_int(hexstr) * VOLT_STEP


def parse_temperature(hexstr):
    return round(-232.1 + parse_int(hexstr) * 0.857)


def to_temperature(celsius):
    """Inverse of parse_temperature -> raw counts for writing."""
    return round((232.1 + celsius) / 0.857)


def checksum(data):
    """8-bit sum of all characters except '_'."""
    return sum(ord(c) for c in data if c != '_') & 0xFF


def validate_checksum(line):
    fields = line.split('_')
    if len(fields[-1]) != 2:
        return False
    want = parse_int(fields[-1])
    got = checksum(line[:line.rfind('_')])
    return want == got


# --------------------------------------------------------------------------
# Writable configuration parameter map
# --------------------------------------------------------------------------
# Each entry: how to turn a human value into the on-wire integer + address(es).
#   ('byte',  addr,          encode)          -> "<XX>-addr!"
#   ('word',  addr_hi, addr_lo, encode)       -> "<HH>-hi!" then "<LL>-lo!"
# encode() returns the integer to send.
def _v(step):        # voltage-like
    return lambda x: round(float(x) / step)


CONFIG_WRITES = {
    # name: (kind, ..., encode, help)
    "cell_voltage_min":        ("word", "040", "041", _v(VOLT_STEP),
                                "Cell undervoltage limit (V), e.g. 2.8"),
    "cell_voltage_max":        ("word", "050", "051", _v(VOLT_STEP),
                                "Cell overvoltage limit (V), e.g. 3.6"),
    "cell_voltage_min_hyst":   ("byte", "042", _v(VOLT_HYST_STEP),
                                "Undervoltage recovery hysteresis (V)"),
    "cell_voltage_max_hyst":   ("byte", "052", _v(VOLT_HYST_STEP),
                                "Overvoltage recovery hysteresis (V)"),
    "cell_voltage_low":        ("word", "044", "045", _v(VOLT_STEP),
                                "'Low' cell voltage threshold (V)"),
    "cell_voltage_nominal":    ("word", "046", "047", _v(VOLT_STEP),
                                "Nominal cell voltage (V)"),
    "temp_charge_min":         ("word", "060", "061", to_temperature,
                                "Min charge temperature (C)"),
    "temp_discharge_min":      ("word", "062", "063", to_temperature,
                                "Min discharge temperature (C)"),
    "temp_max":                ("word", "070", "071", to_temperature,
                                "Max temperature (C)"),
    "charge_restart_delay":    ("word", "082", "083",
                                lambda x: round(float(x) / RESTART_DELAY_STEP),
                                "Charge restart delay (s)"),
    "discharge_restart_delay": ("word", "084", "085",
                                lambda x: round(float(x) / RESTART_DELAY_STEP),
                                "Discharge restart delay (s)"),
    "charge_pause_delay":      ("byte", "086",
                                lambda x: round(float(x) / PAUSE_DELAY_STEP),
                                "Charge pause delay (s)"),
    "discharge_pause_delay":   ("byte", "087",
                                lambda x: round(float(x) / PAUSE_DELAY_STEP),
                                "Discharge pause delay (s)"),
    "sync_full_tail_current":  ("byte", "092",
                                lambda x: round(float(x) / SYNC_TAIL_STEP),
                                "Full-sync tail current (A)"),
    "solar_peak_power":        ("word", "010", "011",
                                lambda x: round(float(x) / WATT_STEP),
                                "Solar peak power (W)"),
    "inverter_peak_power":     ("word", "020", "021",
                                lambda x: round(float(x) / WATT_STEP),
                                "Inverter peak power (W)"),
    "battery_capacity":        ("word", "030", "031",
                                lambda x: round(float(x) / WATTHOUR_STEP),
                                "Battery factory capacity (Wh)"),
    "battery_chemistry":       ("byte", "015",
                                lambda x: _chemistry(x),
                                "Battery chemistry: Other/LFP/LTO/NMC/NCA"),
    "sensor_type":             ("byte", "016",
                                lambda x: int(x),
                                "Current sensor type code (0,1,2,11)"),
    "critical_mode":           ("byte", "017", lambda x: _bool(x),
                                "Critical mode on/off"),
    "use_measured_capacity":   ("byte", "08B", lambda x: _bool(x),
                                "Use measured capacity on/off"),
    "soc_voltage_correction":  ("byte", "08D", lambda x: _bool(x),
                                "SoC voltage correction on/off"),
    "charge_sensor_invert":    ("byte", "08E", lambda x: _bool(x),
                                "Invert charge current sensor on/off"),
    "discharge_sensor_invert": ("byte", "08F", lambda x: _bool(x),
                                "Invert discharge current sensor on/off"),
    "charge_relay_force_off":  ("byte", "019", lambda x: _bool(x),
                                "Force charge relay off on/off"),
    "discharge_relay_force_off": ("byte", "01A", lambda x: _bool(x),
                                  "Force discharge relay off on/off"),
    # Relay 1 = Charge (base 0x340=832), Relay 2 = Load (+0x20)
    "relay1_function":         ("byte", "340", lambda x: _relay_func(x),
                                "Charge relay function"),
    "relay1_invert":           ("byte", "341", lambda x: _bool(x),
                                "Charge relay invert on/off"),
    "relay1_force_position":   ("byte", "342", lambda x: _relay_pos(x),
                                "Charge relay force position"),
    "relay2_function":         ("byte", "360", lambda x: _relay_func(x),
                                "Load relay function"),
    "relay2_invert":           ("byte", "361", lambda x: _bool(x),
                                "Load relay invert on/off"),
    "relay2_force_position":   ("byte", "362", lambda x: _relay_pos(x),
                                "Load relay force position"),
}


def _bool(x):
    return 1 if str(x).strip().lower() in ("1", "true", "on", "yes") else 0


def _lookup(name, table):
    s = str(name).strip()
    if s.lstrip("-").isdigit():
        return int(s)
    for k, v in table.items():
        if v.lower() == s.lower():
            return k
    raise ValueError(f"unknown value '{name}', expected one of "
                     f"{[v for v in table.values() if v != 'Unknown']}")


def _chemistry(x):
    return _lookup(x, CHEMISTRIES)


def _relay_func(x):
    return _lookup(x, RELAY_FUNCTIONS)


def _relay_pos(x):
    return _lookup(x, RELAY_FORCE_POS)


# --------------------------------------------------------------------------
# BLE transport
# --------------------------------------------------------------------------
class BmsConnection:
    def __init__(self, client, uuids, gen3):
        self.client = client
        self.uuids = uuids
        self.gen3 = gen3
        self.is_blueradios = "mode" in uuids
        self._lines = asyncio.Queue()
        self._partial = ""
        self._rts = 0            # BlueRadios flow control: 0 = clear to send

    async def start(self):
        if self.is_blueradios:
            # BlueRadios BRSP init, in the exact order the app uses:
            #   1. subscribe RTS (flow control)  2. subscribe TX (data)
            #   3. read Info  4. switch module into Data mode.
            # The order matters: the module won't pass UART data otherwise.
            try:
                await self.client.start_notify(self.uuids["rts"], self._on_rts)
            except Exception:
                pass                                  # flow control optional
            await self.client.start_notify(self.uuids["tx"], self._on_notify)
            try:
                await self.client.read_gatt_char(self.uuids["info"])
            except Exception:
                pass
            await self.client.write_gatt_char(
                self.uuids["mode"], bytes([BLUERADIOS_MODE_DATA]), response=True)
            await asyncio.sleep(0.4)                  # let the module settle
        else:
            await self.client.start_notify(self.uuids["tx"], self._on_notify)

    async def wake(self):
        """Nudge the BMS the way the app's bootloader-detect step does.

        Sends a ping and an ENQ so the device leaves any detect state and
        starts answering; also primes reply flushing.
        """
        await self.write_raw(b"$")
        await asyncio.sleep(0.2)
        await self.write_raw(b"\x05\r")
        await asyncio.sleep(0.6)
        self.drain()

    def _on_rts(self, _char, data):
        # Signed int8: 0 means the module is ready to receive.
        self._rts = data[0] - 256 if data and data[0] > 127 else (data[0] if data else 0)

    def _on_notify(self, _char, data):
        # Reassemble ASCII stream into '\r'/space separated lines.
        self._partial += data.decode("ascii", errors="replace")
        # Split on CR and space; keep records intact.
        while True:
            idx = min((i for i in (self._partial.find('\r'),
                                   self._partial.find(' '))
                       if i != -1), default=-1)
            if idx == -1:
                break
            line = self._partial[:idx]
            self._partial = self._partial[idx + 1:]
            if line:
                self._lines.put_nowait(line)

    async def _wait_clear_to_send(self):
        # BlueRadios asserts flow control via RTS; wait until it's 0.
        if not self.is_blueradios:
            return
        deadline = time.monotonic() + 1.0
        while self._rts != 0 and time.monotonic() < deadline:
            await asyncio.sleep(0.02)

    async def write_raw(self, payload):
        # Chunk to 20 bytes (BlueRadios packet size); harmless for Raytac.
        for i in range(0, len(payload), 20):
            await self._wait_clear_to_send()
            await self.client.write_gatt_char(self.uuids["rx"], payload[i:i + 20],
                                              response=self.is_blueradios)

    async def write(self, command):
        await self.write_raw((command + "\r").encode("latin1"))

    async def ping(self):
        # A bare '$' also flushes any reply the device is holding.
        await self.write_raw(b"$")

    def drain(self):
        while not self._lines.empty():
            self._lines.get_nowait()

    async def next_line(self, timeout):
        return await asyncio.wait_for(self._lines.get(), timeout)

    async def query(self, command, timeout=3.0):
        """Send a read command; return the first data line (echo skipped).

        The BMS holds its reply until another byte arrives, so we send '$'
        pings to flush it while waiting.
        """
        self.drain()
        await self.write(command)
        deadline = time.monotonic() + timeout
        last_flush = 0.0
        while time.monotonic() < deadline:
            now = time.monotonic()
            if now - last_flush > 0.4:
                await self.ping()
                last_flush = now
            try:
                line = (await self.next_line(0.4)).strip()
            except asyncio.TimeoutError:
                continue
            if not line or line == command:      # skip blanks and echo
                continue
            if line == "NA":
                raise PermissionError("Not authorized (wrong or missing PIN)")
            if line in ("OK", "KO"):
                continue
            return line
        raise TimeoutError(f"no reply to '{command}'")

    async def command(self, command, timeout=3.0):
        """Send a write/action command; wait for OK/KO/NA result."""
        self.drain()
        await self.write(command)
        deadline = time.monotonic() + timeout
        last_flush = 0.0
        while time.monotonic() < deadline:
            now = time.monotonic()
            if now - last_flush > 0.4:
                await self.ping()
                last_flush = now
            try:
                line = (await self.next_line(0.4)).strip()
            except asyncio.TimeoutError:
                continue
            if line in ("OK", "KO"):
                return line
            if line == "NA":
                raise PermissionError("Not authorized (wrong or missing PIN)")
        raise TimeoutError(f"no result for '{command}'")


# --------------------------------------------------------------------------
# Discovery / connection
# --------------------------------------------------------------------------
# Advertised device types that are Gen3 (from DeviceType enum): SmartBMS Gen3
# BlueRadios (4) and SmartBMS Gen3 Raytac (6). TUNE+ Raytac (5) is also newer.
GEN3_DEVICE_TYPES = {4, 5, 6}


def _decode_manufacturer(company_id, payload):
    """Return info dict from advertisement manufacturer data, or None."""
    full = bytes([company_id & 0xFF, (company_id >> 8) & 0xFF]) + bytes(payload)
    if len(full) < 6:
        return None
    dtype_num = full[3]
    serial = f"{full[4]:02X}{full[5]:02X}"
    try:
        serial = str(int(serial, 16)).zfill(5)
    except ValueError:
        pass
    return {
        "chip": COMPANY_IDS.get(company_id, "?"),
        "type": DEVICE_TYPES.get(dtype_num, f"type{dtype_num}"),
        "type_num": dtype_num,
        "serial": serial,
        "gen3": dtype_num in GEN3_DEVICE_TYPES,
    }


async def discover(timeout=8.0):
    """Scan and return a list of (device, advertisement, info) for BMS units."""
    found = {}

    def callback(device, adv):
        for cid, payload in (adv.manufacturer_data or {}).items():
            if cid in COMPANY_IDS:
                info = _decode_manufacturer(cid, payload)
                if info:
                    found[device.address] = (device, adv, info)
                return
        # Fallback: match by advertised UART service UUID (no gen info).
        uuids = [u.lower() for u in (adv.service_uuids or [])]
        if RAYTAC["service"] in uuids or BLUERADIOS["service"] in uuids:
            found.setdefault(device.address, (device, adv, {
                "chip": "?", "type": "?", "serial": "?", "gen3": None}))

    scanner = BleakScanner(detection_callback=callback)
    await scanner.start()
    await asyncio.sleep(timeout)
    await scanner.stop()
    return list(found.values())


async def _detect_uuids(client):
    """Pick the UART characteristic set based on which service is present."""
    svc_uuids = {s.uuid.lower() for s in client.services}
    if RAYTAC["service"] in svc_uuids:
        return RAYTAC, "Raytac"
    if BLUERADIOS["service"] in svc_uuids:
        return BLUERADIOS, "BlueRadios"
    raise RuntimeError("No 123\\SmartBMS UART service found on this device")


async def _probe_generation(conn):
    """Fallback: guess generation from the V@ firmware field length.

    Gen3 firmware encodes major/minor/build as separate nibbles (>=3 chars,
    e.g. '367' -> 3.6.7); Gen2 reports a shorter decimal-ish field.
    """
    try:
        fw = (await conn.query("V@")).split('_')[0].strip()
        return len(fw) >= 3
    except Exception:
        return True     # default to Gen3 (current hardware)


async def connect(address, pin=None, read_settings=True, verbose=True):
    """Connect, authorize, optionally read settings. Returns (client, conn, bms)."""
    def log(*a):
        if verbose:
            print(*a, file=sys.stderr)

    gen3 = None      # generation comes from the advertisement device type
    if address is None:
        log("Scanning for 123\\SmartBMS ...")
        devices = await discover()
        if not devices:
            raise RuntimeError("No BMS found. Is it in range and powered?")
        device, _adv, info = devices[0]
        address = device.address
        gen3 = info.get("gen3")
        log(f"Using {info['type']} #{info['serial']} ({info['chip']}) "
            f"at {address}")
    else:
        # Explicit address: still scan briefly to learn the generation.
        for _dev, _adv, info in await discover(4.0):
            if _dev.address.lower() == address.lower():
                gen3 = info.get("gen3")
                break

    client = BleakClient(address)
    await client.connect()
    uuids, chip = await _detect_uuids(client)
    conn = BmsConnection(client, uuids, bool(gen3))
    await conn.start()

    await conn.wake()

    # If generation is still unknown, infer it from the firmware string:
    # Gen3 firmware is like "3.6.7"; Gen2 reports a single-digit-ish major.
    if gen3 is None:
        try:
            gen3 = await _probe_generation(conn)
        except Exception:
            gen3 = True
        conn.gen3 = gen3
    log(f"Connected (chip {chip}, {'Gen3+' if gen3 else 'Gen2'})")

    bms = {"gen3": gen3}

    if pin is not None:
        try:
            result = await conn.command(f"PW{pin}!")
        except PermissionError:
            await client.disconnect()
            raise PermissionError(
                f"Device rejected PIN {pin} (replied 'Not Authorized'). "
                f"Double-check the PIN, or that it was actually saved to the "
                f"device by the app.")
        if result != "OK":
            await client.disconnect()
            raise PermissionError(f"Authorization failed (PIN {pin}): {result}")
        log("Authorized")

    if read_settings:
        bms.update(await read_config(conn))

    return client, conn, bms


# --------------------------------------------------------------------------
# Configuration read
# --------------------------------------------------------------------------
async def read_config(conn):
    """Read V@/S1@/S2@/S3@/SRY0@/SRY1@ and return a parsed dict."""
    cfg = {}

    # --- Version ---
    v = (await conn.query("V@")).split('_')
    if len(v) >= 6:
        # Gen3 firmware: nibble major, nibble minor, rest build.
        fw = v[0]
        try:
            major = parse_int(fw[0]); minor = parse_int(fw[1])
            build = parse_int(fw[2:]) if len(fw) > 2 else 0
        except Exception:
            major = minor = build = 0
        gen3 = conn.gen3
        if not gen3:
            n = parse_int(fw)
            major, minor, build = n // 10, n % 10, 0
        cfg["firmware"] = f"{major}.{minor}.{build}"
        cfg["firmware_tuple"] = (major, minor, build)
        cfg["balance_voltage"] = round(parse_voltage(v[4]), 3)
        cfg["status_byte1"] = parse_int(v[3])

    # --- Settings 1 ---
    s1 = (await conn.query("S1@")).split('_')
    if len(s1) >= 5:
        cfg["solar_peak_power_w"] = parse_int(s1[0]) * WATT_STEP
        cfg["inverter_peak_power_w"] = parse_int(s1[1]) * WATT_STEP
        cfg["battery_capacity_wh"] = parse_int(s1[2]) * WATTHOUR_STEP
        # PIN stored as 4 bytes, each an ASCII digit encoded in 2 hex chars.
        try:
            pin_field = s1[3]
            cfg["pin"] = "".join(chr(int(pin_field[i:i+2], 16))
                                 for i in range(0, 8, 2))
        except Exception:
            cfg["pin"] = None

    # --- Settings 2 ---
    s2 = (await conn.query("S2@")).split('_')
    n = len(s2) - 1
    if n >= 6:
        cfg["cell_voltage_min"] = round(parse_voltage(s2[0]), 3)
        cfg["cell_voltage_max"] = round(parse_voltage(s2[1]), 3)
        cfg["temp_charge_min"] = parse_temperature(s2[2])
        cfg["temp_max"] = parse_temperature(s2[3])
        cfg["charge_restart_percent"] = parse_int(s2[4])
        cfg["discharge_restart_percent"] = parse_int(s2[5])
        if n > 6:
            t = parse_temperature(s2[6])
            cfg["temp_discharge_min"] = t if -40 <= t <= 80 else cfg["temp_charge_min"]
        if n > 7:
            cfg["cell_voltage_low"] = round(parse_voltage(s2[7]), 3)
        if n > 8:
            cfg["cell_voltage_nominal"] = round(parse_voltage(s2[8]), 3)
        if n > 9:
            cfg["cell_voltage_min_hyst"] = round(parse_int(s2[9]) * VOLT_HYST_STEP, 3)
        if n > 10:
            cfg["cell_voltage_max_hyst"] = round(parse_int(s2[10]) * VOLT_HYST_STEP, 3)

    # --- Settings 3 (Gen3) ---
    if conn.gen3:
        try:
            s3 = (await conn.query("S3@")).split('_')
        except (TimeoutError, PermissionError):
            s3 = []
        n = len(s3) - 1
        if n >= 2:
            cfg["sensor_type"] = SENSOR_TYPES.get(parse_int(s3[0]), parse_int(s3[0]))
            cfg["critical_mode"] = parse_int(s3[1]) == 1
            if n > 2:
                cfg["charge_relay_force_off"] = parse_int(s3[2]) == 1
            if n > 3:
                cfg["discharge_relay_force_off"] = parse_int(s3[3]) == 1
            if n > 4:
                cfg["use_measured_capacity"] = parse_int(s3[4]) == 1
            if n > 5:
                cfg["charge_restart_delay_s"] = parse_int(s3[5]) * RESTART_DELAY_STEP
            if n > 6:
                cfg["discharge_restart_delay_s"] = parse_int(s3[6]) * RESTART_DELAY_STEP
            if n > 7:
                cfg["charge_pause_delay_s"] = parse_int(s3[7]) * PAUSE_DELAY_STEP
            if n > 8:
                cfg["discharge_pause_delay_s"] = parse_int(s3[8]) * PAUSE_DELAY_STEP
            if n > 9:
                cfg["soc_voltage_correction"] = parse_int(s3[9]) == 1
            if n > 10:
                cfg["battery_chemistry"] = CHEMISTRIES.get(parse_int(s3[10]),
                                                           parse_int(s3[10]))
            if n > 11:
                cfg["sync_full_tail_current_a"] = round(parse_int(s3[11]) * SYNC_TAIL_STEP, 2)

        # --- Relay settings ---
        for nr, label in ((0, "relay1_charge"), (1, "relay2_load")):
            try:
                r = (await conn.query(f"SRY{nr}@")).split('_')
            except (TimeoutError, PermissionError):
                continue
            if len(r) >= 4:
                cfg[f"{label}_force_position"] = RELAY_FORCE_POS.get(parse_int(r[0]))
                cfg[f"{label}_function"] = RELAY_FUNCTIONS.get(parse_int(r[1]))
                cfg[f"{label}_invert"] = parse_int(r[2]) == 1

    return cfg


# --------------------------------------------------------------------------
# Live data parsing
# --------------------------------------------------------------------------
class LiveState:
    """Accumulates streamed records into a snapshot."""
    def __init__(self, gen3):
        self.gen3 = gen3
        self.power_step = POWER_STEP_GEN3 if gen3 else POWER_STEP_GEN2
        self.data = {}
        self.cells = {}

    def feed(self, line):
        parts = line.split('_')
        tag = parts[0].strip().replace('\r', '')
        if len(tag) != 1:
            return
        t = tag[0]
        d = self.data
        try:
            if t == 'U' and len(parts) == 5:
                d["battery_voltage"] = round(parse_voltage(parts[1]), 3)
                d["solar_current"] = None if is_na(parts[2]) else round(parse_int(parts[2]) * self.power_step, 2)
                d["battery_current"] = round(parse_int(parts[3]) * self.power_step, 2)
                d["consumption_current"] = None if is_na(parts[4]) else round(parse_int(parts[4]) * self.power_step, 2)
            elif t == 'V' and len(parts) == 6:
                d["cell_v_min"] = round(parse_voltage(parts[1]), 3)
                d["cell_v_min_nr"] = parse_int(parts[2])
                d["cell_v_max"] = round(parse_voltage(parts[3]), 3)
                d["cell_v_max_nr"] = parse_int(parts[4])
                d["balance_voltage"] = round(parse_voltage(parts[5]), 3)
            elif t == 'T' and len(parts) in (5, 6):
                d["temp_min"] = parse_temperature(parts[1])
                d["temp_min_nr"] = parse_int(parts[2])
                d["temp_max"] = parse_temperature(parts[3])
                d["temp_max_nr"] = parse_int(parts[4])
                if len(parts) == 6:
                    d["cell_log_index"] = -1 if is_na(parts[5]) else parse_int(parts[5])
            elif t == 'C' and len(parts) >= 6:
                nr = parse_int(parts[1])
                if nr == 0:
                    return
                d["cell_count"] = parse_int(parts[2])
                v = round(parse_voltage(parts[3]), 3)
                temp = parse_temperature(parts[4])
                self._status1(parse_int(parts[5]))
                if len(parts) >= 7:
                    self._status2(parse_int(parts[6]))
                if not d.get("communication_error") and nr <= d["cell_count"]:
                    self.cells[nr] = {"voltage": v, "temperature": temp}
            elif t == 'E' and len(parts) == 5:
                d["solar_energy_today_kwh"] = parse_int(parts[1]) / 1000.0
                d["battery_energy_stored_kwh"] = parse_int(parts[2]) / 1000.0
                d["energy_consumed_today_kwh"] = parse_int(parts[3]) / 1000.0
                d["state_of_charge_percent"] = parse_int(parts[4])
            elif t == 'M' and len(parts) == 4:
                d["solar_energy_total_kwh"] = parse_int(parts[1])
                d["energy_consumed_total_kwh"] = parse_int(parts[2])
                hm = parts[3].split(':')
                if len(hm) == 2:
                    d["device_time"] = f"{parse_int(hm[0]):02d}:{parse_int(hm[1]):02d}"
            elif t == 'H' and len(parts) == 7:
                d["state_of_health_percent"] = parse_int(parts[1])
                d["measured_capacity_wh"] = parse_int(parts[2])
                d["measured_charge_capacity_wh"] = parse_int(parts[3])
                d["charge_efficiency_percent"] = parse_int(parts[4])
                d["charge_cycles"] = parse_int(parts[5]) / 100.0
                d["discharge_cycles"] = parse_int(parts[6]) / 100.0
            elif t == 'B' and len(parts) == 5:
                d["charged_energy_total_kwh"] = parse_int(parts[1])
                d["discharged_energy_total_kwh"] = parse_int(parts[2])
                d["charged_energy_today_kwh"] = parse_int(parts[3]) / 1000.0
                d["discharged_energy_today_kwh"] = parse_int(parts[4]) / 1000.0
        except (IndexError, ValueError):
            pass

    def _status1(self, state):
        b = [(state >> i) & 1 for i in range(8)]
        d = self.data
        d["allow_to_charge"] = bool(b[0])
        d["allow_to_discharge"] = bool(b[1])
        d["communication_error"] = bool(b[2])
        d["exceed_v_min"] = bool(b[3])
        d["exceed_v_max"] = bool(b[4])
        d["exceed_t_min"] = bool(b[5])
        d["exceed_t_max"] = bool(b[6])
        d["soc_not_calibrated"] = bool(b[7])
        d["error"] = any((b[2], b[3], b[4], b[5], b[6]))

    def _status2(self, state):
        b = [(state >> i) & 1 for i in range(8)]
        d = self.data
        d["early_warning"] = bool(b[1])
        d["exceed_t_min_charge"] = bool(b[2])
        d["exceed_t_min_discharge"] = bool(b[3])
        d["relay1_closed"] = bool(b[4])
        d["relay2_closed"] = bool(b[5])

    def snapshot(self):
        s = dict(self.data)
        s["cells"] = [dict(nr=nr, **self.cells[nr]) for nr in sorted(self.cells)]
        return s


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------
async def cmd_scan(args):
    print("Scanning for 123\\SmartBMS devices (%.0fs)...\n" % args.timeout)
    devices = await discover(args.timeout)
    if not devices:
        print("No devices found. Ensure the BMS is powered and in range,")
        print("and that Bluetooth is on (you may need sudo/bluetooth perms).")
        return
    for device, adv, info in devices:
        rssi = getattr(adv, "rssi", "?")
        print(f"  {device.address}   {info['type']} #{info['serial']} "
              f"({info['chip']})   RSSI {rssi}")


async def cmd_read(args):
    client, conn, bms = await connect(args.address, args.pin, verbose=not args.json)
    try:
        cfg = {k: v for k, v in bms.items()
               if k not in ("gen3", "firmware_tuple", "status_byte1")}
        if args.json:
            print(json.dumps(cfg, indent=2))
        else:
            print("\n=== Configuration ===")
            for k, v in cfg.items():
                print(f"  {k:32} {v}")
    finally:
        await client.disconnect()


async def cmd_monitor(args):
    client, conn, bms = await connect(args.address, args.pin)
    state = LiveState(bms["gen3"])
    try:
        await conn.command("D!")           # stop any existing stream
        r = await conn.command("E!")       # start streaming
        if r != "OK":
            print(f"Failed to start data stream: {r}", file=sys.stderr)
            return
        print("Streaming live data. Press Ctrl-C to stop.\n", file=sys.stderr)
        last_ping = time.monotonic()
        last_print = 0.0
        while True:
            try:
                line = await conn.next_line(2.0)
                state.feed(line.strip())
            except asyncio.TimeoutError:
                pass
            now = time.monotonic()
            if now - last_ping > 1.5:       # keep-alive
                await conn.ping()
                last_ping = now
            if now - last_print > args.interval:
                _print_live(state.snapshot(), args.json)
                last_print = now
    except KeyboardInterrupt:
        pass
    finally:
        try:
            await conn.command("D!", timeout=1.0)
        except Exception:
            pass
        await client.disconnect()


def _print_live(s, as_json):
    if as_json:
        print(json.dumps(s))
        return
    # Clear screen and print a compact dashboard.
    print("\033[2J\033[H", end="")
    soc = s.get("state_of_charge_percent", "?")
    print(f"123\\SmartBMS live @ {s.get('device_time','--:--')}   "
          f"SoC {soc}%   SoH {s.get('state_of_health_percent','?')}%")
    print("-" * 56)
    print(f"  Battery voltage : {s.get('battery_voltage','?')} V")
    print(f"  Battery current : {s.get('battery_current','?')} A")
    if s.get("solar_current") is not None:
        print(f"  Solar current   : {s.get('solar_current')} A")
    if s.get("consumption_current") is not None:
        print(f"  Load current    : {s.get('consumption_current')} A")
    print(f"  Stored energy   : {s.get('battery_energy_stored_kwh','?')} kWh")
    print(f"  Cell V min/max  : {s.get('cell_v_min','?')} (#{s.get('cell_v_min_nr','?')}) "
          f"/ {s.get('cell_v_max','?')} (#{s.get('cell_v_max_nr','?')}) V")
    print(f"  Temp min/max    : {s.get('temp_min','?')} (#{s.get('temp_min_nr','?')}) "
          f"/ {s.get('temp_max','?')} (#{s.get('temp_max_nr','?')}) C")
    flags = []
    for name, key in (("CHG-OK", "allow_to_charge"), ("DSG-OK", "allow_to_discharge"),
                      ("Vmin!", "exceed_v_min"), ("Vmax!", "exceed_v_max"),
                      ("Tmin!", "exceed_t_min"), ("Tmax!", "exceed_t_max"),
                      ("COMM-ERR", "communication_error"), ("ERROR", "error")):
        if s.get(key):
            flags.append(name)
    print(f"  Status          : {'  '.join(flags) if flags else '-'}")
    cells = s.get("cells", [])
    if cells:
        print(f"  Cells ({len(cells)}):")
        for c in cells:
            print(f"    #{c['nr']:<2} {c['voltage']:.3f} V  {c['temperature']}C")


async def cmd_set(args):
    if args.list or not args.param:
        print("Writable configuration parameters:\n")
        for name, spec in CONFIG_WRITES.items():
            help_ = spec[-1]
            print(f"  {name:26} {help_}")
        print("\nExample: python smartbms.py set cell_voltage_max 3.60 --pin 0000")
        return
    if args.param not in CONFIG_WRITES:
        print(f"Unknown parameter '{args.param}'. Use --list to see options.",
              file=sys.stderr)
        sys.exit(2)
    if args.value is None:
        print("A value is required.", file=sys.stderr)
        sys.exit(2)
    if args.pin is None:
        print("Writing configuration requires --pin.", file=sys.stderr)
        sys.exit(2)

    spec = CONFIG_WRITES[args.param]
    kind = spec[0]
    encode = spec[-2]
    try:
        raw = int(encode(args.value))
    except (ValueError, TypeError) as e:
        print(f"Invalid value: {e}", file=sys.stderr)
        sys.exit(2)

    client, conn, bms = await connect(args.address, args.pin, read_settings=False)
    try:
        if kind == "byte":
            addr = spec[1]
            cmd = f"{raw & 0xFF:02X}-{addr}!"
            print(f"Writing {args.param}={args.value} (raw 0x{raw & 0xFF:02X}) -> {cmd}")
            r = await conn.command(cmd)
            _report(r)
        elif kind == "word":
            hi_addr, lo_addr = spec[1], spec[2]
            word = raw & 0xFFFF
            hi, lo = (word >> 8) & 0xFF, word & 0xFF
            print(f"Writing {args.param}={args.value} (raw 0x{word:04X}) -> "
                  f"{hi:02X}-{hi_addr}! then {lo:02X}-{lo_addr}!")
            r1 = await conn.command(f"{hi:02X}-{hi_addr}!")
            if r1 != "OK":
                _report(r1); return
            r2 = await conn.command(f"{lo:02X}-{lo_addr}!")
            _report(r2)
    finally:
        await client.disconnect()


def _report(result):
    if result == "OK":
        print("OK - written and saved.")
    else:
        print(f"Device rejected the write (result: {result}).", file=sys.stderr)
        sys.exit(1)


async def cmd_raw(args):
    client, conn, bms = await connect(args.address, args.pin, read_settings=False)
    try:
        c = args.command.strip()
        if c.endswith("!"):          # write/action command -> expects OK/KO/NA
            print(await conn.command(c))
        else:                        # query -> expects a data line
            print(await conn.query(c))
    finally:
        await client.disconnect()


# --------------------------------------------------------------------------
def main():
    # Common options usable either before OR after the subcommand.
    # SUPPRESS default on the shared copy stops it clobbering a value given
    # before the subcommand.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-a", "--address", default=argparse.SUPPRESS,
                        help="BLE MAC address (default: auto-scan)")
    common.add_argument("--pin", default=argparse.SUPPRESS,
                        help="PIN to authorize (needed to write, and to read/"
                             "stream on locked units)")

    p = argparse.ArgumentParser(
        description="123\\SmartBMS Linux client (Bluetooth LE).",
        parents=[common])
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="Scan for nearby BMS devices", parents=[common])
    s.add_argument("--timeout", type=float, default=8.0)
    s.set_defaults(func=cmd_scan)

    m = sub.add_parser("monitor", help="Show live values", parents=[common])
    m.add_argument("--interval", type=float, default=1.0,
                   help="Refresh interval seconds (default 1)")
    m.add_argument("--json", action="store_true", help="Emit JSON lines")
    m.set_defaults(func=cmd_monitor)

    r = sub.add_parser("read", help="Read and print configuration", parents=[common])
    r.add_argument("--json", action="store_true")
    r.set_defaults(func=cmd_read)

    w = sub.add_parser("set", help="Write a configuration parameter", parents=[common])
    w.add_argument("param", nargs="?", help="Parameter name (see --list)")
    w.add_argument("value", nargs="?", help="New value")
    w.add_argument("--list", action="store_true", help="List writable params")
    w.set_defaults(func=cmd_set)

    rw = sub.add_parser("raw", help="Send a raw protocol command (advanced)",
                        parents=[common])
    rw.add_argument("command", help='e.g. "V@" or "3F-050!"')
    rw.set_defaults(func=cmd_raw)

    args = p.parse_args()
    # Fill defaults for options that were suppressed (never provided).
    if not hasattr(args, "address"):
        args.address = None
    if not hasattr(args, "pin"):
        args.pin = None
    try:
        asyncio.run(args.func(args))
    except KeyboardInterrupt:
        pass
    except (PermissionError, TimeoutError, RuntimeError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
