// What the inotify watcher does when it cannot have seen everything: the kernel's queue
// overflowing, and files appearing in a new directory before its watch exists. Both moments are
// interposed, so they happen every time.
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <sys/inotify.h>
#include <sys/select.h>
#include <sys/stat.h>
#include <unistd.h>

#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <functional>
#include <fstream>
#include <iterator>
#include <mutex>
#include <set>
#include <string>
#include <thread>
#include <vector>

static std::atomic<bool> overflow_next { false };
static std::function<void()> before_overflow;
static std::string create_before_watch_of;
static std::string created_before_watch;

static ssize_t interposed_read(int fd, void* buffer, size_t size)
{
    if (overflow_next.exchange(false)) {
        if (before_overflow) {
            before_overflow();
        }
        inotify_event event;
        memset(&event, 0, sizeof(event));
        event.wd = -1;
        event.mask = IN_Q_OVERFLOW;
        memcpy(buffer, &event, sizeof(event));
        return sizeof(event);
    }
    return read(fd, buffer, size);
}

static int interposed_add_watch(int fd, const char* path, uint32_t mask)
{
    if (create_before_watch_of == path) {
        close(open(created_before_watch.c_str(), O_CREAT | O_WRONLY, 0644));
    }
    return inotify_add_watch(fd, path, mask);
}

#define read interposed_read
#define inotify_add_watch interposed_add_watch
#include "watch_inotify.cc"
#undef read
#undef inotify_add_watch

static int failures = 0;

static void check(bool condition, const std::string& name)
{
    printf("%s %s\n", condition ? "PASS" : "FAIL", name.c_str());
    if (!condition) {
        ++failures;
    }
}

static std::string make_scratch()
{
    char pattern[] = "/tmp/notify-forwarder-resync-XXXXXX";
    if (!mkdtemp(pattern)) {
        perror("mkdtemp");
        exit(2);
    }
    return pattern;
}

static void touch(const std::string& path) { close(open(path.c_str(), O_CREAT | O_WRONLY, 0644)); }

// A running watcher on a thread, and what it has delivered so far.
class Watching {
public:
    explicit Watching(const std::string& root)
        : m_plugin({ root }, [this](std::vector<std::string> paths) { deliver(paths); })
    {
        if (pipe(m_stop) != 0) {
            perror("pipe");
            exit(2);
        }
        m_thread = std::thread([this] { m_plugin.start(m_stop[0]); });
        // The scan has finished once a change at the root comes through. Only a new name is a
        // change: reopening a file raises nothing this watcher listens for.
        for (int attempt = 0;; ++attempt) {
            const std::string probe = root + "/probe-" + std::to_string(attempt);
            touch(probe);
            if (wait_for(probe, 100)) {
                break;
            }
            if (attempt > 300) {
                fprintf(stderr, "the watcher forwarded nothing in 30 seconds\n");
                exit(2);
            }
        }
    }

    ~Watching()
    {
        if (write(m_stop[1], "", 1) != 1) {
            perror("write");
        }
        m_thread.join();
        close(m_stop[0]);
        close(m_stop[1]);
    }

    bool wait_for(const std::string& path, int milliseconds = 5000)
    {
        const auto deadline
            = std::chrono::steady_clock::now() + std::chrono::milliseconds(milliseconds);
        while (std::chrono::steady_clock::now() < deadline) {
            {
                std::lock_guard<std::mutex> lock(m_mutex);
                if (m_paths.count(path)) {
                    return true;
                }
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(10));
        }
        return false;
    }

private:
    void deliver(const std::vector<std::string>& paths)
    {
        std::lock_guard<std::mutex> lock(m_mutex);
        m_paths.insert(paths.begin(), paths.end());
    }

private:
    InotifyWatchPlugin m_plugin;
    std::mutex m_mutex;
    std::set<std::string> m_paths;
    int m_stop[2];
    std::thread m_thread;
};

static void test_new_directory_contents()
{
    const std::string root = make_scratch();
    Watching watching(root);
    const std::string directory = root + "/new";
    create_before_watch_of = directory;
    created_before_watch = directory + "/early";
    mkdir(directory.c_str(), 0755);
    check(watching.wait_for(directory), "a new directory is forwarded");
    check(watching.wait_for(directory + "/early"),
        "a file made before the new directory's watch is forwarded");
    touch(directory + "/late");
    check(watching.wait_for(directory + "/late"),
        "a file made after the new directory's watch is forwarded");
    create_before_watch_of.clear();
}

static void test_queue_overflow()
{
    const std::string root = make_scratch();
    mkdir((root + "/sub").c_str(), 0755);
    touch(root + "/sub/existing");

    char name[] = "/tmp/notify-forwarder-stderr-XXXXXX";
    int file = mkstemp(name);
    fflush(stderr);
    const int saved = dup(2);
    dup2(file, 2);
    {
        Watching watching(root);
        before_overflow = [&root] {
            mkdir((root + "/lost").c_str(), 0755);
            touch(root + "/lost/file");
        };
        overflow_next = true;
        // Something must wake the watcher for the overflow to be read.
        touch(root + "/wake");
        check(watching.wait_for(root + "/sub/existing"), "an overflow resyncs what was there");
        check(watching.wait_for(root + "/lost/file"), "an overflow resyncs what was missed");
        touch(root + "/lost/after");
        check(watching.wait_for(root + "/lost/after"),
            "a directory missed in an overflow is watched afterwards");
        before_overflow = nullptr;
    }
    fflush(stderr);
    dup2(saved, 2);
    close(saved);
    close(file);
    std::ifstream stream(name);
    const std::string message(
        (std::istreambuf_iterator<char>(stream)), std::istreambuf_iterator<char>());
    unlink(name);
    check(message.find("lost") != std::string::npos, "an overflow is reported");
}

int main()
{
    test_new_directory_contents();
    test_queue_overflow();
    return failures ? 1 : 0;
}
