#!/usr/bin/env python3
"""What make would build under a stubbed uname: a supported target's flags, or an error naming what it refuses.

Most cases run make -n, so nothing is compiled and no cross compiler is needed. -B prints the compile
lines even when a build of the same target is already in _build. The environment holds PATH and
nothing else, because a make that runs this test exports its own settings in MAKEFLAGS.

make -n does not run a named target's sub-make, so the real_run cases run make itself, with stub
compilers, in a copy of the tree. The real_compiler cases ask the machine's own c++.
"""
import os
from pathlib import Path
import platform
import shutil
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

# name, uname -s, uname -m, make arguments, extra environment, the operating system the error must name
REFUSED_OS = [
    ("host_freebsd", "FreeBSD", "x86_64", ["build"], {}, "FreeBSD"),
    ("host_freebsd_riscv64", "FreeBSD", "riscv64", ["build"], {}, "FreeBSD"),
    ("host_freebsd_test", "FreeBSD", "x86_64", ["test"], {}, "FreeBSD"),
    ("host_freebsd_test_inotify", "FreeBSD", "x86_64", ["test-inotify"], {}, "FreeBSD"),
    ("host_freebsd_test_receiver_root", "FreeBSD", "x86_64", ["test-receiver-root"], {}, "FreeBSD"),
    ("host_freebsd_clean_then_build", "FreeBSD", "x86_64", ["clean", "build"], {}, "FreeBSD"),
    ("override_freebsd", "Linux", "x86_64", ["TARGET_OS=FreeBSD", "build"], {}, "FreeBSD"),
    ("override_lower_case", "Linux", "x86_64", ["TARGET_OS=linux", "build"], {}, "linux"),
    ("override_empty", "Linux", "x86_64", ["TARGET_OS=", "build"], {}, ""),
    ("override_two_words", "Linux", "x86_64", ["TARGET_OS=Linux Darwin", "build"], {}, "Linux Darwin"),
    ("override_no_goal", "Linux", "x86_64", ["TARGET_OS=FreeBSD"], {}, "FreeBSD"),
    ("override_all", "Linux", "x86_64", ["TARGET_OS=FreeBSD", "all"], {}, "FreeBSD"),
    ("override_output_file", "Linux", "x86_64",
     ["TARGET_OS=FreeBSD", "_build/FreeBSD_x86_64/notify-forwarder"], {}, "FreeBSD"),
    ("environment_freebsd", "Linux", "x86_64", ["build"], {"TARGET_OS": "FreeBSD"}, "FreeBSD"),
    ("environment_no_goal", "Linux", "x86_64", [], {"TARGET_OS": "FreeBSD"}, "FreeBSD"),
]

# name, uname -m, make arguments, extra environment, stub compilers beyond c++,
# the CXX the error must name, the architecture it must say CXX builds for, the TARGET_ARCH it must name
REFUSED_CXX = [
    ("arm64_directory", "x86_64", ["CXX=c++", "TARGET_ARCH=arm64", "build"], {}, {}, "c++", "x86_64", "arm64"),
    ("armv7l_directory", "x86_64", ["CXX=c++", "TARGET_ARCH=armv7l", "build"], {}, {}, "c++", "x86_64", "armv7l"),
    ("x86_64_directory", "aarch64", ["CXX=c++", "TARGET_ARCH=x86_64", "build"], {}, {}, "c++", "arm64", "x86_64"),
    ("armv7l_host", "armv7l", ["CXX=c++", "TARGET_ARCH=arm64", "build"], {}, {}, "c++", "armv7l", "arm64"),
    ("test", "x86_64", ["CXX=c++", "TARGET_ARCH=arm64", "test"], {}, {}, "c++", "x86_64", "arm64"),
    ("test_inotify", "x86_64", ["CXX=c++", "TARGET_ARCH=arm64", "test-inotify"], {}, {}, "c++", "x86_64", "arm64"),
    ("test_receiver_root", "x86_64", ["CXX=c++", "TARGET_ARCH=arm64", "test-receiver-root"], {}, {},
     "c++", "x86_64", "arm64"),
    ("clean_then_build", "x86_64", ["CXX=c++", "TARGET_ARCH=arm64", "clean", "build"], {}, {},
     "c++", "x86_64", "arm64"),
    ("no_goal", "x86_64", ["CXX=c++", "TARGET_ARCH=arm64"], {}, {}, "c++", "x86_64", "arm64"),
    ("output_file", "x86_64", ["CXX=c++", "TARGET_ARCH=arm64", "_build/Linux_arm64/notify-forwarder"], {}, {},
     "c++", "x86_64", "arm64"),
    ("environment", "x86_64", ["TARGET_ARCH=arm64", "build"], {"CXX": "c++"}, {}, "c++", "x86_64", "arm64"),
    ("environment_both", "x86_64", ["build"], {"CXX": "c++", "TARGET_ARCH": "arm64"}, {}, "c++", "x86_64", "arm64"),
    ("named_target_settings", "x86_64",
     ["CXX=c++", "TARGET_OS=Linux", "TARGET_ARCH=arm64", "ZIG_ARCH=aarch64", "build"], {}, {},
     "c++", "x86_64", "arm64"),
    # A cross compiler in the host's directory is the same fault with no TARGET_ARCH in sight.
    ("cross_compiler_in_host_directory", "x86_64", ["CXX=aarch64-linux-gnu-g++", "build"], {},
     {"aarch64-linux-gnu-g++": "aarch64"}, "aarch64-linux-gnu-g++", "arm64", "x86_64"),
    # The flags reach the compile, so they are part of what CXX builds for.
    ("m32_in_cxx", "x86_64", ["CXX=c++ -m32", "build"], {}, {}, "c++ -m32", None, "x86_64"),
    ("m32_in_extra_cxxflags", "x86_64", ["CXX=c++", "EXTRA_CXXFLAGS=-m32", "build"], {}, {}, "c++", None, "x86_64"),
    ("m32_in_cxxflags", "x86_64", ["CXX=c++", "CXXFLAGS=-m32", "build"], {}, {}, "c++", None, "x86_64"),
    ("m32_in_cppflags", "x86_64", ["CXX=c++", "CPPFLAGS=-m32", "build"], {}, {}, "c++", None, "x86_64"),
    ("compiler_for_no_target", "x86_64", ["CXX=riscv64-linux-gnu-g++", "build"], {},
     {"riscv64-linux-gnu-g++": "riscv64"}, "riscv64-linux-gnu-g++", None, "x86_64"),
    ("missing_compiler", "x86_64", ["CXX=no-such-compiler", "build"], {}, {}, "no-such-compiler", None, "x86_64"),
    ("empty_compiler", "x86_64", ["CXX=", "build"], {}, {}, "", None, "x86_64"),
]

# name, uname -m, make arguments, extra environment, stub compilers beyond c++,
# the command that must compile src/main.cc, the output directory it must use
ALLOWED_CXX = [
    ("ci_build_x86_64", "x86_64", ["CXX=c++", "build"], {}, {}, "c++", "_build/Linux_x86_64/"),
    ("ci_build_aarch64", "aarch64", ["CXX=c++", "build"], {}, {}, "c++", "_build/Linux_arm64/"),
    ("ci_build_armv7l", "armv7l", ["CXX=c++", "build"], {}, {}, "c++", "_build/Linux_armv7l/"),
    ("ci_test", "x86_64", ["CXX=c++", "test"], {}, {}, "c++", "_build/Linux_x86_64/"),
    ("ci_test_inotify", "x86_64", ["CXX=c++", "test-inotify"], {}, {}, "c++", "_build/Linux_x86_64/"),
    ("ci_test_receiver_root", "x86_64", ["CXX=c++", "test-receiver-root"], {}, {}, "c++", "_build/Linux_x86_64/"),
    ("environment", "x86_64", ["build"], {"CXX": "c++"}, {}, "c++", "_build/Linux_x86_64/"),
    ("explicit_host_architecture", "x86_64", ["CXX=c++", "TARGET_ARCH=x86_64", "build"], {}, {},
     "c++", "_build/Linux_x86_64/"),
    ("cross_compiler_arm64", "x86_64", ["CXX=aarch64-linux-gnu-g++", "TARGET_ARCH=arm64", "build"], {},
     {"aarch64-linux-gnu-g++": "aarch64"}, "aarch64-linux-gnu-g++", "_build/Linux_arm64/"),
    ("cross_compiler_armv7l", "x86_64", ["CXX=arm-linux-gnueabihf-g++", "TARGET_ARCH=armv7l", "build"], {},
     {"arm-linux-gnueabihf-g++": "armv7l"}, "arm-linux-gnueabihf-g++", "_build/Linux_armv7l/"),
]

# What a compiler predefines for each architecture, written here apart from the Makefile's own list.
PREDEFINED = {
    "x86_64": "__x86_64__",
    "aarch64": "__aarch64__",
    "arm64": "__aarch64__",
    "armv7l": "__arm__",
    "riscv64": "__riscv",
    "i386": "__i386__",
}


def write_executable(path, text):
    path.write_text(text)
    path.chmod(0o755)


def stub_compiler(architecture):
    """Answers a macro query for one architecture, and otherwise creates the file named by -o."""
    return (
        "#!/bin/sh\n"
        'case " $* " in\n'
        f"  *' -m32 '*) macro={PREDEFINED['i386']} ;;\n"
        f"  *) macro={PREDEFINED.get(architecture, '__unknown__')} ;;\n"
        "esac\n"
        'case " $* " in\n'
        "  *' -dM '*) echo '#define __STDC__ 1'; echo \"#define $macro 1\"; exit 0 ;;\n"
        "esac\n"
        'while [ "$#" -gt 0 ]; do\n'
        '  if [ "$1" = -o ]; then : > "$2"; exit 0; fi\n'
        "  shift\n"
        "done\n"
        'echo "stub compiler: no -o" >&2\n'
        "exit 64\n"
    )


def make(host_os, host_arch, arguments, environment, compilers=None, real_compiler=False, dry_run=True, cwd=ROOT):
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
        # The stub c++ agrees with the stub uname, whatever machine runs this test.
        stubs = {} if real_compiler else {"c++": host_arch}
        stubs.update(compilers or {})
        for name, architecture in stubs.items():
            write_executable(Path(scratch) / name, stub_compiler(architecture))
        return subprocess.run(
            ["make", *(["-n", "-B"] if dry_run else []), *arguments],
            cwd=cwd,
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


def require_refused_os(result, name):
    assert result.returncode > 0, (
        f"status {result.returncode}, would run {compile_flags(result.stdout)!r}"
    )
    assert f"Unsupported TARGET_OS '{name}'" in result.stderr, f"TARGET_OS not named in {result.stderr!r}"
    for target in TARGETS:
        assert target in result.stderr, f"no supported target {target} in {result.stderr!r}"
    assert "src/main.cc" not in result.stdout, f"would still compile: {result.stdout!r}"


def clean_on_any_host_os(arguments):
    result = make("FreeBSD", "x86_64", arguments, {})
    assert result.returncode == 0, f"status {result.returncode}, stderr {result.stderr!r}"
    assert "rm -rf _build" in result.stdout, f"no rm in {result.stdout!r}"


def named_target_on_any_host_os(target):
    """A named target sets its own TARGET_OS, so the host's operating system does not matter."""
    require_sub_make(make("FreeBSD", "x86_64", [target], {}), target)
    require_sub_make(make("Linux", "x86_64", ["TARGET_OS=FreeBSD", target], {}), target)
    target_os, target_arch = NAMED[target]
    settings = [f"TARGET_OS={target_os}", f"TARGET_ARCH={target_arch}", "build"]
    require_builds(make("FreeBSD", "x86_64", settings, {}), target)


def all_targets_on_any_host_os(arguments):
    result = make("FreeBSD", "x86_64", arguments, {})
    for target in NAMED:
        require_sub_make(result, target)


def cxx_refusal(compiler, compiler_arch, target_arch):
    built_for = compiler_arch if compiler_arch else "no supported target"
    return f"CXX '{compiler}' builds for {built_for}, not TARGET_ARCH '{target_arch}'"


def require_refused_cxx(result, compiler, compiler_arch, target_arch):
    assert result.returncode > 0, (
        f"status {result.returncode}, would run {compile_flags(result.stdout)!r}"
    )
    message = cxx_refusal(compiler, compiler_arch, target_arch)
    assert message in result.stderr, f"no {message!r} in {result.stderr!r}"
    assert "src/main.cc" not in result.stdout, f"would still compile: {result.stdout!r}"


def require_allowed_cxx(result, command, directory):
    assert result.returncode == 0, f"status {result.returncode}, stderr {result.stderr!r}"
    line = compile_flags(result.stdout)
    assert line.startswith(f"{command} "), f"{command} does not compile main.cc: {line!r}"
    assert directory in line, f"no {directory} in {line!r}"


def darwin_passes_arch_to_any_compiler():
    """-arch reaches CXX on Darwin, so an override there has nothing to prove."""
    result = make("Darwin", "arm64", ["CXX=c++", "TARGET_ARCH=x86_64", "build"], {})
    require_allowed_cxx(result, "c++", "_build/Darwin_x86_64/")
    assert "-arch x86_64" in compile_flags(result.stdout), f"no -arch in {compile_flags(result.stdout)!r}"


def clean_asks_no_compiler():
    result = make("Linux", "x86_64", ["CXX=no-such-compiler", "clean"], {})
    assert result.returncode == 0, f"status {result.returncode}, stderr {result.stderr!r}"
    assert "rm -rf _build" in result.stdout, f"no rm in {result.stdout!r}"


def named_target_leaves_the_question_to_its_sub_make():
    require_sub_make(make("Linux", "x86_64", ["CXX=no-such-compiler", "linux_arm64"], {}), "linux_arm64")


def copy_of_the_tree(scratch):
    tree = Path(scratch) / "tree"
    tree.mkdir()
    shutil.copy(ROOT / "Makefile", tree)
    shutil.copytree(ROOT / "src", tree / "src")
    return tree


def real_run_named_target_with_another_architectures_compiler():
    """The fault as the issue saw it: CXX reaches the sub-make, and the sub-make must refuse it."""
    with tempfile.TemporaryDirectory(prefix="notify-forwarder-target-arch-") as scratch:
        tree = copy_of_the_tree(scratch)
        result = make("Linux", "x86_64", ["CXX=c++", "linux_arm64"], {}, dry_run=False, cwd=tree)
        binary = tree / "_build" / "Linux_arm64" / "notify-forwarder"
        assert not binary.exists(), f"an x86_64 compiler wrote {binary.relative_to(tree)}, status {result.returncode}"
        assert result.returncode > 0, f"status {result.returncode}, stderr {result.stderr!r}"
        message = cxx_refusal("c++", "x86_64", "arm64")
        assert message in result.stderr, f"no {message!r} in {result.stderr!r}"


def real_run_builds(host_os, host_arch, arguments, compilers, binary):
    with tempfile.TemporaryDirectory(prefix="notify-forwarder-target-arch-") as scratch:
        tree = copy_of_the_tree(scratch)
        result = make(host_os, host_arch, arguments, {}, compilers=compilers, dry_run=False, cwd=tree)
        assert result.returncode == 0, f"status {result.returncode}, stderr {result.stderr!r}"
        assert (tree / binary).exists(), f"no {binary} after {result.stdout[-300:]!r}"


def machine_target():
    machine = platform.machine()
    names = {"x86_64": "x86_64", "amd64": "x86_64", "arm64": "arm64", "aarch64": "arm64", "armv7l": "armv7l"}
    assert machine in names, f"this test has no target name for machine {machine!r}"
    return names[machine]


def real_compiler_runs(target_arch):
    """make -n with the machine's own c++, in a copy of the tree so that a stray file shows."""
    with tempfile.TemporaryDirectory(prefix="notify-forwarder-target-arch-") as scratch:
        tree = copy_of_the_tree(scratch)
        arguments = ["CXX=c++", "TARGET_OS=Linux", f"TARGET_ARCH={target_arch}", "build"]
        result = make("Linux", machine_target(), arguments, {}, real_compiler=True, cwd=tree)
        left_behind = sorted(path.name for path in tree.iterdir() if path.name not in ("Makefile", "src"))
        assert not left_behind, f"asking the compiler left {left_behind!r}"
        return result


def real_compiler_builds_for_its_own_architecture():
    target = machine_target()
    require_allowed_cxx(real_compiler_runs(target), "c++", f"_build/Linux_{target}/")


def real_compiler_refused_for_another_architecture():
    target = machine_target()
    other = "x86_64" if target != "x86_64" else "arm64"
    require_refused_cxx(real_compiler_runs(other), "c++", target, other)


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
    for name, host_os, host_arch, arguments, environment, target_os in REFUSED_OS:
        yield f"refused_os_{name}", lambda h=host_os, a=host_arch, g=arguments, e=environment, n=target_os: (
            require_refused_os(make(h, a, g, e), n)
        )
    for target in NAMED:
        yield f"named_{target}_on_freebsd", lambda t=target: named_target_on_any_host_os(t)
    yield "clean_on_freebsd", lambda: clean_on_any_host_os(["clean"])
    yield "clean_for_target_os_freebsd", lambda: clean_on_any_host_os(["TARGET_OS=FreeBSD", "clean"])
    yield "all_on_freebsd", lambda: all_targets_on_any_host_os(["all"])
    yield "no_goal_on_freebsd", lambda: all_targets_on_any_host_os([])
    yield "real_run_named_target_on_freebsd", lambda: real_run_builds(
        "FreeBSD", "x86_64", ["linux_x86_64"], {"zig": "x86_64"}, "_build/Linux_x86_64/notify-forwarder"
    )
    for name, host_arch, arguments, environment, compilers, compiler, compiler_arch, target_arch in REFUSED_CXX:
        yield f"refused_cxx_{name}", lambda a=host_arch, g=arguments, e=environment, s=compilers, c=compiler, \
                ca=compiler_arch, t=target_arch: (
            require_refused_cxx(make("Linux", a, g, e, compilers=s), c, ca, t)
        )
    for name, host_arch, arguments, environment, compilers, command, directory in ALLOWED_CXX:
        yield f"allowed_cxx_{name}", lambda a=host_arch, g=arguments, e=environment, s=compilers, c=command, \
                d=directory: (
            require_allowed_cxx(make("Linux", a, g, e, compilers=s), c, d)
        )
    yield "darwin_passes_arch_to_any_compiler", darwin_passes_arch_to_any_compiler
    yield "clean_asks_no_compiler", clean_asks_no_compiler
    yield "named_target_leaves_the_question_to_its_sub_make", named_target_leaves_the_question_to_its_sub_make
    yield "real_run_named_target_with_another_architectures_compiler", (
        real_run_named_target_with_another_architectures_compiler
    )
    yield "real_run_cxx_for_its_own_named_target", lambda: real_run_builds(
        "Linux", "x86_64", ["CXX=c++", "linux_x86_64"], {}, "_build/Linux_x86_64/notify-forwarder"
    )
    yield "real_run_cxx_build", lambda: real_run_builds(
        "Linux", "x86_64", ["CXX=c++", "build"], {}, "_build/Linux_x86_64/notify-forwarder"
    )
    yield "real_compiler_builds_for_its_own_architecture", real_compiler_builds_for_its_own_architecture
    yield "real_compiler_refused_for_another_architecture", real_compiler_refused_for_another_architecture


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
