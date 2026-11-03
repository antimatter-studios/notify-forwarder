// What the inotify scan reports when a directory cannot be watched, on Linux. The scan's calls are
// interposed, so that each failure happens on the directory a test chooses.
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <sys/inotify.h>
#include <sys/stat.h>
#include <unistd.h>

#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <iterator>
#include <set>
#include <stdexcept>
#include <string>

static std::string remove_before_watch;
static std::string remove_before_open;
static std::string refuse_watch;
static int refuse_error = 0;

static int interposed_add_watch(int fd, const char* path, uint32_t mask)
{
    if (remove_before_watch == path) {
        rmdir(path);
    }
    if (refuse_watch == path) {
        errno = refuse_error;
        return -1;
    }
    return inotify_add_watch(fd, path, mask);
}

static DIR* interposed_opendir(const char* path)
{
    if (remove_before_open == path) {
        rmdir(path);
    }
    return opendir(path);
}

#define inotify_add_watch interposed_add_watch
#define opendir interposed_opendir
#include "watch_inotify.cc"
#undef inotify_add_watch
#undef opendir

static int failures = 0;

static void check(bool condition, const std::string& name)
{
    printf("%s %s\n", condition ? "PASS" : "FAIL", name.c_str());
    if (!condition) {
        ++failures;
    }
}

static std::string make_tree(const std::string& extra)
{
    char pattern[] = "/tmp/notify-forwarder-failures-XXXXXX";
    if (!mkdtemp(pattern)) {
        perror("mkdtemp");
        exit(2);
    }
    const std::string root = pattern;
    if (mkdir((root + "/sub").c_str(), 0755) != 0
        || mkdir((root + "/" + extra).c_str(), 0755) != 0) {
        perror("mkdir");
        exit(2);
    }
    return root;
}

struct Scan {
    std::string message;
    bool threw;
    bool watched_sub;
};

// Runs a scan with stderr in a file.
static Scan scan(const std::string& root)
{
    Scan result { "", false, false };
    char name[] = "/tmp/notify-forwarder-stderr-XXXXXX";
    int file = mkstemp(name);
    fflush(stderr);
    const int saved = dup(2);
    dup2(file, 2);

    int fd = ::inotify_init();
    DirectoryMap map;
    try {
        add_directory_watch(fd, root, map);
    }
    catch (const std::exception& error) {
        result.threw = true;
        result.message = error.what();
    }
    close(fd);

    fflush(stderr);
    dup2(saved, 2);
    close(saved);
    close(file);
    std::ifstream stream(name);
    result.message.append(
        (std::istreambuf_iterator<char>(stream)), std::istreambuf_iterator<char>());
    unlink(name);

    for (const auto& entry : map) {
        result.watched_sub = result.watched_sub || entry.second == root + "/sub";
    }
    return result;
}

static void expect_skipped(
    const std::string& name, const std::string& root, const std::string& skipped)
{
    const Scan result = scan(root);
    check(!result.threw, name + ": not fatal");
    check(result.message.find(skipped) != std::string::npos, name + ": reported by name");
    check(result.watched_sub, name + ": the rest of the tree is watched");
}

int main()
{
    std::string root = make_tree("gone");
    remove_before_watch = root + "/gone";
    expect_skipped("a directory vanished before its watch", root, root + "/gone");
    remove_before_watch.clear();

    root = make_tree("gone");
    remove_before_open = root + "/gone";
    expect_skipped("a directory vanished before it is opened", root, root + "/gone");
    remove_before_open.clear();

    root = make_tree("secret");
    refuse_watch = root + "/secret";
    refuse_error = EACCES;
    expect_skipped("a directory this user may not read", root, root + "/secret");

    root = make_tree("deep");
    refuse_watch = root + "/deep";
    refuse_error = ENOSPC;
    const Scan out_of_watches = scan(root);
    check(out_of_watches.threw, "running out of watches is fatal");
    check(out_of_watches.message.find("max_user_watches") != std::string::npos,
        "running out of watches says what to raise");
    check(out_of_watches.message.find(root + "/deep") != std::string::npos,
        "running out of watches names the directory");

    return failures ? 1 : 0;
}
