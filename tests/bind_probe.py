#!/usr/bin/env python3
"""Readiness probes leave the receiver's port free, including during concurrent startup."""
import argparse
import contextlib
import errno
import io
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from inotify import free_port, stop, wait_for_bind


def held_on_another_address():
    """A socket the kernel gave a source address and port, as a DNS lookup holds one. Sends nothing."""
    holder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    holder.connect(("192.0.2.1", 9))
    return holder


def automatic_port_range():
    """The ports the kernel hands to any socket on the machine that does not ask for one."""
    if sys.platform == "darwin":
        return tuple(
            int(subprocess.check_output(["sysctl", "-n", f"net.inet.ip.portrange.{bound}"]))
            for bound in ("first", "last")
        )
    return tuple(map(int, Path("/proc/sys/net/ipv4/ip_local_port_range").read_text().split()))


def measured(binary, starts=1):
    """(failures, what the measurement printed), so an expected failure does not read as a real one."""
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        failures = measure_starts(binary, starts)
    return failures, output.getvalue()


class BindProbeTests(unittest.TestCase):
    binary = None

    def test_a_port_held_on_another_address_is_free_only_on_loopback(self):
        with held_on_another_address() as holder:
            address, port = holder.getsockname()
            self.assertFalse(address.startswith("127."), f"{address} is not another address")
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as loopback:
                loopback.bind(("127.0.0.1", port))
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as wildcard:
                with self.assertRaises(OSError) as refused:
                    wildcard.bind(("0.0.0.0", port))
            self.assertEqual(refused.exception.errno, errno.EADDRINUSE)

    def test_measurement_does_not_start_on_a_port_held_on_another_address(self):
        socket_type = socket.socket
        with held_on_another_address() as holder:
            held_port = holder.getsockname()[1]

            class UnluckyAllocator(socket_type):
                def bind(self, address):
                    if address[1] == 0 and address[0] != "0.0.0.0":
                        address = (address[0], held_port)
                    super().bind(address)

            with patch("inotify.socket.socket", UnluckyAllocator):
                failures, output = measured(self.binary)
        self.assertEqual(failures, 0, output)

    def test_measurement_chooses_ports_the_kernel_does_not_hand_out(self):
        first, last = automatic_port_range()
        popen = subprocess.Popen
        ports = []

        def recording(command, **options):
            ports.append(int(command[command.index("-p") + 1]))
            return popen(command, **options)

        with patch.object(subprocess, "Popen", recording):
            failures, output = measured(self.binary, starts=3)
        self.assertEqual(failures, 0, output)
        self.assertEqual(len(ports), 3)
        for port in ports:
            self.assertFalse(first <= port <= last, f"{port} is in the automatic range {first}-{last}")

    def test_a_receiver_that_cannot_bind_still_fails(self):
        popen = subprocess.Popen
        holders = []

        def held_first(command, **options):
            holder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            holders.append(holder)
            holder.bind(("0.0.0.0", int(command[command.index("-p") + 1])))
            return popen(command, **options)

        try:
            with patch.object(subprocess, "Popen", held_first):
                failures, output = measured(self.binary)
        finally:
            for holder in holders:
                holder.close()
        self.assertEqual(failures, 1, output)
        self.assertIn("Could not bind port", output)
        self.assertIn("Address already in use", output)

    def test_selection_passes_over_a_port_in_use(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as holder:
            holder.bind(("127.0.0.1", 0))
            held = holder.getsockname()[1]
            free = free_port()
            with patch("inotify.random.sample", return_value=[held, free]):
                self.assertEqual(free_port(), free)

    def test_selection_with_no_free_port_is_a_failure(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as holder:
            holder.bind(("0.0.0.0", 0))
            held = holder.getsockname()[1]
            with patch("inotify.RECEIVER_PORTS", range(held, held + 1)):
                with self.assertRaisesRegex(AssertionError, "no free port"):
                    free_port()

    def test_receiver_can_bind_before_each_probe_closes(self):
        socket_type = socket.socket
        port = free_port()
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
        port = free_port()
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
            port = free_port()
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
        BindProbeTests.binary = str(args.binary.resolve())
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(BindProbeTests)
        passed = unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful()
    failures = measure_starts(str(args.binary.resolve()), args.starts)
    raise SystemExit(0 if passed and not failures else 1)


if __name__ == "__main__":
    main()
