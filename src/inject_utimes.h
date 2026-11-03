#pragma once

#include "inject.h"
#include "receive_paths.h"

class UtimesInjectPlugin : public IInjectPlugin {
public:
    explicit UtimesInjectPlugin(const std::vector<std::string>& roots = { });
    virtual ~UtimesInjectPlugin();

public:
    virtual void inject(const std::vector<std::string>& paths);

private:
    ReceivePaths m_paths;
    bool m_warned_attribute = false;
};
