#include <stdlib.h>
#include <string.h>
#include <sysexits.h>

#include <memory>
#include <stdexcept>
#include <string>
#include <vector>

#include "main.h"
#include "port.h"
#include "utils.h"

#include "receive.h"
#include "receive_udp.h"

#include "inject.h"
#include "inject_utimes.h"

static void print_usage(char* argv0)
{
    fprintf(stderr, "Usage: %s receive [-p port] [root ...]\n", argv0);
    fprintf(stderr, "\n");
    fprintf(stderr, "  -p port   the UDP port to listen on, from 1 to 65535. Defaults to 29324\n");
    fprintf(stderr, "  root ...  only regular files resolving beneath these directories are acted on\n");
    fprintf(stderr, "\n");
}

int main_receive(char* argv0, int argc, char** argv)
{
    uint16_t port = 29324;
    std::vector<std::string> roots;

    while (argc) {
        if (argc >= 2 && strcmp(argv[0], "-p") == 0) {
            if (!parse_port(argv[1], port)) {
                fprintf(stderr,
                    "notify-forwarder: invalid port '%s': must be a whole number from 1 to 65535\n",
                    argv[1]);
                print_usage(argv0);
                return EX_USAGE;
            }
            argc -= 2;
            argv += 2;
        }
        else if (argv[0][0] != '-') {
            roots.emplace_back(argv[0]);
            --argc;
            ++argv;
        }
        else {
            print_usage(argv0);
            return EX_USAGE;
        }
    }

    if (roots.empty()) {
        fprintf(stderr,
            "WARNING: no receive roots configured; any regular file may be notified. "
            "Configure guest roots to restrict access.\n");
    }

    std::unique_ptr<UtimesInjectPlugin> injector;
    try {
        injector.reset(new UtimesInjectPlugin(roots));
    }
    catch (const std::exception& error) {
        fprintf(stderr, "%s\n", error.what());
        return EX_USAGE;
    }

    ReceiveCallback callback
        = [&injector](std::vector<std::string> paths) { injector->inject(paths); };

    StopSignals stop_signals;
    UDPReceivePlugin receive_plugin(port, callback);

    receive_plugin.start(stop_signals.fd());

    return EX_OK;
}
