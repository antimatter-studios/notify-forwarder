#pragma once

#include <string>
#include <vector>

class ReceivePaths {
public:
    explicit ReceivePaths(const std::vector<std::string>& roots);
    ~ReceivePaths();
    ReceivePaths(const ReceivePaths&) = delete;
    ReceivePaths& operator=(const ReceivePaths&) = delete;

    int open_regular(const std::string& path) const;

private:
    struct Root {
        std::string path;
        int descriptor;
    };
    std::vector<Root> m_roots;
};
