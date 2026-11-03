#include <cstdio>
#include <stdexcept>

#include "inject_utimes.h"
#include "utils.h"
#include "watch_logic.h"

static int failures = 0;

static void check(bool condition, const char* name)
{
    printf("%s %s\n", condition ? "PASS" : "FAIL", name);
    if (!condition) {
        ++failures;
    }
}

int main()
{
    PrefixMap prefixes { { "/host/project", "/guest/project" }, { "/other", "/second" } };
    check(replace_prefix(prefixes, "/host/project/file") == "/guest/project/file",
        "prefix mapping preserves the suffix");
    check(replace_prefix(prefixes, "/other/file") == "/second/file",
        "prefix mapping checks later entries");
    check(replace_prefix(prefixes, "/unmapped/file") == "/unmapped/file",
        "unmapped paths remain unchanged");
    check(replace_prefix({}, "/file") == "/file", "empty prefix maps are accepted");

    FeedbackMap feedback;
    check(filter_feedback(feedback, { "/file" }) == std::vector<std::string> { "/file" },
        "first notification passes");
    check(filter_feedback(feedback, { "/file" }).empty(), "immediate feedback is suppressed");
    feedback["/file"] = Clock::now() - std::chrono::seconds(1);
    check(filter_feedback(feedback, { "/file" }) == std::vector<std::string> { "/file" },
        "expired feedback passes again");
    feedback.clear();
    check(filter_feedback(feedback, { "/file", "/file", "/other" })
            == std::vector<std::string> { "/file", "/other" },
        "duplicate notifications are filtered within one batch");
    check(filter_feedback(feedback, {}).empty(), "empty notification batches are accepted");

    int value = 4;
    {
        VariableOverrideGuard<int> guard(value, 8);
        check(value == 8, "override guard installs the replacement");
    }
    check(value == 4, "override guard restores the previous value");

    UtimesInjectPlugin injector;
    injector.inject({});
    injector.inject({ "/notify-forwarder-nonexistent-parent/missing" });
    check(true, "receiver tolerates empty batches and missing files");

    return failures ? 1 : 0;
}
