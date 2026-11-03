#!/usr/bin/env python3
import argparse
import errno
import os
from pathlib import Path
import select
import signal
import socket
import subprocess
import tempfile
import time


def read_until(stream, expected, timeout=10):
    deadline = time.monotonic() + timeout
    output = b""
    while expected not in output:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([stream], [], [], remaining)[0]:
            raise AssertionError(f"missing {expected!r}; received {output!r}")
        chunk = os.read(stream.fileno(), 65536)
        if not chunk:
            raise AssertionError(f"process exited before {expected!r}: {output!r}")
        output += chunk
    return output


def snapshot(path):
    status = path.stat()
    return path.read_bytes(), status.st_size, status.st_mtime_ns


def stop(process):
    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


MUTATIONS = (
    "?write,?pwrite64,?pwritev,?pwritev2,?writev,?truncate,?ftruncate,?fallocate,?utime,?utimes,"
    "?utimensat,?futimesat,?chmod,?fchmod,?fchmodat,?fchmodat2,?chown,?fchown,?lchown,?fchownat"
)


def wait_for_bind(port, timeout=10):
    deadline = time.monotonic() + timeout
    source_port = 1024
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            for source_port in range(source_port, 65536):
                if source_port == port:
                    continue
                try:
                    probe.bind(("127.0.0.1", source_port))
                    break
                except OSError as error:
                    if error.errno != errno.EADDRINUSE:
                        raise
            else:
                raise AssertionError("no source port available for the readiness probe")
            probe.connect(("127.0.0.1", port))
            probe.settimeout(0.02)
            try:
                probe.send(b"")
                probe.recv(1)
            except ConnectionRefusedError:
                pass
            except socket.timeout:
                return
            else:
                return
        source_port += 1
        time.sleep(0.02)
    raise AssertionError(f"receiver did not bind port {port}")


class TracedProcess(subprocess.Popen):
    """strace leaves its tracee running when it is killed, so both go down as one process group."""

    def __init__(self, command, **options):
        super().__init__(command, start_new_session=True, **options)

    def terminate(self):
        os.killpg(self.pid, signal.SIGTERM)


def assert_no_mutation(trace_path, forwarded):
    """The receiver's own syscalls, read by strace: an oracle that shares no code with the receiver."""
    trace = trace_path.read_text()
    name = str(forwarded)
    acted = [line for line in trace.splitlines() if name in line]
    assert any("openat(" in line for line in acted), f"strace saw no open of {name}: {trace[:2000]}"
    mutations = [line for line in acted if "openat(" not in line]
    assert not mutations, f"receiver submitted changes to the file: {mutations[:5]}"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("binary", type=Path)
    parser.add_argument("--directory", type=Path)
    args = parser.parse_args()
    binary = str(args.binary.resolve())
    with tempfile.TemporaryDirectory(prefix="notify-forwarder-", dir=args.directory) as directory:
        root = Path(directory)
        source = root / "source"
        target = root / "target"
        source.mkdir()
        target.mkdir()
        (source / "file").write_bytes(b"source")
        forwarded = target / "file"
        forwarded.write_bytes(b"unchanged\x00binary\xffdata")
        os.utime(forwarded, ns=(1234567890123456789, 1234567891987654321))
        before = snapshot(forwarded)
        processes = []
        try:
            observer = subprocess.Popen(
                ["inotifywait", "-m", "--format", "%e", "-e", "attrib,close_write,modify", str(forwarded)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            processes.append(observer)
            read_until(observer.stderr, b"Watches established.")
            node = subprocess.Popen(
                ["node", "-e", "const fs=require('fs'); fs.watch(process.argv[1], (event)=>console.log(event)); console.log('ready');", str(forwarded)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            processes.append(node)
            read_until(node.stdout, b"ready")
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]
            trace_path = root / "receiver.strace"
            receiver = TracedProcess(
                ["strace", "-f", "-y", "-s", "4096", "-o", str(trace_path),
                 "-e", f"trace=openat,{MUTATIONS}", binary, "receive", "-p", str(port)],
                stderr=subprocess.PIPE,
            )
            processes.append(receiver)
            wait_for_bind(port)
            sender = subprocess.Popen(
                [binary, "watch", "-c", f"127.0.0.1:{port}", str(source), str(target)],
                stderr=subprocess.PIPE,
            )
            processes.append(sender)
            deadline = time.monotonic() + 30
            events = b""
            iteration = 0
            next_write = 0
            while b"ATTRIB" not in events or b"CLOSE_WRITE" not in events:
                now = time.monotonic()
                if now >= deadline:
                    raise AssertionError(f"forwarded events missing: {events!r}")
                for process in (receiver, sender):
                    if process.poll() is not None:
                        raise AssertionError(process.stderr.read().decode())
                if now >= next_write:
                    # A change per repeat window, so that each one is forwarded without waiting.
                    (source / "file").write_text(f"source update {iteration}")
                    iteration += 1
                    next_write = now + 1.2
                if select.select([observer.stdout], [], [], 0.05)[0]:
                    events += os.read(observer.stdout.fileno(), 65536)
            read_until(node.stdout, b"change")
            assert snapshot(forwarded) == before, "receiver changed bytes, size or nanosecond mtime"
            stop(sender)
            stop(receiver)
            assert_no_mutation(trace_path, forwarded)
            print("PASS strace shows no write, truncate, allocate, timestamp, mode or owner call on the target")
            print(f"PASS watched-side change -> UDP -> {events.decode().strip()} -> fs.watch change")
            print("PASS receiver preserves target bytes, size and nanosecond mtime")
        finally:
            for process in reversed(processes):
                stop(process)


if __name__ == "__main__":
    main()
