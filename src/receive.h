#pragma once

#include <functional>
#include <string>
#include <vector>

typedef std::function<void(std::vector<std::string>)> ReceiveCallback;

class IReceivePlugin {
public:
    virtual ~IReceivePlugin() {}

public:
    // Runs until stop_fd is readable.
    virtual void start(int stop_fd) = 0;
};
