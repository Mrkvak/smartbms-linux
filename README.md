# 123 SmartBMS Linux Client

`smartbms.py` is a Linux command-line client for 123 SmartBMS battery
management systems over Bluetooth Low Energy. It can scan for devices, monitor
live values, read and write configuration, read the cell/error log, export SoC
history, and send raw protocol commands.

The protocol is an ASCII line protocol over BLE UART and was reverse engineered
from the official Android app.

## Project Layout

- `smartbmslib/core.py`: reusable BLE transport, protocol commands, parsers,
  config readers, live-state parser, log reader, and SoC history reader
- `smartbmslib/cli.py`: command-line interface built on the library
- `smartbmslib/__main__.py`: package entry point for `python -m smartbmslib`
- `smartbms.py`: small executable wrapper that calls `smartbmslib.cli.main()`

## Requirements

- Linux with Bluetooth LE support
- Python 3.10 or newer
- `bleak`
- Bluetooth permissions for your user, or run with the privileges required by
  your distribution

Install dependencies in a virtual environment:

```bash
python3 -m venv venv
./venv/bin/python -m pip install bleak
```

For examples that need authorization or a fixed device, set local shell
variables first:

```bash
export BMS_PIN="<your-pin>"
export BMS_ADDRESS="<your-ble-address>"
```

## Basic Usage

Scan for nearby SmartBMS devices:

```bash
./venv/bin/python smartbms.py scan
```

The package entry point is equivalent:

```bash
./venv/bin/python -m smartbmslib scan
```

Monitor live values using auto-discovery:

```bash
./venv/bin/python smartbms.py monitor --pin "$BMS_PIN"
```

Monitor a specific device address:

```bash
./venv/bin/python smartbms.py monitor --address "$BMS_ADDRESS" --pin "$BMS_PIN"
```

The monitor shows battery voltage/current/power, solar current/power, load
current/power, SoC, SoH, charged/discharged energy today, min/max cell values,
relay/status flags, and per-cell status:

- `OK`: no voltage or temperature error
- `BAL`: cell voltage is at or above the configured balance voltage
- `ERR`: voltage or temperature is outside configured limits

JSON live output:

```bash
./venv/bin/python smartbms.py monitor --pin "$BMS_PIN" --json
```

## Reading Configuration

Print the parsed configuration:

```bash
./venv/bin/python smartbms.py read --pin "$BMS_PIN"
```

Print configuration as JSON:

```bash
./venv/bin/python smartbms.py read --pin "$BMS_PIN" --json
```

## Writing Configuration

List supported writable parameters:

```bash
./venv/bin/python smartbms.py set --list
```

Set a cell overvoltage limit:

```bash
./venv/bin/python smartbms.py set cell_voltage_max 3.60 --pin "$BMS_PIN"
```

Set a battery capacity:

```bash
./venv/bin/python smartbms.py set battery_capacity 2400 --pin "$BMS_PIN"
```

Configuration writes require a PIN. Review values carefully before writing:
the script sends commands directly to the BMS.

## Cell/Error Log

Read the BMS cell/error log as a table:

```bash
./venv/bin/python smartbms.py log --pin "$BMS_PIN"
```

Equivalent aliases:

```bash
./venv/bin/python smartbms.py error-log --pin "$BMS_PIN"
./venv/bin/python smartbms.py cell-log --pin "$BMS_PIN"
```

Export the log as CSV:

```bash
./venv/bin/python smartbms.py log --pin "$BMS_PIN" --csv --output bms-log.csv
```

The log reader mirrors the app: it sends `TS@`, then `L0@` through `L9@`, and
decodes the entry type, cell number, timestamp, voltage, or temperature.

## SoC History

Export state-of-charge history as CSV:

```bash
./venv/bin/python smartbms.py soc-history --pin "$BMS_PIN" --output soc-history.csv
```

Write the CSV to stdout:

```bash
./venv/bin/python smartbms.py soc-history --pin "$BMS_PIN"
```

The app reads SoC history by sending `H0@` through `HD@`. Each response contains
12 hourly SoC values from a 168-hour ring buffer. This client exports those raw
points as:

```csv
hours_ago,state_of_charge_percent
0,80
1,79
```

It does not generate a graph.

## Raw Commands

Send a raw query:

```bash
./venv/bin/python smartbms.py raw "V@" --pin "$BMS_PIN"
```

Send a raw action/write command:

```bash
./venv/bin/python smartbms.py raw "D!" --pin "$BMS_PIN"
```

Raw commands are intended for protocol investigation. Use them carefully.

## Library Usage

Use `smartbmslib` directly from another Python program when you do not want the
CLI formatting:

```python
import asyncio
from smartbmslib import connect, read_soc_history


async def main():
    client, conn, bms = await connect(address=None, pin="<your-pin>")
    try:
        print(bms["firmware"])
        rows = await read_soc_history(conn)
        print(rows[:3])
    finally:
        await client.disconnect()


asyncio.run(main())
```

For live stream parsing, instantiate `LiveState` with the config returned by
`connect()` and feed it live protocol lines from `conn.next_line()`.

## Notes

- Do not publish your real SmartBMS BLE address, serial number, or PIN.
- If auto-discovery finds more than one BMS, pass `--address` to select the
  intended device.
- Some commands and log/history features are Gen3-oriented and may not be
  available on older units.

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE).
