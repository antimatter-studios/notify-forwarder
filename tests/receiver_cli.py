#!/usr/bin/env python3
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time

from inotify import read_until, stop


def notify_and_acknowledge(receiver, sender, port, rejected, control):
    before = control.stat().st_ctime_ns
    deadline = time.monotonic() + 8
    while control.stat().st_ctime_ns == before:
        assert receiver.poll() is None, receiver.stderr.read().decode()
        assert time.monotonic() < deadline, "receiver did not process the permitted file after rejected paths"
        for path in rejected + [str(control)]:
            sender.sendto(os.fsencode(path), ("127.0.0.1", port))
        threading.Event().wait(0.01)


def start_receiver(binary, roots):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    process = subprocess.Popen(
        [binary, "receive", "-p", str(port), *map(str, roots)], stderr=subprocess.PIPE,
    )
    return process, port


def main():
    binary = str(Path(sys.argv[1]).resolve())
    with tempfile.TemporaryDirectory(prefix="receiver-cli-") as temporary:
        directory = Path(temporary)
        allowed = directory / "allowed"
        other_root = directory / "other-root"
        outside = directory / "allowed-sibling"
        for root in (allowed, other_root, outside):
            root.mkdir()
        control = allowed / "control"
        second = other_root / "control"
        victim = outside / "victim"
        for path in (control, second, victim):
            path.write_text("unchanged")
        (allowed / "escape").symlink_to(victim)
        os.mkfifo(allowed / "fifo")
        before = victim.stat()
        rejected = [str(victim), str(allowed / "escape"), str(allowed / "fifo"), "/dev/null", str(victim) + "\0ignored"]
        receiver, port = start_receiver(binary, [allowed, other_root])
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
                notify_and_acknowledge(receiver, sender, port, rejected, control)
                notify_and_acknowledge(receiver, sender, port, [], second)
            after = victim.stat()
            assert (before.st_ctime_ns, before.st_mtime_ns, before.st_size) == (
                after.st_ctime_ns, after.st_mtime_ns, after.st_size,
            ), "rejected receiver path was acted on"
            assert victim.read_text() == "unchanged"
            print("PASS receiver CLI accepts multiple roots and refuses outside, escaping, FIFO, device and NUL paths")
            swapped = allowed / "swapped"
            nodes = allowed / ".nodes"
            nodes.mkdir()
            (nodes / "link").symlink_to(victim)
            os.mkfifo(nodes / "fifo")
            device = Path("/dev/null")
            (nodes / "device").symlink_to(device)
            stop_swapping = threading.Event()

            def swap_types():
                index = 0
                names = ["link", "fifo", "device"]
                while not stop_swapping.is_set():
                    name = names[index % len(names)]
                    index += 1
                    os.link(nodes / name, nodes / "next", follow_symlinks=False)
                    os.replace(nodes / "next", swapped)

            swapper = threading.Thread(target=swap_types)
            swapper.start()
            try:
                deadline = time.monotonic() + 2
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
                    while time.monotonic() < deadline:
                        sender.sendto(os.fsencode(str(swapped)), ("127.0.0.1", port))
                        threading.Event().wait(0.001)
                    notify_and_acknowledge(receiver, sender, port, [], control)
            finally:
                stop_swapping.set()
                swapper.join(timeout=10)
            after = victim.stat()
            assert (before.st_ctime_ns, before.st_mtime_ns, before.st_size) == (
                after.st_ctime_ns, after.st_mtime_ns, after.st_size,
            ), "a path replaced by a symlink, FIFO or device was acted on"
            print("PASS receiver survives a path replaced by a symlink, FIFO or device and leaves the target alone")
        finally:
            stop(receiver)
        legacy, port = start_receiver(binary, [])
        try:
            read_until(legacy.stderr, b"WARNING: no receive roots configured")
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
                notify_and_acknowledge(legacy, sender, port, [], control)
            print("PASS legacy plugin command still runs, warns and notifies regular files")
        finally:
            stop(legacy)
        invalid = subprocess.run(
            [binary, "receive", str(control)], capture_output=True, timeout=5,
        )
        assert invalid.returncode == 64, invalid.stderr.decode()
        print("PASS non-directory roots fail startup with a diagnostic")


if __name__ == "__main__":
    main()
