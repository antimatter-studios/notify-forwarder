#include <stdlib.h>
#include <string.h>
#include <sys/param.h>
#include <sysexits.h>

#include "main.h"
#include "watch_logic.h"

#include "utils.h"

#include "watch.h"
#include "watch_fsevents.h"
#include "watch_inotify.h"
#include "watch_null.h"

#include "notify.h"
#include "notify_udp.h"

#include "platform.h"

static IWatchPlugin* gCurrentWatchInstance = nullptr;

static void handle_sigint(__unused int signal)
{
    if (gCurrentWatchInstance) {
        gCurrentWatchInstance->stop();
    }
    else {
        exit(EX_OK);
    }
}

static void print_usage(char* argv0)
{
    fprintf(stderr, "Usage: %s watch [-c host[:port]] (<from> <to>)...\n", argv0);
    fprintf(stderr, "\n");
    fprintf(stderr, "  (<from> <to>)   pairs of paths, the first being the local path to watch\n");
    fprintf(stderr, "                  and the second one being the remote path\n");
    fprintf(stderr, "  -c host:[port]  specifies where to connect. Defaults to 127.0.0.1:29324\n");
    fprintf(stderr, "\n");
}

int main_watch(char* argv0, int argc, char** argv)
{
    std::vector<std::string> watch_paths;
    std::vector<std::pair<std::string, std::string>> prefix_map;
    FeedbackMap feedback_map;

    std::string dest_addr = "127.0.0.1";
    short port = 29324;

    if (argc && strcmp(argv[0], "-c") == 0) {
        if (argc < 2) {
            print_usage(argv0);
            return EX_USAGE;
        }

        std::string param = argv[1];
        size_t pos = param.find(':');

        if (pos == std::string::npos) {
            dest_addr = param;
        }
        else {
            dest_addr = param.substr(0, pos);
            port = static_cast<short>(atoi(param.substr(pos + 1).c_str()));
        }

        argc -= 2;
        argv += 2;
    }

    if (argc % 2 != 0 || argc == 0) {
        print_usage(argv0);
        return EX_USAGE;
    }

    for (int i = 0; i < argc; i += 2) {
        char out_path[MAXPATHLEN];
        char* in_path = argv[i];

        if (!realpath(in_path, out_path)) {
            throw std::runtime_error("Could not resolve path");
        }

        watch_paths.emplace_back(out_path);
        prefix_map.emplace_back(out_path, argv[i + 1]);
    }

    UDPNotifyPlugin notify_plugin(dest_addr, port);

    WatchCallback callback
        = [&notify_plugin, &prefix_map, &feedback_map](std::vector<std::string> paths) {
              std::vector<std::string> out_paths;

              for (auto& path : paths) {
                  out_paths.emplace_back(replace_prefix(prefix_map, path));
              }

              notify_plugin.notify(filter_feedback(feedback_map, out_paths));
          };

    WATCH_PLUGIN_TYPE plugin(watch_paths, callback);
    SignalOverride(SIGINT, handle_sigint);
    VariableOverrideGuard<IWatchPlugin*> var_guard(gCurrentWatchInstance, &plugin);

    plugin.start();

    return EX_OK;
}
