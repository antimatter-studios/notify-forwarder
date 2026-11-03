#pragma once

#include <dirent.h>
#include <string.h>
#include <sys/stat.h>

#include <memory>
#include <string>
#include <vector>

// Appends every path beneath a directory, for when its events were lost and nothing else can say
// what changed. Symlinks are named and not followed. What vanishes during the walk is left out, as
// a changing tree makes ordinary.
inline void append_tree(const std::string& directory, std::vector<std::string>& paths)
{
    std::unique_ptr<DIR, int (*)(DIR*)> dir(opendir(directory.c_str()), closedir);
    if (!dir) {
        return;
    }

    while (const dirent* entry = readdir(dir.get())) {
        if (strcmp(entry->d_name, ".") == 0 || strcmp(entry->d_name, "..") == 0) {
            continue;
        }

        const std::string path = directory + "/" + entry->d_name;
        paths.emplace_back(path);

        bool is_directory = entry->d_type == DT_DIR;
        if (entry->d_type == DT_UNKNOWN) {
            struct stat status;
            is_directory = lstat(path.c_str(), &status) == 0 && S_ISDIR(status.st_mode);
        }
        if (is_directory) {
            append_tree(path, paths);
        }
    }
}
