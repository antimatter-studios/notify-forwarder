#!/usr/bin/env python3
"""The receiver against a real shared folder, while the host writes the files it is notifying.

Run on the host, against a running VM:
    tests/shared_folder.py <vm.sh> <receiver path in the guest> <host share> [--sudo]
The share must be mounted at /share in the guest; run the command with a harness's vm.sh, which
provides that. Needs inotify-tools, node and python3 in the guest.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import select
import shutil
import socket
import subprocess
import tempfile
import threading
import time

from inotify import free_port, read_until, snapshot, stop

FILES = 168
SETTLE_SECONDS = 2.5


def send_message(connection, message):
    connection.sendall(json.dumps(message).encode() + b"\n")


def receive_message(stream):
    line = stream.readline()
    if not line:
        raise AssertionError("shared-folder peer disconnected")
    return json.loads(line)


def guest(binary, directory, port):
    directory = Path(directory)
    paths = sorted(directory.glob("file-*"))
    assert len(paths) == FILES, f"expected {FILES} shared files, found {len(paths)}"
    processes = []
    done = threading.Event()
    traffic = None
    with socket.create_connection(("10.0.2.2", port), timeout=90) as control:
        control.settimeout(90)
        stream = control.makefile("rb")
        try:
            observer = subprocess.Popen(
                ["inotifywait", "-m", "--format", "%e", "-e", "attrib,close_write,modify", str(directory)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            processes.append(observer)
            read_until(observer.stderr, b"Watches established.")
            node = subprocess.Popen(
                ["node", "-e", "const fs=require('fs'); fs.watch(process.argv[1], (event)=>console.log(event)); console.log('ready');", str(directory)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            processes.append(node)
            read_until(node.stdout, b"ready")
            receiver_port = free_port()
            receiver = subprocess.Popen(
                [binary, "receive", "-p", str(receiver_port)], stderr=subprocess.PIPE,
            )
            processes.append(receiver)

            def forward():
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
                    while not done.is_set():
                        for path in paths:
                            sender.sendto(os.fsencode(path), ("127.0.0.1", receiver_port))
                            if done.wait(0.001):
                                return

            traffic = threading.Thread(target=forward)
            traffic.start()
            events = b""
            changes = b""
            deadline = time.monotonic() + 30
            while b"ATTRIB" not in events or b"CLOSE_WRITE" not in events or b"change" not in changes:
                if time.monotonic() >= deadline:
                    raise AssertionError(f"missing shared events: {events[:400]!r}; fs.watch={changes[:400]!r}")
                ready = select.select([observer.stdout, node.stdout], [], [], 1)[0]
                for readable in ready:
                    output = os.read(readable.fileno(), 65536)
                    if not output:
                        raise AssertionError("shared-folder observer exited")
                    if readable is observer.stdout:
                        events += output
                    else:
                        changes += output
            send_message(control, {"ready": True})
            assert receive_message(stream) == {"write": True}
            send_message(control, {"writing": True})
            events = b""
            changes = b""
            while True:
                ready = select.select([control, observer.stdout, node.stdout], [], [], 90)[0]
                if not ready:
                    raise AssertionError("host writer timed out")
                if control in ready:
                    assert receive_message(stream) == {"done": True}
                    break
                for readable in ready:
                    output = os.read(readable.fileno(), 65536)
                    if not output:
                        raise AssertionError("shared-folder observer exited during host writes")
                    if readable is observer.stdout:
                        assert b"Q_OVERFLOW" not in output, "inotify queue overflowed"
                        events += output
                    else:
                        changes += output
            done.set()
            traffic.join(timeout=10)
            assert not traffic.is_alive(), "UDP traffic thread did not stop"
            for process in reversed(processes):
                stop(process)
            processes.clear()
            send_message(control, {"stopped": True})
            result = receive_message(stream)
            counts = {
                "attrib_during_writes": events.count(b"ATTRIB"),
                "close_write_during_writes": events.count(b"CLOSE_WRITE"),
                "fs_watch_during_writes": changes.count(b"change"),
            }
            print(json.dumps({**result, **counts}, sort_keys=True))
            assert result["failures"] == [], "receiver changed host file bytes, size, mode or mtime"
            assert counts["attrib_during_writes"], "no IN_ATTRIB while the host was writing"
            assert counts["close_write_during_writes"], "no IN_CLOSE_WRITE while the host was writing"
            assert counts["fs_watch_during_writes"], "no fs.watch change while the host was writing"
        finally:
            done.set()
            if traffic is not None:
                traffic.join(timeout=10)
            for process in reversed(processes):
                stop(process)


def extended_attributes(path):
    if hasattr(os, "listxattr"):
        return [name for name in os.listxattr(path) if "notify-forwarder" in name]
    listed = subprocess.run(["xattr", str(path)], capture_output=True, text=True, check=True).stdout
    return [name for name in listed.split() if "notify-forwarder" in name]


def host(harness, binary, share, sudo):
    with tempfile.TemporaryDirectory(prefix="receiver-shared-", dir=share) as temporary:
        directory = Path(temporary)
        scripts = directory / "scripts"
        scripts.mkdir()
        for script in ("shared_folder.py", "inotify.py"):
            shutil.copy(Path(__file__).with_name(script), scripts / script)
        paths = [directory / f"file-{index:03d}" for index in range(FILES)]
        for path in paths:
            path.touch()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            listener.settimeout(90)
            port = listener.getsockname()[1]
            guest_directory = f"/share/{directory.name}"
            prefix = "sudo " if sudo else ""
            command = f"{prefix}python3 -B {guest_directory}/scripts/shared_folder.py --guest {binary} {guest_directory} {port}"
            worker = subprocess.Popen([str(harness), "exec", command])
            try:
                control, _ = listener.accept()
                with control:
                    control.settimeout(90)
                    stream = control.makefile("rb")
                    assert receive_message(stream) == {"ready": True}
                    send_message(control, {"write": True})
                    assert receive_message(stream) == {"writing": True}
                    expected = {}
                    for index, path in enumerate(paths):
                        block = bytes([index % 251]) * 65536
                        with path.open("wb", buffering=0) as output:
                            for chunk in range(16):
                                output.write(block)
                        mode = 0o640 if index % 2 else 0o600
                        os.chmod(path, mode)
                        timestamp = 1800000000000000000 + index * 1000000000 + 123456789
                        os.utime(path, ns=(timestamp, timestamp))
                        expected[path.name] = (hashlib.sha256(block * 16).hexdigest(), 1048576, timestamp, mode)
                    # The receiver stays busy past the last write, for longer than the guest caches attributes.
                    time.sleep(SETTLE_SECONDS)
                    send_message(control, {"done": True})
                    assert receive_message(stream) == {"stopped": True}
                    failures = []
                    for path in paths:
                        contents, size, mtime = snapshot(path)
                        observed = (hashlib.sha256(contents).hexdigest(), size, mtime, path.stat().st_mode & 0o7777)
                        residue = extended_attributes(path)
                        if observed != expected[path.name] or residue:
                            failures.append({"file": path.name, "expected": expected[path.name], "observed": observed, "residue": residue})
                    result = {"files": FILES, "bytes_per_file": 1048576, "failures": failures}
                    send_message(control, result)
                    print(json.dumps({"files": FILES, "failed": len(failures), "first_failures": failures[:3]}, sort_keys=True), flush=True)
                status = worker.wait(timeout=30)
                assert status == 0, f"guest shared-folder test exited {status}"
                assert not failures, "host file invariants failed"
            finally:
                stop(worker)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--guest", action="store_true")
    parser.add_argument("--sudo", action="store_true")
    parser.add_argument("arguments", nargs="+")
    args = parser.parse_args()
    if args.guest:
        binary, directory, port = args.arguments
        guest(binary, directory, int(port))
    else:
        vm_script, binary, share = args.arguments
        host(Path(vm_script), binary, Path(share).resolve(), args.sudo)


if __name__ == "__main__":
    main()
