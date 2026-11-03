#!/usr/bin/env python3
"""SIGINT and SIGTERM stop the real watcher and the real receiver, each with exit status 0."""
import argparse
from pathlib import Path
import select
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time

from inotify import wait_for_bind

CYCLES = 5
STOP_TIMEOUT = 5


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as reservation:
        reservation.bind(("127.0.0.1", 0))
        return reservation.getsockname()[1]


def wait_for_stop(process, name):
    try:
        status = process.wait(timeout=STOP_TIMEOUT)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)
        raise AssertionError(f"{name} still running {STOP_TIMEOUT} s after the signal")
    assert status == 0, f"{name} exited with {status}"


class Receiver:
    def __init__(self, binary):
        self.port = free_port()
        self.process = subprocess.Popen([binary, "receive", "-p", str(self.port)], stderr=subprocess.DEVNULL)
        wait_for_bind(self.port)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=10)


class Watcher:
    """Started and proved to be watching: a watcher reports nothing when it is ready."""

    def __init__(self, binary, directory):
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.listener.bind(("127.0.0.1", 0))
        port = self.listener.getsockname()[1]
        self.process = subprocess.Popen(
            [binary, "watch", "-c", f"127.0.0.1:{port}", str(directory), "/guest"],
        )
        deadline = time.monotonic() + 30
        probe = directory / "probe"
        while not select.select([self.listener], [], [], 0.1)[0]:
            assert self.process.poll() is None, f"watch exited with {self.process.returncode}"
            assert time.monotonic() < deadline, "watch forwarded nothing in 30 seconds"
            probe.write_text(str(time.monotonic()))

    def __enter__(self):
        return self

    def __exit__(self, *_):
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=10)
        self.listener.close()


def receive_idle(binary, _directory, signum, name):
    for _ in range(CYCLES):
        with Receiver(binary) as receiver:
            receiver.process.send_signal(signum)
            wait_for_stop(receiver.process, name)


def receive_busy(binary, directory, signum, name):
    """The receiver stops between two files, and the termination signal waits for its attribute pair."""
    target = directory / "target"
    target.write_text("target")
    for _ in range(CYCLES):
        with Receiver(binary) as receiver, socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            stop_sending = threading.Event()

            def send():
                while not stop_sending.is_set():
                    sender.sendto(str(target).encode(), ("127.0.0.1", receiver.port))
                    time.sleep(0.0005)

            thread = threading.Thread(target=send)
            thread.start()
            try:
                time.sleep(0.1)
                receiver.process.send_signal(signum)
                wait_for_stop(receiver.process, name)
            finally:
                stop_sending.set()
                thread.join()


def watch_idle(binary, directory, signum, name):
    for cycle in range(CYCLES):
        tree = directory / f"watch-{signum}-{cycle}"
        tree.mkdir()
        with Watcher(binary, tree) as watcher:
            time.sleep(0.2)
            watcher.process.send_signal(signum)
            wait_for_stop(watcher.process, name)


def watch_busy(binary, directory, signum, name):
    tree = directory / f"watch-busy-{signum}"
    tree.mkdir()
    with Watcher(binary, tree) as watcher:
        for iteration in range(200):
            (tree / "busy").write_text(str(iteration))
            if iteration == 100:
                watcher.process.send_signal(signum)
        wait_for_stop(watcher.process, name)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("binary", type=Path)
    args = parser.parse_args()
    binary = str(args.binary.resolve())
    failures = 0
    with tempfile.TemporaryDirectory(prefix="notify-forwarder-signals-") as scratch:
        directory = Path(scratch).resolve()
        for case in (receive_idle, receive_busy, watch_idle, watch_busy):
            for signum in (signal.SIGINT, signal.SIGTERM):
                label = f"{case.__name__} {signum.name}"
                try:
                    case(binary, directory, signum, label)
                    print(f"PASS {label} exits 0")
                except AssertionError as error:
                    print(f"FAIL {error}")
                    failures += 1
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
