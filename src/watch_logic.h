#pragma once

#include <sys/stat.h>
#include <time.h>

#include <algorithm>
#include <chrono>
#include <cstdint>
#include <functional>
#include <string>
#include <unordered_map>
#include <vector>

typedef std::chrono::steady_clock Clock;
typedef Clock::time_point TimePoint;
typedef std::vector<std::pair<std::string, std::string>> PrefixMap;

inline std::string replace_prefix(const PrefixMap& prefix_map, std::string src)
{
    for (const auto& pair : prefix_map) {
        if (src.substr(0, pair.first.size()) == pair.first) {
            return pair.second + src.substr(pair.first.size());
        }
    }
    return src;
}

// What a change to a path alters. ctime is left out on purpose: the receiver's extended attribute
// pair changes only ctime, and over a shared folder the host sees that change as a new event.
struct PathState {
    bool exists;
    uint64_t inode;
    uint64_t size;
    int64_t mtime_seconds;
    int64_t mtime_nanoseconds;
    uint32_t mode;
    uint32_t owner;
    uint32_t group;
    // The timestamp is too coarse to tell this state from a later write of the same size. It says
    // how far to trust the state and is not part of it.
    bool ambiguous;
};

inline bool operator==(const PathState& a, const PathState& b)
{
    return a.exists == b.exists && a.inode == b.inode && a.size == b.size
        && a.mtime_seconds == b.mtime_seconds && a.mtime_nanoseconds == b.mtime_nanoseconds
        && a.mode == b.mode && a.owner == b.owner && a.group == b.group;
}

inline bool operator!=(const PathState& a, const PathState& b) { return !(a == b); }

// The longest time a filesystem's mtime can stand still while the file changes: FAT stores two
// seconds, HFS+ and ext3 store one.
constexpr int64_t coarse_mtime_seconds = 2;

inline PathState read_path_state(const std::string& path)
{
    PathState state {};
    struct stat status;
    if (lstat(path.c_str(), &status) != 0) {
        return state;
    }

#ifdef __APPLE__
    const timespec mtime = status.st_mtimespec;
#else
    const timespec mtime = status.st_mtim;
#endif

    state.exists = true;
    state.inode = static_cast<uint64_t>(status.st_ino);
    state.size = static_cast<uint64_t>(status.st_size);
    state.mtime_seconds = static_cast<int64_t>(mtime.tv_sec);
    state.mtime_nanoseconds = static_cast<int64_t>(mtime.tv_nsec);
    state.mode = static_cast<uint32_t>(status.st_mode);
    state.owner = static_cast<uint32_t>(status.st_uid);
    state.group = static_cast<uint32_t>(status.st_gid);

    // A whole number of milliseconds means the filesystem stores no finer time than that.
    timespec wall_clock;
    if (mtime.tv_nsec % 1000000 == 0 && clock_gettime(CLOCK_REALTIME, &wall_clock) == 0) {
        const int64_t age = static_cast<int64_t>(wall_clock.tv_sec) - state.mtime_seconds;
        state.ambiguous = age >= 0 && age < coarse_mtime_seconds;
    }
    return state;
}

typedef std::function<PathState(const std::string&)> PathStateReader;

// Decides which of a watcher's events to forward. Forwarding raises an event on the forwarded path,
// and over a shared folder that event comes back here. Going by the path alone, that echo cannot be
// told from a real change: drop it and a real change is lost, forward it and the two sides feed
// each other for ever. The state of the path tells them apart, because the echo changes nothing in
// it.
//
// - An event for a path not forwarded in the last repeat_window is forwarded at once.
// - An event inside that window that changed the path is forwarded when the window ends, so a busy
//   file is forwarded once per window, the last change included.
// - An event inside that window that changed nothing is an echo, and is dropped.
//
// A timestamp too coarse to show a later write of the same size leaves that last case in doubt. The
// path is then forwarded once more, when the timestamp has moved past doubt, and that bounds it.
class RepeatFilter {
public:
    explicit RepeatFilter(PathStateReader reader = read_path_state)
        : m_reader(reader)
    {
    }

public:
    // Call with an empty batch whenever nothing has arrived, so that delayed paths go out.
    std::vector<std::string> update(const std::vector<std::string>& paths, TimePoint now)
    {
        std::vector<std::string> out_paths = release_due(now);

        for (const auto& path : paths) {
            auto entry = m_entries.find(path);
            if (entry == m_entries.end()) {
                Entry forwarded { m_reader(path), now, false, now };
                m_entries.emplace(path, forwarded);
                out_paths.emplace_back(path);
                continue;
            }

            Entry& seen = entry->second;
            const TimePoint window_end = seen.forwarded_at + repeat_window;
            if (m_reader(path) != seen.state) {
                seen.due = seen.pending ? std::min(seen.due, window_end) : window_end;
                seen.pending = true;
            }
            else if (seen.state.ambiguous && !seen.pending) {
                seen.due = seen.forwarded_at + std::chrono::seconds(coarse_mtime_seconds);
                seen.pending = true;
            }
        }

        return out_paths;
    }

public:
    size_t tracked() const { return m_entries.size(); }

public:
    static constexpr std::chrono::seconds repeat_window { 1 };

private:
    struct Entry {
        PathState state;
        TimePoint forwarded_at;
        bool pending;
        TimePoint due;
    };

private:
    std::vector<std::string> release_due(TimePoint now)
    {
        std::vector<std::string> out_paths;
        for (auto it = m_entries.begin(); it != m_entries.end();) {
            Entry& seen = it->second;
            if (seen.pending && seen.due <= now) {
                seen.state = m_reader(it->first);
                seen.forwarded_at = now;
                seen.pending = false;
                out_paths.emplace_back(it->first);
                ++it;
            }
            else if (!seen.pending && seen.forwarded_at + repeat_window <= now) {
                it = m_entries.erase(it);
            }
            else {
                ++it;
            }
        }
        return out_paths;
    }

private:
    PathStateReader m_reader;

private:
    std::unordered_map<std::string, Entry> m_entries;
};
