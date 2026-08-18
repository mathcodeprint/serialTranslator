# GasWorks ↔ ProLab Serial Translator

A small Python/pySerial bridge that fixes the serial command terminator mismatch:

- **GasWorks sends:** `CR` (`0D`)
- **ProLab expects:** `CR LF` (`0D 0A`)
- **ProLab responses:** forwarded back to GasWorks **byte-for-byte unchanged**

## Windows virtual COM ports (com0com)

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

### When com0com is needed

Use com0com on Windows when the GasWorks desktop program and this translator
run on the same PC and need to talk to each other over serial: it supplies a
virtual null-modem pair so each program opens a different endpoint. The same
topology can be made with another compatible virtual-COM-pair product, or with
two physical serial adapters joined by an appropriate null-modem cable.

The Windows **Simulate Traffic** test bench needs two virtual pairs. The app
does not create Windows virtual ports itself; create the pairs in com0com (or a
compatible tool) before clicking **Simulate Traffic**.

It is not needed on Linux for the built-in simulation (the application creates
temporary PTYs with `socat`). It is also not needed merely because the ProLab
is physical: the translator's Destination side should use the actual USB/RS-232
COM port assigned by Windows. You still need a virtual pair on the Source side
when connecting the local GasWorks program to this translator.

### Install and configure com0com

1. Download com0com from its [SourceForge project page](https://sourceforge.net/projects/com0com/), then run the installer as an administrator.
2. On Windows 10/11, confirm that the driver installed successfully in Device
   Manager. Older builds may be rejected by modern driver-signing protections;
   do not disable Windows security features just to proceed—use a version whose
   driver is trusted by your organization instead.
3. Open **Setup Command Prompt** from the com0com Start-menu group and create a
   pair. For the production topology above, create a pair with endpoints named
   `COM5` and `COM6`. Use the com0com setup GUI instead if you prefer.
4. Restart GasWorks/the translator if either was open, then select the two
   endpoints as shown above.

For the Windows simulator, create two pairs, for example `COM5` ↔ `COM6` and
`COM7` ↔ `COM8`. Select `COM5` in the fake GasWorks client and `COM8` in the
fake ProLab client; select `COM6` and `COM7` respectively in the translator.

The com0com project documents that its installer initially provides the internal
`CNCA0`/`CNCB0` pair and that additional pairs can be created using its setup
tools. [com0com documentation](https://github.com/tanvir-ahmed-m4/com0com)

### Create pairs with the com0com command-line tool

Open **Setup Command Prompt** from the com0com Start-menu group. Run it as an
administrator if com0com requires elevation on that computer. Before creating a
pair, choose unused COM numbers that do not overlap with physical instruments.

For a GasWorks-to-translator pair, create `COM5` ↔ `COM6`:

```text
install 0 PortName=COM5 PortName=COM6
```

Then enter `list` to confirm both endpoints. Configure GasWorks to use `COM5`
and configure this translator's **Source port** as `COM6`.

For the Windows fake test bench, create a second pair as well:

```text
install 0 PortName=COM7 PortName=COM8
list
```

Use `COM5` in the fake GasWorks client, `COM6` as the translator Source port,
`COM7` as the translator Destination port, and `COM8` in the fake ProLab
client. If your com0com version does not accept `PortName=COM5` directly, use
its Setup GUI to rename the generated `CNCA*`/`CNCB*` endpoints to those unused
COM numbers.

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

### Build a Windows installer

Install [Inno Setup 6](https://jrsoftware.org/isdl.php) on Windows, then run:

```bat
scripts\build-windows-installer.bat
```

It builds the application bundle and produces
`dist-installer\Serial-Protocol-Translator-Setup.exe`. The installer creates a
Start Menu shortcut and can optionally create a Desktop shortcut. It does not
install com0com; install and configure that separately only when your topology
needs virtual COM-port pairs.

To download the official Inno Setup installer and build without opening a web
browser, run:

```bat
scripts\download-inno-setup-and-build-installer.bat
```

It uses PowerShell to download Inno Setup 6.7.3 from its official GitHub release
and installs it silently. On Windows 7, TLS 1.2 must be enabled and available.

### Windows 7 / Python 3.7 builds

`scripts\build-windows.bat` detects Python 3.7 and automatically uses the
Windows-7-compatible build dependencies (`PyInstaller 5.13.2` and Pillow 9.5).
Use the 32-bit or 64-bit Python 3.7 interpreter that matches the target Windows
installation. Windows 7 is legacy and should be isolated and kept offline where
practical.

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

The main window opens as a session dashboard: use **Session Setup** to reveal
the port and per-side serial settings, and keep the rest of the window focused
on live activity. The activity view can filter new entries, copy or clear the
visible history, pause automatic following, and retain a bounded number of
lines. **Edit → Preferences** contains persistent startup/reconnect behavior,
serial timing defaults, activity display options, logging, and stop-confirmation
controls.

The GUI retries a dropped or unavailable serial connection every three seconds
by default. The status changes to **Reconnecting** while it retries; use
**Stop Bridge** to stop retrying, or turn off **Auto reconnect** for a
single-attempt session.

Use **Serial Console** to open a compact, independent serial terminal for an
available device. It has separate port, baud, framing, and flow-control
settings, displays transmitted and received bytes as hex and ASCII, and can
send Latin-1 text or exact hexadecimal bytes. A serial port can be opened by
only one program at a time, so do not connect the console to a port currently
used by the bridge, GasWorks, or ProLab.

On Windows, **Start bridge at sign-in** adds a per-user Startup entry. At the
next sign-in it opens the normal application and starts the bridge with the
saved settings; automatic reconnect keeps retrying until the configured COM
ports become available. This does not require administrator rights and is not a
Windows Service.

Use **Start minimized** to hide the window at launch. On Windows, the
notification-area icon is green while the bridge is active and red otherwise;
use its **Show** or **Quit** menu actions to restore or close the application.

Linux Mint uses the same green/red notification-area icon and **Show**/**Quit**
menu. Install the project dependencies with `python3 -m pip install -r
requirements.txt`; if the icon is not visible, enable Mint's notification-area
applet for the current desktop panel.

## Install on Linux Mint

From the project directory, install a per-user launcher and menu entry:

```bash
scripts/install-linux-mint.sh
```

It creates a virtual environment under `~/.local/share/serial-protocol-translator`
and adds **Serial Protocol Translator** to the Mint application menu. If Mint's
tray backend packages are missing, install them (administrator password needed):

```bash
scripts/install-linux-mint.sh --system-deps
```

Then enable Cinnamon's **System Tray** panel applet if it is not already visible.

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
