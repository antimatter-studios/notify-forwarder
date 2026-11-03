#!/usr/bin/env python3
"""The receiver as root on Linux: nothing but a permitted regular file is ever opened for writing.

The oracle is the kernel. IN_CLOSE_WRITE follows the inode and fires only when a writable descriptor
closes, so a watch on each forbidden node sees a stray open however the path reached it. strace is no
use here: its description of a descriptor stats the path, which a swapped path has since changed.
"""
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time

from inotify import read_until, stop, wait_for_bind


def main():
    assert sys.platform == "linux", "tests/receiver_root.py needs Linux: run it in the Linux kernel events job"
    assert os.geteuid() == 0, "tests/receiver_root.py needs root: run it with sudo"
    binary = str(Path(sys.argv[1]).resolve())
    with tempfile.TemporaryDirectory(prefix="receiver-root-") as temporary:
        directory = Path(temporary).resolve()
        allowed = directory / "allowed"
        outside = directory / "allowed-sibling"
        nodes = directory / "nodes"
        for path in (allowed, outside, nodes):
            path.mkdir()
        regular = allowed / "regular"
        regular.write_text("unchanged")
        victim = outside / "victim"
        victim.write_text("unchanged")
        os.mkfifo(allowed / "fifo")
        os.mknod(allowed / "character", stat.S_IFCHR | 0o666, os.makedev(1, 3))
        os.mknod(allowed / "block", stat.S_IFBLK | 0o666, os.makedev(7, 250))
        (allowed / "escape").symlink_to(victim)
        (allowed / "directory").mkdir()
        os.mknod(nodes / "character", stat.S_IFCHR | 0o666, os.makedev(1, 3))
        os.mkfifo(nodes / "fifo")
        (nodes / "regular").write_text("swapped")
        (nodes / "link").symlink_to(victim)
        victim_before = victim.stat()

        # A reader on each FIFO, so a stray writable open would succeed and be seen.
        readers = [os.open(path, os.O_RDONLY | os.O_NONBLOCK) for path in (allowed / "fifo", nodes / "fifo")]
        forbidden = [victim, allowed / "fifo", allowed / "character", nodes / "character", nodes / "fifo"]
        watch = subprocess.Popen(
            ["inotifywait", "-m", "--format", "%w %e", "-e", "close_write", *map(str, forbidden), str(regular)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        refused = [victim, allowed / "fifo", allowed / "character", allowed / "block", allowed / "escape", allowed / "directory", Path("/dev/null"), allowed / "missing"]
        swapped = allowed / "swapped"
        stop_swapping = threading.Event()

        def swap_types():
            names = ["character", "regular", "fifo", "link"]
            index = 0
            while not stop_swapping.is_set():
                os.link(nodes / names[index % len(names)], nodes / "next", follow_symlinks=False)
                os.replace(nodes / "next", swapped)
                index += 1

        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        read_until(watch.stderr, b"Watches established.")
        receiver = subprocess.Popen([binary, "receive", "-p", str(port), str(allowed)], stderr=subprocess.PIPE)
        swapper = threading.Thread(target=swap_types)
        swapper.start()
        try:
            wait_for_bind(port)
            deadline = time.monotonic() + 3
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
                while time.monotonic() < deadline:
                    for path in refused + [swapped, regular]:
                        sender.sendto(os.fsencode(str(path)), ("127.0.0.1", port))
                    time.sleep(0.002)
                    assert receiver.poll() is None, receiver.stderr.read().decode()
                # The single-threaded receiver answering last shows nothing above hung it.
                settle = time.monotonic() + 8
                marker = regular.stat().st_ctime_ns
                while regular.stat().st_ctime_ns == marker:
                    assert time.monotonic() < settle, "receiver stopped answering after the refused paths"
                    sender.sendto(os.fsencode(str(regular)), ("127.0.0.1", port))
                    time.sleep(0.01)
        finally:
            stop_swapping.set()
            swapper.join(timeout=10)
            stop(receiver)
            for reader in readers:
                os.close(reader)

        time.sleep(0.2)
        stop(watch)
        lines = watch.stdout.read().decode().splitlines()
        stray = sorted({line for line in lines if not line.startswith(f"{regular} ")})
        assert not stray, f"opened for writing: {stray[:5]}"
        assert lines, "the permitted file raised no IN_CLOSE_WRITE, so this oracle saw nothing"
        victim_after = victim.stat()
        assert (victim_before.st_ctime_ns, victim_before.st_mtime_ns, victim_before.st_size) == (
            victim_after.st_ctime_ns, victim_after.st_mtime_ns, victim_after.st_size,
        ), "the file outside the root was acted on"
        assert victim.read_text() == "unchanged"
        print(f"PASS as root, {len(lines)} writable closes, all of the permitted regular file")
        print("PASS as root, no FIFO, device or file outside the root was opened for writing, even by a swapped path")


if __name__ == "__main__":
    main()
