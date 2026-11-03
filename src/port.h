#pragma once

#include <stdint.h>

#include <charconv>
#include <string>
#include <system_error>

// A port is a whole number from 1 to 65535, written as decimal digits and nothing else. Anything
// else is refused rather than read as a different port: atoi made "abc" port 0, which the kernel
// replaces with an ephemeral one, and a short cast made 70000 port 4464.
inline bool parse_port(const std::string& text, uint16_t& port)
{
    const char* begin = text.data();
    const char* end = begin + text.size();
    unsigned long value = 0;
    auto result = std::from_chars(begin, end, value, 10);
    if (result.ec != std::errc() || result.ptr != end || value < 1 || value > 65535) {
        return false;
    }
    port = static_cast<uint16_t>(value);
    return true;
}
