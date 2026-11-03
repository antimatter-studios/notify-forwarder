#!/usr/bin/env python3
"""A datagram longer than a path can be must be refused, not cut down into a different path."""
import argparse
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time

from inotify import wait_for_bind

# MAXPATHLEN on the platforms this runs on. A longer limit here only makes the padding longer.
LONGEST_PATH = {"Linux": 4096, "Darwin": 1024}


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as reservation:
        reservation.bind(("127.0.0.1", 0))
        return reservation.getsockname()[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("binary", type=Path)
    args = parser.parse_args()
    binary = str(args.binary.resolve())
    limit = LONGEST_PATH[os.uname().sysname]
    with tempfile.TemporaryDirectory(prefix="notify-forwarder-oversized-") as scratch:
        directory = Path(scratch).resolve()
        target = directory / "target"
        target.write_text("target")

        # Cut at the limit, this names the target: slashes pad it to the limit, and the final name
        # is one only the whole datagram has.
        padded = str(directory) + "/" + "/" * (limit - len(str(directory)) - len("/target") - 1) + "/target"
        assert len(padded) == limit, len(padded)
        datagram = padded.encode() + b"/not-the-target"

        port = free_port()
        with tempfile.TemporaryFile() as stderr:
            receiver = subprocess.Popen([binary, "receive", "-p", str(port)], stderr=stderr)
            try:
                wait_for_bind(port)
                before = target.stat().st_ctime_ns
                time.sleep(0.05)
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
                    sender.sendto(datagram, ("127.0.0.1", port))
                    # A later datagram proves the receiver has got past the long one.
                    probe = directory / "probe"
                    probe.write_text("probe")
                    probe_before = probe.stat().st_ctime_ns
                    deadline = time.monotonic() + 10
                    while probe.stat().st_ctime_ns == probe_before:
                        assert time.monotonic() < deadline, "the receiver stopped acting"
                        assert receiver.poll() is None, f"the receiver exited with {receiver.returncode}"
                        sender.sendto(str(probe).encode(), ("127.0.0.1", port))
                        time.sleep(0.02)
                failures = 0
                if target.stat().st_ctime_ns != before:
                    print("FAIL an oversized datagram was cut down and acted on as another path")
                    failures += 1
                else:
                    print("PASS an oversized datagram is not acted on")
                stderr.seek(0)
                message = stderr.read().decode()
                if "oversized" in message:
                    print("PASS an oversized datagram is reported")
                else:
                    print(f"FAIL an oversized datagram was not reported: {message!r}")
                    failures += 1
            finally:
                receiver.kill()
                receiver.wait(timeout=10)
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
