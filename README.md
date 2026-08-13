# GasWorks ↔ ProLab Serial Translator

A small Python/pySerial bridge that fixes the serial command terminator mismatch:

- **GasWorks sends:** `CR` (`0D`)
- **ProLab expects:** `CR LF` (`0D 0A`)
- **ProLab responses:** forwarded back to GasWorks **byte-for-byte unchanged**

## Correct com0com topology

A com0com pair has two ends. GasWorks and this translator must open **different ends** of the pair.

```text
GasWorks
   |
   v
 COM5              COM6                COM4
   |                  |                   |
   +==== com0com =====+ -> Translator ->  + -> ProLab analyzer
```

Recommended example:

- GasWorks configured for **COM5**
- com0com pair: **COM5 ↔ COM6**
- Translator `--gw-port COM6`
- Physical ProLab analyzer: **COM4**

Do **not** configure GasWorks and the translator to both open COM5.

## Install

Windows command prompt or PowerShell:

```powershell
py -m pip install -r requirements.txt
```

## Build the Windows application

Build on a Windows computer (PyInstaller does not cross-compile Windows
executables from Linux). Run:

```bat
scripts\build-windows.bat
```

This produces a windowed, one-folder application at:

```text
dist\GasWorks-ProLab-Serial-Translator\GasWorks-ProLab-Serial-Translator.exe
```

The application stores its default GUI traffic log and saved settings in the
current user's application-settings directory, not beside the installed `.exe`.
Every bridge start creates a separate timestamped session log; each session log
still rotates at 5 MiB with three retained backups.

## See available ports

```powershell
py translator.py --list-ports
```

## Run

If both sides use 9600 8-N-1:

```powershell
py translator.py --gw-port COM6 --pl-port COM4 --baud 9600
```

Traffic is printed to the console and written to a timestamped session log,
such as `prolab_translator_20260813-142530-123456.log`. Each session log
rotates at 5 MiB and retains three backups.

## Run the desktop UI

```powershell
py translator.py --gui
```

The GUI keeps serial work on background threads and shows the same traffic log
live. GasWorks and ProLab each have their own baud rate, data bits, parity,
stop bits, and XON/XOFF, RTS/CTS, and DSR/DTR flow-control settings.

The GUI retries a dropped or unavailable serial connection every three seconds
by default. The status changes to **Reconnecting** while it retries; use
**Stop Bridge** to stop retrying, or turn off **Auto reconnect** for a
single-attempt session.

## Simulate traffic without instruments

On Linux, use the **Simulate Traffic** button in the main GUI. It starts the
two `socat` PTY pairs, selects the correct endpoints in the bridge, and opens
compact GasWorks and ProLab test-client windows. Click **Connect** in both
clients, then click **Start Bridge** in the main window.

To start the whole fake bench in one step, use the restored launcher:

```bash
scripts/start-test-bench.sh
```

It opens the same single GUI application, starts the PTY pairs, connects both
fake clients, configures the bridge to `/tmp/translator_gw` and
`/tmp/translator_pl`, and starts the bridge. The main window removes only the
PTY aliases and relay processes it started when it closes.

This mode requires `socat`. It is Linux-only; on Windows, select your own
virtual COM-port pairs in the main window and test clients.

For a no-hardware text demonstration of the protocol conversion, run:

```powershell
py translator.py --simulate-traffic
```

Example log:

```text
2026-08-07 11:30:00.123 GW RX     | HEX: 53 4E 20 2D 63 30 31 0D | ASCII: SN -c01\r
2026-08-07 11:30:00.143 GW -> PL  | HEX: 53 4E 20 2D 63 30 31 0D 0A | ASCII: SN -c01\r\n  [CR normalized]
2026-08-07 11:30:00.200 PL -> GW  | HEX: 4F 4B 0D 0A | ASCII: OK\r\n
```

## Different baud rates

You can configure each side separately:

```powershell
py translator.py --gw-port COM6 --gw-baud 9600 --pl-port COM4 --pl-baud 19200
```

Other serial options are available:

```text
--bytesize 8
--parity N
--stopbits 1
--xonxoff
--rtscts
--dsrdtr
```

For devices whose non-baud settings differ, use the side-specific CLI options,
for example `--gw-parity E --pl-parity N` or `--gw-rtscts`. The GUI provides
these settings directly for each side; automatic reconnect is currently a GUI
feature.

Run:

```powershell
py translator.py --help
```

for all options.

## CR handling

The translator does not blindly replace every buffer ending. It keeps a trailing CR pending for a short guard interval (20 ms by default):

- `... 0D` with no LF -> sends `... 0D 0A`
- `... 0D 0A` -> stays `... 0D 0A`
- CR and LF received in separate reads -> still stays `CR LF`

Change the guard interval if needed:

```powershell
py translator.py --cr-wait-ms 50
```

## Shutdown

Press `Ctrl+C`. Both ports are closed cleanly.

## Test the normalization logic

```powershell
py -m unittest discover -v
```

On Linux, this suite also creates two temporary pseudo-terminal pairs and runs
an end-to-end byte-flow test through both bridge worker threads. No serial
hardware or `socat` installation is required. The PTY-specific test is skipped
on Windows.
