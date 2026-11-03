#include <sys/xattr.h>

#include <cerrno>
#include <csignal>
#include <cstdio>
#include <cstring>
#include <fcntl.h>
#include <unistd.h>

#include <vector>

#include "inject_utimes.h"

namespace {

const char attribute_name[] = "user.notify-forwarder";

int set_attribute(int fd)
{
#ifdef __APPLE__
    return fsetxattr(fd, attribute_name, "", 0, 0, 0);
#else
    return fsetxattr(fd, attribute_name, "", 0, 0);
#endif
}

int remove_attribute(int fd)
{
#ifdef __APPLE__
    return fremovexattr(fd, attribute_name, 0);
#else
    return fremovexattr(fd, attribute_name);
#endif
}

}

UtimesInjectPlugin::~UtimesInjectPlugin() { }

// A writable open and close raises IN_OPEN and IN_CLOSE_WRITE, and creating and removing a private
// extended attribute raises IN_ATTRIB. Neither submits a value the receiver read earlier, so
// nothing can overwrite a change the file's writer makes at the same moment. Data, size, mtime and
// mode are never written. fchmod and futimens with the current values would raise IN_ATTRIB too,
// but both put back an older value when the writer got in first, and a shared folder's attribute
// cache can make that value a second old. No call raises IN_MODIFY without changing mtime.
void UtimesInjectPlugin::inject(const std::vector<std::string>& paths)
{
    for (const auto& path : paths) {
        int fd = open(path.c_str(), O_WRONLY | O_CLOEXEC);
        if (fd >= 0) {
            // A signal delivered between the two calls would leave the attribute on the file.
            sigset_t termination;
            sigset_t previous;
            sigemptyset(&termination);
            sigaddset(&termination, SIGHUP);
            sigaddset(&termination, SIGINT);
            sigaddset(&termination, SIGQUIT);
            sigaddset(&termination, SIGTERM);
            sigprocmask(SIG_BLOCK, &termination, &previous);
            const bool attribute_set = set_attribute(fd) == 0;
            const int attribute_error = errno;
            if (attribute_set) {
                (void)remove_attribute(fd);
            }
            sigprocmask(SIG_SETMASK, &previous, nullptr);
            if (!attribute_set && !m_warned_attribute) {
                m_warned_attribute = true;
                fprintf(stderr,
                    "notify-forwarder: no IN_ATTRIB on %s: extended attribute refused: %s; "
                    "watchers that need IN_ATTRIB or IN_MODIFY, such as Node fs.watch, see "
                    "nothing here\n",
                    path.c_str(),
                    strerror(attribute_error));
            }
            close(fd);
        }
    }
}
