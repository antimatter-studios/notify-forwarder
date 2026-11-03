#pragma once

#include "inject.h"

class UtimesInjectPlugin : public IInjectPlugin {
public:
    virtual ~UtimesInjectPlugin();

public:
    virtual void inject(const std::vector<std::string>& paths);

private:
    bool m_warned_attribute = false;
};
