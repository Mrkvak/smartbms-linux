"""Command-line interface for the 123 SmartBMS client."""

import argparse
import asyncio
import csv
import json
import sys
import time

from .core import (
    CONFIG_WRITES,
    STREAM_PING_INTERVAL,
    STREAM_POLL_INTERVAL,
    LiveState,
    connect,
    discover,
    read_cell_log,
    read_soc_history,
)


def _write_csv(rows, fieldnames, output):
    f = open(output, "w", newline="") if output else sys.stdout
    try:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    finally:
        if output:
            f.close()


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
    state = LiveState(bms)
    refresh_interval = max(args.interval, 0.05)
    try:
        await conn.command("D!")           # stop any existing stream
        conn.drain()
        r = await conn.command("E!")       # start streaming
        if r != "OK":
            print(f"Failed to start data stream: {r}", file=sys.stderr)
            return
        conn.drain()
        print("Streaming live data. Press Ctrl-C to stop.\n", file=sys.stderr)
        last_ping = time.monotonic()
        last_print = 0.0
        while True:
            timeout = min(STREAM_POLL_INTERVAL, refresh_interval)
            try:
                lines = [await conn.next_line(timeout)]
            except asyncio.TimeoutError:
                lines = []
            lines.extend(conn.get_pending_lines())
            for line in lines:
                line = line.strip()
                if line in ("", "E!", "D!", "OK", "KO", "NA"):
                    continue
                state.feed(line)
            now = time.monotonic()
            if now - last_ping >= STREAM_PING_INTERVAL:
                await conn.ping()
                last_ping = now
            if now - last_print >= refresh_interval:
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
        print(json.dumps(s), flush=True)
        return
    # Clear screen and print a compact dashboard in one write to avoid partial
    # repaints when BLE notifications arrive rapidly.
    out = ["\033[2J\033[3J\033[H"]
    soc = s.get("state_of_charge_percent", "?")
    if "state_of_health_percent" in s:
        soh = s["state_of_health_percent"]
        soh_text = "Unknown" if soh is None else f"{soh}%"
    else:
        soh_text = "?"
    out.append(f"123\\SmartBMS live @ {s.get('device_time','--:--')}   "
               f"SoC {soc}%   SoH {soh_text}")
    out.append("-" * 56)
    out.append(f"  Battery voltage : {s.get('battery_voltage','?')} V")
    out.append(f"  Battery current : {s.get('battery_current','?')} A")
    if "battery_power_w" in s:
        out.append(f"  Battery power   : {s.get('battery_power_w')} W")
    if s.get("solar_current") is not None:
        out.append(f"  Solar current   : {s.get('solar_current')} A")
    if "solar_power_w" in s:
        out.append(f"  Solar power     : {s.get('solar_power_w')} W")
    if s.get("consumption_current") is not None:
        out.append(f"  Load current    : {s.get('consumption_current')} A")
    if "load_power_w" in s:
        out.append(f"  Load power      : {s.get('load_power_w')} W")
    out.append(f"  Stored energy   : {s.get('battery_energy_stored_kwh','?')} kWh")
    if "charged_energy_today_kwh" in s or "discharged_energy_today_kwh" in s:
        out.append(f"  Charged today   : {s.get('charged_energy_today_kwh','?')} kWh")
        out.append(f"  Discharged today: {s.get('discharged_energy_today_kwh','?')} kWh")
    out.append(f"  Cell V min/max  : {s.get('cell_v_min','?')} (#{s.get('cell_v_min_nr','?')}) "
               f"/ {s.get('cell_v_max','?')} (#{s.get('cell_v_max_nr','?')}) V")
    out.append(f"  Temp min/max    : {s.get('temp_min','?')} (#{s.get('temp_min_nr','?')}) "
               f"/ {s.get('temp_max','?')} (#{s.get('temp_max_nr','?')}) C")
    flags = []
    for name, key in (("CHG-OK", "allow_to_charge"), ("DSG-OK", "allow_to_discharge"),
                      ("Vmin!", "exceed_v_min"), ("Vmax!", "exceed_v_max"),
                      ("Tmin!", "exceed_t_min"), ("Tmax!", "exceed_t_max"),
                      ("COMM-ERR", "communication_error"), ("ERROR", "error")):
        if s.get(key):
            flags.append(name)
    out.append(f"  Status          : {'  '.join(flags) if flags else '-'}")
    cells = s.get("cells", [])
    if cells:
        out.append(f"  Cells ({len(cells)}):")
        for c in cells:
            out.append(f"    #{c['nr']:<2} {c['voltage']:.3f} V  "
                       f"{c['temperature']}C  {_cell_status_label(c)}")
    print("\n".join(out), flush=True)


def _cell_status_label(cell):
    status = cell.get("status", "ok")
    labels = {"ok": "OK", "balancing": "BAL", "error": "ERR"}
    label = labels.get(status, status.upper())
    if not sys.stdout.isatty():
        return label
    colors = {"ok": "\033[32m", "balancing": "\033[33m", "error": "\033[31m"}
    return f"{colors.get(status, '')}{label}\033[0m"


async def cmd_log(args):
    client, conn, _bms = await connect(args.address, args.pin, read_settings=False)
    try:
        rows = await read_cell_log(conn)
    finally:
        await client.disconnect()
    if args.json:
        print(json.dumps(rows, indent=2))
    elif args.csv or args.output:
        _write_csv(rows, ["log_nr", "occurred_at", "seconds_ago", "type",
                          "cell_nr", "voltage", "temperature", "timestamp"],
                   args.output)
    else:
        if not rows:
            print("No log entries.")
            return
        print("Log  Time                       Type            Cell  Value")
        for row in rows:
            value = f"{row['voltage']} V" if row["voltage"] != "" else (
                f"{row['temperature']} C" if row["temperature"] != "" else "")
            print(f"{row['log_nr']:<4} {row['occurred_at']:<25} "
                  f"{row['type']:<15} {row['cell_nr']:<5} {value}")


async def cmd_soc_history(args):
    client, conn, _bms = await connect(args.address, args.pin, read_settings=False)
    try:
        rows = await read_soc_history(conn)
    finally:
        await client.disconnect()
    _write_csv(rows, ["hours_ago", "state_of_charge_percent"], args.output)


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

    client, conn, _bms = await connect(args.address, args.pin, read_settings=False)
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
                _report(r1)
                return
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
    client, conn, _bms = await connect(args.address, args.pin, read_settings=False)
    try:
        c = args.command.strip()
        if c.endswith("!"):          # write/action command -> expects OK/KO/NA
            print(await conn.command(c))
        else:                        # query -> expects a data line
            print(await conn.query(c))
    finally:
        await client.disconnect()


def build_parser():
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

    lg = sub.add_parser("log", aliases=["error-log", "cell-log"],
                        help="Read the BMS cell/error log", parents=[common])
    lg.add_argument("--json", action="store_true", help="Emit JSON")
    lg.add_argument("--csv", action="store_true", help="Emit CSV")
    lg.add_argument("--output", help="Write CSV to this file")
    lg.set_defaults(func=cmd_log)

    sh = sub.add_parser("soc-history", help="Export hourly SoC history as CSV",
                        parents=[common])
    sh.add_argument("--output", help="Write CSV to this file (default stdout)")
    sh.set_defaults(func=cmd_soc_history)

    w = sub.add_parser("set", help="Write a configuration parameter", parents=[common])
    w.add_argument("param", nargs="?", help="Parameter name (see --list)")
    w.add_argument("value", nargs="?", help="New value")
    w.add_argument("--list", action="store_true", help="List writable params")
    w.set_defaults(func=cmd_set)

    rw = sub.add_parser("raw", help="Send a raw protocol command (advanced)",
                        parents=[common])
    rw.add_argument("command", help='e.g. "V@" or "3F-050!"')
    rw.set_defaults(func=cmd_raw)
    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    # Fill defaults for options that were suppressed (never provided).
    if not hasattr(args, "address"):
        args.address = None
    if not hasattr(args, "pin"):
        args.pin = None
    try:
        asyncio.run(args.func(args))
    except KeyboardInterrupt:
        pass
    except (PermissionError, TimeoutError, RuntimeError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
