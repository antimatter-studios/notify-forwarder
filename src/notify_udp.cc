#include <arpa/inet.h>
#include <errno.h>
#include <netinet/in.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>
#include <stdexcept>

#include "notify_udp.h"

class UDPNotifyPluginImpl {
public:
    int socket_fd;

public:
    sockaddr_in dest_addr;

public:
    size_t skipped = 0;
};

namespace {

const int send_attempts = 5;

// The local socket has no room for now, which a burst of paths causes.
bool is_worth_retrying(int error)
{
    return error == ENOBUFS || error == ENOMEM || error == EAGAIN || error == EWOULDBLOCK
        || error == EINTR;
}

// A mistake in this program. Everything else is about the network or this one path, and may pass.
bool is_fatal(int error)
{
    return error == EBADF || error == ENOTSOCK || error == EFAULT || error == EINVAL
        || error == EDESTADDRREQ;
}

void wait_before_retry(int attempt)
{
    timespec delay = { 0, 1000000L << attempt };
    nanosleep(&delay, nullptr);
}

}

UDPNotifyPlugin::UDPNotifyPlugin(const std::string& addr, uint16_t port)
    : m_impl(new UDPNotifyPluginImpl())
{
    m_impl->socket_fd = socket(AF_INET, SOCK_DGRAM, 0);
    if (m_impl->socket_fd == -1) {
        throw std::runtime_error("Could not create sending socket");
    }

    sockaddr_in dest_addr;
    memset(&dest_addr, 0, sizeof(dest_addr));
    dest_addr.sin_family = AF_INET;
    if (inet_aton(addr.c_str(), &dest_addr.sin_addr) == 0) {
        throw std::runtime_error(
            "Could not read the address " + addr + ": it must be an IPv4 address");
    }
    dest_addr.sin_port = htons(port);

    m_impl->dest_addr = dest_addr;
}

UDPNotifyPlugin::~UDPNotifyPlugin()
{
    if (m_impl->socket_fd >= 0) {
        close(m_impl->socket_fd);
    }
}

void UDPNotifyPlugin::notify(const std::vector<std::string>& paths)
{
    for (auto& path : paths) {
        // While sending is failing, retries would only hold up the rest of a burst.
        const int attempts = m_impl->skipped ? 1 : send_attempts;
        int error = 0;

        for (int attempt = 0; attempt < attempts; ++attempt) {
            ssize_t sent_bytes = sendto(m_impl->socket_fd,
                path.c_str(),
                path.size(),
                0,
                reinterpret_cast<sockaddr*>(&m_impl->dest_addr),
                sizeof(m_impl->dest_addr));

            error = sent_bytes == -1 ? errno : 0;

            if (!is_worth_retrying(error)) {
                break;
            }

            if (attempt + 1 < attempts) {
                wait_before_retry(attempt);
            }
        }

        if (error == 0) {
            if (m_impl->skipped) {
                fprintf(stderr,
                    "notify-forwarder: sending works again, %zu paths were skipped\n",
                    m_impl->skipped);
                m_impl->skipped = 0;
            }
            continue;
        }

        if (is_fatal(error)) {
            throw std::runtime_error("Could not send " + path + ": " + strerror(error));
        }

        if (!m_impl->skipped) {
            fprintf(stderr,
                "notify-forwarder: skipped %s, and any more until sending works again: %s\n",
                path.c_str(),
                strerror(error));
        }
        ++m_impl->skipped;
    }
}
