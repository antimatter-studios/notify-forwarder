#include <dirent.h>
#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <string.h>
#include <sys/inotify.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>
#include <algorithm>
#include <stdexcept>

#include <memory>
#include <unordered_map>

#include "watch_inotify.h"
#include "watch_tree.h"

#define READ_BUF_LEN (1024 * (sizeof(inotify_event) + 16))

typedef std::unordered_map<int, std::string> DirectoryMap;

namespace {

typedef std::unique_ptr<DIR, int (*)(DIR*)> DirHandle;

// d_type is one value, not a set of bits. Filesystems that do not fill it in leave DT_UNKNOWN, and
// a symlink is never followed, whichever way its type is learned.
bool is_directory(const dirent* entry, const std::string& path)
{
    if (entry->d_type != DT_UNKNOWN) {
        return entry->d_type == DT_DIR;
    }

    struct stat status;
    return lstat(path.c_str(), &status) == 0 && S_ISDIR(status.st_mode);
}

// A directory that went, or that this user may not read. Neither stops the rest of the tree.
bool can_be_skipped(int error) { return error == ENOENT || error == ENOTDIR || error == EACCES; }

bool skip_directory(const std::string& path, int error)
{
    fprintf(stderr, "notify-forwarder: skipped %s: %s\n", path.c_str(), strerror(error));
    return false;
}

// Returns false if root could not be watched, which a changing tree makes an ordinary outcome.
bool add_directory_watch(int fd, const std::string& root, DirectoryMap& directory_map)
{
    int result = inotify_add_watch(fd,
        root.c_str(),
        IN_CREATE | IN_MODIFY | IN_DELETE | IN_ATTRIB | IN_MOVED_TO | IN_MOVED_FROM | IN_ONLYDIR
            | IN_DONT_FOLLOW);

    if (result == -1) {
        const int error = errno;
        if (can_be_skipped(error)) {
            return skip_directory(root, error);
        }
        if (error == ENOSPC) {
            throw std::runtime_error("Could not watch " + root
                + ": the inotify watch limit is reached, raise fs.inotify.max_user_watches");
        }
        throw std::runtime_error("Could not watch " + root + ": " + strerror(error));
    }

    // Stays in the map if the directory goes now: events already queued for this watch name it.
    directory_map[result] = root;

    DirHandle dir(opendir(root.c_str()), closedir);
    if (!dir) {
        const int error = errno;
        if (can_be_skipped(error)) {
            return skip_directory(root, error);
        }
        throw std::runtime_error("Could not open " + root + ": " + strerror(error));
    }

    dirent* entry;
    while ((entry = readdir(dir.get()))) {
        if (strcmp(entry->d_name, ".") == 0 || strcmp(entry->d_name, "..") == 0) {
            continue;
        }

        std::string path = root + "/" + entry->d_name;

        if (is_directory(entry, path)) {
            add_directory_watch(fd, path, directory_map);
        }
    }

    return true;
}
}

class InotifyWatchPluginImpl {
public:
    int fd;

public:
    std::vector<std::string> paths;

public:
    WatchCallback callback;
};

InotifyWatchPlugin::InotifyWatchPlugin(
    const std::vector<std::string>& paths, const WatchCallback& callback)
    : m_impl(new InotifyWatchPluginImpl())
{
    m_impl->fd = inotify_init();
    if (m_impl->fd == -1) {
        throw std::runtime_error("Could not initialize inotify");
    }

    m_impl->paths = paths;
    m_impl->callback = callback;
}

InotifyWatchPlugin::~InotifyWatchPlugin()
{
    if (m_impl->fd != -1) {
        close(m_impl->fd);
    }
}

void InotifyWatchPlugin::start(int stop_fd)
{
    DirectoryMap directory_map;

    for (auto& path : m_impl->paths) {
        if (!add_directory_watch(m_impl->fd, path, directory_map)) {
            throw std::runtime_error("Could not watch " + path);
        }
    }

    char buf[READ_BUF_LEN];

    timespec timeout;
    timeout.tv_sec = 0;
    timeout.tv_nsec = 100000000;

    sigset_t emptyset;
    if (sigemptyset(&emptyset) == -1) {
        throw std::logic_error("Could not clear out emptyset");
    }

    for (;;) {
        fd_set fds;
        FD_ZERO(&fds);
        FD_SET(m_impl->fd, &fds);
        FD_SET(stop_fd, &fds);

        const int highest_fd = std::max(m_impl->fd, stop_fd);
        int select_result = pselect(highest_fd + 1, &fds, nullptr, nullptr, &timeout, &emptyset);

        if (select_result == -1 && errno != EINTR) {
            throw std::runtime_error("Could not select on inotify file");
        }
        else if (select_result > 0 && FD_ISSET(stop_fd, &fds)) {
            return;
        }
        else if (select_result > 0) {
            int length = read(m_impl->fd, buf, sizeof(buf));

            std::vector<std::string> paths_to_notify;
            bool overflowed = false;

            if (length == -1) {
                if (errno == EINTR) {
                    continue;
                }
                throw std::runtime_error(
                    std::string("Could not read from inotify: ") + strerror(errno));
            }

            for (int i = 0; i < length;) {
                inotify_event* event = reinterpret_cast<inotify_event*>(&buf[i]);

                if (event->mask & IN_Q_OVERFLOW) {
                    overflowed = true;
                }
                else if (event->len) {
                    std::string root = directory_map.at(event->wd);
                    std::string path = root + "/" + event->name;

                    paths_to_notify.emplace_back(path);

                    if (event->mask & IN_ISDIR) {
                        if (event->mask & IN_CREATE || event->mask & IN_MOVED_TO) {
                            // Anything made in it before its watch existed raised no event.
                            if (add_directory_watch(m_impl->fd, path, directory_map)) {
                                append_tree(path, paths_to_notify);
                            }
                        }
                    }
                }

                i += sizeof(inotify_event) + event->len;
            }

            if (overflowed) {
                // The queue lost events, so nothing says which directories are new or what changed.
                for (auto& path : m_impl->paths) {
                    fprintf(stderr,
                        "notify-forwarder: the inotify queue overflowed and events were lost, "
                        "forwarding everything under %s\n",
                        path.c_str());
                    if (add_directory_watch(m_impl->fd, path, directory_map)) {
                        paths_to_notify.emplace_back(path);
                        append_tree(path, paths_to_notify);
                    }
                }
            }

            m_impl->callback(paths_to_notify);
        }
        else {
            m_impl->callback({});
        }
    }
}
