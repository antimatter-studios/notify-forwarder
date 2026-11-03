// What the sender does when sendto fails. The call is interposed, so that each failure happens on
// the path a test chooses.
#include <errno.h>
#include <sys/socket.h>
#include <unistd.h>

#include <cstdio>
#include <cstdlib>
#include <deque>
#include <fstream>
#include <iterator>
#include <stdexcept>
#include <string>
#include <vector>

static std::deque<int> planned_errors;
static std::vector<std::string> attempts;
static std::vector<std::string> delivered;

static ssize_t interposed_sendto(
    int, const void* data, size_t size, int, const sockaddr*, socklen_t)
{
    const std::string path(static_cast<const char*>(data), size);
    attempts.emplace_back(path);
    if (!planned_errors.empty()) {
        const int error = planned_errors.front();
        planned_errors.pop_front();
        if (error) {
            errno = error;
            return -1;
        }
    }
    delivered.emplace_back(path);
    return static_cast<ssize_t>(size);
}

#define sendto interposed_sendto
#include "notify_udp.cc"
#undef sendto

static int failures = 0;

static void check(bool condition, const std::string& name)
{
    printf("%s %s\n", condition ? "PASS" : "FAIL", name.c_str());
    if (!condition) {
        ++failures;
    }
}

typedef std::vector<std::string> Paths;

// Runs a batch with stderr in a file. Returns what was written there, and whether the batch threw.
static std::string send_batch(UDPNotifyPlugin& plugin, const Paths& paths, bool* threw = nullptr)
{
    char name[] = "/tmp/notify-forwarder-stderr-XXXXXX";
    int file = mkstemp(name);
    fflush(stderr);
    const int saved = dup(2);
    dup2(file, 2);
    bool caught = false;
    try {
        plugin.notify(paths);
    }
    catch (const std::runtime_error&) {
        caught = true;
    }
    fflush(stderr);
    dup2(saved, 2);
    close(saved);
    close(file);
    std::ifstream stream(name);
    const std::string text(
        (std::istreambuf_iterator<char>(stream)), std::istreambuf_iterator<char>());
    unlink(name);
    if (threw) {
        *threw = caught;
    }
    return text;
}

static void reset(const std::deque<int>& errors)
{
    planned_errors = errors;
    attempts.clear();
    delivered.clear();
}

int main()
{
    UDPNotifyPlugin plugin("127.0.0.1", 9);

    reset({ ENOBUFS, ENOBUFS });
    bool threw = false;
    std::string message = send_batch(plugin, { "/first", "/second" }, &threw);
    check(!threw, "a full socket buffer ends nothing");
    check(delivered == Paths({ "/first", "/second" }), "a full socket buffer is retried");
    check(message.empty(), "a retry that succeeds reports nothing");

    // Every retry of the first path fails.
    reset(std::deque<int>(64, ENOBUFS));
    message = send_batch(plugin, { "/lost" }, &threw);
    check(!threw, "a buffer that stays full ends nothing");
    check(delivered.empty(), "the path is skipped when retries run out");
    check(message.find("/lost") != std::string::npos, "the skipped path is reported");
    check(message.find("No buffer space") != std::string::npos, "the reason is reported");

    // Still failing: no second message, and no retries to hold up the rest of a burst.
    planned_errors = std::deque<int>(64, ENOBUFS);
    attempts.clear();
    message = send_batch(plugin, { "/a", "/b", "/c" }, &threw);
    check(!threw, "failures go on being skipped");
    check(
        attempts == Paths({ "/a", "/b", "/c" }), "paths are not retried while sending is failing");
    check(message.empty(), "a failure that goes on is reported once");

    // Recovery is reported with what was lost.
    reset({});
    message = send_batch(plugin, { "/back" }, &threw);
    check(delivered == Paths({ "/back" }), "sending resumes");
    check(message.find("4") != std::string::npos, "recovery reports how many paths were skipped");

    // A network that is down or a firewall that refuses is not retried, and ends nothing.
    for (int error :
        { ENETUNREACH, EHOSTUNREACH, ENETDOWN, EACCES, EPERM, ECONNREFUSED, EMSGSIZE }) {
        reset({ error });
        message = send_batch(plugin, { "/path" }, &threw);
        check(!threw && attempts.size() == 1,
            std::string("skipped without a retry: ") + strerror(error));
        reset({});
        send_batch(plugin, { "/clear" });
    }

    // These are mistakes in this program, and going on would lose everything silently.
    for (int error : { EBADF, ENOTSOCK, EFAULT, EINVAL, EDESTADDRREQ }) {
        reset({ error });
        send_batch(plugin, { "/path" }, &threw);
        check(threw, std::string("fatal: ") + strerror(error));
    }

    bool bad_address = false;
    try {
        UDPNotifyPlugin refused("not-an-address", 9);
    }
    catch (const std::runtime_error& error) {
        bad_address = std::string(error.what()).find("not-an-address") != std::string::npos;
    }
    check(bad_address, "an address that cannot be read is refused by name");

    return failures ? 1 : 0;
}
