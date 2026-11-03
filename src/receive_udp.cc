#include <arpa/inet.h>
#include <errno.h>
#include <netinet/in.h>
#include <poll.h>
#include <stdio.h>
#include <string.h>
#include <sys/param.h>
#include <sys/socket.h>
#include <sys/uio.h>
#include <unistd.h>
#include <chrono>
#include <cstdint>
#include <stdexcept>
#include <string>

#include "receive_udp.h"

class UDPReceivePluginImpl {
public:
    ReceiveCallback callback;

public:
    int socket_fd;
};

namespace {

const int report_interval_milliseconds = 1000;

// What the receiver could not act on, on stderr at most once a second, as a burst would otherwise
// print one line per path. The kernel counts the datagrams a full socket buffer drops and hands the
// total over with later datagrams. Only Linux has this count.
class DropReport {
public:
    void read_kernel_count(const msghdr& message)
    {
#ifdef SO_RXQ_OVFL
        for (cmsghdr* header = CMSG_FIRSTHDR(&message); header;
             header = CMSG_NXTHDR(const_cast<msghdr*>(&message), header)) {
            if (header->cmsg_level == SOL_SOCKET && header->cmsg_type == SO_RXQ_OVFL) {
                memcpy(&m_dropped, CMSG_DATA(header), sizeof(m_dropped));
            }
        }
#else
        (void)message;
#endif
    }

public:
    void refuse_oversized() { ++m_oversized; }

public:
    void print_if_due()
    {
        const auto now = std::chrono::steady_clock::now();
        if (now - m_last_print < std::chrono::milliseconds(report_interval_milliseconds)) {
            return;
        }

        if (m_dropped != m_printed_dropped) {
            fprintf(stderr,
                "notify-forwarder: %u paths lost to a full socket buffer so far\n",
                m_dropped);
            m_printed_dropped = m_dropped;
            m_last_print = now;
        }

        if (m_oversized) {
            fprintf(stderr,
                "notify-forwarder: refused %zu oversized datagrams, longer than any path here\n",
                m_oversized);
            m_oversized = 0;
            m_last_print = now;
        }
    }

private:
    uint32_t m_dropped = 0;

private:
    uint32_t m_printed_dropped = 0;

private:
    size_t m_oversized = 0;

private:
    std::chrono::steady_clock::time_point m_last_print;
};

}

UDPReceivePlugin::UDPReceivePlugin(uint16_t port, const ReceiveCallback& callback)
    : m_impl(new UDPReceivePluginImpl())
{
    m_impl->socket_fd = socket(AF_INET, SOCK_DGRAM, 0);
    if (m_impl->socket_fd == -1) {
        throw std::runtime_error(std::string("Could not create the socket: ") + strerror(errno));
    }

    // As much room for a burst as the system allows. A limit lower than this changes nothing else.
    const int buffer_size = 16 * 1024 * 1024;
    setsockopt(m_impl->socket_fd, SOL_SOCKET, SO_RCVBUF, &buffer_size, sizeof(buffer_size));

#ifdef SO_RXQ_OVFL
    const int report_drops = 1;
    setsockopt(m_impl->socket_fd, SOL_SOCKET, SO_RXQ_OVFL, &report_drops, sizeof(report_drops));
#endif

    sockaddr_in dest_addr;
    memset(&dest_addr, 0, sizeof(dest_addr));
    dest_addr.sin_family = AF_INET;
    dest_addr.sin_addr.s_addr = inet_addr("0.0.0.0");
    dest_addr.sin_port = htons(port);

    int bind_result
        = bind(m_impl->socket_fd, reinterpret_cast<sockaddr*>(&dest_addr), sizeof(dest_addr));

    if (bind_result != 0) {
        throw std::runtime_error(
            "Could not bind port " + std::to_string(port) + ": "
            + strerror(errno));
    }

    m_impl->callback = callback;
}

UDPReceivePlugin::~UDPReceivePlugin()
{
    if (m_impl->socket_fd >= 0) {
        close(m_impl->socket_fd);
    }
}

void UDPReceivePlugin::start(int stop_fd)
{
    // One byte longer than a path can be, so that a longer datagram cannot pass for a path.
    char buffer[MAXPATHLEN + 1];
    sockaddr_in src_addr;
    DropReport report;

    pollfd fds[2];
    fds[0].fd = stop_fd;
    fds[0].events = POLLIN;
    fds[1].fd = m_impl->socket_fd;
    fds[1].events = POLLIN;

    for (;;) {
        report.print_if_due();

        if (poll(fds, 2, report_interval_milliseconds) == -1) {
            if (errno == EINTR) {
                continue;
            }

            throw std::runtime_error("Could not wait for a UDP message");
        }

        if (fds[0].revents) {
            return;
        }

        if (!fds[1].revents) {
            continue;
        }

        iovec data = { buffer, sizeof(buffer) };
        alignas(cmsghdr) char control[CMSG_SPACE(sizeof(uint32_t))];

        msghdr message;
        memset(&message, 0, sizeof(message));
        message.msg_name = &src_addr;
        message.msg_namelen = sizeof(src_addr);
        message.msg_iov = &data;
        message.msg_iovlen = 1;
        message.msg_control = control;
        message.msg_controllen = sizeof(control);

        ssize_t bytes_read = recvmsg(m_impl->socket_fd, &message, 0);

        if (bytes_read == -1) {
            if (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK) {
                continue;
            }

            throw std::runtime_error("Could not receive UDP message");
        }

        report.read_kernel_count(message);

        if (bytes_read >= MAXPATHLEN) {
            report.refuse_oversized();
            continue;
        }

        if (bytes_read > 0) {
            std::string filename(buffer, static_cast<size_t>(bytes_read));
            m_impl->callback({ filename });
        }
    }
}
