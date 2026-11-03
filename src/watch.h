#pragma once

#include <functional>
#include <string>
#include <vector>

// Called with the paths that changed, and with an empty batch about every 100 ms while nothing
// does, so that work waiting on the clock still happens.
typedef std::function<void(std::vector<std::string>)> WatchCallback;

class IWatchPlugin {
    // public: IWatchPlugin(
    //     const std::vector<std::string>& paths,
    //     const WatchCallback& callback)
public:
    virtual ~IWatchPlugin() {}

public:
    // Runs until stop_fd is readable.
    virtual void start(int stop_fd) = 0;
};
