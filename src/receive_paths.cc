#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>

#include <cerrno>
#include <cstdlib>
#include <stdexcept>

#include "receive_paths.h"

namespace {

class Descriptor {
public:
    explicit Descriptor(int descriptor)
        : m_descriptor(descriptor)
    {
    }
    ~Descriptor()
    {
        if (m_descriptor >= 0) {
            close(m_descriptor);
        }
    }
    Descriptor(const Descriptor&) = delete;
    Descriptor& operator=(const Descriptor&) = delete;
    int get() const { return m_descriptor; }
    void reset(int descriptor)
    {
        if (m_descriptor >= 0) {
            close(m_descriptor);
        }
        m_descriptor = descriptor;
    }

private:
    int m_descriptor;
};

std::string resolve(const std::string& path)
{
    if (path.empty() || path.find('\0') != std::string::npos) {
        return { };
    }
    char* resolved = realpath(path.c_str(), nullptr);
    if (!resolved) {
        return { };
    }
    std::string result(resolved);
    free(resolved);
    return result;
}

int open_final(int parent, const std::string& name)
{
#ifdef __linux__
    Descriptor reference(
        openat(parent, name.c_str(), O_PATH | O_NOFOLLOW | O_CLOEXEC | O_NONBLOCK));
    struct stat status;
    if (reference.get() < 0 || fstat(reference.get(), &status) != 0 || !S_ISREG(status.st_mode)) {
        return -1;
    }
    std::string descriptor_path = "/proc/self/fd/" + std::to_string(reference.get());
    return open(descriptor_path.c_str(), O_WRONLY | O_CLOEXEC | O_NONBLOCK);
#else
    struct stat expected;
    if (fstatat(parent, name.c_str(), &expected, AT_SYMLINK_NOFOLLOW) != 0
        || !S_ISREG(expected.st_mode)) {
        return -1;
    }
    int descriptor = openat(parent, name.c_str(), O_WRONLY | O_NOFOLLOW | O_CLOEXEC | O_NONBLOCK);
    struct stat actual;
    if (descriptor >= 0
        && (fstat(descriptor, &actual) != 0 || !S_ISREG(actual.st_mode)
            || expected.st_dev != actual.st_dev || expected.st_ino != actual.st_ino)) {
        close(descriptor);
        return -1;
    }
    return descriptor;
#endif
}

}

ReceivePaths::ReceivePaths(const std::vector<std::string>& roots)
{
    std::vector<std::string> configured = roots.empty() ? std::vector<std::string> { "/" } : roots;
    try {
        for (const auto& path : configured) {
            std::string canonical = resolve(path);
            if (canonical.empty()) {
                throw std::runtime_error("Could not resolve receive root: " + path);
            }
            m_roots.push_back({ canonical, -1 });
            m_roots.back().descriptor = open(
                canonical.c_str(), O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC | O_NONBLOCK);
            if (m_roots.back().descriptor < 0) {
                throw std::runtime_error("Receive root is not an accessible directory: " + path);
            }
        }
    } catch (...) {
        for (const auto& root : m_roots) {
            if (root.descriptor >= 0) {
                close(root.descriptor);
            }
        }
        throw;
    }
}

ReceivePaths::~ReceivePaths()
{
    for (const auto& root : m_roots) {
        close(root.descriptor);
    }
}

int ReceivePaths::open_regular(const std::string& path) const
{
    std::string canonical = resolve(path);
    if (canonical.empty()) {
        return -1;
    }
    for (const auto& root : m_roots) {
        size_t prefix = root.path == "/" ? 0 : root.path.size();
        if (canonical.size() <= prefix || canonical.compare(0, prefix, root.path, 0, prefix) != 0
            || canonical[prefix] != '/') {
            continue;
        }
        Descriptor directory(fcntl(root.descriptor, F_DUPFD_CLOEXEC, 0));
        size_t start = prefix + 1;
        while (directory.get() >= 0) {
            size_t separator = canonical.find('/', start);
            std::string component = canonical.substr(start, separator - start);
            if (separator == std::string::npos) {
                return open_final(directory.get(), component);
            }
            directory.reset(openat(directory.get(),
                component.c_str(),
                O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC | O_NONBLOCK));
            start = separator + 1;
        }
    }
    return -1;
}
