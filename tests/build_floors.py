#!/usr/bin/env python3
"""Every compile and link command chooses the release floor explicitly."""

import os
from pathlib import Path
import subprocess
import unittest


ROOT = Path(__file__).resolve().parent.parent


def build_commands(target_os, architecture, environment=None, settings=()):
    result = subprocess.run(
        ["make", "-Bn", f"TARGET_OS={target_os}", f"TARGET_ARCH={architecture}", *settings, "build"],
        cwd=ROOT, env={"PATH": os.environ["PATH"], **(environment or {})},
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode:
        raise AssertionError(result.stderr)
    commands = [line for line in result.stdout.splitlines()
                if " -c src/" in line or " -o _build/" in line]
    if not commands or not any(" -c src/" in line for line in commands):
        raise AssertionError(f"No compile commands: {result.stdout}")
    if not any(" -o _build/" in line and " -c src/" not in line for line in commands):
        raise AssertionError(f"No link command: {result.stdout}")
    return commands


class BuildFloorTests(unittest.TestCase):
    def test_both_darwin_architectures_compile_and_link_for_macos_15(self):
        for architecture in ("arm64", "x86_64"):
            with self.subTest(architecture=architecture):
                for command in build_commands("Darwin", architecture):
                    self.assertIn("-mmacosx-version-min=15.0", command)
                    self.assertIn(f"-arch {architecture}", command)

    def test_runner_environment_cannot_raise_the_darwin_floor(self):
        for command in build_commands("Darwin", "arm64", {"MACOSX_DEPLOYMENT_TARGET": "26.0"}):
            self.assertIn("-mmacosx-version-min=15.0", command)
            self.assertNotIn("-mmacosx-version-min=26.0", command)

    def test_all_linux_targets_compile_and_link_against_glibc_227(self):
        for architecture, triple in (("arm64", "aarch64-linux-gnu.2.27"),
                                     ("armv7l", "arm-linux-gnueabihf.2.27"),
                                     ("x86_64", "x86_64-linux-gnu.2.27")):
            with self.subTest(architecture=architecture):
                for command in build_commands("Linux", architecture):
                    self.assertIn(f"zig c++ -target {triple} ", command)

    def test_explicit_darwin_floor_override_reaches_compile_and_link(self):
        for command in build_commands("Darwin", "arm64", settings=("MACOSX_DEPLOYMENT_TARGET=14.0",)):
            self.assertIn("-mmacosx-version-min=14.0", command)


if __name__ == "__main__":
    unittest.main(verbosity=2)
