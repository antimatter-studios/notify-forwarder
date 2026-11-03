// Puts a concurrent writer in front of every call the injector could use to change a file, however
// the injector is written. The writer's mode and mtime must survive: an injector that read them
// earlier and submits them again overwrites the writer, however small the window is in real life.
#include <fcntl.h>
#include <sys/stat.h>
#include <sys/time.h>
#include <sys/wait.h>
#include <sys/xattr.h>
#include <time.h>
#include <unistd.h>

#include <cerrno>
#include <cstdio>
#include <cstdlib>
#include <csignal>
#include <cstring>
#include <fstream>
#include <iterator>
#include <string>
#include <vector>

static std::string target;
static int arrivals = 0;
static bool refuse_attributes = false;
static bool terminate_after_set = false;
static mode_t writer_mode = 0;
static timespec writer_mtime = { 0, 0 };

static void writer_arrives()
{
    ++arrivals;
    writer_mode = (arrivals % 2) ? 0640 : 0600;
    writer_mtime = { 1800000000 + arrivals, 987654321 };
    const timespec times[2] = { writer_mtime, writer_mtime };
    if (chmod(target.c_str(), writer_mode) != 0
        || utimensat(AT_FDCWD, target.c_str(), times, 0) != 0) {
        perror("writer");
        exit(2);
    }
}

static int attribute_set(int result)
{
    if (terminate_after_set) {
        raise(SIGTERM);
    }
    return result;
}

#define CONCURRENT(call) (writer_arrives(), (call))

#define write(...) CONCURRENT(::write(__VA_ARGS__))
#define pwrite(...) CONCURRENT(::pwrite(__VA_ARGS__))
#define ftruncate(...) CONCURRENT(::ftruncate(__VA_ARGS__))
#define utimes(...) CONCURRENT(::utimes(__VA_ARGS__))
#define futimens(...) CONCURRENT(::futimens(__VA_ARGS__))
#define fchmod(...) CONCURRENT(::fchmod(__VA_ARGS__))
#define fchown(...) CONCURRENT(::fchown(__VA_ARGS__))
#define fsetxattr(...) (refuse_attributes ? (errno = ENOTSUP, -1) : attribute_set(CONCURRENT(::fsetxattr(__VA_ARGS__))))
#define fremovexattr(...) CONCURRENT(::fremovexattr(__VA_ARGS__))

#include "inject_utimes.cc"

#undef write
#undef pwrite
#undef ftruncate
#undef utimes
#undef futimens
#undef fchmod
#undef fchown
#undef fsetxattr
#undef fremovexattr

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

static std::string injected_stderr(UtimesInjectPlugin& injector, int repeats)
{
    fflush(stderr);
    char capture[] = "/tmp/notify-forwarder-stderr-XXXXXX";
    int descriptor = mkstemp(capture);
    int saved = dup(STDERR_FILENO);
    if (descriptor < 0 || saved < 0 || dup2(descriptor, STDERR_FILENO) < 0) {
        perror("capture");
        exit(2);
    }
    for (int repeat = 0; repeat < repeats; ++repeat) {
        injector.inject({ target });
    }
    fflush(stderr);
    dup2(saved, STDERR_FILENO);
    close(saved);
    close(descriptor);
    std::ifstream input(capture, std::ios::binary);
    std::string text((std::istreambuf_iterator<char>(input)), std::istreambuf_iterator<char>());
    unlink(capture);
    return text;
}

static bool holds_attribute()
{
    char value[16];
#ifdef __APPLE__
    return getxattr(target.c_str(), "user.notify-forwarder", value, sizeof(value), 0, 0) >= 0;
#else
    return getxattr(target.c_str(), "user.notify-forwarder", value, sizeof(value)) >= 0;
#endif
}

static void termination_case()
{
    pid_t child = fork();
    if (child < 0) {
        perror("fork");
        exit(2);
    }
    if (child == 0) {
        terminate_after_set = true;
        UtimesInjectPlugin injector;
        injector.inject({ target });
        _exit(0);
    }
    int status = 0;
    check(waitpid(child, &status, 0) == child, "the terminated injector is collected");
    check(WIFSIGNALED(status) && WTERMSIG(status) == SIGTERM, "SIGTERM still ends the injector");
    check(!holds_attribute(), "SIGTERM between the two attribute calls leaves no attribute behind");
}

static void leftover_case()
{
#ifdef __APPLE__
    const int planted = setxattr(target.c_str(), "user.notify-forwarder", "", 0, 0, 0);
#else
    const int planted = setxattr(target.c_str(), "user.notify-forwarder", "", 0, 0);
#endif
    check(planted == 0, "a leftover attribute is planted");
    UtimesInjectPlugin injector;
    injector.inject({ target });
    check(!holds_attribute(), "the next event removes a leftover attribute");
}

int main()
{
    char path[] = "/tmp/notify-forwarder-race-XXXXXX";
    int descriptor = mkstemp(path);
    if (descriptor < 0) {
        perror("mkstemp");
        return 1;
    }
    close(descriptor);
    target = path;
    { std::ofstream(target, std::ios::binary) << "contents"; }
    writer_arrives();

    {
        UtimesInjectPlugin injector;
        const int before = arrivals;
        injector.inject({ target });
        check(arrivals > before, "the writer interleaved with the injector");
    }

    struct stat status;
    check(stat(path, &status) == 0, "metadata after the race is readable");
    timespec mtime = modification_time(status);
    check(mtime.tv_sec == writer_mtime.tv_sec && mtime.tv_nsec == writer_mtime.tv_nsec,
        "the writer's mtime survives the injector");
    check((status.st_mode & 07777) == writer_mode, "the writer's mode survives the injector");

    refuse_attributes = true;
    {
        UtimesInjectPlugin injector;
        std::string text = injected_stderr(injector, 3);
        size_t first = text.find("no IN_ATTRIB");
        check(first != std::string::npos, "a refused attribute is reported");
        check(first == std::string::npos || text.find("no IN_ATTRIB", first + 1) == std::string::npos,
            "a refused attribute is reported once");
    }
    refuse_attributes = false;

    termination_case();
    leftover_case();

    check(unlink(path) == 0, "fixture file is removed");
    return failures ? 1 : 0;
}
