#pragma once

#include <stdint.h>

#include <memory>

#include "receive.h"

class UDPReceivePluginImpl;

class UDPReceivePlugin : public IReceivePlugin {
public:
    UDPReceivePlugin(uint16_t port, const ReceiveCallback& callback);

public:
    virtual ~UDPReceivePlugin();

public:
    virtual void start(int stop_fd);

private:
    std::unique_ptr<UDPReceivePluginImpl> m_impl;
};
