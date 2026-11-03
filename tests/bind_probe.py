#!/usr/bin/env python3
"""Readiness probes leave the receiver's port free, including during concurrent startup."""
import argparse
import errno
from pathlib import Path
import socket
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from inotify import stop, wait_for_bind


class BindProbeTests(unittest.TestCase):
    def test_receiver_can_bind_before_each_probe_closes(self):
        socket_type = socket.socket
        with socket_type(socket.AF_INET, socket.SOCK_DGRAM) as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        conflicts = []
        attempts = []

        class ObservedProbe(socket_type):
            def __exit__(self, *exception):
                with socket_type(socket.AF_INET, socket.SOCK_DGRAM) as receiver:
                    attempts.append(port)
                    try:
                        receiver.bind(("0.0.0.0", port))
                    except OSError as error:
                        conflicts.append(error)
                return super().__exit__(*exception)

        with patch("inotify.socket.socket", ObservedProbe):
            with self.assertRaisesRegex(AssertionError, "receiver did not bind"):
                wait_for_bind(port, timeout=0.08)
        self.assertTrue(attempts, "no live readiness probe was inspected")
        self.assertEqual(conflicts, [], "a readiness probe held the receiver's port at bind time")

    def test_bound_receiver_is_probed_with_an_empty_datagram(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as receiver:
            receiver.bind(("127.0.0.1", 0))
            port = receiver.getsockname()[1]
            wait_for_bind(port, timeout=1)
            receiver.settimeout(1)
            datagram, source = receiver.recvfrom(1024)
            self.assertEqual(datagram, b"")
            self.assertNotEqual(source[1], port)

    def test_unexpected_probe_error_is_not_readiness(self):
        socket_type = socket.socket

        class BrokenProbe(socket_type):
            def bind(self, address):
                raise PermissionError(errno.EACCES, "probe denied")

            def connect(self, address):
                raise PermissionError(errno.EACCES, "probe denied")

        with patch("inotify.socket.socket", BrokenProbe):
            with self.assertRaises(PermissionError):
                wait_for_bind(12345, timeout=0.08)

    def test_automatic_source_allocation_cannot_take_the_receiver_port(self):
        socket_type = socket.socket
        with socket_type(socket.AF_INET, socket.SOCK_DGRAM) as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        sources = []

        class UnluckyProbe(socket_type):
            def connect(self, address):
                if self.getsockname()[1] == 0:
                    super().bind(("127.0.0.1", port))
                sources.append(self.getsockname()[1])
                return super().connect(address)

        with patch("inotify.socket.socket", UnluckyProbe):
            with self.assertRaisesRegex(AssertionError, "receiver did not bind"):
                wait_for_bind(port, timeout=0.08)
        self.assertTrue(sources)
        self.assertNotIn(port, sources)

    def test_busy_source_ports_are_retried_without_using_the_receiver_port(self):
        with patch("inotify.socket.socket") as factory:
            probe = factory.return_value.__enter__.return_value
            probe.bind.side_effect = [OSError(errno.EADDRINUSE, "source busy"), None]
            probe.recv.side_effect = socket.timeout
            wait_for_bind(1025, timeout=1)
        self.assertEqual(
            [attempt.args[0] for attempt in probe.bind.call_args_list],
            [("127.0.0.1", 1024), ("127.0.0.1", 1026)],
        )
        probe.connect.assert_called_once_with(("127.0.0.1", 1025))
        probe.send.assert_called_once_with(b"")

    def test_source_port_exhaustion_is_not_readiness(self):
        socket_type = socket.socket

        class BusyProbe(socket_type):
            def bind(self, address):
                raise OSError(errno.EADDRINUSE, "source busy")

        with patch("inotify.socket.socket", BusyProbe):
            with self.assertRaisesRegex(AssertionError, "no source port available"):
                wait_for_bind(12345, timeout=1)

    def test_a_datagram_reply_is_readiness(self):
        with patch("inotify.socket.socket") as factory:
            probe = factory.return_value.__enter__.return_value
            probe.recv.return_value = b"reply"
            wait_for_bind(12345, timeout=1)
        probe.recv.assert_called_once_with(1)


def measure_starts(binary, starts):
    failures = 0
    with tempfile.TemporaryDirectory(prefix="notify-forwarder-bind-") as root:
        for iteration in range(starts):
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]
            receiver = subprocess.Popen(
                [binary, "receive", "-p", str(port), root], stderr=subprocess.PIPE,
            )
            error = None
            try:
                wait_for_bind(port, timeout=2)
                if receiver.poll() is not None:
                    error = f"receiver exited with {receiver.returncode}"
            except (AssertionError, OSError) as failure:
                error = str(failure)
            finally:
                stop(receiver)
                diagnostic = receiver.stderr.read().decode()
                receiver.stderr.close()
            if error is not None or receiver.returncode != 0:
                failures += 1
                print(f"FAIL start {iteration + 1}: {error}; {diagnostic.strip()}", flush=True)
    print(f"{'FAIL' if failures else 'PASS'} receiver starts: {failures} of {starts} failed", flush=True)
    return failures


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("binary", type=Path)
    parser.add_argument("--starts", type=int, default=2000)
    parser.add_argument("--measure-only", action="store_true")
    args = parser.parse_args()
    if args.starts < 1:
        parser.error("--starts must be positive")
    passed = True
    if not args.measure_only:
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(BindProbeTests)
        passed = unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful()
    failures = measure_starts(str(args.binary.resolve()), args.starts)
    raise SystemExit(0 if passed and not failures else 1)


if __name__ == "__main__":
    main()
