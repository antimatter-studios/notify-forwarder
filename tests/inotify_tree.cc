// The watcher's tree scan, on Linux, with a tree that changes under it. The scan's own calls are
// interposed, so that each moment a directory can vanish happens every time.
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <sys/inotify.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <set>
#include <stdexcept>
#include <string>

static std::string remove_before_watch;
static std::string replace_before_watch;
static std::string remove_before_open;
static std::string refuse_watch;
static std::string report_as_block_device;
static bool report_unknown_types = false;

static int interposed_add_watch(int fd, const char* path, uint32_t mask)
{
    if (remove_before_watch == path) {
        rmdir(path);
    }
    if (replace_before_watch == path) {
        rmdir(path);
        close(open(path, O_CREAT | O_WRONLY, 0644));
    }
    if (refuse_watch == path) {
        errno = ENOSPC;
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

static dirent* interposed_readdir(DIR* dir)
{
    dirent* entry = readdir(dir);
    if (entry && report_unknown_types) {
        entry->d_type = DT_UNKNOWN;
    }
    if (entry && report_as_block_device == entry->d_name) {
        entry->d_type = DT_BLK;
    }
    return entry;
}

#define inotify_add_watch interposed_add_watch
#define opendir interposed_opendir
#define readdir interposed_readdir
#include "watch_inotify.cc"
#undef inotify_add_watch
#undef opendir
#undef readdir

static int failures = 0;

static void check(bool condition, const std::string& name)
{
    printf("%s %s\n", condition ? "PASS" : "FAIL", name.c_str());
    if (!condition) {
        ++failures;
    }
}

static int open_descriptors()
{
    DIR* dir = opendir("/proc/self/fd");
    int count = 0;
    while (readdir(dir)) {
        ++count;
    }
    closedir(dir);
    return count;
}

static void reset()
{
    remove_before_watch.clear();
    replace_before_watch.clear();
    remove_before_open.clear();
    refuse_watch.clear();
    report_as_block_device.clear();
    report_unknown_types = false;
}

static void make_directory(const std::string& path)
{
    if (mkdir(path.c_str(), 0755) != 0) {
        perror(path.c_str());
        exit(2);
    }
}

static void make_file(const std::string& path) { close(open(path.c_str(), O_CREAT | O_WRONLY, 0644)); }

// Beneath the root: a directory with a file in it, a file, a socket, a FIFO, a dangling symlink and
// a symlink to the directory.
static std::string make_tree()
{
    char pattern[] = "/tmp/notify-forwarder-tree-XXXXXX";
    if (!mkdtemp(pattern)) {
        perror("mkdtemp");
        exit(2);
    }
    const std::string root = pattern;
    make_directory(root + "/sub");
    make_file(root + "/sub/file");
    make_file(root + "/file");
    mkfifo((root + "/fifo").c_str(), 0644);
    if (symlink("/notify-forwarder-nonexistent", (root + "/dangling").c_str()) != 0
        || symlink("sub", (root + "/link-to-sub").c_str()) != 0) {
        perror("symlink");
        exit(2);
    }

    const std::string socket_path = root + "/socket";
    sockaddr_un address;
    memset(&address, 0, sizeof(address));
    address.sun_family = AF_UNIX;
    strncpy(address.sun_path, socket_path.c_str(), sizeof(address.sun_path) - 1);
    int server = socket(AF_UNIX, SOCK_STREAM, 0);
    if (bind(server, reinterpret_cast<sockaddr*>(&address), sizeof(address)) != 0) {
        perror("bind");
        exit(2);
    }
    close(server);
    return root;
}

static std::set<std::string> watched(const DirectoryMap& map)
{
    std::set<std::string> paths;
    for (const auto& entry : map) {
        paths.insert(entry.second);
    }
    return paths;
}

static void scan_watches(const std::string& name, const std::string& root,
    const std::set<std::string>& expected)
{
    int fd = ::inotify_init();
    DirectoryMap map;
    try {
        add_directory_watch(fd, root, map);
        check(true, name + ": no error");
    }
    catch (const std::exception& error) {
        check(false, name + ": threw " + error.what());
    }
    check(watched(map) == expected, name + ": watches exactly the directories that are there");
    close(fd);
}

static void test_scans()
{
    std::string root = make_tree();
    scan_watches("socket, FIFO and symlinks", root, { root, root + "/sub" });

    report_unknown_types = true;
    root = make_tree();
    scan_watches("the same tree with DT_UNKNOWN", root, { root, root + "/sub" });
    reset();

    report_as_block_device = "file";
    root = make_tree();
    scan_watches("an entry of type DT_BLK", root, { root, root + "/sub" });
    reset();

    root = make_tree();
    make_directory(root + "/gone");
    remove_before_watch = root + "/gone";
    scan_watches("a directory removed before its watch", root, { root, root + "/sub" });
    reset();

    root = make_tree();
    make_directory(root + "/gone");
    remove_before_open = root + "/gone";
    {
        int fd = ::inotify_init();
        DirectoryMap map;
        try {
            add_directory_watch(fd, root, map);
            check(true, "a directory removed before it is opened: no error");
        }
        catch (const std::exception& error) {
            check(false, std::string("a directory removed before it is opened: threw ") + error.what());
        }
        check(watched(map).count(root + "/sub") == 1,
            "a directory removed before it is opened: the rest of the tree is watched");
        close(fd);
    }
    reset();

    root = make_tree();
    make_directory(root + "/replaced");
    replace_before_watch = root + "/replaced";
    scan_watches("a directory replaced by a file", root, { root, root + "/sub" });
    reset();
}

static void test_failure_keeps_no_descriptor()
{
    const std::string root = make_tree();
    make_directory(root + "/sub/deep");
    refuse_watch = root + "/sub/deep";
    int fd = ::inotify_init();
    const int before = open_descriptors();
    DirectoryMap map;
    bool threw = false;
    try {
        add_directory_watch(fd, root, map);
    }
    catch (const std::runtime_error&) {
        threw = true;
    }
    check(threw, "a refused watch is still an error");
    check(open_descriptors() == before, "a refused watch deep in the tree leaks no directory handle");
    close(fd);
    reset();
}

static void test_destructor_closes()
{
    const int before = open_descriptors();
    {
        InotifyWatchPlugin plugin({ "/tmp" }, [](std::vector<std::string>) {});
        check(open_descriptors() == before + 1, "the plugin holds one inotify descriptor");
    }
    check(open_descriptors() == before, "the destructor closes the inotify descriptor");
}

static void test_missing_root_is_an_error()
{
    InotifyWatchPlugin plugin({ "/notify-forwarder-nonexistent" }, [](std::vector<std::string>) {});
    bool threw = false;
    try {
        plugin.start(-1);
    }
    catch (const std::runtime_error&) {
        threw = true;
    }
    check(threw, "a root that does not exist is an error, not an empty watch");
}

int main()
{
    test_scans();
    test_failure_keeps_no_descriptor();
    test_destructor_closes();
    test_missing_root_is_an_error();
    return failures ? 1 : 0;
}
