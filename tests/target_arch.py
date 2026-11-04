#!/usr/bin/env python3
"""What make would build under a stubbed uname: a supported target's flags, or an error naming the architecture.

Every case runs make -n, so nothing is compiled and no cross compiler is needed. -B prints the compile
lines even when a build of the same target is already in _build. The environment holds PATH and
nothing else, because a make that runs this test exports its own settings in MAKEFLAGS.
"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent

TARGETS = {
    "darwin_arm64": ("-arch arm64", "_build/Darwin_arm64/"),
    "darwin_x86_64": ("-arch x86_64", "_build/Darwin_x86_64/"),
    "linux_arm64": ("-target aarch64-linux-gnu", "_build/Linux_arm64/"),
    "linux_armv7l": ("-target arm-linux-gnueabihf", "_build/Linux_armv7l/"),
    "linux_x86_64": ("-target x86_64-linux-gnu", "_build/Linux_x86_64/"),
}

# name, uname -s, uname -m, make arguments, extra environment, the architecture the error must name
REFUSED = [
    ("host_riscv64", "Linux", "riscv64", ["build"], {}, "riscv64"),
    ("host_armv6l", "Linux", "armv6l", ["build"], {}, "armv6l"),
    ("host_i386_on_darwin", "Darwin", "i386", ["build"], {}, "i386"),
    ("host_riscv64_test", "Linux", "riscv64", ["test"], {}, "riscv64"),
    ("host_riscv64_test_inotify", "Linux", "riscv64", ["test-inotify"], {}, "riscv64"),
    ("host_riscv64_test_receiver_root", "Linux", "riscv64", ["test-receiver-root"], {}, "riscv64"),
    ("host_riscv64_clean_then_build", "Linux", "riscv64", ["clean", "build"], {}, "riscv64"),
    ("override_aarch64", "Linux", "x86_64", ["TARGET_ARCH=aarch64", "build"], {}, "aarch64"),
    ("override_aarch64_on_arm_host", "Linux", "aarch64", ["TARGET_ARCH=aarch64", "build"], {}, "aarch64"),
    ("override_aarch64_on_darwin", "Darwin", "arm64", ["TARGET_ARCH=aarch64", "build"], {}, "aarch64"),
    ("override_riscv64", "Linux", "x86_64", ["TARGET_ARCH=riscv64", "build"], {}, "riscv64"),
    ("override_amd64", "Linux", "x86_64", ["TARGET_ARCH=amd64", "build"], {}, "amd64"),
    ("override_upper_case", "Linux", "x86_64", ["TARGET_ARCH=ARM64", "build"], {}, "ARM64"),
    ("override_empty", "Linux", "x86_64", ["TARGET_ARCH=", "build"], {}, ""),
    ("override_two_words", "Linux", "x86_64", ["TARGET_ARCH=arm64 x86_64", "build"], {}, "arm64 x86_64"),
    ("override_no_goal", "Linux", "x86_64", ["TARGET_ARCH=riscv64"], {}, "riscv64"),
    ("override_all", "Linux", "x86_64", ["TARGET_ARCH=riscv64", "all"], {}, "riscv64"),
    ("override_test", "Linux", "x86_64", ["TARGET_ARCH=riscv64", "test"], {}, "riscv64"),
    ("override_output_file", "Linux", "x86_64",
     ["TARGET_ARCH=riscv64", "_build/Linux_riscv64/notify-forwarder"], {}, "riscv64"),
    ("environment_riscv64", "Linux", "x86_64", ["build"], {"TARGET_ARCH": "riscv64"}, "riscv64"),
    ("environment_no_goal", "Linux", "x86_64", [], {"TARGET_ARCH": "riscv64"}, "riscv64"),
    # Both names are supported, but not together: this pair has no target.
    ("armv7l_on_darwin", "Linux", "x86_64", ["TARGET_OS=Darwin", "TARGET_ARCH=armv7l", "build"], {}, "armv7l"),
    ("armv7l_host_for_darwin", "Linux", "armv7l", ["TARGET_OS=Darwin", "build"], {}, "armv7l"),
]

# name, uname -s, uname -m, make arguments, the target whose flags and output directory must appear
BUILDS = [
    ("host_linux_x86_64", "Linux", "x86_64", ["build"], "linux_x86_64"),
    ("host_linux_aarch64", "Linux", "aarch64", ["build"], "linux_arm64"),
    ("host_linux_arm64", "Linux", "arm64", ["build"], "linux_arm64"),
    ("host_linux_armv7l", "Linux", "armv7l", ["build"], "linux_armv7l"),
    ("host_darwin_arm64", "Darwin", "arm64", ["build"], "darwin_arm64"),
    ("host_darwin_x86_64", "Darwin", "x86_64", ["build"], "darwin_x86_64"),
    ("host_linux_x86_64_test", "Linux", "x86_64", ["test"], "linux_x86_64"),
    ("host_linux_x86_64_test_inotify", "Linux", "x86_64", ["test-inotify"], "linux_x86_64"),
    ("override_arm64", "Linux", "x86_64", ["TARGET_ARCH=arm64", "build"], "linux_arm64"),
    ("override_armv7l", "Linux", "x86_64", ["TARGET_ARCH=armv7l", "build"], "linux_armv7l"),
    ("override_x86_64", "Linux", "aarch64", ["TARGET_ARCH=x86_64", "build"], "linux_x86_64"),
    ("override_on_darwin", "Darwin", "arm64", ["TARGET_ARCH=x86_64", "build"], "darwin_x86_64"),
    ("override_os_and_arch", "Darwin", "arm64", ["TARGET_OS=Linux", "TARGET_ARCH=armv7l", "build"], "linux_armv7l"),
]

# What each named target passes to its sub-make. make -n prints that line and does not run it, because
# add_target expands $(MAKE) when it is evaluated, so a case runs the same settings itself.
NAMED = {
    "darwin_arm64": ("Darwin", "arm64"),
    "darwin_x86_64": ("Darwin", "x86_64"),
    "linux_arm64": ("Linux", "arm64"),
    "linux_armv7l": ("Linux", "armv7l"),
    "linux_x86_64": ("Linux", "x86_64"),
}


def make(host_os, host_arch, arguments, environment):
    with tempfile.TemporaryDirectory(prefix="notify-forwarder-target-arch-") as scratch:
        uname = Path(scratch) / "uname"
        uname.write_text(
            "#!/bin/sh\n"
            'case "$1" in\n'
            f"  -s) echo '{host_os}' ;;\n"
            f"  -m) echo '{host_arch}' ;;\n"
            '  *) echo "stub uname: unexpected argument $1" >&2; exit 64 ;;\n'
            "esac\n"
        )
        uname.chmod(0o755)
        return subprocess.run(
            ["make", "-n", "-B", *arguments],
            cwd=ROOT,
            env={"PATH": f"{scratch}{os.pathsep}{os.environ['PATH']}", **environment},
            capture_output=True,
            text=True,
            timeout=60,
        )


def require_refused(result, architecture):
    assert result.returncode > 0, (
        f"status {result.returncode}, would run {compile_flags(result.stdout)!r}"
    )
    assert f"'{architecture}'" in result.stderr, f"no '{architecture}' in {result.stderr!r}"
    for target in TARGETS:
        assert target in result.stderr, f"no supported target {target} in {result.stderr!r}"
    assert "src/main.cc" not in result.stdout, f"would still compile: {result.stdout!r}"


def require_builds(result, target):
    assert result.returncode == 0, f"status {result.returncode}, stderr {result.stderr!r}"
    for needle in TARGETS[target]:
        assert needle in result.stdout, f"no {needle!r} in {compile_flags(result.stdout)!r}"
    wrong = [
        needle
        for other, needles in TARGETS.items()
        if other != target
        for needle in needles
        if needle in result.stdout
    ]
    assert not wrong, f"{target} would also use {wrong!r}"


def compile_flags(stdout):
    """The one line that says which machine a run builds for, not the whole transcript."""
    for line in stdout.splitlines():
        if "src/main.cc" in line:
            return line
    return stdout[:200]


def clean_needs_no_supported_host():
    result = make("Linux", "riscv64", ["clean"], {})
    assert result.returncode == 0, f"status {result.returncode}, stderr {result.stderr!r}"
    assert "rm -rf _build" in result.stdout, f"no rm in {result.stdout!r}"


def require_sub_make(result, target):
    assert result.returncode == 0, f"status {result.returncode}, stderr {result.stderr!r}"
    target_os, target_arch = NAMED[target]
    needle = f"TARGET_OS={target_os} TARGET_ARCH={target_arch} "
    assert needle in result.stdout, f"{target}: no {needle!r} in {result.stdout[:300]!r}"


def named_target_on_an_unsupported_host(target):
    """A named target sets its own TARGET_OS and TARGET_ARCH, so the host's architecture does not matter."""
    require_sub_make(make("Linux", "riscv64", [target], {}), target)
    target_os, target_arch = NAMED[target]
    settings = [f"TARGET_OS={target_os}", f"TARGET_ARCH={target_arch}", "build"]
    require_builds(make("Linux", "riscv64", settings, {}), target)


def all_targets_on_an_unsupported_host(arguments):
    result = make("Linux", "riscv64", arguments, {})
    for target in NAMED:
        require_sub_make(result, target)


def ci_compiler_override():
    result = make("Linux", "x86_64", ["CXX=c++", "test"], {})
    assert result.returncode == 0, f"status {result.returncode}, stderr {result.stderr!r}"
    assert "_build/Linux_x86_64/" in result.stdout, f"no output directory in {result.stdout[:200]!r}"


def cases():
    for name, host_os, host_arch, arguments, environment, architecture in REFUSED:
        yield f"refused_{name}", lambda h=host_os, a=host_arch, g=arguments, e=environment, n=architecture: (
            require_refused(make(h, a, g, e), n)
        )
    for name, host_os, host_arch, arguments, target in BUILDS:
        yield f"builds_{name}", lambda h=host_os, a=host_arch, g=arguments, t=target: (
            require_builds(make(h, a, g, {}), t)
        )
    for target in NAMED:
        yield f"named_{target}_on_riscv64", lambda t=target: named_target_on_an_unsupported_host(t)
    yield "clean_needs_no_supported_host", clean_needs_no_supported_host
    yield "all_on_riscv64_host", lambda: all_targets_on_an_unsupported_host(["all"])
    yield "no_goal_on_riscv64_host", lambda: all_targets_on_an_unsupported_host([])
    yield "ci_compiler_override", ci_compiler_override


def main():
    failures = 0
    for name, case in cases():
        try:
            case()
            print(f"PASS {name}")
        except (AssertionError, subprocess.TimeoutExpired) as error:
            print(f"FAIL {name}: {error}")
            failures += 1
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
