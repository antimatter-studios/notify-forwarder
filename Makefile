HOST_OS := $(shell uname -s)
HOST_ARCH := $(shell uname -m)

TARGET_OS ?= $(HOST_OS)
TARGET_ARCH ?= $(HOST_ARCH)

SUPPORTED_TARGETS := darwin_arm64 darwin_x86_64 linux_arm64 linux_armv7l linux_x86_64
DEFAULT_TARGETS := $(SUPPORTED_TARGETS)

.PHONY: $(SUPPORTED_TARGETS)

define add_target
.PHONY: $1
$1:
	@echo "Building $1"
	@$(MAKE) --no-print-directory TARGET_OS=$2 TARGET_ARCH=$3 ZIG_ARCH=$(if $(filter arm64,$3),aarch64,$(if $(filter armv7l,$3),arm,x86_64)) build
endef

$(eval $(call add_target,darwin_arm64,Darwin,arm64))
$(eval $(call add_target,darwin_x86_64,Darwin,x86_64))
$(eval $(call add_target,linux_arm64,Linux,arm64))
$(eval $(call add_target,linux_armv7l,Linux,armv7l))
$(eval $(call add_target,linux_x86_64,Linux,x86_64))

ifneq ($(strip $(MAKECMDGOALS)),)
ifneq ($(filter all,$(MAKECMDGOALS)),)
# keep default list
else ifneq ($(filter $(SUPPORTED_TARGETS) build clean,$(MAKECMDGOALS)),)
# explicit targets already provided
else
DEFAULT_TARGETS := build
endif
endif

ifneq ($(filter command environment environment\ override,$(origin TARGET_OS)),)
DEFAULT_TARGETS := build
endif
ifneq ($(filter command environment environment\ override,$(origin TARGET_ARCH)),)
DEFAULT_TARGETS := build
endif

.DEFAULT_GOAL := all

OUT_DIR = _build/$(TARGET_OS)_$(TARGET_ARCH)
OBJ_DIR = $(OUT_DIR)/obj
OUT = $(OUT_DIR)/notify-forwarder

COMMON_SRCS := \
	src/watch_null.cc \
	src/notify_udp.cc \
	src/receive_udp.cc \
	src/receive_paths.cc \
	src/inject_utimes.cc \
	src/main.cc \
	src/main_receive.cc \
	src/main_watch.cc

SRCS := $(COMMON_SRCS)
WATCH_PLUGIN_TYPE := NullWatchPlugin
CXXWARNINGS := -Wall -Wextra
PLATFORM_CXXFLAGS :=
PLATFORM_LDFLAGS :=
CXX_TOOLCHAIN := c++

ZIG_ARCH := $(if $(filter arm64,$(TARGET_ARCH)),aarch64,$(if $(filter armv7l,$(TARGET_ARCH)),arm,x86_64))

ifeq ($(TARGET_OS),Darwin)
	SRCS += src/watch_fsevents.cc
	WATCH_PLUGIN_TYPE := FSEventsWatchPlugin
	CXXWARNINGS := -Weverything -Wno-c++98-compat -Wno-weak-vtables -Wno-padded
	PLATFORM_CXXFLAGS += -stdlib=libc++
	PLATFORM_LDFLAGS += -framework CoreServices
	SDKROOT ?= $(shell xcrun --sdk macosx --show-sdk-path 2>/dev/null)
	ifneq ($(SDKROOT),)
		PLATFORM_CXXFLAGS += -isysroot $(SDKROOT)
		PLATFORM_LDFLAGS += -isysroot $(SDKROOT)
	endif
	ARCHFLAG := $(if $(filter arm64,$(TARGET_ARCH)),-arch arm64,-arch x86_64)
	PLATFORM_CXXFLAGS += $(ARCHFLAG)
	PLATFORM_LDFLAGS += $(ARCHFLAG)
	CXX_TOOLCHAIN := xcrun --sdk macosx clang++
else ifeq ($(TARGET_OS),Linux)
	SRCS += src/watch_inotify.cc
	WATCH_PLUGIN_TYPE := InotifyWatchPlugin
	ZIG_TARGET := $(ZIG_ARCH)-linux-$(if $(filter arm,$(ZIG_ARCH)),gnueabihf,gnu)
	CXX_TOOLCHAIN := zig c++ -target $(ZIG_TARGET)
else
	$(error Unsupported TARGET_OS '$(TARGET_OS)')
endif

CXXFLAGS = -MD -g -O0 -std=c++17 $(CXXWARNINGS) -DWATCH_PLUGIN_TYPE=$(WATCH_PLUGIN_TYPE) $(PLATFORM_CXXFLAGS) $(EXTRA_CXXFLAGS)

ifeq ($(origin CXX), default)
CXX := $(CXX_TOOLCHAIN)
else
CXX ?= $(CXX_TOOLCHAIN)
endif

OBJECTS = $(patsubst src/%.cc,$(OBJ_DIR)/%.o,$(SRCS))
DEPFILES = $(OBJECTS:.o=.d)

all: $(DEFAULT_TARGETS)

build: $(OUT)

TEST_OUT = $(OUT_DIR)/unit-tests

$(TEST_OUT): tests/unit.cc src/inject_utimes.cc src/inject_utimes.h src/inject.h src/utils.h src/watch_logic.h src/receive_paths.cc src/receive_paths.h
	@mkdir -p $(dir $@)
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) -Isrc tests/unit.cc src/inject_utimes.cc src/receive_paths.cc $(PLATFORM_LDFLAGS) -o $@

RECEIVER_TEST_OUT = $(OUT_DIR)/receiver-tests

$(RECEIVER_TEST_OUT): tests/receiver.cc src/inject_utimes.cc src/inject_utimes.h src/inject.h src/receive_paths.cc src/receive_paths.h
	@mkdir -p $(dir $@)
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) -Isrc tests/receiver.cc src/inject_utimes.cc src/receive_paths.cc $(PLATFORM_LDFLAGS) -o $@

RACE_TEST_OUT = $(OUT_DIR)/receiver-race-tests

$(RACE_TEST_OUT): tests/receiver_race.cc src/inject_utimes.cc src/inject_utimes.h src/inject.h src/receive_paths.cc src/receive_paths.h
	@mkdir -p $(dir $@)
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) -Isrc tests/receiver_race.cc src/receive_paths.cc $(PLATFORM_LDFLAGS) -o $@

PATH_TEST_OUT = $(OUT_DIR)/path-tests

$(PATH_TEST_OUT): tests/paths.cc src/receive_paths.cc src/receive_paths.h
	@mkdir -p $(dir $@)
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) -Isrc tests/paths.cc src/receive_paths.cc $(PLATFORM_LDFLAGS) -o $@

test: build $(TEST_OUT) $(RECEIVER_TEST_OUT) $(RACE_TEST_OUT) $(PATH_TEST_OUT)
	status=0; for suite in $(TEST_OUT) $(RECEIVER_TEST_OUT) $(RACE_TEST_OUT) $(PATH_TEST_OUT); do $$suite || status=1; done; python3 tests/receiver_cli.py $(OUT) || status=1; python3 tests/forwarding.py $(OUT) || status=1; exit $$status

TREE_TEST_OUT = $(OUT_DIR)/inotify-tree-tests

$(TREE_TEST_OUT): tests/inotify_tree.cc src/watch_inotify.cc src/watch_inotify.h src/watch.h
	@mkdir -p $(dir $@)
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) -Isrc tests/inotify_tree.cc $(PLATFORM_LDFLAGS) -o $@

test-inotify: build $(TREE_TEST_OUT)
	status=0; $(TREE_TEST_OUT) || status=1; python3 tests/inotify_tree.py $(OUT) || status=1; exit $$status
	python3 tests/inotify.py $(OUT)
	python3 tests/kernel_events.py

$(OUT): $(OBJECTS)
	@mkdir -p $(dir $@)
	$(CXX) $(CFLAGS) $(LDFLAGS) $(PLATFORM_LDFLAGS) -o $@ $(OBJECTS)
$(OBJ_DIR):
	@mkdir -p $@

$(OBJ_DIR)/%.o: src/%.cc | $(OBJ_DIR)
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) -c $< -o $@

clean:
	rm -rf _build

test-receiver-root: build
	sudo python3 tests/receiver_root.py $(OUT)

.PHONY: all clean build test test-inotify test-receiver-root $(SUPPORTED_TARGETS)

-include $(DEPFILES)
