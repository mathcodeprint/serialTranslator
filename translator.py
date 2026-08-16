#!/usr/bin/env python3
"""GasWorks <-> ProLab serial protocol translator.

GasWorks side:
    CR-terminated commands are normalized to CR+LF before being sent to ProLab.

ProLab side:
    All bytes are forwarded back to GasWorks unchanged.

Typical com0com topology:
    GasWorks -> COM5 <== virtual pair ==> COM6 -> this program -> COM4 -> ProLab
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

import serial
from serial import SerialException
from serial.tools import list_ports


APP_NAME = "Serial Protocol Translator"
TEMPLATES = {"Generic bidirectional": False, "GasWorks ↔ ProLab": True}
WINDOWS_STARTUP_VALUE = "GasWorksProLabSerialTranslator"


def session_log_path(log_file: Optional[str], now: Optional[datetime] = None) -> Optional[str]:
    """Derive a unique timestamped log path for one bridge session."""
    if not log_file:
        return None
    path = Path(log_file).expanduser()
    timestamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S-%f")
    suffix = path.suffix or ".log"
    return str(path.with_name(f"{path.stem}_{timestamp}{suffix}"))


def windows_startup_command() -> str:
    """Return a quoted command that starts this installed app after user sign-in."""
    if getattr(sys, "frozen", False):
        return subprocess.list2cmdline([sys.executable, "--gui", "--autostart", "--start-minimized"])
    return subprocess.list2cmdline([sys.executable, str(Path(__file__).resolve()), "--gui", "--autostart", "--start-minimized"])


def set_windows_startup(enabled: bool) -> None:
    """Enable/disable the current user's Windows startup entry."""
    if not is_windows():
        raise RuntimeError("Windows startup is available on Windows only.")
    import winreg

    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, WINDOWS_STARTUP_VALUE, 0, winreg.REG_SZ, windows_startup_command())
        else:
            try:
                winreg.DeleteValue(key, WINDOWS_STARTUP_VALUE)
            except FileNotFoundError:
                pass


def windows_startup_enabled() -> bool:
    if not is_windows():
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
            winreg.QueryValueEx(key, WINDOWS_STARTUP_VALUE)
            return True
    except FileNotFoundError:
        return False


def ascii_view(data: bytes) -> str:
    """Return a compact, readable ASCII representation of binary data."""
    out: list[str] = []
    for b in data:
        if b == 0x0D:
            out.append(r"\r")
        elif b == 0x0A:
            out.append(r"\n")
        elif b == 0x09:
            out.append(r"\t")
        elif 0x20 <= b <= 0x7E:
            out.append(chr(b))
        else:
            out.append(".")
    return "".join(out)


def hex_view(data: bytes) -> str:
    return " ".join(f"{b:02X}" for b in data)


class TrafficLogger:
    """Thread-safe traffic logger with hex + ASCII output."""

    def __init__(
        self,
        log_file: Optional[str],
        console: bool = True,
        max_log_bytes: int = 5 * 1024 * 1024,
        backup_count: int = 3,
    ) -> None:
        self._logger = logging.getLogger("prolab_bridge")
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False
        self._lock = threading.Lock()

        # Avoid duplicate handlers if main() is called more than once in tests.
        self._logger.handlers.clear()

        formatter = logging.Formatter(
            fmt="%(asctime)s.%(msecs)03d %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

        if console:
            sh = logging.StreamHandler(sys.stdout)
            sh.setFormatter(formatter)
            self._logger.addHandler(sh)

        if log_file:
            path = Path(log_file).expanduser().resolve()
            path.parent.mkdir(parents=True, exist_ok=True)
            fh = RotatingFileHandler(
                path,
                maxBytes=max_log_bytes,
                backupCount=backup_count,
                encoding="utf-8",
            )
            fh.setFormatter(formatter)
            self._logger.addHandler(fh)

    def add_handler(self, handler: logging.Handler) -> None:
        """Attach an additional destination, such as a GUI log view."""
        with self._lock:
            self._logger.addHandler(handler)

    def remove_handler(self, handler: logging.Handler) -> None:
        with self._lock:
            self._logger.removeHandler(handler)

    def close(self) -> None:
        """Flush and close owned output handlers."""
        with self._lock:
            handlers = list(self._logger.handlers)
            self._logger.handlers.clear()
            for handler in handlers:
                handler.close()

    def traffic(self, direction: str, data: bytes, note: str = "") -> None:
        if not data:
            return
        suffix = f"  [{note}]" if note else ""
        line = (
            f"{direction:<9} | HEX: {hex_view(data)} | "
            f"ASCII: {ascii_view(data)}{suffix}"
        )
        with self._lock:
            self._logger.info(line)

    def info(self, message: str) -> None:
        with self._lock:
            self._logger.info(message)

    def error(self, message: str) -> None:
        with self._lock:
            self._logger.error(message)


class CrLfNormalizer:
    """Streaming CR -> CRLF normalizer.

    A CR is held briefly so that an LF arriving in the next serial chunk can be
    recognized. This prevents an existing CRLF sequence from becoming CRLFLF.
    """

    def __init__(self) -> None:
        self.pending_cr = False

    def feed(self, data: bytes) -> tuple[bytes, bool]:
        """Process bytes.

        Returns (output, changed). A trailing CR is retained internally until
        either another byte arrives or flush_pending() is called.
        """
        output = bytearray()
        changed = False

        for b in data:
            if self.pending_cr:
                if b == 0x0A:
                    # Already CR+LF: preserve unchanged.
                    output.extend(b"\r\n")
                    self.pending_cr = False
                    continue

                # Previous CR was CR-only. Normalize it before this byte.
                output.extend(b"\r\n")
                changed = True
                self.pending_cr = False

            if b == 0x0D:
                self.pending_cr = True
            else:
                output.append(b)

        return bytes(output), changed

    def flush_pending(self) -> tuple[bytes, bool]:
        """Convert a pending CR terminator to CR+LF."""
        if not self.pending_cr:
            return b"", False
        self.pending_cr = False
        return b"\r\n", True


class PassThroughNormalizer:
    pending_cr = False
    def feed(self, data: bytes) -> tuple[bytes, bool]: return data, False
    def flush_pending(self) -> tuple[bytes, bool]: return b"", False


def simulated_traffic() -> list[tuple[str, bytes, str]]:
    """Return a representative in-process bridge exchange for demonstration."""
    command = b"SN -c01\r"
    response = b"OK\r\n"
    normalizer = CrLfNormalizer()
    output, changed = normalizer.feed(command)
    final_output, final_changed = normalizer.flush_pending()
    return [
        ("SIM GW TX", command, "CR-only command"),
        (
            "SIM GW -> PL",
            output + final_output,
            "CR normalized" if changed or final_changed else "",
        ),
        ("SIM PL -> GW", response, "unchanged response"),
    ]


def print_simulated_traffic() -> int:
    for direction, data, note in simulated_traffic():
        suffix = f"  [{note}]" if note else ""
        print(f"{direction:<12} | HEX: {hex_view(data)} | ASCII: {ascii_view(data)}{suffix}")
    return 0


@dataclass(frozen=True)
class PortSettings:
    port: str
    baudrate: int
    bytesize: int
    parity: str
    stopbits: float
    timeout: float
    write_timeout: float
    xonxoff: bool
    rtscts: bool
    dsrdtr: bool


def open_serial(settings: PortSettings) -> serial.Serial:
    return serial.Serial(
        port=settings.port,
        baudrate=settings.baudrate,
        bytesize=settings.bytesize,
        parity=settings.parity,
        stopbits=settings.stopbits,
        timeout=settings.timeout,
        write_timeout=settings.write_timeout,
        xonxoff=settings.xonxoff,
        rtscts=settings.rtscts,
        dsrdtr=settings.dsrdtr,
    )


def write_all(port: serial.Serial, data: bytes) -> None:
    """Write the complete byte sequence or raise on timeout/disconnect."""
    view = memoryview(data)
    total = 0
    while total < len(data):
        count = port.write(view[total:])
        if count is None:
            # Defensive: pySerial 3.x normally returns an integer.
            count = 0
        if count <= 0:
            raise SerialException(f"Write to {port.port} returned {count} bytes")
        total += count


def read_available(port: serial.Serial, fallback_size: int = 1) -> bytes:
    """Read currently buffered bytes; block only up to the configured timeout."""
    waiting = port.in_waiting
    return port.read(waiting if waiting > 0 else fallback_size)


def gw_to_pl_worker(
    gw: serial.Serial,
    pl: serial.Serial,
    stop_event: threading.Event,
    traffic_log: TrafficLogger,
    cr_wait_s: float,
    error_event: Optional[threading.Event] = None,
    normalize_cr: bool = True,
) -> None:
    normalizer = CrLfNormalizer() if normalize_cr else PassThroughNormalizer()
    pending_since: Optional[float] = None

    try:
        while not stop_event.is_set():
            data = read_available(gw)
            now = time.monotonic()

            if data:
                traffic_log.traffic("GW RX", data)
                output, changed = normalizer.feed(data)

                if normalizer.pending_cr:
                    # Start/restart the short CR guard window.
                    pending_since = now
                else:
                    pending_since = None

                if output:
                    write_all(pl, output)
                    traffic_log.traffic(
                        "GW -> PL", output, "CR normalized" if changed else ""
                    )

            # If GasWorks stopped after CR, finalize it as CR+LF after guard time.
            if (
                normalizer.pending_cr
                and pending_since is not None
                and (now - pending_since) >= cr_wait_s
            ):
                output, changed = normalizer.flush_pending()
                write_all(pl, output)
                traffic_log.traffic(
                    "GW -> PL", output, "CR normalized" if changed else ""
                )
                pending_since = None

    except (SerialException, OSError) as exc:
        if not stop_event.is_set():
            traffic_log.error(f"GW -> PL bridge error: {exc}")
            if error_event is not None:
                error_event.set()
            stop_event.set()
    finally:
        # Best effort: do not silently discard a final CR during shutdown.
        if normalizer.pending_cr and pl.is_open:
            try:
                output, changed = normalizer.flush_pending()
                write_all(pl, output)
                traffic_log.traffic(
                    "GW -> PL", output, "CR normalized during shutdown" if changed else ""
                )
            except (SerialException, OSError):
                pass


def pl_to_gw_worker(
    pl: serial.Serial,
    gw: serial.Serial,
    stop_event: threading.Event,
    traffic_log: TrafficLogger,
    error_event: Optional[threading.Event] = None,
) -> None:
    try:
        while not stop_event.is_set():
            data = read_available(pl)
            if not data:
                continue

            # Required transparent pass-through: no line-ending/framing changes.
            write_all(gw, data)
            traffic_log.traffic("PL -> GW", data)

    except (SerialException, OSError) as exc:
        if not stop_event.is_set():
            traffic_log.error(f"PL -> GW bridge error: {exc}")
            if error_event is not None:
                error_event.set()
            stop_event.set()


def print_ports() -> None:
    ports = list(list_ports.comports())
    if not ports:
        print("No serial ports found.")
        return

    print("Available serial ports:")
    for p in ports:
        details = p.description or ""
        hwid = p.hwid or ""
        print(f"  {p.device:<10} {details}  [{hwid}]")


def parse_stopbits(value: str) -> float:
    mapping = {
        "1": serial.STOPBITS_ONE,
        "1.0": serial.STOPBITS_ONE,
        "1.5": serial.STOPBITS_ONE_POINT_FIVE,
        "2": serial.STOPBITS_TWO,
        "2.0": serial.STOPBITS_TWO,
    }
    try:
        return mapping[value]
    except KeyError as exc:
        raise argparse.ArgumentTypeError("stop bits must be 1, 1.5, or 2") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Bidirectional GasWorks/ProLab serial bridge. GasWorks CR-only "
            "commands are normalized to CR+LF; ProLab responses pass unchanged."
        )
    )
    parser.add_argument("--list-ports", action="store_true", help="List COM ports and exit")
    parser.add_argument(
        "--simulate-traffic",
        action="store_true",
        help="Print a representative in-process CR normalization exchange and exit",
    )
    parser.add_argument(
        "--gw-port",
        default="COM6",
        help="Translator side of the com0com pair (default: COM6)",
    )
    parser.add_argument(
        "--pl-port",
        default="COM4",
        help="Physical ProLab analyzer port (default: COM4)",
    )
    parser.add_argument(
        "--baud",
        type=int,
        default=9600,
        help="Default baud rate for both sides (default: 9600)",
    )
    parser.add_argument("--gw-baud", type=int, help="Override GasWorks-side baud rate")
    parser.add_argument("--pl-baud", type=int, help="Override ProLab-side baud rate")
    parser.add_argument(
        "--bytesize",
        type=int,
        choices=(5, 6, 7, 8),
        default=8,
        help="Data bits (default: 8)",
    )
    parser.add_argument("--gw-bytesize", type=int, choices=(5, 6, 7, 8), help="Override GasWorks-side data bits")
    parser.add_argument("--pl-bytesize", type=int, choices=(5, 6, 7, 8), help="Override ProLab-side data bits")
    parser.add_argument(
        "--parity",
        choices=("N", "E", "O", "M", "S"),
        default="N",
        help="Parity: N/E/O/M/S (default: N)",
    )
    parser.add_argument("--gw-parity", choices=("N", "E", "O", "M", "S"), help="Override GasWorks-side parity")
    parser.add_argument("--pl-parity", choices=("N", "E", "O", "M", "S"), help="Override ProLab-side parity")
    parser.add_argument(
        "--stopbits",
        type=parse_stopbits,
        default=serial.STOPBITS_ONE,
        help="Stop bits: 1, 1.5, or 2 (default: 1)",
    )
    parser.add_argument("--gw-stopbits", type=parse_stopbits, help="Override GasWorks-side stop bits")
    parser.add_argument("--pl-stopbits", type=parse_stopbits, help="Override ProLab-side stop bits")
    parser.add_argument(
        "--read-timeout-ms",
        type=float,
        default=10.0,
        help="Serial read timeout in milliseconds (default: 10)",
    )
    parser.add_argument(
        "--cr-wait-ms",
        type=float,
        default=20.0,
        help=(
            "How long to hold a trailing CR waiting for a possible LF before "
            "adding LF (default: 20 ms)"
        ),
    )
    parser.add_argument(
        "--write-timeout",
        type=float,
        default=2.0,
        help="Serial write timeout in seconds (default: 2.0)",
    )
    parser.add_argument("--xonxoff", action="store_true", help="Enable XON/XOFF flow control")
    parser.add_argument("--rtscts", action="store_true", help="Enable RTS/CTS flow control")
    parser.add_argument("--dsrdtr", action="store_true", help="Enable DSR/DTR flow control")
    parser.add_argument("--gw-xonxoff", action="store_true", help="Enable XON/XOFF on the GasWorks side")
    parser.add_argument("--pl-xonxoff", action="store_true", help="Enable XON/XOFF on the ProLab side")
    parser.add_argument("--gw-rtscts", action="store_true", help="Enable RTS/CTS on the GasWorks side")
    parser.add_argument("--pl-rtscts", action="store_true", help="Enable RTS/CTS on the ProLab side")
    parser.add_argument("--gw-dsrdtr", action="store_true", help="Enable DSR/DTR on the GasWorks side")
    parser.add_argument("--pl-dsrdtr", action="store_true", help="Enable DSR/DTR on the ProLab side")
    parser.add_argument(
        "--log-file",
        default="prolab_translator.log",
        help="Traffic log file; use empty string to disable (default: prolab_translator.log)",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Disable console traffic logging (file logging can remain enabled)",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    if args.list_ports:
        print_ports()
        return 0
    if args.simulate_traffic:
        return print_simulated_traffic()

    if args.gw_port.upper() == args.pl_port.upper():
        print("ERROR: --gw-port and --pl-port must be different ports.", file=sys.stderr)
        return 2

    if args.read_timeout_ms < 0 or args.cr_wait_ms < 0:
        print("ERROR: timeout values cannot be negative.", file=sys.stderr)
        return 2
    if args.write_timeout <= 0:
        print("ERROR: --write-timeout must be greater than zero.", file=sys.stderr)
        return 2

    session_log = session_log_path(args.log_file)
    traffic_log = TrafficLogger(session_log, console=not args.quiet)

    gw_settings = PortSettings(
        port=args.gw_port,
        baudrate=args.gw_baud or args.baud,
        bytesize=args.gw_bytesize or args.bytesize,
        parity=args.gw_parity or args.parity,
        stopbits=args.gw_stopbits or args.stopbits,
        timeout=args.read_timeout_ms / 1000.0,
        write_timeout=args.write_timeout,
        xonxoff=args.xonxoff or args.gw_xonxoff,
        rtscts=args.rtscts or args.gw_rtscts,
        dsrdtr=args.dsrdtr or args.gw_dsrdtr,
    )
    pl_settings = PortSettings(
        port=args.pl_port,
        baudrate=args.pl_baud or args.baud,
        bytesize=args.pl_bytesize or args.bytesize,
        parity=args.pl_parity or args.parity,
        stopbits=args.pl_stopbits or args.stopbits,
        timeout=args.read_timeout_ms / 1000.0,
        write_timeout=args.write_timeout,
        xonxoff=args.xonxoff or args.pl_xonxoff,
        rtscts=args.rtscts or args.pl_rtscts,
        dsrdtr=args.dsrdtr or args.pl_dsrdtr,
    )

    gw: Optional[serial.Serial] = None
    pl: Optional[serial.Serial] = None
    stop_event = threading.Event()
    error_event = threading.Event()

    try:
        traffic_log.info(f"Starting {APP_NAME}")
        if session_log:
            traffic_log.info(f"Session log: {session_log}")
        traffic_log.info(
            f"GasWorks-side translator port: {gw_settings.port} "
            f"({gw_settings.baudrate},{gw_settings.bytesize},{gw_settings.parity},{args.stopbits})"
        )
        traffic_log.info(
            f"ProLab physical port:          {pl_settings.port} "
            f"({pl_settings.baudrate},{pl_settings.bytesize},{pl_settings.parity},{args.stopbits})"
        )
        traffic_log.info(
            "NOTE: GasWorks must open the OTHER end of the com0com pair "
            f"(for example COM5 if this program opens {gw_settings.port})."
        )

        gw = open_serial(gw_settings)
        traffic_log.info(f"Opened GasWorks-side port {gw.port}")
        pl = open_serial(pl_settings)
        traffic_log.info(f"Opened ProLab-side port {pl.port}")

        t_gw_pl = threading.Thread(
            target=gw_to_pl_worker,
            name="gw-to-pl",
            args=(gw, pl, stop_event, traffic_log, args.cr_wait_ms / 1000.0, error_event),
            daemon=True,
        )
        t_pl_gw = threading.Thread(
            target=pl_to_gw_worker,
            name="pl-to-gw",
            args=(pl, gw, stop_event, traffic_log, error_event),
            daemon=True,
        )
        t_gw_pl.start()
        t_pl_gw.start()

        traffic_log.info("Bridge running. Press Ctrl+C to stop.")

        while not stop_event.wait(0.25):
            if not t_gw_pl.is_alive() or not t_pl_gw.is_alive():
                stop_event.set()
                break

        t_gw_pl.join(timeout=2.0)
        t_pl_gw.join(timeout=2.0)

        return 1 if error_event.is_set() else 0

    except KeyboardInterrupt:
        traffic_log.info("Ctrl+C received; shutting down.")
        stop_event.set()
        return 0
    except SerialException as exc:
        traffic_log.error(f"Serial port error: {exc}")
        traffic_log.error(
            "Check the COM numbers, confirm no other program has the translator/analyzer "
            "port open, and verify baud/parity/stop-bit settings."
        )
        return 1
    finally:
        stop_event.set()
        for port in (gw, pl):
            if port is not None and port.is_open:
                try:
                    port.close()
                except OSError:
                    pass
        traffic_log.info("Bridge stopped.")
        traffic_log.close()



# --- Shared port discovery and settings helpers ---

import json
import os
import platform
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from serial.tools import list_ports


LINUX_TEST_ALIASES = {
    "fake_gasworks": "/tmp/gasworks",
    "translator_gw": "/tmp/translator_gw",
    "translator_pl": "/tmp/translator_pl",
    "fake_prolab": "/tmp/prolab",
}

WINDOWS_TEST_PORTS = {
    "fake_gasworks": "COM5",
    "translator_gw": "COM6",
    "translator_pl": "COM7",
    "fake_prolab": "COM8",
}


@dataclass(frozen=True)
class PortChoice:
    device: str
    description: str

    @property
    def display(self) -> str:
        return f"{self.device} — {self.description}" if self.description else self.device


def platform_name() -> str:
    return platform.system().lower()


def is_windows() -> bool:
    return platform_name() == "windows"


def is_linux() -> bool:
    return platform_name() == "linux"


def _device_key(device: str) -> str:
    return device.casefold() if is_windows() else device


def discover_serial_ports(extra_devices: Iterable[str] = ()) -> list[PortChoice]:
    """Return physical/virtual serial ports plus known Linux test aliases.

    pySerial lists normal COM and /dev/tty devices. Linux PTY aliases created by
    socat under /tmp are not always returned by list_ports, so the known test
    aliases are added explicitly when they exist. Current/saved device names can
    also be supplied through extra_devices so editable combo-box values are not
    lost during refreshes.
    """
    found: dict[str, PortChoice] = {}

    for p in list_ports.comports():
        device = str(p.device)
        description = (p.description or "Serial port").strip()
        found[_device_key(device)] = PortChoice(device, description)

    if is_linux():
        for role, device in LINUX_TEST_ALIASES.items():
            if Path(device).exists():
                description = f"ProLab test PTY ({role.replace('_', ' ')})"
                found.setdefault(_device_key(device), PortChoice(device, description))

    for raw in extra_devices:
        device = (raw or "").strip()
        if not device:
            continue
        # Only add an explicit extra if it currently exists on POSIX, or always
        # on Windows where a temporarily disconnected COM selection is useful.
        if is_windows() or Path(device).exists():
            found.setdefault(_device_key(device), PortChoice(device, "Saved/manual selection"))

    return sorted(found.values(), key=lambda item: _device_key(item.device))


def device_is_available(device: str, ports: Iterable[PortChoice]) -> bool:
    key = _device_key(device.strip())
    return any(_device_key(p.device) == key for p in ports)


def preferred_role_device(
    role: str,
    ports: Iterable[PortChoice],
    saved: Optional[str] = None,
    exclude: Iterable[str] = (),
) -> str:
    """Choose a sensible device for a simulator/test-bench role."""
    port_list = list(ports)
    excluded = {_device_key(x) for x in exclude if x}

    if saved:
        # Remember an explicit user choice even if the cable/virtual port is
        # temporarily absent. Editable combo boxes still allow correction.
        return saved

    preferred = None
    if is_windows():
        preferred = WINDOWS_TEST_PORTS.get(role)
    elif is_linux():
        preferred = LINUX_TEST_ALIASES.get(role)

    if preferred and _device_key(preferred) not in excluded and device_is_available(preferred, port_list):
        return preferred

    for p in port_list:
        if _device_key(p.device) not in excluded:
            return p.device
    return preferred or ""


def translator_default_devices(
    ports: Iterable[PortChoice],
    saved_gw: Optional[str] = None,
    saved_pl: Optional[str] = None,
) -> tuple[str, str]:
    """Select translator ports, preferring the full fake test bench when found."""
    port_list = list(ports)

    if saved_gw or saved_pl:
        gw = saved_gw or preferred_role_device("translator_gw", port_list)
        pl = saved_pl or preferred_role_device("translator_pl", port_list, exclude=(gw,))
        return gw, pl

    if is_windows():
        # Full test bench present: COM5<->COM6 and COM7<->COM8.
        full_test = all(device_is_available(p, port_list) for p in ("COM5", "COM6", "COM7", "COM8"))
        if full_test:
            return "COM6", "COM7"

        gw = "COM6" if device_is_available("COM6", port_list) else preferred_role_device("translator_gw", port_list)
        if device_is_available("COM4", port_list) and _device_key("COM4") != _device_key(gw):
            return gw, "COM4"
        pl = preferred_role_device("translator_pl", port_list, exclude=(gw,))
        return gw, pl

    if is_linux():
        aliases_ready = all(Path(p).exists() for p in LINUX_TEST_ALIASES.values())
        if aliases_ready:
            return LINUX_TEST_ALIASES["translator_gw"], LINUX_TEST_ALIASES["translator_pl"]

    gw = preferred_role_device("translator_gw", port_list)
    pl = preferred_role_device("translator_pl", port_list, exclude=(gw,))
    return gw, pl


def settings_directory() -> Path:
    if is_windows():
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        path = Path(base) / "ProLabSerialTranslator" if base else Path.home() / ".prolab_serial_translator"
    else:
        base = os.environ.get("XDG_CONFIG_HOME")
        path = Path(base) / "prolab_serial_translator" if base else Path.home() / ".config" / "prolab_serial_translator"
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_settings(name: str) -> dict:
    path = settings_directory() / f"{name}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def save_settings(name: str, data: dict) -> None:
    path = settings_directory() / f"{name}.json"
    temp = path.with_suffix(".tmp")
    try:
        temp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        temp.replace(path)
    except OSError:
        # Settings persistence should never stop the serial application.
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


# --- Main translator GUI ---

import logging
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Optional

import serial
from serial import SerialException



COMMON_BAUD_RATES = (
    "300",
    "600",
    "1200",
    "2400",
    "4800",
    "9600",
    "19200",
    "38400",
    "57600",
    "115200",
)


class TrayIcon:
    """Small notification-area indicator for Windows and Linux desktop sessions."""
    def __init__(self, root: tk.Tk, on_quit: callable) -> None:
        self.root, self.on_quit, self.icon = root, on_quit, None
        if not (is_windows() or is_linux()):
            return
        try:
            import pystray
            from PIL import Image, ImageDraw
            self.pystray, self.Image, self.ImageDraw = pystray, Image, ImageDraw
            self.icon = pystray.Icon("GasWorksProLabTranslator", self._image(False), APP_NAME)
            self.icon.menu = pystray.Menu(
                pystray.MenuItem("Show", self._show), pystray.MenuItem("Quit", self._quit)
            )
            threading.Thread(target=self.icon.run, daemon=True).start()
        except ImportError:
            self.icon = None

    def _image(self, active: bool):
        image = self.Image.new("RGBA", (32, 32), (0, 0, 0, 0))
        draw = self.ImageDraw.Draw(image)
        draw.ellipse((3, 3, 29, 29), fill=(30, 150, 70, 255) if active else (180, 45, 45, 255))
        return image

    def set_active(self, active: bool) -> None:
        if self.icon:
            self.icon.icon = self._image(active)
            self.icon.title = f"{APP_NAME}: {'Active' if active else 'Inactive'}"

    def _show(self, *_args) -> None:
        self.root.after(0, lambda: (self.root.deiconify(), self.root.lift()))

    def _quit(self, *_args) -> None:
        self.root.after(0, self.on_quit)

    def close(self) -> None:
        if self.icon:
            self.icon.stop()


class QueueLogHandler(logging.Handler):
    """Send formatted Python logging records to a thread-safe queue."""

    def __init__(self, output_queue: queue.Queue[tuple[str, str]]) -> None:
        super().__init__()
        self.output_queue = output_queue

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.output_queue.put(("log", self.format(record)))
        except Exception:
            self.handleError(record)


class SerialBridgeController:
    """Run the bridge workers without blocking Tkinter's UI thread."""

    def __init__(self, event_queue: queue.Queue[tuple[str, str]]) -> None:
        self.event_queue = event_queue
        self.stop_event = threading.Event()
        self._supervisor: Optional[threading.Thread] = None
        self._manual_stop = False
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        thread = self._supervisor
        return bool(thread and thread.is_alive())

    def start(
        self,
        gw_settings: PortSettings,
        pl_settings: PortSettings,
        cr_wait_s: float,
        log_file: Optional[str],
        auto_reconnect: bool,
        reconnect_delay_s: float,
        normalize_cr: bool = True,
    ) -> None:
        with self._lock:
            if self.running:
                raise RuntimeError("Bridge is already running")

            self.stop_event = threading.Event()
            self._manual_stop = False
            self._supervisor = threading.Thread(
                target=self._run,
                name="bridge-supervisor",
                args=(
                    gw_settings, pl_settings, cr_wait_s, log_file,
                    auto_reconnect, reconnect_delay_s, normalize_cr,
                ),
                daemon=True,
            )
            self._supervisor.start()

    def stop(self) -> None:
        with self._lock:
            if not self.running:
                return
            self._manual_stop = True
            self.stop_event.set()
            self.event_queue.put(("state", "stopping|Stopping bridge..."))

    def _run(
        self,
        gw_settings: PortSettings,
        pl_settings: PortSettings,
        cr_wait_s: float,
        log_file: Optional[str],
        auto_reconnect: bool,
        reconnect_delay_s: float,
        normalize_cr: bool,
    ) -> None:
        queue_handler: Optional[QueueLogHandler] = None
        traffic_log: Optional[TrafficLogger] = None
        terminal_error = False

        try:
            traffic_log = TrafficLogger(log_file, console=False)
            queue_handler = QueueLogHandler(self.event_queue)
            queue_handler.setFormatter(
                logging.Formatter(
                    fmt="%(asctime)s.%(msecs)03d %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S",
                )
            )
            # TrafficLogger owns this logger.  Adding a GUI handler preserves its
            # normal file logging while also mirroring every line into Tkinter.
            traffic_log.add_handler(queue_handler)

            traffic_log.info(f"Starting {APP_NAME}")
            if log_file:
                traffic_log.info(f"Session log: {log_file}")
            traffic_log.info(
                "GasWorks-side translator port: "
                f"{gw_settings.port} ({gw_settings.baudrate},"
                f"{gw_settings.bytesize},{gw_settings.parity},{gw_settings.stopbits})"
            )
            traffic_log.info(
                "ProLab physical port:          "
                f"{pl_settings.port} ({pl_settings.baudrate},"
                f"{pl_settings.bytesize},{pl_settings.parity},{pl_settings.stopbits})"
            )

            attempt = 0
            while not self.stop_event.is_set():
                gw: Optional[serial.Serial] = None
                pl: Optional[serial.Serial] = None
                t_gw_pl: Optional[threading.Thread] = None
                t_pl_gw: Optional[threading.Thread] = None
                session_stop = threading.Event()
                session_failed = False
                try:
                    if attempt:
                        message = f"Reconnecting (attempt {attempt})..."
                        traffic_log.info(message)
                        self.event_queue.put(("state", f"reconnecting|{message}"))
                    gw = open_serial(gw_settings)
                    traffic_log.info(f"Opened GasWorks-side port {gw.port}")
                    pl = open_serial(pl_settings)
                    traffic_log.info(f"Opened ProLab-side port {pl.port}")
                    t_gw_pl = threading.Thread(
                        target=gw_to_pl_worker,
                        name="gw-to-pl",
                        args=(gw, pl, session_stop, traffic_log, cr_wait_s, None, normalize_cr),
                        daemon=True,
                    )
                    t_pl_gw = threading.Thread(
                        target=pl_to_gw_worker,
                        name="pl-to-gw",
                        args=(pl, gw, session_stop, traffic_log),
                        daemon=True,
                    )
                    t_gw_pl.start()
                    t_pl_gw.start()
                    traffic_log.info("Bridge running.")
                    self.event_queue.put(("state", "running|Bridge running"))
                    while not self.stop_event.wait(0.20):
                        if session_stop.is_set() or not t_gw_pl.is_alive() or not t_pl_gw.is_alive():
                            session_failed = True
                            session_stop.set()
                            break
                except (SerialException, OSError, ValueError) as exc:
                    session_failed = True
                    traffic_log.error(f"Serial port error: {exc}")
                except Exception as exc:
                    session_failed = True
                    traffic_log.error(f"Unexpected bridge error: {exc}")
                finally:
                    session_stop.set()
                    for thread in (t_gw_pl, t_pl_gw):
                        if thread is not None:
                            thread.join(timeout=2.0)
                    for port in (gw, pl):
                        if port is not None and port.is_open:
                            try:
                                port.close()
                            except OSError:
                                pass

                if self.stop_event.is_set() or self._manual_stop:
                    break
                if not session_failed:
                    break
                if not auto_reconnect:
                    terminal_error = True
                    self.event_queue.put(("state", "error|Bridge stopped due to a serial error"))
                    break
                attempt += 1
                self.event_queue.put((
                    "state",
                    f"reconnecting|Serial connection lost; retrying in {reconnect_delay_s:g} seconds",
                ))
                if self.stop_event.wait(reconnect_delay_s):
                    break
        except Exception as exc:
            terminal_error = True
            if traffic_log is not None:
                traffic_log.error(f"Unexpected bridge error: {exc}")
            else:
                self.event_queue.put(("log", f"Unexpected bridge error: {exc}"))
            self.event_queue.put(("state", f"error|{exc}"))
        finally:
            if traffic_log is not None:
                traffic_log.info("Bridge stopped.")
                if queue_handler is not None:
                    traffic_log.remove_handler(queue_handler)
                    queue_handler.close()
                traffic_log.close()

            if not terminal_error:
                self.event_queue.put(("state", "stopped|Bridge stopped"))


class ProLabTranslatorGUI:
    """Desktop interface for configuring and running the serial translator."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(APP_NAME)
        self.root.geometry("860x600")
        self.root.minsize(720, 500)

        self.event_queue: queue.Queue[tuple[str, str]] = queue.Queue()
        self.controller = SerialBridgeController(self.event_queue)
        self.test_bench: Optional[LinuxVirtualTestBench] = None
        self.test_clients: list[BaseTestClient] = []
        self.port_display_to_device: dict[str, str] = {}
        self.saved_settings = load_settings("translator_gui")
        self.profiles = load_settings("translator_profiles")

        self._make_variables()
        self.tray = TrayIcon(self.root, self.on_close)
        self._build_ui()
        self.refresh_ports()
        self._set_running_ui(False)

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(75, self._drain_events)

    def _make_variables(self) -> None:
        saved = self.saved_settings
        self.gw_port_var = tk.StringVar(value=str(saved.get("gw_port", "")))
        self.template_var = tk.StringVar(value=str(saved.get("template", "Generic bidirectional")))
        self.profile_var = tk.StringVar()
        self.pl_port_var = tk.StringVar(value=str(saved.get("pl_port", "")))
        self.gw_baud_var = tk.StringVar(value=str(saved.get("gw_baud", "9600")))
        self.pl_baud_var = tk.StringVar(value=str(saved.get("pl_baud", "9600")))
        # Fall back to the pre-per-port settings so existing installations keep
        # their working serial profile on upgrade.
        self.gw_bytesize_var = tk.StringVar(value=str(saved.get("gw_bytesize", saved.get("bytesize", "8"))))
        self.pl_bytesize_var = tk.StringVar(value=str(saved.get("pl_bytesize", saved.get("bytesize", "8"))))
        self.gw_parity_var = tk.StringVar(value=str(saved.get("gw_parity", saved.get("parity", "N"))))
        self.pl_parity_var = tk.StringVar(value=str(saved.get("pl_parity", saved.get("parity", "N"))))
        self.gw_stopbits_var = tk.StringVar(value=str(saved.get("gw_stopbits", saved.get("stopbits", "1"))))
        self.pl_stopbits_var = tk.StringVar(value=str(saved.get("pl_stopbits", saved.get("stopbits", "1"))))
        self.read_timeout_var = tk.StringVar(value=str(saved.get("read_timeout_ms", "10")))
        self.cr_wait_var = tk.StringVar(value=str(saved.get("cr_wait_ms", "20")))
        self.write_timeout_var = tk.StringVar(value=str(saved.get("write_timeout", "2.0")))
        self.gw_xonxoff_var = tk.BooleanVar(value=bool(saved.get("gw_xonxoff", saved.get("xonxoff", False))))
        self.pl_xonxoff_var = tk.BooleanVar(value=bool(saved.get("pl_xonxoff", saved.get("xonxoff", False))))
        self.gw_rtscts_var = tk.BooleanVar(value=bool(saved.get("gw_rtscts", saved.get("rtscts", False))))
        self.pl_rtscts_var = tk.BooleanVar(value=bool(saved.get("pl_rtscts", saved.get("rtscts", False))))
        self.gw_dsrdtr_var = tk.BooleanVar(value=bool(saved.get("gw_dsrdtr", saved.get("dsrdtr", False))))
        self.pl_dsrdtr_var = tk.BooleanVar(value=bool(saved.get("pl_dsrdtr", saved.get("dsrdtr", False))))
        self.auto_reconnect_var = tk.BooleanVar(value=bool(saved.get("auto_reconnect", True)))
        self.reconnect_delay_var = tk.StringVar(value=str(saved.get("reconnect_delay_s", "3")))
        self.start_minimized_var = tk.BooleanVar(value=bool(saved.get("start_minimized", False)))
        default_log_file = settings_directory() / "prolab_translator.log"
        self.log_file_var = tk.StringVar(
            value=str(saved.get("log_file") or default_log_file)
        )
        self.status_var = tk.StringVar(value="Stopped")

    def _build_ui(self) -> None:
        self._build_menu()
        outer = ttk.Frame(self.root, padding=6)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(4, weight=1)

        title = ttk.Label(
            outer,
            text="Serial Protocol Translator",
            font=("Segoe UI", 13, "bold"),
        )
        title.grid(row=0, column=0, sticky="w")

        topology = ttk.Label(
            outer,
            text=(
                "Bidirectional serial bridge. Choose a template or save a reusable profile."
            ),
        )
        topology.grid(row=1, column=0, sticky="w", pady=(1, 6))

        ports_frame = ttk.LabelFrame(outer, text="Serial ports", padding=6)
        ports_frame.grid(row=2, column=0, sticky="ew")
        ports_frame.columnconfigure(1, weight=1)
        ports_frame.columnconfigure(3, weight=1)

        ttk.Label(ports_frame, text="Source port").grid(
            row=0, column=0, sticky="w", padx=(0, 6), pady=3
        )
        self.gw_port_combo = ttk.Combobox(
            ports_frame, textvariable=self.gw_port_var, state="normal", width=34
        )
        self.gw_port_combo.grid(row=0, column=1, sticky="ew", padx=(0, 10), pady=2)

        ttk.Label(ports_frame, text="Source baud").grid(
            row=0, column=2, sticky="w", padx=(0, 6), pady=3
        )
        self.gw_baud_combo = ttk.Combobox(
            ports_frame,
            textvariable=self.gw_baud_var,
            values=COMMON_BAUD_RATES,
            state="normal",
            width=12,
        )
        self.gw_baud_combo.grid(row=0, column=3, sticky="ew", pady=2)

        ttk.Label(ports_frame, text="Destination port").grid(
            row=1, column=0, sticky="w", padx=(0, 6), pady=3
        )
        self.pl_port_combo = ttk.Combobox(
            ports_frame, textvariable=self.pl_port_var, state="normal", width=34
        )
        self.pl_port_combo.grid(row=1, column=1, sticky="ew", padx=(0, 10), pady=2)

        ttk.Label(ports_frame, text="Destination baud").grid(
            row=1, column=2, sticky="w", padx=(0, 6), pady=3
        )
        self.pl_baud_combo = ttk.Combobox(
            ports_frame,
            textvariable=self.pl_baud_var,
            values=COMMON_BAUD_RATES,
            state="normal",
            width=12,
        )
        self.pl_baud_combo.grid(row=1, column=3, sticky="ew", pady=2)

        self.refresh_button = ttk.Button(
            ports_frame, text="Refresh ports", command=self.refresh_ports
        )
        self.refresh_button.grid(row=0, column=4, rowspan=2, padx=(12, 0), sticky="ns")

        settings_frame = ttk.LabelFrame(outer, text="Serial / translator settings", padding=6)
        settings_frame.grid(row=3, column=0, sticky="ew", pady=(6, 6))

        settings_frame.columnconfigure(1, weight=1)
        settings_frame.columnconfigure(2, weight=1)
        ttk.Label(settings_frame, text="", width=14).grid(row=0, column=0)
        ttk.Label(settings_frame, text="GasWorks side", font=("TkDefaultFont", 9, "bold")).grid(row=0, column=1, sticky="w")
        ttk.Label(settings_frame, text="ProLab side", font=("TkDefaultFont", 9, "bold")).grid(row=0, column=2, sticky="w")

        def side_combo(row: int, label: str, gw_var: tk.StringVar, pl_var: tk.StringVar, values: tuple[str, ...]) -> tuple[ttk.Combobox, ttk.Combobox]:
            ttk.Label(settings_frame, text=label).grid(row=row, column=0, sticky="w", pady=(3, 0))
            gw_combo = ttk.Combobox(settings_frame, textvariable=gw_var, values=values, state="readonly", width=8)
            pl_combo = ttk.Combobox(settings_frame, textvariable=pl_var, values=values, state="readonly", width=8)
            gw_combo.grid(row=row, column=1, sticky="w", pady=(3, 0))
            pl_combo.grid(row=row, column=2, sticky="w", pady=(3, 0))
            return gw_combo, pl_combo

        self.gw_bytesize_combo, self.pl_bytesize_combo = side_combo(1, "Data bits", self.gw_bytesize_var, self.pl_bytesize_var, ("5", "6", "7", "8"))
        self.gw_parity_combo, self.pl_parity_combo = side_combo(2, "Parity", self.gw_parity_var, self.pl_parity_var, ("N", "E", "O", "M", "S"))
        self.gw_stopbits_combo, self.pl_stopbits_combo = side_combo(3, "Stop bits", self.gw_stopbits_var, self.pl_stopbits_var, ("1", "1.5", "2"))

        def side_check(row: int, label: str, gw_var: tk.BooleanVar, pl_var: tk.BooleanVar) -> tuple[ttk.Checkbutton, ttk.Checkbutton]:
            ttk.Label(settings_frame, text=label).grid(row=row, column=0, sticky="w", pady=(3, 0))
            gw_check = ttk.Checkbutton(settings_frame, variable=gw_var)
            pl_check = ttk.Checkbutton(settings_frame, variable=pl_var)
            gw_check.grid(row=row, column=1, sticky="w", pady=(3, 0))
            pl_check.grid(row=row, column=2, sticky="w", pady=(3, 0))
            return gw_check, pl_check

        self.gw_xonxoff_check, self.pl_xonxoff_check = side_check(4, "XON/XOFF", self.gw_xonxoff_var, self.pl_xonxoff_var)
        self.gw_rtscts_check, self.pl_rtscts_check = side_check(5, "RTS/CTS", self.gw_rtscts_var, self.pl_rtscts_var)
        self.gw_dsrdtr_check, self.pl_dsrdtr_check = side_check(6, "DSR/DTR", self.gw_dsrdtr_var, self.pl_dsrdtr_var)

        timing_frame = ttk.Frame(settings_frame)
        timing_frame.grid(row=1, column=3, rowspan=6, sticky="nsew", padx=(14, 0))
        ttk.Label(timing_frame, text="CR wait (ms)").grid(row=0, column=0, sticky="w")
        self.cr_wait_entry = ttk.Entry(timing_frame, textvariable=self.cr_wait_var, width=9)
        self.cr_wait_entry.grid(row=0, column=1, sticky="w", padx=(5, 0))
        ttk.Label(timing_frame, text="Read timeout (ms)").grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.read_timeout_entry = ttk.Entry(timing_frame, textvariable=self.read_timeout_var, width=9)
        self.read_timeout_entry.grid(row=1, column=1, sticky="w", padx=(5, 0), pady=(4, 0))
        ttk.Label(timing_frame, text="Write timeout (s)").grid(row=2, column=0, sticky="w", pady=(4, 0))
        self.write_timeout_entry = ttk.Entry(timing_frame, textvariable=self.write_timeout_var, width=9)
        self.write_timeout_entry.grid(row=2, column=1, sticky="w", padx=(5, 0), pady=(4, 0))
        self.auto_reconnect_check = ttk.Checkbutton(timing_frame, text="Auto reconnect", variable=self.auto_reconnect_var)
        self.auto_reconnect_check.grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Label(timing_frame, text="Retry delay (s)").grid(row=4, column=0, sticky="w", pady=(4, 0))
        self.reconnect_delay_entry = ttk.Entry(timing_frame, textvariable=self.reconnect_delay_var, width=9)
        self.reconnect_delay_entry.grid(row=4, column=1, sticky="w", padx=(5, 0), pady=(4, 0))

        log_frame = ttk.Frame(settings_frame)
        log_frame.grid(row=7, column=0, columnspan=4, sticky="ew", pady=(6, 0))
        log_frame.columnconfigure(1, weight=1)
        ttk.Label(log_frame, text="Log file").grid(row=0, column=0, sticky="w", padx=(0, 6))
        self.log_entry = ttk.Entry(log_frame, textvariable=self.log_file_var)
        self.log_entry.grid(row=0, column=1, sticky="ew")
        self.browse_button = ttk.Button(log_frame, text="Browse...", command=self.choose_log_file)
        self.browse_button.grid(row=0, column=2, padx=(6, 0))

        traffic_frame = ttk.LabelFrame(outer, text="Live traffic", padding=6)
        traffic_frame.grid(row=4, column=0, sticky="nsew")
        traffic_frame.columnconfigure(0, weight=1)
        traffic_frame.rowconfigure(0, weight=1)

        self.traffic_text = tk.Text(
            traffic_frame,
            wrap="none",
            font=("Consolas", 9),
            height=12,
            state="disabled",
        )
        self.traffic_text.grid(row=0, column=0, sticky="nsew")

        yscroll = ttk.Scrollbar(traffic_frame, orient="vertical", command=self.traffic_text.yview)
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll = ttk.Scrollbar(traffic_frame, orient="horizontal", command=self.traffic_text.xview)
        xscroll.grid(row=1, column=0, sticky="ew")
        self.traffic_text.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)

        controls = ttk.Frame(outer)
        controls.grid(row=5, column=0, sticky="ew", pady=(6, 0))
        controls.columnconfigure(3, weight=1)

        self.start_button = ttk.Button(controls, text="Start Bridge", command=self.start_bridge)
        self.start_button.grid(row=0, column=0, padx=(0, 6))

        self.stop_button = ttk.Button(controls, text="Stop Bridge", command=self.stop_bridge)
        self.stop_button.grid(row=0, column=1, padx=(0, 6))

        self.clear_button = ttk.Button(controls, text="Clear Traffic", command=self.clear_traffic)
        self.clear_button.grid(row=0, column=2)

        self.simulate_button = ttk.Button(
            controls, text="Simulate Traffic", command=self.simulate_traffic
        )
        self.simulate_button.grid(row=0, column=3, padx=(6, 0))

        self.startup_button = ttk.Button(controls, command=self.toggle_windows_startup)
        self.startup_button.grid(row=0, column=4, padx=(6, 0))
        self._refresh_startup_button()

        self.minimized_check = ttk.Checkbutton(controls, text="Start minimized", variable=self.start_minimized_var)
        self.minimized_check.grid(row=0, column=5, padx=(6, 0))

        ttk.Label(controls, text="Status:").grid(row=0, column=6, padx=(12, 4))
        self.status_label = ttk.Label(
            controls, textvariable=self.status_var, font=("Segoe UI", 9, "bold")
        )
        self.status_label.grid(row=0, column=7, sticky="e")

        self.config_widgets = [
            self.gw_port_combo,
            self.pl_port_combo,
            self.gw_baud_combo,
            self.pl_baud_combo,
            self.gw_bytesize_combo, self.pl_bytesize_combo,
            self.gw_parity_combo, self.pl_parity_combo,
            self.gw_stopbits_combo, self.pl_stopbits_combo,
            self.cr_wait_entry,
            self.read_timeout_entry,
            self.write_timeout_entry,
            self.gw_xonxoff_check, self.pl_xonxoff_check,
            self.gw_rtscts_check, self.pl_rtscts_check,
            self.gw_dsrdtr_check, self.pl_dsrdtr_check,
            self.auto_reconnect_check,
            self.reconnect_delay_entry,
            self.minimized_check,
            self.log_entry,
            self.browse_button,
            self.refresh_button,
        ]

    def _build_menu(self) -> None:
        menu = tk.Menu(self.root)
        file_menu = tk.Menu(menu, tearoff=False)
        templates = tk.Menu(file_menu, tearoff=False)
        for name in TEMPLATES:
            templates.add_radiobutton(label=name, variable=self.template_var, value=name)
        file_menu.add_cascade(label="Template", menu=templates)
        profiles = tk.Menu(file_menu, tearoff=False)
        for name in self.profiles:
            profiles.add_command(label=name, command=lambda n=name: self.load_profile(n))
        file_menu.add_cascade(label="Open Profile", menu=profiles)
        file_menu.add_command(label="Save Profile…", command=self.save_profile)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self.on_close)
        menu.add_cascade(label="File", menu=file_menu)
        edit_menu = tk.Menu(menu, tearoff=False)
        edit_menu.add_command(label="Clear Traffic", command=self.clear_traffic)
        edit_menu.add_command(label="Copy Traffic", state="disabled")
        edit_menu.add_command(label="Preferences…", state="disabled")
        menu.add_cascade(label="Edit", menu=edit_menu)
        view_menu = tk.Menu(menu, tearoff=False)
        view_menu.add_command(label="Refresh Ports", command=self.refresh_ports)
        view_menu.add_command(label="Show in Tray", state="disabled")
        menu.add_cascade(label="View", menu=view_menu)
        help_menu = tk.Menu(menu, tearoff=False)
        help_menu.add_command(label="About", command=lambda: messagebox.showinfo("About", APP_NAME, parent=self.root))
        menu.add_cascade(label="Help", menu=help_menu)
        self.root.configure(menu=menu)

    def _device_from_combo_text(self, value: str) -> str:
        value = value.strip()
        if value in self.port_display_to_device:
            return self.port_display_to_device[value]
        # Editable combo boxes also allow manually entering COM7, COM12, etc.
        if " — " in value:
            return value.split(" — ", 1)[0].strip()
        return value

    def refresh_ports(self) -> None:
        current_gw = self._device_from_combo_text(self.gw_port_var.get())
        current_pl = self._device_from_combo_text(self.pl_port_var.get())

        ports = discover_serial_ports((current_gw, current_pl))
        if not current_gw and not current_pl:
            current_gw, current_pl = translator_default_devices(ports)

        displays: list[str] = []
        mapping: dict[str, str] = {}
        for port in ports:
            display = port.display
            displays.append(display)
            mapping[display] = port.device

        self.port_display_to_device = mapping
        self.gw_port_combo["values"] = displays
        self.pl_port_combo["values"] = displays

        def choose_display(device: str) -> str:
            for display, dev in mapping.items():
                if dev.upper() == device.upper():
                    return display
            return device

        self.gw_port_var.set(choose_display(current_gw or "COM6"))
        self.pl_port_var.set(choose_display(current_pl or "COM4"))
        self._append_log(f"Detected {len(displays)} serial port(s).")

    def _save_settings(self) -> None:
        """Persist UI choices without making serial operation depend on disk I/O."""
        save_settings(
            "translator_gui",
            {
                "gw_port": self._device_from_combo_text(self.gw_port_var.get()),
                "template": self.template_var.get(),
                "pl_port": self._device_from_combo_text(self.pl_port_var.get()),
                "gw_baud": self.gw_baud_var.get(),
                "pl_baud": self.pl_baud_var.get(),
                "gw_bytesize": self.gw_bytesize_var.get(),
                "pl_bytesize": self.pl_bytesize_var.get(),
                "gw_parity": self.gw_parity_var.get(),
                "pl_parity": self.pl_parity_var.get(),
                "gw_stopbits": self.gw_stopbits_var.get(),
                "pl_stopbits": self.pl_stopbits_var.get(),
                "read_timeout_ms": self.read_timeout_var.get(),
                "cr_wait_ms": self.cr_wait_var.get(),
                "write_timeout": self.write_timeout_var.get(),
                "gw_xonxoff": self.gw_xonxoff_var.get(),
                "pl_xonxoff": self.pl_xonxoff_var.get(),
                "gw_rtscts": self.gw_rtscts_var.get(),
                "pl_rtscts": self.pl_rtscts_var.get(),
                "gw_dsrdtr": self.gw_dsrdtr_var.get(),
                "pl_dsrdtr": self.pl_dsrdtr_var.get(),
                "auto_reconnect": self.auto_reconnect_var.get(),
                "reconnect_delay_s": self.reconnect_delay_var.get(),
                "start_minimized": self.start_minimized_var.get(),
                "log_file": self.log_file_var.get(),
            },
        )

    def save_profile(self) -> None:
        name = simpledialog.askstring("Save profile", "Profile name:", parent=self.root)
        name = (name or "").strip()
        if not name:
            messagebox.showerror("Save profile", "Enter a profile name first.", parent=self.root)
            return
        self._save_settings()
        self.profiles[name] = load_settings("translator_gui")
        save_settings("translator_profiles", self.profiles)

    def load_profile(self, name: str) -> None:
        profile = self.profiles.get(name)
        if not profile:
            messagebox.showerror("Load profile", "Select a saved profile.", parent=self.root)
            return
        for key, variable in (("gw_port", self.gw_port_var), ("pl_port", self.pl_port_var), ("template", self.template_var), ("gw_baud", self.gw_baud_var), ("pl_baud", self.pl_baud_var)):
            if key in profile:
                variable.set(str(profile[key]))
        self.refresh_ports()

    def choose_log_file(self) -> None:
        current = self.log_file_var.get().strip() or "prolab_translator.log"
        path = filedialog.asksaveasfilename(
            title="Choose translator log file",
            initialfile=Path(current).name,
            defaultextension=".log",
            filetypes=(("Log files", "*.log"), ("Text files", "*.txt"), ("All files", "*.*")),
        )
        if path:
            self.log_file_var.set(path)

    def _parse_stopbits(self, value: str) -> float:
        value = value.strip()
        mapping = {
            "1": serial.STOPBITS_ONE,
            "1.0": serial.STOPBITS_ONE,
            "1.5": serial.STOPBITS_ONE_POINT_FIVE,
            "2": serial.STOPBITS_TWO,
            "2.0": serial.STOPBITS_TWO,
        }
        if value not in mapping:
            raise ValueError("Stop bits must be 1, 1.5, or 2")
        return mapping[value]

    def _build_settings(self) -> tuple[PortSettings, PortSettings, float, Optional[str]]:
        gw_port = self._device_from_combo_text(self.gw_port_var.get())
        pl_port = self._device_from_combo_text(self.pl_port_var.get())

        if not gw_port or not pl_port:
            raise ValueError("Select both the GasWorks-side and ProLab COM ports")
        if gw_port.upper() == pl_port.upper():
            raise ValueError("GasWorks-side and ProLab ports must be different")

        gw_baud = int(self.gw_baud_var.get())
        pl_baud = int(self.pl_baud_var.get())
        gw_bytesize = int(self.gw_bytesize_var.get())
        pl_bytesize = int(self.pl_bytesize_var.get())
        gw_parity = self.gw_parity_var.get().strip().upper()
        pl_parity = self.pl_parity_var.get().strip().upper()
        gw_stopbits = self._parse_stopbits(self.gw_stopbits_var.get())
        pl_stopbits = self._parse_stopbits(self.pl_stopbits_var.get())
        read_timeout_ms = float(self.read_timeout_var.get())
        cr_wait_ms = float(self.cr_wait_var.get())
        write_timeout = float(self.write_timeout_var.get())
        reconnect_delay_s = float(self.reconnect_delay_var.get())

        if gw_baud <= 0 or pl_baud <= 0:
            raise ValueError("Baud rates must be greater than zero")
        if read_timeout_ms < 0 or cr_wait_ms < 0:
            raise ValueError("Timeout values cannot be negative")
        if write_timeout <= 0:
            raise ValueError("Write timeout must be greater than zero")
        if reconnect_delay_s <= 0:
            raise ValueError("Reconnect delay must be greater than zero")

        common = dict(
            timeout=read_timeout_ms / 1000.0,
            write_timeout=write_timeout,
        )

        gw_settings = PortSettings(
            port=gw_port, baudrate=gw_baud, bytesize=gw_bytesize,
            parity=gw_parity, stopbits=gw_stopbits,
            xonxoff=self.gw_xonxoff_var.get(), rtscts=self.gw_rtscts_var.get(),
            dsrdtr=self.gw_dsrdtr_var.get(), **common,
        )
        pl_settings = PortSettings(
            port=pl_port, baudrate=pl_baud, bytesize=pl_bytesize,
            parity=pl_parity, stopbits=pl_stopbits,
            xonxoff=self.pl_xonxoff_var.get(), rtscts=self.pl_rtscts_var.get(),
            dsrdtr=self.pl_dsrdtr_var.get(), **common,
        )
        log_file = session_log_path(self.log_file_var.get().strip() or None)
        return gw_settings, pl_settings, cr_wait_ms / 1000.0, log_file, self.auto_reconnect_var.get(), reconnect_delay_s, TEMPLATES.get(self.template_var.get(), False)

    def start_bridge(self) -> None:
        try:
            gw_settings, pl_settings, cr_wait_s, log_file, auto_reconnect, reconnect_delay_s, normalize_cr = self._build_settings()
        except (ValueError, TypeError) as exc:
            messagebox.showerror("Invalid settings", str(exc), parent=self.root)
            return

        self.status_var.set("Starting...")
        self._set_running_ui(True)
        try:
            self.controller.start(gw_settings, pl_settings, cr_wait_s, log_file, auto_reconnect, reconnect_delay_s, normalize_cr)
            self._save_settings()
        except Exception as exc:
            self._set_running_ui(False)
            self.status_var.set("Stopped")
            messagebox.showerror("Could not start bridge", str(exc), parent=self.root)

    def stop_bridge(self) -> None:
        self.controller.stop()

    def _refresh_startup_button(self) -> None:
        if is_windows():
            self.startup_button.configure(
                text="Disable start at sign-in" if windows_startup_enabled() else "Start bridge at sign-in"
            )
        else:
            self.startup_button.configure(text="Start at sign-in (Windows)", state="disabled")

    def toggle_windows_startup(self) -> None:
        try:
            set_windows_startup(not windows_startup_enabled())
            self._save_settings()
            self._refresh_startup_button()
        except (OSError, RuntimeError) as exc:
            messagebox.showerror("Start at sign-in", str(exc), parent=self.root)

    def _set_running_ui(self, running: bool) -> None:
        for widget in self.config_widgets:
            try:
                if running:
                    widget.configure(state="disabled")
                else:
                    # Restore the intended state for the read-only combo boxes.
                    if widget in (
                        self.gw_bytesize_combo, self.pl_bytesize_combo,
                        self.gw_parity_combo, self.pl_parity_combo,
                        self.gw_stopbits_combo, self.pl_stopbits_combo,
                    ):
                        widget.configure(state="readonly")
                    else:
                        widget.configure(state="normal")
            except tk.TclError:
                pass

        self.start_button.configure(state="disabled" if running else "normal")
        self.stop_button.configure(state="normal" if running else "disabled")
        self.simulate_button.configure(state="disabled" if running else "normal")

    def simulate_traffic(self) -> bool:
        """Create the Linux test topology and open its paired test clients."""
        try:
            if is_linux():
                if self.test_bench is None:
                    self.test_bench = LinuxVirtualTestBench()
                    self.test_bench.start()
                self.gw_port_var.set(self.test_bench.translator_gw)
                self.pl_port_var.set(self.test_bench.translator_pl)
                self.refresh_ports()
                self.test_clients = [
                    GasWorksTestClient(self.root, self.test_bench.gasworks),
                    ProLabTestClient(self.root, self.test_bench.prolab),
                ]
                self.status_var.set("Linux test clients opened")
            else:
                self.test_clients = [GasWorksTestClient(self.root), ProLabTestClient(self.root)]
                self.status_var.set("Test clients opened; select virtual ports")
            return True
        except RuntimeError as exc:
            messagebox.showerror("Simulate Traffic", str(exc), parent=self.root)
            return False

    def start_simulated_test_bench(self) -> None:
        """Open and connect the complete Linux test bench for the shell launcher."""
        if not is_linux():
            messagebox.showerror(
                "Start test bench",
                "The automatic fake test bench is available on Linux only.",
                parent=self.root,
            )
            return
        if not self.simulate_traffic():
            return
        for client in self.test_clients:
            client.connect()
        # Let the endpoint workers open their PTYs before the bridge does.
        self.root.after(100, self.start_bridge)

    def _append_log(self, text: str) -> None:
        self.traffic_text.configure(state="normal")
        self.traffic_text.insert("end", text.rstrip("\n") + "\n")
        self.traffic_text.see("end")
        self.traffic_text.configure(state="disabled")

    def clear_traffic(self) -> None:
        self.traffic_text.configure(state="normal")
        self.traffic_text.delete("1.0", "end")
        self.traffic_text.configure(state="disabled")

    def _drain_events(self) -> None:
        try:
            while True:
                kind, payload = self.event_queue.get_nowait()
                if kind == "log":
                    self._append_log(payload)
                elif kind == "state":
                    state, _, message = payload.partition("|")
                    self.status_var.set(message or state.title())
                    if state == "running":
                        self._set_running_ui(True)
                        self.tray.set_active(True)
                    elif state in ("stopped", "error"):
                        self._set_running_ui(False)
                        self.tray.set_active(False)
                        if state == "error" and message:
                            # Keep errors visible in the traffic pane without
                            # creating repeated modal dialogs from worker threads.
                            self._append_log(f"ERROR: {message}")
        except queue.Empty:
            pass
        finally:
            self.root.after(75, self._drain_events)

    def on_close(self) -> None:
        self._save_settings()
        if self.controller.running:
            self.controller.stop()
        for client in self.test_clients:
            client.close()
        self.test_clients.clear()
        if self.test_bench is not None:
            self.test_bench.close()
        self.tray.close()
        self.root.destroy()


def gui_main(start_test_bench: bool = False, autostart: bool = False, start_minimized: bool = False) -> None:
    root = tk.Tk()
    app = ProLabTranslatorGUI(root)
    if start_test_bench:
        root.after_idle(app.start_simulated_test_bench)
    elif autostart:
        root.after_idle(app.start_bridge)
    if start_minimized:
        root.after_idle(root.withdraw)
    root.mainloop()


class LinuxVirtualTestBench:
    """Own the two socat PTY pairs used by the embedded Linux test clients."""

    gasworks = "/tmp/gasworks"
    translator_gw = "/tmp/translator_gw"
    translator_pl = "/tmp/translator_pl"
    prolab = "/tmp/prolab"

    def __init__(self) -> None:
        self.processes: list[subprocess.Popen[bytes]] = []

    @property
    def aliases(self) -> tuple[str, str, str, str]:
        return (self.gasworks, self.translator_gw, self.translator_pl, self.prolab)

    def start(self) -> None:
        if not is_linux():
            raise RuntimeError("The built-in virtual test bench is available on Linux only.")
        if any(Path(alias).exists() or Path(alias).is_symlink() for alias in self.aliases):
            raise RuntimeError(
                "Test-port aliases already exist. Close the prior test session or remove only its stale /tmp aliases."
            )
        try:
            self.processes = [
                subprocess.Popen(
                    [
                        "socat", "-d", "-d",
                        f"pty,raw,echo=0,link={self.gasworks}",
                        f"pty,raw,echo=0,link={self.translator_gw}",
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                ),
                subprocess.Popen(
                    [
                        "socat", "-d", "-d",
                        f"pty,raw,echo=0,link={self.translator_pl}",
                        f"pty,raw,echo=0,link={self.prolab}",
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                ),
            ]
        except FileNotFoundError as exc:
            self.close()
            raise RuntimeError("Install socat to use Linux Simulate Traffic mode.") from exc

        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if all(Path(alias).is_symlink() for alias in self.aliases):
                return
            if any(process.poll() is not None for process in self.processes):
                break
            time.sleep(0.05)
        self.close()
        raise RuntimeError("Could not create the Linux virtual serial-port pairs.")

    def close(self) -> None:
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
        for process in self.processes:
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                process.kill()
        self.processes.clear()
        for alias in self.aliases:
            path = Path(alias)
            if path.is_symlink():
                path.unlink()


class TestClientEndpoint:
    """Small serial endpoint shared by the two test-client windows."""

    def __init__(self, events: queue.Queue[tuple[str, object]]) -> None:
        self.events = events
        self.stop_event = threading.Event()
        self.tx_queue: queue.Queue[bytes] = queue.Queue()
        self.thread: Optional[threading.Thread] = None

    @property
    def running(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    def start(self, port: str, baudrate: int) -> None:
        self.stop_event = threading.Event()
        self.tx_queue = queue.Queue()
        self.thread = threading.Thread(
            target=self._worker, args=(port, baudrate), daemon=True
        )
        self.thread.start()

    def send(self, data: bytes) -> None:
        if not self.running:
            raise RuntimeError("Not connected")
        self.tx_queue.put(data)

    def stop(self) -> None:
        self.stop_event.set()

    def _worker(self, port: str, baudrate: int) -> None:
        ser: Optional[serial.Serial] = None
        try:
            ser = serial.Serial(
                port=port, baudrate=baudrate, bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_ONE,
                timeout=0.02, write_timeout=1.0,
            )
            self.events.put(("state", "connected|Connected"))
            while not self.stop_event.is_set():
                try:
                    while True:
                        data = self.tx_queue.get_nowait()
                        ser.write(data)
                        ser.flush()
                        self.events.put(("tx", data))
                except queue.Empty:
                    pass
                data = ser.read(ser.in_waiting or 1)
                if data:
                    self.events.put(("rx", data))
        except (SerialException, OSError, ValueError) as exc:
            if not self.stop_event.is_set():
                self.events.put(("error", str(exc)))
        finally:
            if ser is not None and ser.is_open:
                ser.close()
            self.events.put(("state", "disconnected|Disconnected"))


class BaseTestClient:
    """Compact Toplevel UI for a simulated serial endpoint."""

    def __init__(self, parent: tk.Misc, title: str, role: str) -> None:
        self.window = tk.Toplevel(parent)
        self.window.title(title)
        self.window.geometry("560x440")
        self.window.minsize(500, 370)
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.endpoint = TestClientEndpoint(self.events)
        self.role = role
        self.port_map: dict[str, str] = {}
        self.port_var = tk.StringVar()
        self.baud_var = tk.StringVar(value="9600")
        self.status_var = tk.StringVar(value="Disconnected")
        self._build_base(title)
        self.refresh_ports()
        self._set_connected(False)
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self.window.after(50, self._drain_events)

    def _build_base(self, title: str) -> None:
        outer = ttk.Frame(self.window, padding=6)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text=title, font=("TkDefaultFont", 13, "bold")).pack(anchor="w")
        conn = ttk.Frame(outer)
        conn.pack(fill="x", pady=(5, 4))
        ttk.Label(conn, text="Port:").pack(side="left")
        self.port_combo = ttk.Combobox(conn, textvariable=self.port_var, width=29)
        self.port_combo.pack(side="left", fill="x", expand=True, padx=4)
        ttk.Button(conn, text="Refresh", command=self.refresh_ports).pack(side="left")
        ttk.Label(conn, text="Baud:").pack(side="left", padx=(8, 2))
        self.baud_combo = ttk.Combobox(conn, textvariable=self.baud_var, values=COMMON_BAUD_RATES, width=7)
        self.baud_combo.pack(side="left")
        self.connect_btn = ttk.Button(conn, text="Connect", command=self.toggle_connection)
        self.connect_btn.pack(side="left", padx=(5, 0))
        self.actions = ttk.LabelFrame(outer, text="Traffic controls", padding=6)
        self.actions.pack(fill="x")
        traffic = ttk.LabelFrame(outer, text="Traffic", padding=5)
        traffic.pack(fill="both", expand=True, pady=(5, 0))
        ttk.Button(traffic, text="Clear", command=self.clear_traffic).pack(anchor="e")
        self.traffic = tk.Text(traffic, height=12, wrap="none", font=("TkFixedFont", 9), state="disabled")
        self.traffic.pack(fill="both", expand=True)
        ttk.Label(outer, textvariable=self.status_var).pack(anchor="w", pady=(4, 0))

    def refresh_ports(self) -> None:
        current = self.port_map.get(self.port_var.get(), self.port_var.get().split(" — ", 1)[0].strip())
        ports = discover_serial_ports((current,))
        self.port_map = {p.display: p.device for p in ports}
        self.port_combo["values"] = list(self.port_map)
        current = current or preferred_role_device(self.role, ports)
        display = next((label for label, device in self.port_map.items() if device == current), current)
        self.port_var.set(display)

    def toggle_connection(self) -> None:
        if self.endpoint.running:
            self.endpoint.stop()
            return
        self.connect()

    def connect(self) -> None:
        """Connect using the selected endpoint settings without toggling state."""
        port = self.port_map.get(self.port_var.get(), self.port_var.get().split(" — ", 1)[0].strip())
        try:
            self.endpoint.start(port, int(self.baud_var.get()))
        except ValueError:
            messagebox.showerror("Connection settings", "Enter a valid port and baud rate.", parent=self.window)

    def _set_connected(self, connected: bool) -> None:
        self.connect_btn.configure(text="Disconnect" if connected else "Connect")
        self.port_combo.configure(state="disabled" if connected else "normal")
        self.baud_combo.configure(state="disabled" if connected else "normal")

    def _append(self, direction: str, data: bytes) -> None:
        self.traffic.configure(state="normal")
        self.traffic.insert("end", f"{direction:<3} | HEX: {hex_view(data)} | ASCII: {ascii_view(data)}\n")
        self.traffic.see("end")
        self.traffic.configure(state="disabled")

    def clear_traffic(self) -> None:
        self.traffic.configure(state="normal")
        self.traffic.delete("1.0", "end")
        self.traffic.configure(state="disabled")

    def _drain_events(self) -> None:
        try:
            while True:
                kind, data = self.events.get_nowait()
                if kind in ("tx", "rx"):
                    self.on_data(kind.upper(), data)  # type: ignore[arg-type]
                elif kind == "state":
                    state, text = str(data).split("|", 1)
                    self.status_var.set(text)
                    self._set_connected(state == "connected")
                elif kind == "error":
                    self.status_var.set(f"Error: {data}")
                    messagebox.showerror("Serial error", str(data), parent=self.window)
        except queue.Empty:
            pass
        if self.window.winfo_exists():
            self.window.after(50, self._drain_events)

    def on_data(self, direction: str, data: bytes) -> None:
        self._append(direction, data)

    def close(self) -> None:
        self.endpoint.stop()
        self.window.destroy()


class GasWorksTestClient(BaseTestClient):
    def __init__(self, parent: tk.Misc, default_port: Optional[str] = None) -> None:
        super().__init__(parent, "GasWorks Test Client", "fake_gasworks")
        if default_port:
            self.port_var.set(default_port)
            self.refresh_ports()
        self.command_var = tk.StringVar(value="SN -c01")
        ttk.Entry(self.actions, textvariable=self.command_var).pack(side="left", fill="x", expand=True)
        for label, terminator in (("CR", b"\r"), ("CR+LF", b"\r\n"), ("None", b"")):
            ttk.Button(self.actions, text=label, command=lambda t=terminator: self.send_command(t)).pack(side="left", padx=(4, 0))

    def send_command(self, terminator: bytes) -> None:
        try:
            self.endpoint.send(self.command_var.get().encode("latin-1") + terminator)
        except (UnicodeEncodeError, RuntimeError) as exc:
            messagebox.showerror("Send command", str(exc), parent=self.window)


class ProLabTestClient(BaseTestClient):
    def __init__(self, parent: tk.Misc, default_port: Optional[str] = None) -> None:
        super().__init__(parent, "ProLab Test Client", "fake_prolab")
        if default_port:
            self.port_var.set(default_port)
            self.refresh_ports()
        self.response_var = tk.StringVar(value="OK")
        self.auto_var = tk.BooleanVar(value=True)
        self.buffer = bytearray()
        ttk.Entry(self.actions, textvariable=self.response_var, width=16).pack(side="left", fill="x", expand=True)
        ttk.Checkbutton(self.actions, text="Auto CR+LF reply", variable=self.auto_var).pack(side="left", padx=4)
        ttk.Button(self.actions, text="Send", command=self.send_response).pack(side="left")

    def send_response(self) -> None:
        try:
            self.endpoint.send(self.response_var.get().encode("latin-1") + b"\r\n")
        except (UnicodeEncodeError, RuntimeError) as exc:
            messagebox.showerror("Send response", str(exc), parent=self.window)

    def on_data(self, direction: str, data: bytes) -> None:
        super().on_data(direction, data)
        if direction != "RX":
            return
        self.buffer.extend(data)
        while b"\r\n" in self.buffer:
            _, _, remaining = self.buffer.partition(b"\r\n")
            self.buffer = bytearray(remaining)
            if self.auto_var.get():
                self.send_response()



if __name__ == "__main__":
    if getattr(sys, "frozen", False) or "--gui" in sys.argv[1:]:
        start_test_bench = "--start-test-bench" in sys.argv[1:]
        autostart = "--autostart" in sys.argv[1:]
        start_minimized = "--start-minimized" in sys.argv[1:]
        if "--gui" in sys.argv:
            sys.argv.remove("--gui")
        if "--start-test-bench" in sys.argv:
            sys.argv.remove("--start-test-bench")
        if "--autostart" in sys.argv:
            sys.argv.remove("--autostart")
        if "--start-minimized" in sys.argv:
            sys.argv.remove("--start-minimized")
        gui_main(start_test_bench=start_test_bench, autostart=autostart, start_minimized=start_minimized)
    else:
        raise SystemExit(main())
