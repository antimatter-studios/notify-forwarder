#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>

#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <string>

#include "receive_paths.h"

static int failures = 0;

static void check(bool condition, const char* name)
{
    printf("%s %s\n", condition ? "PASS" : "FAIL", name);
    if (!condition) {
        ++failures;
    }
}

int main()
{
    alarm(10);
    char temporary[] = "/tmp/notify-forwarder-paths-XXXXXX";
    if (!mkdtemp(temporary)) {
        perror("mkdtemp");
        return 1;
    }
    std::string root(temporary);
    std::string sibling = root + "-sibling";
    check(mkdir(sibling.c_str(), 0700) == 0, "outside sibling fixture is created");
    std::string inside = root + "/regular";
    std::string outside = sibling + "/outside";
    std::ofstream(inside) << "inside";
    std::ofstream(outside) << "outside";
    check(symlink(outside.c_str(), (root + "/escape").c_str()) == 0,
        "escaping symlink fixture is created");
    check(symlink(inside.c_str(), (root + "/internal").c_str()) == 0,
        "internal symlink fixture is created");
    check(mkfifo((root + "/fifo").c_str(), 0600) == 0, "FIFO fixture is created");

    {
        ReceivePaths allowed({ root + "/" });
        int descriptor = allowed.open_regular(inside);
        check(descriptor >= 0, "regular file under an allowed root is opened");
        if (descriptor >= 0) {
            int flags = fcntl(descriptor, F_GETFL);
            check(flags >= 0 && (flags & O_NONBLOCK), "regular file is opened O_NONBLOCK");
            check((flags & O_ACCMODE) == O_WRONLY, "regular file uses a writable close descriptor");
            struct stat status;
            check(fstat(descriptor, &status) == 0 && S_ISREG(status.st_mode),
                "returned descriptor names a regular file");
            close(descriptor);
        }
        check(allowed.open_regular(outside) < 0, "path outside the roots is refused");
        check(allowed.open_regular(root + "/escape") < 0, "symlink leading outside is refused");
        check(allowed.open_regular(root + "/fifo") < 0, "FIFO is refused without blocking");
        check(allowed.open_regular(root) < 0, "directory is refused");
        check(allowed.open_regular(root + "/missing") < 0, "missing path is refused");
        check(allowed.open_regular(inside + std::string("\0ignored", 8)) < 0,
            "embedded NUL cannot select a different file");
        descriptor = allowed.open_regular(root + "/internal");
        check(descriptor >= 0, "symlink resolving within a root is accepted");
        if (descriptor >= 0) {
            close(descriptor);
        }
    }
    {
        ReceivePaths devices({ "/dev" });
        check(devices.open_regular("/dev/null") < 0, "device inside an allowed root is refused");
    }
    {
        ReceivePaths legacy({ });
        int descriptor = legacy.open_regular(inside);
        check(descriptor >= 0, "legacy no-root mode still accepts a regular file");
        if (descriptor >= 0) {
            close(descriptor);
        }
        check(legacy.open_regular("/dev/null") < 0, "legacy mode also refuses devices");
        check(legacy.open_regular(root + "/fifo") < 0, "legacy mode also refuses FIFOs");
    }
    bool invalid_root_refused = false;
    try {
        ReceivePaths invalid({ inside });
    } catch (const std::exception&) {
        invalid_root_refused = true;
    }
    check(invalid_root_refused, "non-directory root is refused at startup");

    for (const auto& name : { "regular", "escape", "internal", "fifo" }) {
        check(unlink((root + "/" + name).c_str()) == 0, "inside fixture is removed");
    }
    check(unlink(outside.c_str()) == 0, "outside fixture is removed");
    check(rmdir(sibling.c_str()) == 0, "outside fixture directory is removed");
    check(rmdir(root.c_str()) == 0, "allowed fixture directory is removed");
    return failures ? 1 : 0;
}
