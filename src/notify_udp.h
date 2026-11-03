#pragma once

#include <stdint.h>

#include <memory>

#include "notify.h"

class UDPNotifyPluginImpl;

class UDPNotifyPlugin : public INotifyPlugin {
public:
    UDPNotifyPlugin(const std::string& addr, uint16_t port);

public:
    virtual ~UDPNotifyPlugin();

public:
    virtual void notify(const std::vector<std::string>& paths);

private:
    std::unique_ptr<UDPNotifyPluginImpl> m_impl;
};
