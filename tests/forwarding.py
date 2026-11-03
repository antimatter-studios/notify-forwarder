#!/usr/bin/env python3
"""The sender's repeat filter between the real sender and the real receiver, on a real filesystem.

The sender watches a directory and the receiver acts on the same directory, which is what a shared
folder amounts to: everything the receiver does to a file comes back to the sender as an event. A
relay between the two counts what the sender forwards.
"""
import argparse
import os
from pathlib import Path
import select
import socket
import subprocess
import sys
import tempfile
import threading
import time

WINDOW = 1.0


class Relay(threading.Thread):
    def __init__(self, receiver_port):
        super().__init__(daemon=True)
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.bind(("127.0.0.1", 0))
        self.port = self.socket.getsockname()[1]
        self.receiver_port = receiver_port
        self.seen = []
        self.finished = False

    def run(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as out:
            while not self.finished:
                if select.select([self.socket], [], [], 0.05)[0]:
                    data = self.socket.recv(65536)
                    self.seen.append((time.monotonic(), data.decode()))
                    out.sendto(data, ("127.0.0.1", self.receiver_port))

    def forwarded(self, path):
        return [when for when, name in list(self.seen) if name == str(path)]


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as reservation:
        reservation.bind(("127.0.0.1", 0))
        return reservation.getsockname()[1]


def wait_for(condition, timeout, description):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out after {timeout} s waiting for {description}")


def wait_until_watching(relay, directory):
    """A watcher reports nothing when it is ready, so write until a change comes through."""
    probe = directory / "probe"
    deadline = time.monotonic() + 30
    while not relay.forwarded(probe):
        if time.monotonic() > deadline:
            raise AssertionError("the sender forwarded nothing in 30 seconds")
        probe.write_text(str(time.monotonic()))
        time.sleep(0.2)
    time.sleep(WINDOW + 1)


def second_change(relay, directory):
    path = directory / "second-change"
    path.write_text("one")
    wait_for(lambda: relay.forwarded(path), 10, "the first change to be forwarded")
    time.sleep(0.5)
    path.write_text("two")
    second_written = time.monotonic()
    time.sleep(3)
    forwarded = relay.forwarded(path)
    assert any(when > second_written for when in forwarded), (
        f"a second change 0.5 s after the first was not forwarded: {len(forwarded)} forwarded"
    )
    print(f"PASS a second change 0.5 s after the first is forwarded ({len(forwarded)} forwarded)")


def busy_file(relay, directory):
    path = directory / "busy"
    last_written = 0
    for iteration in range(60):
        path.write_text(f"change {iteration:04}")
        last_written = time.monotonic()
        time.sleep(0.1)
    time.sleep(3)
    forwarded = relay.forwarded(path)
    assert len(forwarded) >= 5, f"60 changes in 6 s forwarded {len(forwarded)} times"
    assert forwarded[-1] > last_written, (
        f"the last change was not forwarded: {forwarded[-1] - last_written:.2f} s, "
        f"{len(forwarded)} forwarded"
    )
    print(f"PASS 60 changes in 6 s forwarded {len(forwarded)} times, the last one included")


def no_feedback_loop(relay, directory):
    path = directory / "quiet"
    path.write_text("one")
    written = path.stat()
    wait_for(lambda: relay.forwarded(path), 10, "the change to be forwarded")
    # A test that sees no echo proves nothing about one.
    wait_for(lambda: path.stat().st_ctime_ns != written.st_ctime_ns, 5, "the receiver to act")
    assert path.stat().st_mtime_ns == written.st_mtime_ns, "the receiver changed mtime"
    time.sleep(8)
    forwarded = relay.forwarded(path)
    assert len(forwarded) <= 2, f"one change forwarded {len(forwarded)} times in 8 s"
    assert time.monotonic() - forwarded[-1] > 4, f"still forwarding after 4 quiet seconds: {forwarded}"
    print(f"PASS one change forwarded {len(forwarded)} time(s) in 8 s with the receiver's echo")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("binary", type=Path)
    args = parser.parse_args()
    binary = str(args.binary.resolve())
    with tempfile.TemporaryDirectory(prefix="notify-forwarder-forwarding-") as scratch:
        directory = Path(scratch).resolve()
        receiver_port = free_port()
        relay = Relay(receiver_port)
        relay.start()
        processes = []
        failures = 0
        try:
            processes.append(subprocess.Popen([binary, "receive", "-p", str(receiver_port)]))
            processes.append(subprocess.Popen(
                [binary, "watch", "-c", f"127.0.0.1:{relay.port}", str(directory), str(directory)],
            ))
            wait_until_watching(relay, directory)
            for case in (no_feedback_loop, second_change, busy_file):
                try:
                    case(relay, directory)
                except AssertionError as error:
                    print(f"FAIL {case.__name__}: {error}")
                    failures += 1
                for process in processes:
                    if process.poll() is not None:
                        raise AssertionError(f"{process.args[1]} exited with {process.returncode}")
        finally:
            relay.finished = True
            for process in processes:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=10)
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
