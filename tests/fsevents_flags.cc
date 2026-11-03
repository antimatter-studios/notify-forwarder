// What FSEvents' dropped-events flags lead to. The stream's callback is called directly, with the
// flags a test chooses, because nothing can make the kernel drop events on demand.
#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>

#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <iterator>
#include <set>
#include <stdexcept>
#include <string>
#include <vector>

#include "watch_fsevents.cc"

static int failures = 0;

static void check(bool condition, const std::string& name)
{
    printf("%s %s\n", condition ? "PASS" : "FAIL", name.c_str());
    if (!condition) {
        ++failures;
    }
}

typedef std::set<std::string> Paths;

struct Delivery {
    Paths paths;
    std::string message;
};

static Delivery deliver(const std::string& path, FSEventStreamEventFlags flags)
{
    Delivery delivery;
    FSEventsWatchPluginImpl impl;
    impl.callback = [&delivery](std::vector<std::string> paths) {
        delivery.paths.insert(paths.begin(), paths.end());
    };

    const char* event_paths[] = { path.c_str() };
    const FSEventStreamEventFlags event_flags[] = { flags };
    const FSEventStreamEventId event_ids[] = { 1 };

    char name[] = "/tmp/notify-forwarder-stderr-XXXXXX";
    int file = mkstemp(name);
    fflush(stderr);
    const int saved = dup(2);
    dup2(file, 2);
    fsevents_callback(nullptr, &impl, 1, event_paths, event_flags, event_ids);
    fflush(stderr);
    dup2(saved, 2);
    close(saved);
    close(file);
    std::ifstream stream(name);
    delivery.message.assign(
        (std::istreambuf_iterator<char>(stream)), std::istreambuf_iterator<char>());
    unlink(name);
    return delivery;
}

int main()
{
    char pattern[] = "/tmp/notify-forwarder-flags-XXXXXX";
    const std::string root = realpath(mkdtemp(pattern), nullptr);
    const std::string sub = root + "/sub";
    mkdir(sub.c_str(), 0755);
    mkdir((sub + "/nested").c_str(), 0755);
    close(open((sub + "/file").c_str(), O_CREAT | O_WRONLY, 0644));
    close(open((sub + "/nested/file").c_str(), O_CREAT | O_WRONLY, 0644));
    close(open((root + "/outside").c_str(), O_CREAT | O_WRONLY, 0644));
    symlink(root.c_str(), (sub + "/link-out").c_str());

    const Paths whole_subtree
        = { sub, sub + "/file", sub + "/nested", sub + "/nested/file", sub + "/link-out" };

    Delivery ordinary = deliver(sub + "/file", kFSEventStreamEventFlagItemModified);
    check(ordinary.paths == Paths { sub + "/file" }, "an ordinary event names its path alone");
    check(ordinary.message.empty(), "an ordinary event reports nothing");

    const std::pair<const char*, FSEventStreamEventFlags> dropped[] = {
        { "MustScanSubDirs", kFSEventStreamEventFlagMustScanSubDirs },
        { "UserDropped",
            kFSEventStreamEventFlagMustScanSubDirs | kFSEventStreamEventFlagUserDropped },
        { "KernelDropped",
            kFSEventStreamEventFlagMustScanSubDirs | kFSEventStreamEventFlagKernelDropped },
    };
    for (const auto& flag : dropped) {
        Delivery delivery = deliver(sub, flag.second);
        check(delivery.paths == whole_subtree,
            std::string(flag.first) + " resyncs that subtree and no more");
        check(delivery.message.find(sub) != std::string::npos,
            std::string(flag.first) + " is reported");
    }

    Delivery trailing_slash = deliver(sub + "/", kFSEventStreamEventFlagMustScanSubDirs);
    check(trailing_slash.paths.count(sub + "/nested/file") == 1,
        "a trailing slash on the event path is accepted");

    Delivery gone = deliver(root + "/gone", kFSEventStreamEventFlagMustScanSubDirs);
    check(gone.paths == Paths { root + "/gone" }, "a subtree that has vanished resyncs as itself");

    return failures ? 1 : 0;
}
