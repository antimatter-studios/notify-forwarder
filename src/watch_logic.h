#pragma once

#include <chrono>
#include <string>
#include <unordered_map>
#include <vector>

typedef std::chrono::system_clock Clock;
typedef std::chrono::time_point<Clock> TimePoint;
typedef std::unordered_map<std::string, TimePoint> FeedbackMap;
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

inline void clear_expired(FeedbackMap& feedback_map)
{
    TimePoint now = Clock::now();
    for (auto it = feedback_map.begin(); it != feedback_map.end();) {
        if (it->second < now) {
            it = feedback_map.erase(it);
        }
        else {
            ++it;
        }
    }
}

inline std::vector<std::string> filter_feedback(
    FeedbackMap& feedback_map, std::vector<std::string> paths)
{
    clear_expired(feedback_map);
    std::vector<std::string> out_paths;
    for (const auto& path : paths) {
        if (!feedback_map.count(path)) {
            out_paths.emplace_back(path);
        }
        feedback_map[path] = Clock::now() + std::chrono::seconds(1);
    }
    return out_paths;
}
