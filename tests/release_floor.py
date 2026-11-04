#!/usr/bin/env python3
"""The release gate rejects drift, unreadable assets, and missing tools."""

import importlib.util
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("release_floor", ROOT / "scripts/check_release_floor.py")
CHECK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECK)


class ReleaseFloorTests(unittest.TestCase):
    def test_macos_exact_minimum(self):
        for minimum in ("15.0", "15.0.0"):
            with self.subTest(minimum=minimum):
                CHECK.check_macos(f" platform MACOS\n minos {minimum}\n sdk 26.2\n", "15.0")

    def test_macos_rejects_higher_and_lower_minimum(self):
        for minimum in ("14.0", "15.1", "26.0"):
            with self.subTest(minimum=minimum), self.assertRaisesRegex(ValueError, "expected exactly"):
                CHECK.check_macos(f" platform MACOS\n minos {minimum}\n", "15.0")

    def test_macos_checks_every_slice(self):
        with self.assertRaisesRegex(ValueError, "expected exactly"):
            CHECK.check_macos(" platform MACOS\n minos 15.0\n platform MACOS\n minos 26.0\n", "15.0")

    def test_macos_rejects_missing_or_foreign_build_version(self):
        for output in ("", " sdk 15.0\n", " minos 15.0\n", " platform IOS\n minos 15.0\n"):
            with self.subTest(output=output), self.assertRaises(ValueError):
                CHECK.check_macos(output, "15.0")

    def test_glibc_accepts_equal_or_older_requirements(self):
        for version in ("2.9", "2.17", "2.27", "2.27.0"):
            with self.subTest(version=version):
                CHECK.check_glibc(f"  Name: GLIBC_{version}  Flags: none  Version: 2\n", "2.27")

    def test_glibc_rejects_newer_requirement_among_older_ones(self):
        with self.assertRaisesRegex(ValueError, "requires GLIBC_2.34"):
            CHECK.check_glibc("Name: GLIBC_2.9\nName: GLIBC_2.34\nName: GLIBC_2.27\n", "2.27")

    def test_glibc_compares_versions_numerically(self):
        with self.assertRaisesRegex(ValueError, "GLIBC_2.100"):
            CHECK.check_glibc("Name: GLIBC_2.100\n", "2.27")

    def test_glibc_rejects_unknown_or_missing_requirements(self):
        for output in ("", "No version information found", "Name: GLIBC_PRIVATE\n", "Name: GLIBC_2.bad\n"):
            with self.subTest(output=output), self.assertRaises(ValueError):
                CHECK.check_glibc(output, "2.27")

    def test_bad_versions_are_not_silently_accepted(self):
        for version in ("", "15", "15.x", "PRIVATE"):
            with self.subTest(version=version), self.assertRaises(ValueError):
                CHECK.version_tuple(version)

    def test_cli_invokes_binary_inspector_for_each_target(self):
        for target, floor in CHECK.FLOORS.items():
            macos = target.startswith("darwin_")
            output = f" platform MACOS\n minos {floor}\n" if macos else f"Name: GLIBC_{floor}\n"
            with self.subTest(target=target), patch.object(sys, "argv", ["check", target, "asset"]), patch.object(
                CHECK.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, output, "")
            ) as run:
                self.assertEqual(CHECK.main(), 0)
                expected = (["xcrun", "vtool", "-show-build", "asset"] if macos
                            else ["readelf", "--version-info", "--wide", "asset"])
                self.assertEqual(run.call_args.args[0], expected)
                self.assertTrue(run.call_args.kwargs["check"])

    def test_cli_fails_when_tool_missing_or_asset_unreadable(self):
        for error in (FileNotFoundError("readelf"), subprocess.CalledProcessError(1, "readelf", stderr="bad ELF")):
            with self.subTest(error=error), patch.object(sys, "argv", ["check", "linux_arm64", "asset"]), patch.object(
                CHECK.subprocess, "run", side_effect=error
            ):
                self.assertEqual(CHECK.main(), 1)

    def test_cli_fails_when_binary_exceeds_floor(self):
        with patch.object(sys, "argv", ["check", "linux_armv7l", "asset"]), patch.object(
            CHECK.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "Name: GLIBC_2.34\n", "")
        ):
            self.assertEqual(CHECK.main(), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
