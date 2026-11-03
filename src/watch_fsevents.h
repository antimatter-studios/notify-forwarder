#pragma once

#include <memory>

#include "watch.h"

#include <string>
#include <vector>

class FSEventsWatchPluginImpl;

class FSEventsWatchPlugin : public IWatchPlugin {
public:
    FSEventsWatchPlugin(const std::vector<std::string>& paths, const WatchCallback& callback);

public:
    ~FSEventsWatchPlugin();

public:
    virtual void start(int stop_fd);

private:
    std::unique_ptr<FSEventsWatchPluginImpl> m_impl;
};
