#!/usr/bin/env python3
"""Check the compatibility contract on the binary that will be packaged."""

import argparse
import os
import re
import subprocess
import sys


FLOORS = {
    "darwin_arm64": "15.0",
    "darwin_x86_64": "15.0",
    "linux_arm64": "2.27",
    "linux_armv7l": "2.27",
    "linux_x86_64": "2.27",
}


def version_tuple(value):
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)+", value):
        raise ValueError(f"Unrecognized version: {value}")
    parts = [int(part) for part in value.split(".")]
    while len(parts) > 2 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def check_macos(output, floor):
    minimums = re.findall(r"^\s*minos\s+(\S+)\s*$", output, re.MULTILINE)
    platforms = re.findall(r"^\s*platform\s+(\S+)\s*$", output, re.MULTILINE)
    if not minimums or platforms != ["MACOS"] * len(minimums):
        raise ValueError("vtool did not report a macOS build version for every minimum")
    for minimum in minimums:
        if version_tuple(minimum) != version_tuple(floor):
            raise ValueError(f"macOS minos {minimum}; expected exactly {floor}")
    return f"macOS minos {floor}"


def check_glibc(output, floor):
    requirements = set(re.findall(r"\bName:\s+GLIBC_(\S+)", output))
    if not requirements:
        raise ValueError("readelf did not report any GLIBC version requirements")
    newest = max(requirements, key=version_tuple)
    if version_tuple(newest) > version_tuple(floor):
        raise ValueError(f"requires GLIBC_{newest}; maximum allowed is GLIBC_{floor}")
    return f"newest required GLIBC_{newest} <= GLIBC_{floor}"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", choices=FLOORS)
    parser.add_argument("binary")
    arguments = parser.parse_args()
    macos = arguments.target.startswith("darwin_")
    command = (["xcrun", "vtool", "-show-build"] if macos
               else ["readelf", "--version-info", "--wide"])
    try:
        result = subprocess.run(
            command + [arguments.binary], check=True, capture_output=True, text=True,
            env={**os.environ, "LC_ALL": "C"},
        )
        check = check_macos if macos else check_glibc
        verdict = check(result.stdout, FLOORS[arguments.target])
    except (OSError, subprocess.CalledProcessError, ValueError) as error:
        print(f"{arguments.target}: {error}", file=sys.stderr)
        if isinstance(error, subprocess.CalledProcessError):
            print(error.stderr, file=sys.stderr)
        return 1
    print(f"{arguments.target}: {verdict}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
