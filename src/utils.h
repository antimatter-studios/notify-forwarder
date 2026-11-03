#pragma once

#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <string.h>
#include <unistd.h>

#include <stdexcept>

template <typename T> class VariableOverrideGuard {
public:
    VariableOverrideGuard(T& var, const T& value)
        : m_old_value(var)
        , m_var(var)
    {
        m_var = value;
    }

public:
    ~VariableOverrideGuard() { m_var = m_old_value; }

private:
    T m_old_value;

private:
    T& m_var;
};

// While it exists, SIGINT and SIGTERM make fd() readable. A signal handler can do very little
// safely, and write is one of those things, so a stop is a byte in a pipe that the main loop waits
// on beside its work. A signal that arrives before the loop starts waiting is not lost. SA_RESTART
// keeps the signal from failing a call in progress elsewhere with EINTR.
class StopSignals {
public:
    StopSignals()
    {
        int fds[2];
        if (pipe(fds) != 0) {
            throw std::runtime_error("Could not create the stop pipe");
        }
        fcntl(fds[0], F_SETFD, FD_CLOEXEC);
        fcntl(fds[1], F_SETFD, FD_CLOEXEC);
        fcntl(fds[1], F_SETFL, O_NONBLOCK);
        m_read_fd = fds[0];
        s_write_fd = fds[1];

        struct sigaction action;
        memset(&action, 0, sizeof(action));
        action.sa_handler = handle;
        action.sa_flags = SA_RESTART;
        sigemptyset(&action.sa_mask);
        if (sigaction(SIGINT, &action, &m_old_int) != 0
            || sigaction(SIGTERM, &action, &m_old_term) != 0) {
            throw std::logic_error("Could not install the stop signal handlers");
        }
    }

public:
    ~StopSignals()
    {
        sigaction(SIGINT, &m_old_int, nullptr);
        sigaction(SIGTERM, &m_old_term, nullptr);
        close(m_read_fd);
        close(s_write_fd);
        s_write_fd = -1;
    }

public:
    StopSignals(const StopSignals&) = delete;

public:
    StopSignals& operator=(const StopSignals&) = delete;

public:
    int fd() const { return m_read_fd; }

private:
    static void handle(int)
    {
        const int saved_errno = errno;
        // A full pipe already holds a stop, so a failed write loses nothing.
        ssize_t written = write(s_write_fd, "", 1);
        (void)written;
        errno = saved_errno;
    }

private:
    static inline int s_write_fd = -1;

private:
    int m_read_fd;

private:
    struct sigaction m_old_int;

private:
    struct sigaction m_old_term;
};
