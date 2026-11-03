#include <CoreServices/CoreServices.h>

#include "watch_fsevents.h"

class FSEventsWatchPluginImpl {
public:
    FSEventStreamRef stream;

public:
    CFRunLoopRef run_loop;

public:
    CFRunLoopTimerRef idle_timer;

public:
    WatchCallback callback;
};

static const CFTimeInterval idle_interval = 0.1;

static void idle_timer_callback(__unused CFRunLoopTimerRef timer, void* info)
{
    static_cast<FSEventsWatchPluginImpl*>(info)->callback({});
}

static void fsevents_callback(__unused ConstFSEventStreamRef stream,
    void* client_callback_info,
    size_t num_events,
    void* event_paths,
    __unused const FSEventStreamEventFlags event_flags[],
    __unused const FSEventStreamEventId event_ids[])
{
    FSEventsWatchPluginImpl* impl = static_cast<FSEventsWatchPluginImpl*>(client_callback_info);

    std::vector<std::string> paths;

    for (size_t i = 0; i < num_events; ++i) {
        const char* path = static_cast<const char**>(event_paths)[i];
        paths.emplace_back(path);
    }

    impl->callback(paths);
}

static void stop_callback(
    __unused CFFileDescriptorRef descriptor, __unused CFOptionFlags flags, __unused void* info)
{
    CFRunLoopStop(CFRunLoopGetCurrent());
}

FSEventsWatchPlugin::FSEventsWatchPlugin(
    const std::vector<std::string>& paths, const WatchCallback& callback)
    : m_impl(new FSEventsWatchPluginImpl())
{

    CFMutableArrayRef paths_array = CFArrayCreateMutable(kCFAllocatorDefault, 0, &kCFTypeArrayCallBacks);

    for (auto& path : paths) {
        CFStringRef path_str
            = CFStringCreateWithCString(kCFAllocatorDefault, path.c_str(), kCFStringEncodingUTF8);

        CFArrayAppendValue(paths_array, path_str);

        CFRelease(path_str);
    }

    FSEventStreamContext context = { 1, m_impl.get(), nullptr, nullptr, nullptr };

    FSEventStreamRef stream = FSEventStreamCreate(kCFAllocatorDefault,
        fsevents_callback,
        &context,
        paths_array,
        kFSEventStreamEventIdSinceNow,
        0.1,
        kFSEventStreamCreateFlagFileEvents);

    if (!stream) {
        throw std::runtime_error("Could not create FSEventStream");
    }

    m_impl->stream = stream;
    m_impl->run_loop = CFRunLoopGetCurrent();
    m_impl->callback = callback;
}

FSEventsWatchPlugin::~FSEventsWatchPlugin()
{
    if (m_impl->idle_timer) {
        CFRunLoopTimerInvalidate(m_impl->idle_timer);
        CFRelease(m_impl->idle_timer);
    }

    FSEventStreamStop(m_impl->stream);

    FSEventStreamUnscheduleFromRunLoop(m_impl->stream, m_impl->run_loop, kCFRunLoopCommonModes);

    FSEventStreamInvalidate(m_impl->stream);

    FSEventStreamRelease(m_impl->stream);
}

void FSEventsWatchPlugin::start(int stop_fd)
{
    FSEventStreamScheduleWithRunLoop(m_impl->stream, m_impl->run_loop, kCFRunLoopCommonModes);

    Boolean started = FSEventStreamStart(m_impl->stream);

    if (!started) {
        throw std::runtime_error("Could not start FSEventStream");
    }

    CFRunLoopTimerContext timer_context = { 0, m_impl.get(), nullptr, nullptr, nullptr };

    m_impl->idle_timer = CFRunLoopTimerCreate(kCFAllocatorDefault,
        CFAbsoluteTimeGetCurrent() + idle_interval,
        idle_interval,
        0,
        0,
        idle_timer_callback,
        &timer_context);

    CFRunLoopAddTimer(m_impl->run_loop, m_impl->idle_timer, kCFRunLoopCommonModes);

    // Stopping the run loop is not safe from a signal handler. Here it runs on the run loop itself.
    CFFileDescriptorRef stop_descriptor
        = CFFileDescriptorCreate(kCFAllocatorDefault, stop_fd, false, stop_callback, nullptr);

    if (!stop_descriptor) {
        throw std::runtime_error("Could not watch the stop pipe");
    }

    CFFileDescriptorEnableCallBacks(stop_descriptor, kCFFileDescriptorReadCallBack);

    CFRunLoopSourceRef stop_source
        = CFFileDescriptorCreateRunLoopSource(kCFAllocatorDefault, stop_descriptor, 0);

    CFRunLoopAddSource(m_impl->run_loop, stop_source, kCFRunLoopCommonModes);

    CFRunLoopRun();

    CFRunLoopRemoveSource(m_impl->run_loop, stop_source, kCFRunLoopCommonModes);

    CFRelease(stop_source);

    CFFileDescriptorInvalidate(stop_descriptor);

    CFRelease(stop_descriptor);
}
