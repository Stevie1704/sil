cmake_minimum_required(VERSION 3.24)
project(sil LANGUAGES C CXX)

enable_testing()

include(GNUInstallDirs)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

include(FetchContent)

FetchContent_Declare(nlohmann_json
  URL https://github.com/nlohmann/json/releases/download/v3.12.0/json.tar.xz
  URL_HASH SHA256=42f6e95cad6ec532fd372391373363b62a14af6d771056dbfc86160e6dfff7aa
  DOWNLOAD_EXTRACT_TIMESTAMP ON)
FetchContent_MakeAvailable(nlohmann_json)

FetchContent_Declare(mcap
  URL https://github.com/foxglove/mcap/archive/refs/tags/releases/cpp/v2.1.3.tar.gz
  URL_HASH SHA256=181a7c52cc982444cdef533087b321538b025fcc5eec258e0489d68b9ce53ded
  DOWNLOAD_EXTRACT_TIMESTAMP ON)
FetchContent_MakeAvailable(mcap)

add_library(mcap_headers INTERFACE)
target_include_directories(mcap_headers INTERFACE ${mcap_SOURCE_DIR}/cpp/mcap/include)
# Recording is uncompressed in v1: no lz4/zstd dependency in the kernel.
target_compile_definitions(mcap_headers INTERFACE
  MCAP_COMPRESSION_NO_LZ4 MCAP_COMPRESSION_NO_ZSTD)

set(SIL_KERNEL_SOURCES
  kernel/src/main.cpp
  kernel/src/manifest.cpp
  kernel/src/interceptor.cpp
  kernel/src/engine.cpp
  kernel/src/clock_shim.cpp
  kernel/src/mapped_region.cpp
  kernel/src/owned_directory.cpp
  kernel/src/native_participant.cpp
  kernel/src/channel_arenas.cpp
  kernel/src/process_participant.cpp
  kernel/src/mcap_recording.cpp
  kernel/src/replayer.cpp)

add_executable(sil-run ${SIL_KERNEL_SOURCES})
target_include_directories(sil-run PRIVATE include)
target_link_libraries(sil-run PRIVATE nlohmann_json::nlohmann_json mcap_headers)

# The same kernel with routing test instrumentation compiled in (issues #61,
# #75): payload-copy counts plus subscriber-route queue counters.
# It is a second binary rather than a runtime switch so the production runner
# carries no counter, no branch, and no reporting code on the routing path; the
# routing baseline reports copy counts from this one and wall-clock from
# sil-run. See docs/bench/large-message-routing-baseline.md.
# Per #63 this is the whole counter surface: a Run reproduces exactly, so a
# question about pool or route pressure is answered by re-running it here
# rather than by exporting metrics from the production runner. That the two
# binaries agree on exit code and Recording bytes is asserted per determinism
# fixture in tests/test_determinism.py (#82), not assumed here.
add_executable(sil-run-instrumented
  ${SIL_KERNEL_SOURCES} kernel/src/copy_counters.cpp)
target_include_directories(sil-run-instrumented PRIVATE include)
target_compile_definitions(sil-run-instrumented PRIVATE SIL_COPY_COUNTERS)
target_link_libraries(sil-run-instrumented
  PRIVATE nlohmann_json::nlohmann_json mcap_headers)

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

# The Arena layout — slot stride, seq freshness, capacity bounds — is the
# binary contract a Process participant maps, and it holds without a child
# process, so it gets a focused C++ test target.
add_executable(sil_channel_arenas_test
  tests/channel_arenas_test.cpp
  kernel/src/channel_arenas.cpp
  kernel/src/mapped_region.cpp)
target_include_directories(sil_channel_arenas_test PRIVATE kernel/src include)
add_test(NAME sil_channel_arenas COMMAND sil_channel_arenas_test)

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

# The runner is installed in the conventional executable directory and the
# shim in the conventional library directory.  The runner carries the
# relative library directory as a configure-time value so its runtime lookup
# follows custom GNUInstallDirs settings as well as the default layout.
target_compile_definitions(sil-run PRIVATE
  SIL_INSTALL_BINDIR="${CMAKE_INSTALL_BINDIR}"
  SIL_INSTALL_LIBDIR="${CMAKE_INSTALL_LIBDIR}")
target_compile_definitions(sil-run-instrumented PRIVATE
  SIL_INSTALL_BINDIR="${CMAKE_INSTALL_BINDIR}"
  SIL_INSTALL_LIBDIR="${CMAKE_INSTALL_LIBDIR}")

install(TARGETS sil-run
  RUNTIME DESTINATION ${CMAKE_INSTALL_BINDIR})
install(TARGETS sil_clock_shim
  RUNTIME DESTINATION ${CMAKE_INSTALL_BINDIR}
  LIBRARY DESTINATION ${CMAKE_INSTALL_LIBDIR}
  ARCHIVE DESTINATION ${CMAKE_INSTALL_LIBDIR})
install(DIRECTORY include/sil/
  DESTINATION ${CMAKE_INSTALL_INCLUDEDIR}/sil)

# The staged prefix is what the release archives, so the grant travels with the
# binaries rather than staying behind in the checkout. The two header-only
# dependencies are compiled into sil-run, so their MIT notices ship with it.
set(SIL_LICENSE_DIR ${CMAKE_INSTALL_DATAROOTDIR}/licenses/sil)
install(FILES LICENSE NOTICE THIRD-PARTY-NOTICES.md
  DESTINATION ${SIL_LICENSE_DIR})
install(FILES ${mcap_SOURCE_DIR}/LICENSE
  DESTINATION ${SIL_LICENSE_DIR}/mcap)
install(FILES ${nlohmann_json_SOURCE_DIR}/LICENSE.MIT
  DESTINATION ${SIL_LICENSE_DIR}/nlohmann-json)

# Probe executable: exercises every interposed API for the shim's unit tests.
add_executable(sil_clock_probe shim/clock_probe.c)
target_include_directories(sil_clock_probe PRIVATE include)

# A deliberately failing FMI shared library backs the importer run-failure
# tests. The Python test packages it with the Reference FMU description so the
# regression exercises the same archive extraction and ctypes boundary as a
# real imported FMU. Every variant is the same source built once more, with
# the call that fails and the status it answers chosen by the preprocessor.
function(add_failing_fmu name)
  set(target fmi_failure_${name})
  add_library(${target} SHARED tests/fixtures/fmi_failure.c)
  target_compile_definitions(${target} PRIVATE ${ARGN})
  set_target_properties(${target} PROPERTIES
    PREFIX ""
    OUTPUT_NAME "Failing${name}"
    RUNTIME_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}
    LIBRARY_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR})
endfunction()

# The fmi3Status enum, so a variant is named by the status it answers rather
# than by its integer.
set(fmi_status_names OK Warning Discard Error Fatal)
foreach(fmi_step_status IN ITEMS Warning Discard Error Fatal)
  list(FIND fmi_status_names ${fmi_step_status} fmi_status_value)
  add_failing_fmu(${fmi_step_status} FMI_STEP_STATUS=${fmi_status_value})
endforeach()

add_failing_fmu(Initialize FMI_INIT_STATUS=3)
# Fatal at Step, and an fmi3Terminate that would answer Error if the importer
# wrongly called it afterwards.
add_failing_fmu(FatalTerminateError FMI_STEP_STATUS=4 FMI_TERMINATE_STATUS=3)
# Fatal from fmi3Terminate itself, which bars the free that would follow it.
add_failing_fmu(TerminateFatal FMI_TERMINATE_STATUS=4)
add_failing_fmu(Terminate FMI_TERMINATE_STATUS=3)
add_failing_fmu(TerminateFlag FMI_TERMINATE_FLAG=1)

# --- bench participants (routing baseline fixtures, issue #61) ---
# Payload size, publish period, and burst size come from manifest config, so
# the whole baseline matrix is driven without rebuilding.
add_library(bench_publisher MODULE participants/bench_publisher.cpp)
add_library(bench_subscriber MODULE participants/bench_subscriber.cpp)
foreach(bench bench_publisher bench_subscriber)
  target_include_directories(${bench} PRIVATE include)
  target_link_libraries(${bench} PRIVATE nlohmann_json::nlohmann_json)
  set_target_properties(${bench} PROPERTIES PREFIX "" SUFFIX ".silp")
endforeach()

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
