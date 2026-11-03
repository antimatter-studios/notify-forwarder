#!/usr/bin/env python3
"""What a burst loses, on Linux, measured by the kernel's own count of a socket's dropped datagrams.

/proc/net/udp shares no code with this program, so it can say whether the receiver's report is true,
and whether the sender lost anything before its datagrams reached a socket.
"""
import argparse
import os
from pathlib import Path
import re
import select
import socket
import subprocess
import sys
import tempfile
import threading
import time

from inotify import free_port, wait_for_bind

FLOOD = 50000
FILES = 20000
SENDERS = 4


def kernel_counts(port):
    """(datagram bytes queued, datagrams dropped) for the socket bound to a port."""
    with open("/proc/net/udp") as table:
        next(table)
        for line in table:
            fields = line.split()
            if int(fields[1].split(":")[1], 16) == port:
                return int(fields[4].split(":")[1], 16), int(fields[-1])
    raise AssertionError(f"no socket on port {port} in /proc/net/udp")


def wait_for_drain(port, timeout=60):
    deadline = time.monotonic() + timeout
    while kernel_counts(port)[0] != 0:
        assert time.monotonic() < deadline, f"socket on {port} still has data after {timeout} s"
        time.sleep(0.05)


def receiver_reports_what_the_kernel_dropped(binary, directory):
    target = directory / "target"
    target.write_text("target")
    probe = directory / "probe"
    probe.write_text("probe")
    port = free_port()
    with tempfile.TemporaryFile() as stderr:
        receiver = subprocess.Popen([binary, "receive", "-p", str(port)], stderr=stderr)
        try:
            wait_for_bind(port)

            def flood():
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
                    for _ in range(FLOOD // SENDERS):
                        sender.sendto(str(target).encode(), ("127.0.0.1", port))

            threads = [threading.Thread(target=flood) for _ in range(SENDERS)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            wait_for_drain(port)
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
                sender.sendto(str(probe).encode(), ("127.0.0.1", port))
            wait_for_drain(port)
            _, dropped = kernel_counts(port)
            assert dropped > 0, f"{FLOOD} datagrams overflowed nothing, so this measured nothing"

            deadline = time.monotonic() + 10
            reported = None
            while reported != dropped:
                assert receiver.poll() is None, f"the receiver exited with {receiver.returncode}"
                stderr.seek(0)
                counts = re.findall(rb"(\d+) paths lost", stderr.read())
                reported = int(counts[-1]) if counts else None
                assert time.monotonic() < deadline, (
                    f"the kernel dropped {dropped} of {FLOOD}, the receiver reported {reported}"
                )
                time.sleep(0.1)
            print(f"PASS the receiver reports the {dropped} of {FLOOD} datagrams the kernel dropped")
        finally:
            receiver.kill()
            receiver.wait(timeout=10)


def sender_loses_nothing_before_the_socket(binary, directory):
    tree = directory / "burst"
    tree.mkdir()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 64 * 1024 * 1024)
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        forwarded = set()
        finished = threading.Event()

        def collect():
            while not finished.is_set():
                if select.select([listener], [], [], 0.05)[0]:
                    forwarded.add(listener.recv(65536).decode())

        collector = threading.Thread(target=collect)
        collector.start()
        watcher = subprocess.Popen([binary, "watch", "-c", f"127.0.0.1:{port}", str(tree), "/guest"])
        try:
            probe = tree / "probe"
            while "/guest/probe" not in forwarded:
                assert watcher.poll() is None, f"the watcher exited with {watcher.returncode}"
                probe.write_text(str(time.monotonic()))
                time.sleep(0.1)
            for index in range(FILES):
                (tree / f"file-{index}").write_bytes(b"")

            expected = {f"/guest/file-{index}" for index in range(FILES)}
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and expected - forwarded:
                time.sleep(0.2)
            wait_for_drain(port)
            lost = expected - forwarded
            _, dropped = kernel_counts(port)
            print(f"NOTE {FILES} files: {FILES - len(lost)} arrived, the listening socket dropped {dropped}")
            assert len(lost) <= dropped, (
                f"{len(lost)} paths are missing and the socket dropped only {dropped}: "
                "the sender lost the rest"
            )
            print("PASS every path the sender saw reached a socket, or that socket's count says otherwise")
        finally:
            finished.set()
            collector.join()
            if watcher.poll() is None:
                watcher.kill()
            watcher.wait(timeout=10)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("binary", type=Path)
    args = parser.parse_args()
    binary = str(args.binary.resolve())
    failures = 0
    with tempfile.TemporaryDirectory(prefix="notify-forwarder-burst-") as scratch:
        directory = Path(scratch).resolve()
        for case in (receiver_reports_what_the_kernel_dropped, sender_loses_nothing_before_the_socket):
            try:
                case(binary, directory)
            except AssertionError as error:
                print(f"FAIL {case.__name__}: {error}")
                failures += 1
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
