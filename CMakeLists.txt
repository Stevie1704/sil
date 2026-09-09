cmake_minimum_required(VERSION 3.24)
project(sil LANGUAGES C CXX)

enable_testing()

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

include(FetchContent)

FetchContent_Declare(nlohmann_json
  URL https://github.com/nlohmann/json/releases/download/v3.12.0/json.tar.xz
  DOWNLOAD_EXTRACT_TIMESTAMP ON)
FetchContent_MakeAvailable(nlohmann_json)

FetchContent_Declare(mcap
  URL https://github.com/foxglove/mcap/archive/refs/tags/releases/cpp/v2.1.3.tar.gz
  DOWNLOAD_EXTRACT_TIMESTAMP ON)
FetchContent_MakeAvailable(mcap)

add_library(mcap_headers INTERFACE)
target_include_directories(mcap_headers INTERFACE ${mcap_SOURCE_DIR}/cpp/mcap/include)
# Recording is uncompressed in v1: no lz4/zstd dependency in the kernel.
target_compile_definitions(mcap_headers INTERFACE
  MCAP_COMPRESSION_NO_LZ4 MCAP_COMPRESSION_NO_ZSTD)

add_executable(sil-run
  kernel/src/main.cpp
  kernel/src/manifest.cpp
  kernel/src/interceptor.cpp
  kernel/src/engine.cpp
  kernel/src/clock_shim.cpp
  kernel/src/mapped_region.cpp
  kernel/src/native_participant.cpp
  kernel/src/process_participant.cpp
  kernel/src/mcap_recording.cpp
  kernel/src/replayer.cpp)
target_include_directories(sil-run PRIVATE include)
target_link_libraries(sil-run PRIVATE nlohmann_json::nlohmann_json mcap_headers)

# The interceptor plan is pure run-boundary arithmetic with a small interface,
# so it has a focused C++ test target in addition to the end-to-end suite.
add_executable(sil_interceptor_test
  tests/interceptor_test.cpp
  kernel/src/interceptor.cpp
  kernel/src/manifest.cpp)
target_include_directories(sil_interceptor_test PRIVATE kernel/src)
target_link_libraries(sil_interceptor_test PRIVATE nlohmann_json::nlohmann_json)
add_test(NAME sil_interceptor_plan COMMAND sil_interceptor_test)

# Unsigned virtual-time addition is the one arithmetic boundary every
# scheduling and delivery decision rests on, so its table of edge cases is a
# focused C++ test rather than an end-to-end one.
add_executable(sil_virtual_time_test tests/virtual_time_test.cpp)
target_include_directories(sil_virtual_time_test PRIVATE kernel/src)
add_test(NAME sil_virtual_time COMMAND sil_virtual_time_test)

# The mapped region owns a temp file's mapping, descriptor, and directory entry.
# Its release-on-destruction and move semantics are invisible at the run
# boundary, so they get their own focused C++ test target.
add_executable(sil_mapped_region_test
  tests/mapped_region_test.cpp
  kernel/src/mapped_region.cpp)
target_include_directories(sil_mapped_region_test PRIVATE kernel/src)
add_test(NAME sil_mapped_region COMMAND sil_mapped_region_test)

# --- generated message headers (single typed contract per DESIGN #9) ---
set(SIL_GEN_DIR ${CMAKE_BINARY_DIR}/generated)
add_custom_command(
  OUTPUT ${SIL_GEN_DIR}/toy_messages.h
  COMMAND python3 ${CMAKE_SOURCE_DIR}/tools/silschema.py
          ${CMAKE_SOURCE_DIR}/schemas/toy.json ${SIL_GEN_DIR}/toy_messages.h
  DEPENDS tools/silschema.py schemas/toy.json)
add_custom_target(toy_messages DEPENDS ${SIL_GEN_DIR}/toy_messages.h)

# --- virtual clock shim (preload library + probe, issue #26) ---
# One extra shared library built alongside the runner. Opaque POSIX process
# participants preload it (LD_PRELOAD / DYLD_INSERT_LIBRARIES) so their own
# clock reads return stepped virtual time. It knows nothing of the kernel.
# SHARED (not MODULE): macOS DYLD_INSERT_LIBRARIES rejects a Mach-O bundle and
# requires a real dylib; LD_PRELOAD accepts a shared object on Linux.
add_library(sil_clock_shim SHARED shim/clock_shim.c)
target_include_directories(sil_clock_shim PRIVATE include)
# Located next to sil-run so the runner can find it relative to itself.
set_target_properties(sil_clock_shim PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}
  LIBRARY_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR})
if(NOT APPLE)
  # Linux: the region-absent fallback resolves the real libc symbol via
  # dlsym(RTLD_NEXT, ...), which needs libdl.
  target_link_libraries(sil_clock_shim PRIVATE dl)
endif()

# Probe executable: exercises every interposed API for the shim's unit tests.
add_executable(sil_clock_probe shim/clock_probe.c)
target_include_directories(sil_clock_probe PRIVATE include)

# --- toy participants (walking-skeleton fixtures) ---
add_library(toy_producer MODULE participants/toy_producer.cpp)
add_library(toy_accumulator MODULE participants/toy_accumulator.cpp)
# Throws across the C ABI seam on purpose, so exception containment is tested
# through a real shared library (issue #65).
add_library(toy_thrower MODULE participants/toy_thrower.cpp)
foreach(toy toy_producer toy_accumulator toy_thrower)
  add_dependencies(${toy} toy_messages)
  target_include_directories(${toy} PRIVATE include ${SIL_GEN_DIR})
  target_link_libraries(${toy} PRIVATE nlohmann_json::nlohmann_json)
  # Platform-neutral name so the same manifest works on macOS and Linux.
  set_target_properties(${toy} PROPERTIES PREFIX "" SUFFIX ".silp")
endforeach()
