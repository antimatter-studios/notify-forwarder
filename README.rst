====================================
 File system notification forwarder
====================================

A simple tool for forwarding file system notifications from one host to another, possibly remapping
the path in the process. Useful when running a reloading build system on one machine and editing
files on another, or over shared folders to a VM.

Supports Linux/inotify and Mac OS X/fsevents on the host side, and Linux, Mac OS X, and BSD on the
client side.


Downloading
===========

Either grab one of the pre-built binaries from the latest release (details
below), or build from source.

Building from source
====================

The project now ships with a multi-target ``Makefile`` that can produce macOS
and Linux binaries for both ``arm64`` and ``x86_64`` architectures.

.. code-block:: sh

   $ git clone git@github.com:mhallin/notify-forwarder.git
   $ cd notify-forwarder
   $ make all

Artifacts are emitted under ``_build/<TargetOS>_<TargetArch>/notify-forwarder``.
For example, ``_build/Darwin_arm64/notify-forwarder``. You can also build a
single target with:

.. code-block:: sh

   $ make darwin_arm64      # macOS arm64
   $ make darwin_x86_64     # macOS x86_64
   $ make linux_arm64       # Linux arm64 (requires Zig for cross-compilation)
   $ make linux_armv7l      # Linux armv7l/ARM32 (requires Zig for cross-compilation)
   $ make linux_x86_64      # Linux x86_64 (requires Zig for cross-compilation)

On Linux hosts you will need ``zig`` available when building the cross
targets. The GitHub Actions workflow uses ``mlugg/setup-zig`` to install it
during automated builds.


Running
=======

To map the current directory to "/home/user/project" on a remote machine, run the following on the
host side:

.. code-block:: sh

   $ notify-forwarder watch -c <remote-ip> . /home/user/project
   # This maps the current directory "." to "/home/user/project". Add more pairs of paths
   # to watch more folders with different mappings.

And on the client side:

.. code-block:: sh

   $ notify-forwarder receive /home/user/project

The "host side" here being the file system that will send events, i.e. where you run the text
editor. The "client side" is where the auto reloading build system runs.

When something fails
--------------------

Everything is reported on stderr. A ``sendto`` failure that concerns the network or the path is
skipped, and a full socket buffer is retried first. The first skipped path is named, and a later
message says how many were skipped once sending works again. A directory that vanishes during a
scan, or that the user may not read, is named and skipped. These leave the process running.

A watch path that does not resolve, an address that is not IPv4, a port that cannot be bound and an
exhausted inotify watch limit (``fs.inotify.max_user_watches``) each end the process with a message
and a nonzero status.

What the sender forwards
------------------------

An event for a path is forwarded at once, and further events for it in the next second wait until
that second ends, so a busy file is forwarded about once a second and its last change always goes
out. Over a shared folder the receiver's event comes back to the sender, and forwarding it would feed
the two sides for ever. The sender tells it apart by reading the path: inode, size, mtime, mode and
owner. The receiver changes only ctime, so an event inside that second which changed none of them is
dropped.

A filesystem that stores whole seconds or milliseconds cannot show a second write of the same size
inside one timestamp. While a timestamp like that is under two seconds old, a repeat event sends the
path once more when two seconds have passed, and an echo then costs one extra event in the guest. On
a filesystem with a finer timestamp, a write of the same size in the same timestamp tick, after the
sender has read the file, is not seen.

Receiver roots
--------------

Pass the guest directories the receiver may notify as positional arguments; multiple roots and
``-p port`` can be combined. Paths and roots are resolved before checking directory boundaries. A
symlink resolving outside all roots, a directory, a FIFO or a device is refused. Regular files are
opened with ``O_NONBLOCK``. On Linux, an ``O_PATH`` descriptor pins the checked inode before it is
opened for writable close, so a path replacement cannot turn that open into a device or FIFO open.

``notify-forwarder receive [-p port]`` with no roots still runs. It prints a warning and accepts
regular files anywhere; configure roots to restrict it. This mode is not a security boundary. The
UDP protocol does not authenticate senders, so only expose the receiver to trusted networks and
configure the narrowest guest roots needed.

The macOS fallback checks the file before and after opening it, but does not have Linux's ``O_PATH``
pinning guarantee. A privileged macOS receiver must not be exposed to senders that can replace file
types while it is opening them.

What a burst loses
------------------

Each path is one UDP datagram, so a burst can overflow a socket buffer. The receiver asks for a 16
MiB buffer, which the system may cap (``net.core.rmem_max`` on Linux). On Linux it reports what the
kernel dropped, at most once a second, as ``N paths lost to a full socket buffer so far``. Other
platforms have no such count. The guest's watcher misses the event for a lost path, and the next
change to that file raises a new one.

The sender reports its own losses. When FSEvents sets ``MustScanSubDirs``, ``UserDropped`` or
``KernelDropped``, or the inotify queue overflows, nothing says what changed, so the sender prints a
message and forwards every path under that subtree, or under each watched root for an overflow. The
same walk covers a new directory on Linux, whose first files can appear before its watch exists.

A datagram as long as ``MAXPATHLEN`` or longer is refused and counted, and never cut down into a
different path.

What the receiver does to a file
--------------------------------

The receiver raises events in the guest and never writes file data, size, mode or mtime. For each
path it opens the file for writing, creates and removes a private extended attribute
(``user.notify-forwarder``), and closes it. That raises ``IN_OPEN``, ``IN_ATTRIB`` and
``IN_CLOSE_WRITE``. It changes the file's ctime, and over a shared folder the host sees that ctime
change. A receiver killed with ``SIGKILL`` between the two attribute calls leaves the attribute on
the file until the next event for that file removes it.

**It cannot raise IN_MODIFY.** No call measured raises ``IN_MODIFY`` without changing mtime, so a
watcher that only listens for ``IN_MODIFY`` sees nothing. Node's ``fs.watch`` does report
``IN_ATTRIB``, as ``change``.

**A filesystem without user extended attributes gets no IN_ATTRIB.** NFSv3 is one. The receiver
prints one warning and raises only ``IN_OPEN`` and ``IN_CLOSE_WRITE``, which ``inotifywait`` sees and
``fs.watch`` does not. The receiver also raises nothing on a file it cannot open for writing.

What each call does, measured on Linux 6.1 against ext4, virtiofs, NFSv3 and NFS 4.2 with
``tests/kernel_events.py``, with results for the file as the host or NFS server sees it:

==================================  ==========================  ==========  =========================
Call                                Event                       fs.watch    Changes
==================================  ==========================  ==========  =========================
``open(O_WRONLY)``, ``close``       ``OPEN``, ``CLOSE_WRITE``   nothing     nothing
``fsetxattr``, ``fremovexattr``     ``ATTRIB``, not on NFSv3    ``change``  ctime
``fchmod`` to the current mode      ``ATTRIB``                  ``change``  ctime, submits a mode
``fchown`` to the current owner     ``ATTRIB``                  ``change``  ctime, submits an owner
``futimens`` to the current times   ``ATTRIB``                  ``change``  ctime, submits an mtime
``futimens(OMIT, OMIT)``            nothing                     nothing     nothing
``fchown(-1, -1)``                  nothing                     nothing     nothing
``write`` of zero bytes             ``CLOSE_WRITE`` only        nothing     nothing
``fallocate(KEEP_SIZE)``            ``MODIFY`` on ext4,         ``change``  mtime on ext4
                                    refused on virtiofs, NFS
``ftruncate`` to the current size   ``MODIFY``                  ``change``  mtime, submits a size
==================================  ==========================  ==========  =========================

A call that submits a value the receiver read earlier puts that value back if the writer changed it
in the meantime. Over a shared folder the earlier read can come from the guest's attribute cache:
``fchmod`` to the mode from ``fstat`` undid a host ``chmod`` in 10 of 10 tries up to 0.6 seconds later
on virtiofs, and in 0 of 10 at 1.5 seconds.

``tests/shared_folder.py`` runs the receiver on a real shared folder: 168 files, 1 MiB each, written
by the host while the receiver is notified of all of them. Run with this injection, every file kept
its bytes, size, mode and mtime. Injectors that submitted the current mtime or mode did not: 6 and 17
of 168 files lost the host's value.



.. _notify-forwarder_osx_x64: https://github.com/mhallin/notify-forwarder/releases/download/release%2Fv0.1.0/notify-forwarder_osx_x64
.. _notify-forwarder_linux_x64: https://github.com/mhallin/notify-forwarder/releases/download/release%2Fv0.1.0/notify-forwarder_linux_x64
.. _notify-forwarder_freebsd_x64: https://github.com/mhallin/notify-forwarder/releases/download/release%2Fv0.1.0/notify-forwarder_freebsd_x64


Supported architectures
=======================

+-----------------+------+-------+
| Platform        | Host | Guest |
+=================+======+=======+
| macOS arm64     |  ✓   |  —    |
+-----------------+------+-------+
| macOS x86_64    |  ✓   |  —    |
+-----------------+------+-------+
| Linux arm64     |  ✓   |  ✓    |
+-----------------+------+-------+
| Linux armv7l    |  —   |  ✓    |
+-----------------+------+-------+
| Linux x86_64    |  ✓   |  ✓    |
+-----------------+------+-------+


Changelog
=========

This changelog highlights recent changes. Earlier versions did not track
changes here.

v1.1.0 — 2026-04-03
--------------------

- Added Linux armv7l (ARM32) build target for Raspberry Pi guest support.
- Cross-compiled via Zig with ``arm-linux-gnueabihf`` target.

v1.0.0 — 2025-09-26
--------------------

- Added a matrix-based GitHub Actions workflow that builds and packages
  binaries for ``darwin_arm64``, ``darwin_x86_64``, ``linux_arm64`` and
  ``linux_x86_64`` and attaches them to tagged releases.
- Refreshed the ``Makefile`` with per-target build rules and Zig-powered
  cross-compilation for Linux architectures.
- Updated the guest-side injector to perform lossless byte rewrites before
  timestamp updates so tools that rely on ``IN_CLOSE_WRITE`` (for example the
  Go Air hot reload tool) react to forwarded events.
- Documented the new build process and artifact layout in this README.
