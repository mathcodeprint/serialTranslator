"""Regression tests for the protocol-safe parts of the serial bridge."""

from __future__ import annotations

import contextlib
import io
import os
import queue
import select
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from translator import (
    CrLfNormalizer,
    PortSettings,
    SerialBridgeController,
    SerialException,
    TrafficLogger,
    ascii_view,
    build_parser,
    gw_to_pl_worker,
    main,
    open_serial,
    pl_to_gw_worker,
    session_log_path,
    simulated_traffic,
)


class CrLfNormalizerTests(unittest.TestCase):
    def test_cr_only_command_becomes_crlf(self) -> None:
        normalizer = CrLfNormalizer()
        output, changed = normalizer.feed(b"SN -c01\r")
        self.assertEqual(output, b"SN -c01")
        self.assertFalse(changed)
        self.assertEqual(normalizer.flush_pending(), (b"\r\n", True))

    def test_existing_crlf_is_unchanged(self) -> None:
        normalizer = CrLfNormalizer()
        output, changed = normalizer.feed(b"SN -c01\r\n")
        self.assertEqual(output, b"SN -c01\r\n")
        self.assertFalse(changed)
        self.assertEqual(normalizer.flush_pending(), (b"", False))

    def test_crlf_split_across_reads_is_unchanged(self) -> None:
        normalizer = CrLfNormalizer()
        self.assertEqual(normalizer.feed(b"A\r"), (b"A", False))
        self.assertEqual(normalizer.feed(b"\nB"), (b"\r\nB", False))

    def test_multiple_commands_and_binary_payload_are_preserved(self) -> None:
        normalizer = CrLfNormalizer()
        output, changed = normalizer.feed(b"\x00A\rB\xff\r\n")
        self.assertEqual(output, b"\x00A\r\nB\xff\r\n")
        self.assertTrue(changed)

    def test_non_terminated_data_is_unchanged(self) -> None:
        normalizer = CrLfNormalizer()
        self.assertEqual(normalizer.feed(b"\x00\xffpayload"), (b"\x00\xffpayload", False))


class CliValidationTests(unittest.TestCase):
    def test_same_port_is_rejected(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = main(["--gw-port", "COM4", "--pl-port", "com4"])
        self.assertEqual(result, 2)
        self.assertIn("must be different", stderr.getvalue())

    def test_non_positive_write_timeout_is_rejected(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = main(["--write-timeout", "0"])
        self.assertEqual(result, 2)
        self.assertIn("must be greater than zero", stderr.getvalue())

    def test_side_specific_serial_options_are_parsed(self) -> None:
        args = build_parser().parse_args(
            ["--gw-parity", "E", "--pl-stopbits", "2", "--gw-rtscts"]
        )
        self.assertEqual(args.gw_parity, "E")
        self.assertEqual(args.pl_stopbits, 2)
        self.assertTrue(args.gw_rtscts)


class DisplayTests(unittest.TestCase):
    def test_ascii_view_keeps_control_bytes_visible(self) -> None:
        self.assertEqual(ascii_view(b"A\r\n\t\x00"), r"A\r\n\t.")


class SimulationTests(unittest.TestCase):
    def test_simulated_traffic_models_the_protocol_contract(self) -> None:
        exchange = simulated_traffic()
        self.assertEqual(exchange[0][1], b"SN -c01\r")
        self.assertEqual(exchange[1][1], b"SN -c01\r\n")
        self.assertEqual(exchange[2][1], b"OK\r\n")
        self.assertIn("normalized", exchange[1][2])
        self.assertIn("unchanged", exchange[2][2])


class TrafficLoggerTests(unittest.TestCase):
    def test_session_log_path_keeps_parent_and_adds_timestamp(self) -> None:
        result = session_log_path(
            "/tmp/bridge.log", now=datetime(2026, 8, 13, 14, 25, 30, 123456)
        )
        self.assertEqual(result, "/tmp/bridge_20260813-142530-123456.log")

    def test_file_log_rotates_at_configured_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "bridge.log"
            logger = TrafficLogger(
                str(log_path), console=False, max_log_bytes=1, backup_count=1
            )
            try:
                logger.info("first record")
                logger.info("second record")
            finally:
                logger.close()

            self.assertTrue(log_path.exists())
            self.assertTrue(log_path.with_name("bridge.log.1").exists())


class ReconnectTests(unittest.TestCase):
    def test_controller_retries_a_failed_serial_open_until_stopped(self) -> None:
        events: queue.Queue[tuple[str, str]] = queue.Queue()
        controller = SerialBridgeController(events)
        settings = PortSettings(
            port="missing-port", baudrate=9600, bytesize=8, parity="N", stopbits=1,
            timeout=0.01, write_timeout=1.0, xonxoff=False, rtscts=False, dsrdtr=False,
        )
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch("translator.open_serial", side_effect=SerialException("disconnected")) as opener:
                controller.start(settings, settings, 0.01, str(Path(directory) / "bridge.log"), True, 0.01)
                deadline = time.monotonic() + 1.0
                while opener.call_count < 2 and time.monotonic() < deadline:
                    time.sleep(0.01)
                controller.stop()
                deadline = time.monotonic() + 1.0
                while controller.running and time.monotonic() < deadline:
                    time.sleep(0.01)

        self.assertGreaterEqual(opener.call_count, 2)
        states = []
        while not events.empty():
            kind, payload = events.get_nowait()
            if kind == "state":
                states.append(payload)
        self.assertTrue(any(state.startswith("reconnecting|") for state in states))


@unittest.skipIf(os.name == "nt", "PTY integration test requires POSIX")
class SerialBridgeIntegrationTests(unittest.TestCase):
    """Exercise both bridge directions through real pySerial PTYs."""

    @staticmethod
    def _read_until(fd: int, expected: bytes, timeout_s: float = 1.0) -> bytes:
        received = bytearray()
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline and len(received) < len(expected):
            readable, _, _ = select.select([fd], [], [], deadline - time.monotonic())
            if not readable:
                break
            received.extend(os.read(fd, 1024))
        return bytes(received)

    def test_bidirectional_bridge_normalizes_only_gasworks_cr(self) -> None:
        gw_master, gw_slave = os.openpty()
        pl_master, pl_slave = os.openpty()
        gw = pl = None
        logger = TrafficLogger(None, console=False)
        stop_event = threading.Event()
        error_event = threading.Event()
        try:
            common = dict(
                baudrate=9600,
                bytesize=8,
                parity="N",
                stopbits=1,
                timeout=0.01,
                write_timeout=1.0,
                xonxoff=False,
                rtscts=False,
                dsrdtr=False,
            )
            gw = open_serial(PortSettings(port=os.ttyname(gw_slave), **common))
            pl = open_serial(PortSettings(port=os.ttyname(pl_slave), **common))
            gw_thread = threading.Thread(
                target=gw_to_pl_worker,
                args=(gw, pl, stop_event, logger, 0.01, error_event),
            )
            pl_thread = threading.Thread(
                target=pl_to_gw_worker,
                args=(pl, gw, stop_event, logger, error_event),
            )
            gw_thread.start()
            pl_thread.start()

            os.write(gw_master, b"SN -c01\r")
            self.assertEqual(self._read_until(pl_master, b"SN -c01\r\n"), b"SN -c01\r\n")

            os.write(pl_master, b"OK\r")
            self.assertEqual(self._read_until(gw_master, b"OK\r"), b"OK\r")
            self.assertFalse(error_event.is_set())
        finally:
            stop_event.set()
            for thread in (locals().get("gw_thread"), locals().get("pl_thread")):
                if thread is not None:
                    thread.join(timeout=1.0)
            for port in (gw, pl):
                if port is not None and port.is_open:
                    port.close()
            logger.close()
            for fd in (gw_master, gw_slave, pl_master, pl_slave):
                os.close(fd)


if __name__ == "__main__":
    unittest.main()
