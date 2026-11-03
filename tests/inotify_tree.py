#!/usr/bin/env python3
"""The real watcher on a tree holding what is not a directory, while directories come and go."""
import argparse
import os
from pathlib import Path
import select
import socket
import subprocess
import tempfile
import threading
import time


def churn(directory, finished):
    """Directories appear and vanish during the startup scan and after it."""
    iteration = 0
    while not finished.is_set():
        path = directory / f"churn-{iteration % 50}"
        try:
            path.mkdir()
            path.rmdir()
        except OSError:
            pass
        iteration += 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("binary", type=Path)
    args = parser.parse_args()
    binary = str(args.binary.resolve())
    with tempfile.TemporaryDirectory(prefix="notify-forwarder-tree-") as scratch:
        root = Path(scratch).resolve()
        (root / "sub").mkdir()
        (root / "churn").mkdir()
        os.mkfifo(root / "fifo")
        (root / "dangling").symlink_to("/notify-forwarder-nonexistent")
        unix_socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        unix_socket.bind(str(root / "socket"))
        listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]

        finished = threading.Event()
        churner = threading.Thread(target=churn, args=(root / "churn", finished))
        churner.start()
        watcher = subprocess.Popen([binary, "watch", "-c", f"127.0.0.1:{port}", str(root), "/guest"])
        try:
            expected = b"/guest/sub/file"
            deadline = time.monotonic() + 15
            seen = False
            iteration = 0
            while not seen:
                assert watcher.poll() is None, f"the watcher exited with {watcher.returncode}"
                assert time.monotonic() < deadline, "no change in the tree was forwarded"
                (root / "sub" / "file").write_text(str(iteration))
                iteration += 1
                # The churn forwards steadily, so this waits for a time and not for silence.
                write_again = time.monotonic() + 0.2
                while not seen and time.monotonic() < write_again:
                    if select.select([listener], [], [], 0.05)[0]:
                        seen = listener.recv(65536) == expected
            print("PASS a change beneath a socket, a FIFO and a dangling symlink is forwarded")

            time.sleep(3)
            assert watcher.poll() is None, f"the watcher exited with {watcher.returncode}"
            print("PASS the watcher survives directories vanishing around it for 3 s")
        finally:
            finished.set()
            churner.join()
            if watcher.poll() is None:
                watcher.kill()
            watcher.wait(timeout=10)
            unix_socket.close()
            listener.close()


if __name__ == "__main__":
    main()
