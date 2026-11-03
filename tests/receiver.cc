#include <fcntl.h>
#include <sys/stat.h>
#include <sys/xattr.h>
#include <unistd.h>

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <iterator>
#include <string>

#include "inject_utimes.h"

static int failures = 0;

static void check(bool condition, const char* name)
{
    printf("%s %s\n", condition ? "PASS" : "FAIL", name);
    if (!condition) {
        ++failures;
    }
}

static timespec modification_time(const struct stat& status)
{
#ifdef __APPLE__
    return status.st_mtimespec;
#else
    return status.st_mtim;
#endif
}

static bool holds_receiver_attribute(const std::string& path)
{
    char names[4096];
#ifdef __APPLE__
    ssize_t length = listxattr(path.c_str(), names, sizeof(names), 0);
#else
    ssize_t length = listxattr(path.c_str(), names, sizeof(names));
#endif
    for (ssize_t offset = 0; offset < length;
         offset += static_cast<ssize_t>(strlen(names + offset)) + 1) {
        if (strstr(names + offset, "notify-forwarder")) {
            return true;
        }
    }
    return false;
}

static void preservation_case(const std::string& path, const std::string& contents)
{
    {
        std::ofstream output(path, std::ios::binary);
        output << contents;
    }
    const timespec times[2] = { { 1234567890, 123456789 }, { 1234567891, 987654321 } };
    check(chmod(path.c_str(), 0640) == 0, "fixture mode is installed");
    check(utimensat(AT_FDCWD, path.c_str(), times, 0) == 0, "fixture timestamps are installed");
    struct stat before;
    check(stat(path.c_str(), &before) == 0, "fixture metadata is readable");

    UtimesInjectPlugin injector;
    for (int iteration = 0; iteration < 100; ++iteration) {
        injector.inject({ path });
    }

    struct stat after;
    check(stat(path.c_str(), &after) == 0, "receiver result metadata is readable");
    check(before.st_size == after.st_size, "receiver preserves file size");
    check(before.st_mode == after.st_mode, "receiver preserves file mode");
    timespec before_time = modification_time(before);
    timespec after_time = modification_time(after);
    check(before_time.tv_sec == after_time.tv_sec && before_time.tv_nsec == after_time.tv_nsec,
        "receiver preserves nanosecond mtime");
    std::ifstream input(path, std::ios::binary);
    std::string observed((std::istreambuf_iterator<char>(input)), std::istreambuf_iterator<char>());
    check(observed == contents, "receiver preserves every file byte");
    check(!holds_receiver_attribute(path), "receiver leaves no extended attribute behind");
    check(unlink(path.c_str()) == 0, "fixture file is removed");
}

int main()
{
    char root[] = "/tmp/notify-forwarder-receiver-XXXXXX";
    if (!mkdtemp(root)) {
        perror("mkdtemp");
        return 1;
    }
    preservation_case(std::string(root) + "/empty", "");
    preservation_case(std::string(root) + "/binary", std::string("first\0last\xff", 11));
    check(rmdir(root) == 0, "fixture directory is removed");
    return failures ? 1 : 0;
}
