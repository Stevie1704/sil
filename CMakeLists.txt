cmake_minimum_required(VERSION 3.24)

# The Python distribution owns the public version.  The native runner, the
# staged development prefix, and the container are all built from this value
# so a release cannot quietly contain two versions.
file(READ "${CMAKE_CURRENT_SOURCE_DIR}/python/pyproject.toml" SIL_PYPROJECT)
string(REGEX MATCH
  "version[ \t]*=[ \t]*\"([0-9]+\\.[0-9]+\\.[0-9]+)\""
  SIL_VERSION_MATCH "${SIL_PYPROJECT}")
if(NOT SIL_VERSION_MATCH)
  message(FATAL_ERROR "cannot find a semantic project version in python/pyproject.toml")
endif()
set(SIL_VERSION "${CMAKE_MATCH_1}")
set(SIL_SOURCE_REPOSITORY "https://github.com/Stevie1704/sil" CACHE STRING
  "Canonical source repository URL embedded in release metadata")
set(SIL_SOURCE_REVISION "unknown" CACHE STRING
  "Source revision embedded in release metadata")

project(sil VERSION "${SIL_VERSION}" LANGUAGES C CXX)

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
  kernel/src/provenance.cpp
  kernel/src/run_signal.cpp
  kernel/src/mcap_recording.cpp
  kernel/src/recording_file.cpp
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

set(SIL_RELEASE_DEFINITIONS
  SIL_VERSION="${SIL_VERSION}"
  SIL_SOURCE_REPOSITORY="${SIL_SOURCE_REPOSITORY}"
  SIL_SOURCE_REVISION="${SIL_SOURCE_REVISION}"
  SIL_LICENSE_IDENTIFIER="Apache-2.0")
target_compile_definitions(sil-run PRIVATE ${SIL_RELEASE_DEFINITIONS})
target_compile_definitions(sil-run-instrumented PRIVATE ${SIL_RELEASE_DEFINITIONS})

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

# Engine construction is deliberately gated on a successful Run preparation;
# the authored Manifest is not an executable input by itself.
add_executable(sil_engine_interface_test tests/engine_interface_test.cpp)
target_include_directories(sil_engine_interface_test PRIVATE kernel/src)
add_test(NAME sil_engine_interface COMMAND sil_engine_interface_test)

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
  DEPENDS tools/silschema.py python/src/sil/_schema_types.py schemas/toy.json)
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

# Native participants need the schema compiler as well as the C ABI headers.
# It is installed as a small, dependency-free command rather than as a
# development-only build-tree fixture.
install(PROGRAMS tools/silschema.py
  DESTINATION ${CMAKE_INSTALL_BINDIR}
  RENAME silschema)
install(FILES python/src/sil/_schema_types.py
  DESTINATION ${CMAKE_INSTALL_BINDIR}
  RENAME _sil_schema_types.py)

configure_file(cmake/release.json.in ${CMAKE_CURRENT_BINARY_DIR}/release.json
  @ONLY)
install(FILES ${CMAKE_CURRENT_BINARY_DIR}/release.json
  DESTINATION ${CMAKE_INSTALL_DATADIR}/sil)

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

# A clocked FMI shared library backs the Event Mode importer tests. It stands
# in for the FMI-LS-BUS CAN node of proofs/fmi-ls-bus/, which is built inside a
# pinned container and not vendored here: it drives the same lifecycle and
# emits the same operation bytes, so the importer can be measured against the
# fixture's expected exchange without docker. Every variant is the same source
# built once more, with the behavior under test chosen by the preprocessor.
function(add_clocked_fmu name)
  set(target fmi_clock_${name})
  add_library(${target} SHARED tests/fixtures/fmi_clock.c)
  target_compile_definitions(${target} PRIVATE ${ARGN})
  set_target_properties(${target} PROPERTIES
    PREFIX ""
    OUTPUT_NAME "Clocked${name}"
    RUNTIME_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}
    LIBRARY_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR})
endfunction()

add_clocked_fmu(CanNode)
# An event iteration that never converges, so the importer's own bound is what
# ends it.
add_clocked_fmu(NeverConverges FMI_CLOCK_NEVER_CONVERGES=1)
# A next event time at 50 ms, which no step grid this kernel schedules lands
# on; one at 100 ms, which the first Step of a 100 ms grid ends exactly on;
# and one at 550 ms, which only a later Step reaches past.
add_clocked_fmu(NextEvent FMI_CLOCK_NEXT_EVENT_MS=50)
add_clocked_fmu(NextEventAtBoundary FMI_CLOCK_NEXT_EVENT_MS=100)
add_clocked_fmu(NextEventLater FMI_CLOCK_NEXT_EVENT_MS=550)
# The output Clock raised by the discrete-state update that ends the event,
# which is the activation an importer loses if it stops reading there.
add_clocked_fmu(LateActivation FMI_CLOCK_ACTIVATE_IN_UPDATE=1)
# An early return the importer never asked for, and a termination request from
# inside the event.
add_clocked_fmu(EarlyReturn FMI_CLOCK_EARLY_RETURN=1)
add_clocked_fmu(TerminateInEvent FMI_CLOCK_TERMINATE_IN_EVENT=1)
# The node as upstream wrote it: a frame handed to the input Clock is read and
# dropped rather than echoed back out. A node connected to a real peer has to
# be this one; the echo exists only to drive a lone node from both ends.
add_clocked_fmu(CanNodeOnABus FMI_CLOCK_ECHO_RX=0)

# --- the CAN bus simulation stand-in (issue #140) ---
# The other half of the acceptance fixture: the FMU two nodes are connected
# through. Its send side is a countdown Clock rather than a triggered one, so
# what it exercises is the group's own communication points — the instant a
# frame has finished transmitting is not one the kernel's Slot grid contains.
function(add_bus_fmu name)
  set(target fmi_bus_${name})
  add_library(${target} SHARED tests/fixtures/fmi_bus.c)
  target_compile_definitions(${target} PRIVATE ${ARGN})
  set_target_properties(${target} PROPERTIES
    PREFIX ""
    OUTPUT_NAME "Clocked${name}"
    RUNTIME_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}
    LIBRARY_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR})
endfunction()

add_bus_fmu(CanBus)
# A transmission time of 1/3 s, which is no whole number of nanoseconds, and
# one of zero, which asks for the instant it was asked in.
add_bus_fmu(CanBusRaggedInterval FMI_BUS_RAGGED_INTERVAL=1)
add_bus_fmu(CanBusInstantInterval FMI_BUS_INSTANT_INTERVAL=1)

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

# --- shared-library adoption example (issue #184) ---
# examples/library/speed_filter.c is an adopter's library with its own C API;
# the tests drive it through examples/library/adapter.py. The deliberately
# incorrect build and the fault variants are the same library built once more,
# with the defect chosen by the preprocessor. The ".so" suffix on every
# platform keeps the tests' paths platform-neutral; dlopen does not care.
function(add_speed_filter name source)
  add_library(${name} SHARED ${source})
  target_include_directories(${name} PRIVATE examples/library)
  target_compile_definitions(${name} PRIVATE ${ARGN})
  set_target_properties(${name} PROPERTIES
    PREFIX ""
    SUFFIX ".so"
    RUNTIME_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}
    LIBRARY_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR})
endfunction()

add_speed_filter(speed_filter examples/library/speed_filter.c)
add_speed_filter(speed_filter_defect examples/library/speed_filter.c
  SPEED_FILTER_DEFECT)
add_speed_filter(speed_filter_crash tests/fixtures/speed_filter_fault.c
  SPEED_FILTER_CRASH_AT_CYCLE=3)
add_speed_filter(speed_filter_hang tests/fixtures/speed_filter_fault.c
  SPEED_FILTER_HANG_AT_CYCLE=3)
add_speed_filter(speed_filter_no_terminate tests/fixtures/speed_filter_fault.c
  SPEED_FILTER_OMIT_TERMINATE)
