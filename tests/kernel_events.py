#!/usr/bin/env python3
"""Measures each call that could raise an event: what the kernel reports and what it changes.

Run it on the filesystem in question (--directory). Prints a table, and fails when a call the
receiver relies on stops raising its event or starts changing the file.
"""
import argparse
import ctypes
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import time

from inotify import read_until, stop

UTIME_OMIT = 1073741822
CONTENT = b"existing allocated range" * 1024
ATTRIBUTE = "user.notify-forwarder"
WATCHER = "require('fs').watch(process.argv[1], (event) => console.log(event)); console.log('ready');"


class Timespec(ctypes.Structure):
    _fields_ = [("tv_sec", ctypes.c_long), ("tv_nsec", ctypes.c_long)]


libc = ctypes.CDLL(None, use_errno=True)
libc.futimens.argtypes = [ctypes.c_int, ctypes.POINTER(Timespec)]
libc.fallocate.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_longlong, ctypes.c_longlong]


def nothing(_):
    pass


def libc_call(result):
    if result != 0:
        raise OSError(ctypes.get_errno(), os.strerror(ctypes.get_errno()))


def attribute_pair(descriptor):
    os.setxattr(descriptor, ATTRIBUTE, b"")
    os.removexattr(descriptor, ATTRIBUTE)


def current_mode(descriptor):
    os.fchmod(descriptor, os.fstat(descriptor).st_mode & 0o7777)


def current_times(descriptor):
    status = os.fstat(descriptor)
    os.utime(descriptor, ns=(status.st_atime_ns, status.st_mtime_ns))


def current_owner(descriptor):
    status = os.fstat(descriptor)
    os.fchown(descriptor, status.st_uid, status.st_gid)


def omit_times(descriptor):
    times = (Timespec * 2)(Timespec(0, UTIME_OMIT), Timespec(0, UTIME_OMIT))
    libc_call(libc.futimens(descriptor, times))


# name, open flags, call
CALLS = [
    ("open(O_RDONLY), close", os.O_RDONLY, nothing),
    ("open(O_WRONLY), close", os.O_WRONLY, nothing),
    ("fsetxattr + fremovexattr", os.O_RDONLY, attribute_pair),
    ("fchmod(current mode)", os.O_RDONLY, current_mode),
    ("fchown(current owner)", os.O_RDONLY, current_owner),
    ("fchown(-1, -1)", os.O_RDONLY, lambda descriptor: os.fchown(descriptor, -1, -1)),
    ("futimens(current times)", os.O_RDONLY, current_times),
    ("futimens(OMIT, OMIT)", os.O_RDONLY, omit_times),
    ("write(fd, buf, 0)", os.O_WRONLY, lambda descriptor: os.write(descriptor, b"")),
    ("fallocate(KEEP_SIZE, 0, 1)", os.O_WRONLY, lambda descriptor: libc_call(libc.fallocate(descriptor, 1, 0, 1))),
    ("ftruncate(current size)", os.O_WRONLY, lambda descriptor: os.ftruncate(descriptor, os.fstat(descriptor).st_size)),
]


def drain(stream):
    output = b""
    while select.select([stream], [], [], 0.3)[0]:
        chunk = os.read(stream.fileno(), 65536)
        if not chunk:
            break
        output += chunk
    return output.decode().split()


def state(path):
    status = os.stat(path)
    return {
        "size": status.st_size, "mtime": status.st_mtime_ns, "ctime": status.st_ctime_ns,
        "mode": status.st_mode, "owner": (status.st_uid, status.st_gid),
    }


def measure(path, flags, call):
    path.write_bytes(CONTENT)
    os.chmod(path, 0o644)
    os.utime(path, ns=(1234567890123456789, 1234567891987654321))
    before = state(path)
    inotify = subprocess.Popen(
        ["inotifywait", "-m", "--format", "%e", str(path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    node = subprocess.Popen(["node", "-e", WATCHER, str(path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        read_until(inotify.stderr, b"Watches established.")
        read_until(node.stdout, b"ready")
        time.sleep(0.1)
        descriptor = os.open(path, flags | os.O_NONBLOCK | os.O_CLOEXEC)
        try:
            try:
                call(descriptor)
                result = "ok"
            except OSError as error:
                result = os.strerror(error.errno)
        finally:
            os.close(descriptor)
        events = drain(inotify.stdout)
        fs_watch = drain(node.stdout)
    finally:
        stop(inotify)
        stop(node)
    after = state(path)
    changed = [key for key in before if before[key] != after[key]]
    if path.read_bytes() != CONTENT:
        changed.append("bytes")
    return result, events, fs_watch, changed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path)
    args = parser.parse_args()
    results = {}
    with tempfile.TemporaryDirectory(prefix="kernel-events-", dir=args.directory) as directory:
        for index, (name, flags, call) in enumerate(CALLS):
            results[name] = measure(Path(directory) / f"file-{index}", flags, call)
    print("call | result | inotify | fs.watch | changed")
    for name, (result, events, fs_watch, changed) in results.items():
        print(f"{name} | {result} | {' '.join(events)} | {' '.join(fs_watch)} | {' '.join(changed)}")

    failures = []

    def require(condition, message):
        if not condition:
            failures.append(message)

    # What the receiver does: a writable close, and an attribute pair where the filesystem has them.
    result, events, fs_watch, changed = results["open(O_WRONLY), close"]
    require(any("CLOSE_WRITE" in event for event in events), "open(O_WRONLY), close raised no IN_CLOSE_WRITE")
    require(changed == [], f"open(O_WRONLY), close changed {changed}")
    result, events, fs_watch, changed = results["fsetxattr + fremovexattr"]
    if result == "ok":
        require("ATTRIB" in events, "the attribute pair raised no IN_ATTRIB")
        require("change" in fs_watch, "the attribute pair reached no fs.watch")
        require(changed in ([], ["ctime"]), f"the attribute pair changed {changed}")
    else:
        print(f"NOTE fsetxattr here: {result}: the receiver raises no IN_ATTRIB on this filesystem")
    # Why the alternatives are out, so a kernel that changes this is noticed.
    require(results["open(O_WRONLY), close"][2] == [], "fs.watch now sees a bare close: revisit the attribute pair")
    require("mtime" in results["ftruncate(current size)"][3], "ftruncate no longer changes mtime: revisit IN_MODIFY")
    result, events, fs_watch, changed = results["write(fd, buf, 0)"]
    require("MODIFY" not in events, "a zero-byte write now raises IN_MODIFY: revisit IN_MODIFY")
    result, events, fs_watch, changed = results["fallocate(KEEP_SIZE, 0, 1)"]
    require(result != "ok" or "mtime" in changed, "fallocate no longer changes mtime: revisit IN_MODIFY")
    if failures:
        for failure in failures:
            print(f"FAIL {failure}")
        sys.exit(1)
    print("PASS the calls the receiver relies on raise their events and change no data, size, mode or mtime")


if __name__ == "__main__":
    main()
