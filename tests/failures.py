#!/usr/bin/env python3
"""How the real binary ends, or does not, when something fails: a message, and a status that says so."""
import argparse
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time


def finished(command, timeout=10):
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout)


def require_message_and_status(result, needle):
    assert result.returncode > 0, f"status {result.returncode}, stderr {result.stderr!r}"
    assert needle in result.stderr, f"no {needle!r} in {result.stderr!r}"
    assert "terminate" not in result.stderr, f"ended through terminate: {result.stderr!r}"


def unresolvable_watch_path(binary, _directory):
    missing = "/notify-forwarder-nonexistent/watched"
    result = finished([binary, "watch", missing, "/guest"])
    require_message_and_status(result, missing)


def unreadable_address(binary, directory):
    result = finished([binary, "watch", "-c", "not-an-address", str(directory), "/guest"])
    require_message_and_status(result, "not-an-address")


def port_in_use(binary, _directory):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as holder:
        holder.bind(("0.0.0.0", 0))
        port = holder.getsockname()[1]
        result = finished([binary, "receive", "-p", str(port)])
    require_message_and_status(result, str(port))


INVALID_PORTS = ("70000", "65536", "0", "-1", "abc", "80x", " 80", "+80", "")


def require_usage(result, port):
    assert result.returncode == 64, f"port {port!r}: status {result.returncode}, stderr {result.stderr!r}"
    assert "Usage:" in result.stderr, f"port {port!r}: no usage in {result.stderr!r}"
    assert f"'{port}'" in result.stderr, f"port {port!r}: not named in {result.stderr!r}"


def invalid_receive_port(binary, _directory):
    for port in INVALID_PORTS:
        require_usage(finished([binary, "receive", "-p", port], timeout=3), port)


def invalid_watch_port(binary, directory):
    for port in INVALID_PORTS:
        result = finished([binary, "watch", "-c", f"127.0.0.1:{port}", str(directory), "/guest"], timeout=3)
        require_usage(result, port)


def send_failure(binary, directory):
    """A mapped path longer than any datagram is refused by the kernel, on Linux and macOS alike."""
    tree = directory / "send-failure"
    tree.mkdir()
    too_long = "/" + "x" * 70000
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as listener, tempfile.TemporaryFile() as stderr:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        watcher = subprocess.Popen(
            [binary, "watch", "-c", f"127.0.0.1:{port}", str(tree), too_long], stderr=stderr,
        )
        try:
            deadline = time.monotonic() + 30
            message = b""
            iteration = 0
            while b"Message too long" not in message:
                assert watcher.poll() is None, (
                    f"a send failure ended the watcher with {watcher.returncode}"
                )
                assert time.monotonic() < deadline, f"no message about the send failure: {message!r}"
                (tree / "file").write_text(str(iteration))
                iteration += 1
                time.sleep(0.2)
                stderr.seek(0)
                message = stderr.read()
            time.sleep(1)
            assert watcher.poll() is None, f"the watcher exited with {watcher.returncode}"
        finally:
            if watcher.poll() is None:
                watcher.send_signal(signal.SIGKILL)
            watcher.wait(timeout=10)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("binary", type=Path)
    args = parser.parse_args()
    binary = str(args.binary.resolve())
    failures = 0
    with tempfile.TemporaryDirectory(prefix="notify-forwarder-failures-") as scratch:
        directory = Path(scratch).resolve()
        for case in (
            unresolvable_watch_path, unreadable_address, port_in_use, invalid_receive_port,
            invalid_watch_port, send_failure,
        ):
            try:
                case(binary, directory)
                print(f"PASS {case.__name__}")
            except (AssertionError, subprocess.TimeoutExpired) as error:
                print(f"FAIL {case.__name__}: {error}")
                failures += 1
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
