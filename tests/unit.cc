#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>

#include <cstdio>
#include <cstdlib>
#include <stdexcept>

#include "inject_utimes.h"
#include "port.h"
#include "utils.h"
#include "watch_logic.h"

static int failures = 0;

static void check(bool condition, const char* name)
{
    printf("%s %s\n", condition ? "PASS" : "FAIL", name);
    if (!condition) {
        ++failures;
    }
}

// A filesystem the tests change by hand, and a clock they move by hand.
class FakeTree {
public:
    PathStateReader reader()
    {
        return [this](const std::string& path) { return m_states[path]; };
    }

    void write(const std::string& path, bool coarse = false)
    {
        PathState& state = m_states[path];
        state.exists = true;
        state.size = 10;
        state.mtime_seconds = coarse ? 100 : state.mtime_seconds + 1;
        state.ambiguous = coarse;
    }

    void settle(const std::string& path) { m_states[path].ambiguous = false; }

private:
    std::unordered_map<std::string, PathState> m_states;
};

typedef std::vector<std::string> Paths;

static const std::chrono::milliseconds tick(100);

static void test_first_and_duplicates()
{
    FakeTree tree;
    RepeatFilter filter(tree.reader());
    TimePoint now;
    tree.write("/file");
    tree.write("/other");
    check(filter.update({ "/file" }, now) == Paths { "/file" }, "first notification passes");
    check(filter.update({}, now).empty(), "empty notification batches are accepted");
    check(filter.update({ "/other", "/other" }, now) == Paths { "/other" },
        "duplicate notifications are filtered within one batch");
}

static void test_echo_is_dropped()
{
    FakeTree tree;
    RepeatFilter filter(tree.reader());
    TimePoint now;
    tree.write("/file");
    filter.update({ "/file" }, now);

    int forwarded = 0;
    for (int i = 0; i < 9; ++i) {
        now += tick;
        forwarded += static_cast<int>(filter.update({ "/file" }, now).size());
    }
    for (int i = 0; i < 100; ++i) {
        now += tick;
        forwarded += static_cast<int>(filter.update({}, now).size());
    }
    check(forwarded == 0, "an event that changed nothing inside the window is never forwarded");
}

static void test_feedback_loop_stays_closed()
{
    FakeTree tree;
    RepeatFilter filter(tree.reader());
    TimePoint now;
    tree.write("/file");

    // Every forward comes back 200 ms later and changes nothing, as the receiver's echo does.
    int forwarded = 0;
    TimePoint echo_at = TimePoint::max();
    Paths batch = { "/file" };
    for (int i = 0; i < 300; ++i) {
        if (now >= echo_at) {
            batch.emplace_back("/file");
            echo_at = TimePoint::max();
        }
        if (!filter.update(batch, now).empty()) {
            ++forwarded;
            echo_at = now + std::chrono::milliseconds(200);
        }
        batch.clear();
        now += tick;
    }
    check(forwarded == 1, "a change whose echo comes back is forwarded once in 30 seconds");
}

static void test_second_change_inside_window()
{
    FakeTree tree;
    RepeatFilter filter(tree.reader());
    TimePoint now;
    tree.write("/file");
    filter.update({ "/file" }, now);

    now += std::chrono::milliseconds(500);
    tree.write("/file");
    check(filter.update({ "/file" }, now).empty(), "a second change waits for the window to end");

    now += std::chrono::milliseconds(400);
    check(filter.update({}, now).empty(), "the second change is still waiting at 900 ms");

    now += std::chrono::milliseconds(100);
    check(filter.update({}, now) == Paths { "/file" },
        "a second change 500 ms after the first is forwarded at the end of the window");

    now += std::chrono::milliseconds(200);
    filter.update({ "/file" }, now);
    int forwarded = 0;
    for (int i = 0; i < 100; ++i) {
        now += tick;
        forwarded += static_cast<int>(filter.update({}, now).size());
    }
    check(forwarded == 0, "the echo of the delayed forward is dropped");
}

static void test_busy_file()
{
    FakeTree tree;
    RepeatFilter filter(tree.reader());
    TimePoint now;

    // 60 changes at 10 per second. A dropped event must not push the window back.
    int forwarded = 0;
    for (int i = 0; i < 60; ++i) {
        tree.write("/file");
        forwarded += static_cast<int>(filter.update({ "/file" }, now).size());
        now += tick;
    }
    check(forwarded >= 5, "a file changing 10 times a second goes on being forwarded");

    int after_last_change = 0;
    for (int i = 0; i < 20; ++i) {
        after_last_change += static_cast<int>(filter.update({}, now).size());
        now += tick;
    }
    check(after_last_change == 1, "the last change of a busy file is forwarded exactly once");
}

static void test_expired_passes_again()
{
    FakeTree tree;
    RepeatFilter filter(tree.reader());
    TimePoint now;
    tree.write("/file");
    filter.update({ "/file" }, now);
    now += RepeatFilter::repeat_window;
    check(filter.update({ "/file" }, now) == Paths { "/file" },
        "an event after the window passes again even if the state is unchanged");
}

static void test_coarse_timestamps()
{
    FakeTree tree;
    RepeatFilter filter(tree.reader());
    TimePoint now;
    tree.write("/file", true);
    filter.update({ "/file" }, now);

    // The same size in the same second: the state cannot show this write.
    now += std::chrono::milliseconds(500);
    tree.write("/file", true);
    check(filter.update({ "/file" }, now).empty(), "an ambiguous repeat is not forwarded at once");

    int forwarded = 0;
    for (int i = 0; i < 20; ++i) {
        now += tick;
        if (now >= TimePoint() + std::chrono::seconds(coarse_mtime_seconds)) {
            tree.settle("/file");
        }
        forwarded += static_cast<int>(filter.update({}, now).size());
    }
    check(forwarded == 1, "a write hidden by a coarse timestamp is forwarded once");

    now += tick;
    filter.update({ "/file" }, now);
    forwarded = 0;
    for (int i = 0; i < 100; ++i) {
        now += tick;
        forwarded += static_cast<int>(filter.update({}, now).size());
    }
    check(forwarded == 0, "the echo after a coarse timestamp settles is dropped");
}

static void test_forgets_quiet_paths()
{
    FakeTree tree;
    RepeatFilter filter(tree.reader());
    TimePoint now;
    for (int i = 0; i < 1000; ++i) {
        filter.update({ "/file" + std::to_string(i) }, now);
    }
    check(filter.tracked() == 1000, "paths in the window are tracked");
    now += RepeatFilter::repeat_window;
    filter.update({}, now);
    check(filter.tracked() == 0, "quiet paths are forgotten when their window ends");
}

// The same file through the kernel: the only calls the filter's verdict depends on.
static void test_path_state_on_a_real_file()
{
    char directory[] = "/tmp/notify-forwarder-unit-XXXXXX";
    if (!mkdtemp(directory)) {
        check(false, "a scratch directory can be created");
        return;
    }
    const std::string path = std::string(directory) + "/file";

    check(!read_path_state(path).exists, "a missing path reads as not existing");

    int fd = open(path.c_str(), O_CREAT | O_WRONLY, 0644);
    check(write(fd, "one", 3) == 3, "the file can be written");
    close(fd);
    timespec first[2] = { { 1500000000, 123456789 }, { 1500000000, 123456789 } };
    utimensat(AT_FDCWD, path.c_str(), first, 0);
    const PathState before = read_path_state(path);
    check(before.exists, "an existing path reads as existing");

    // chmod to the current mode changes ctime and nothing else, like the receiver's attribute pair.
    struct stat status;
    stat(path.c_str(), &status);
    const time_t ctime_before = status.st_ctime;
    sleep(1);
    chmod(path.c_str(), before.mode & 07777);
    stat(path.c_str(), &status);
    check(status.st_ctime != ctime_before, "the kernel changed ctime");
    check(read_path_state(path) == before, "a ctime change alone is not a change");

    timespec second[2] = { { 1500000000, 123456790 }, { 1500000000, 123456790 } };
    utimensat(AT_FDCWD, path.c_str(), second, 0);
    check(read_path_state(path) != before, "one nanosecond of mtime is a change");
    utimensat(AT_FDCWD, path.c_str(), first, 0);
    check(read_path_state(path) == before, "restoring mtime restores the state");

    chmod(path.c_str(), 0600);
    check(read_path_state(path) != before, "a new mode is a change");
    chmod(path.c_str(), before.mode & 07777);

    fd = open(path.c_str(), O_WRONLY | O_APPEND);
    check(write(fd, "x", 1) == 1, "the file can be appended to");
    close(fd);
    utimensat(AT_FDCWD, path.c_str(), first, 0);
    check(read_path_state(path) != before, "a new size is a change");

    check(!read_path_state(path).ambiguous, "an old timestamp is not ambiguous");
    timespec now_whole;
    clock_gettime(CLOCK_REALTIME, &now_whole);
    now_whole.tv_nsec = 0;
    timespec whole[2] = { now_whole, now_whole };
    utimensat(AT_FDCWD, path.c_str(), whole, 0);
    check(read_path_state(path).ambiguous, "a recent whole-second timestamp is ambiguous");
    timespec future = { now_whole.tv_sec + 100000, 0 };
    timespec later[2] = { future, future };
    utimensat(AT_FDCWD, path.c_str(), later, 0);
    check(!read_path_state(path).ambiguous, "a timestamp in the future is not ambiguous");

    unlink(path.c_str());
    check(read_path_state(path) != before, "a deleted file is a change");
    rmdir(directory);
}

static void test_parse_port()
{
    uint16_t port = 7;
    check(parse_port("1", port) && port == 1, "port 1 is accepted");
    check(parse_port("29324", port) && port == 29324, "a port in range is read as written");
    check(parse_port("65535", port) && port == 65535, "port 65535 is accepted");
    port = 7;
    const char* refused[] = { "0", "65536", "70000", "4294967297", "99999999999999999999", "-1",
        "+80", " 80", "80 ", "80x", "0x50", "abc", "" };
    bool all_refused = true;
    for (const char* text : refused) {
        all_refused = !parse_port(text, port) && all_refused;
    }
    check(all_refused, "a port that is not a whole number from 1 to 65535 is refused");
    check(port == 7, "a refused port leaves the value alone");
}

int main()
{
    PrefixMap prefixes { { "/host/project", "/guest/project" }, { "/other", "/second" } };
    check(replace_prefix(prefixes, "/host/project/file") == "/guest/project/file",
        "prefix mapping preserves the suffix");
    check(replace_prefix(prefixes, "/other/file") == "/second/file",
        "prefix mapping checks later entries");
    check(replace_prefix(prefixes, "/unmapped/file") == "/unmapped/file",
        "unmapped paths remain unchanged");
    check(replace_prefix({}, "/file") == "/file", "empty prefix maps are accepted");
    PrefixMap boundary { { "/foo", "/guest" } };
    check(replace_prefix(boundary, "/foobar/file") == "/foobar/file",
        "a prefix does not match a longer directory name");
    check(replace_prefix(boundary, "/foo") == "/guest", "a prefix matches the directory itself");
    check(replace_prefix(boundary, "/foo/file") == "/guest/file", "a prefix matches beneath it");
    PrefixMap ordered { { "/foo", "/first" }, { "/foobar", "/second" } };
    check(replace_prefix(ordered, "/foobar/file") == "/second/file",
        "a shorter prefix does not take a longer directory from a later entry");
    PrefixMap whole_tree { { "/", "/guest/" } };
    check(replace_prefix(whole_tree, "/file") == "/guest/file", "the root matches everything");

    test_first_and_duplicates();
    test_echo_is_dropped();
    test_feedback_loop_stays_closed();
    test_second_change_inside_window();
    test_busy_file();
    test_expired_passes_again();
    test_coarse_timestamps();
    test_forgets_quiet_paths();
    test_path_state_on_a_real_file();
    test_parse_port();

    int value = 4;
    {
        VariableOverrideGuard<int> guard(value, 8);
        check(value == 8, "override guard installs the replacement");
    }
    check(value == 4, "override guard restores the previous value");

    UtimesInjectPlugin injector;
    injector.inject({});
    injector.inject({ "/notify-forwarder-nonexistent-parent/missing" });
    check(true, "receiver tolerates empty batches and missing files");

    return failures ? 1 : 0;
}
