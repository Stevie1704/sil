# The interceptor plan has a focused C++ test in addition to the Run tests.
add_executable(sil_interceptor_test
  interceptor_test.cpp
  ${PROJECT_SOURCE_DIR}/kernel/src/interceptor.cpp
  ${PROJECT_SOURCE_DIR}/kernel/src/manifest.cpp)
target_include_directories(sil_interceptor_test PRIVATE ${PROJECT_SOURCE_DIR}/kernel/src)
target_link_libraries(sil_interceptor_test PRIVATE nlohmann_json::nlohmann_json)
add_test(NAME sil_interceptor_plan COMMAND sil_interceptor_test
  WORKING_DIRECTORY ${CMAKE_BINARY_DIR})

# Unsigned virtual-time addition is the one arithmetic boundary every
# scheduling and delivery decision rests on, so its table of edge cases is a
# focused C++ test rather than an end-to-end one.
add_executable(sil_virtual_time_test virtual_time_test.cpp)
target_include_directories(sil_virtual_time_test PRIVATE ${PROJECT_SOURCE_DIR}/kernel/src)
add_test(NAME sil_virtual_time COMMAND sil_virtual_time_test
  WORKING_DIRECTORY ${CMAKE_BINARY_DIR})

# Engine construction is deliberately gated on a successful Run preparation;
# the authored Manifest is not an executable input by itself.
add_executable(sil_engine_interface_test engine_interface_test.cpp)
target_include_directories(sil_engine_interface_test PRIVATE ${PROJECT_SOURCE_DIR}/kernel/src)
add_test(NAME sil_engine_interface COMMAND sil_engine_interface_test
  WORKING_DIRECTORY ${CMAKE_BINARY_DIR})

# The Arena layout — slot stride, seq freshness, capacity bounds — is the
# binary contract a Process participant maps, and it holds without a child
# process, so it gets a focused C++ test target.
add_executable(sil_channel_arenas_test
  channel_arenas_test.cpp
  ${PROJECT_SOURCE_DIR}/kernel/src/channel_arenas.cpp
  ${PROJECT_SOURCE_DIR}/kernel/src/mapped_region.cpp)
target_include_directories(sil_channel_arenas_test PRIVATE ${PROJECT_SOURCE_DIR}/kernel/src ${PROJECT_SOURCE_DIR}/include)
add_test(NAME sil_channel_arenas COMMAND sil_channel_arenas_test
  WORKING_DIRECTORY ${CMAKE_BINARY_DIR})

# The mapped region owns a temp file's mapping, descriptor, and directory entry.
# Its release-on-destruction and move semantics are invisible at the run
# boundary, so they get their own focused C++ test target.
add_executable(sil_mapped_region_test
  mapped_region_test.cpp
  ${PROJECT_SOURCE_DIR}/kernel/src/mapped_region.cpp)
target_include_directories(sil_mapped_region_test PRIVATE ${PROJECT_SOURCE_DIR}/kernel/src)
add_test(NAME sil_mapped_region COMMAND sil_mapped_region_test
  WORKING_DIRECTORY ${CMAKE_BINARY_DIR})

# --- generated message headers (single typed contract per DESIGN #9) ---
set(SIL_GEN_DIR ${CMAKE_BINARY_DIR}/generated)
add_custom_command(
  OUTPUT ${SIL_GEN_DIR}/toy_messages.h
  COMMAND python3 ${PROJECT_SOURCE_DIR}/tools/silschema.py
          ${PROJECT_SOURCE_DIR}/tests/fixtures/schemas/toy.json ${SIL_GEN_DIR}/toy_messages.h
  DEPENDS ${PROJECT_SOURCE_DIR}/tools/silschema.py
          ${PROJECT_SOURCE_DIR}/python/src/sil/_schema_types.py fixtures/schemas/toy.json)
add_custom_target(toy_messages DEPENDS ${SIL_GEN_DIR}/toy_messages.h)

# Probe executable: exercises every interposed API for the shim's unit tests.
add_executable(sil_clock_probe fixtures/clock_probe.c)
target_include_directories(sil_clock_probe PRIVATE ${PROJECT_SOURCE_DIR}/include)

# Thread probe: one dynamically linked pthread_create for the shim's thread
# policy tests (issue #262).
find_package(Threads REQUIRED)
add_executable(sil_thread_probe fixtures/thread_probe.c)
target_link_libraries(sil_thread_probe PRIVATE Threads::Threads)

# A deliberately failing FMI shared library backs the importer run-failure
# tests. The Python test packages it with the Reference FMU description so the
# regression exercises the same archive extraction and ctypes boundary as a
# real imported FMU. Every variant is the same source built once more, with
# the call that fails and the status it answers chosen by the preprocessor.
function(add_failing_fmu name)
  set(target fmi_failure_${name})
  add_library(${target} SHARED fixtures/fmi_failure.c)
  target_compile_definitions(${target} PRIVATE ${ARGN})
  set_target_properties(${target} PROPERTIES
    PREFIX ""
    OUTPUT_NAME "Failing${name}")
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

# A small FMI 2.0 co-simulation FMU backs the FMI 2.0 importer tests (issue
# #191). The Reference FMUs ship FMI 2.0 binaries for x86-64 only, so this
# one is built for the host. Every variant is the same source built once
# more, with the call that answers a status other than fmi2OK chosen by the
# preprocessor. The Python tests write its description.
function(add_fmi2_fmu name)
  set(target fmi2_fake_${name})
  add_library(${target} SHARED fixtures/fmi2_fake.c)
  target_compile_definitions(${target} PRIVATE ${ARGN})
  set_target_properties(${target} PROPERTIES
    PREFIX ""
    OUTPUT_NAME "Fmi2${name}")
endfunction()

add_fmi2_fmu(Ok)
# The fmi2Status enum, so a variant is named by the status fmi2DoStep answers.
set(fmi2_status_names OK Warning Discard Error Fatal Pending)
foreach(fmi2_step_status IN ITEMS Warning Discard Error Fatal Pending)
  list(FIND fmi2_status_names ${fmi2_step_status} fmi2_status_value)
  add_fmi2_fmu(Step${fmi2_step_status} FMI2_STEP_STATUS=${fmi2_status_value})
endforeach()
add_fmi2_fmu(InitializeError FMI2_INIT_STATUS=3)
# An fmi2Terminate that answers Error, alone and after a step that already
# answered Error, which must free the instance without terminating it.
add_fmi2_fmu(TerminateError FMI2_TERMINATE_STATUS=3)
add_fmi2_fmu(StepErrorTerminateError FMI2_STEP_STATUS=3 FMI2_TERMINATE_STATUS=3)

# A small FMI 2.0 FMU with OSMP binary variables backs the OSMP mapping tests
# (issue #244). Each variant reports a wrong output, chosen by the
# preprocessor. The Python tests write its description.
function(add_fmi2_osmp_fmu name)
  set(target fmi2_osmp_${name})
  add_library(${target} SHARED fixtures/fmi2_osmp.c)
  target_compile_definitions(${target} PRIVATE ${ARGN})
  set_target_properties(${target} PROPERTIES
    PREFIX ""
    OUTPUT_NAME "Osmp${name}")
endfunction()

add_fmi2_osmp_fmu(Ok)
add_fmi2_osmp_fmu(Oversize OSMP_REPORTED_SIZE=2147483647)
add_fmi2_osmp_fmu(NegativeSize OSMP_REPORTED_SIZE=-1)
add_fmi2_osmp_fmu(NullAddress OSMP_NULL_ADDRESS)

# A clocked FMI shared library backs the Event Mode importer tests. It stands
# in for the FMI-LS-BUS CAN node of proofs/fmi-ls-bus/, which is built inside a
# pinned container and not vendored here: it drives the same lifecycle and
# emits the same operation bytes, so the importer can be measured against the
# fixture's expected exchange without docker. Every variant is the same source
# built once more, with the behavior under test chosen by the preprocessor.
function(add_clocked_fmu name)
  set(target fmi_clock_${name})
  add_library(${target} SHARED fixtures/fmi_clock.c)
  target_compile_definitions(${target} PRIVATE ${ARGN})
  set_target_properties(${target} PROPERTIES
    PREFIX ""
    OUTPUT_NAME "Clocked${name}")
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
  add_library(${target} SHARED fixtures/fmi_bus.c)
  target_compile_definitions(${target} PRIVATE ${ARGN})
  set_target_properties(${target} PROPERTIES
    PREFIX ""
    OUTPUT_NAME "Clocked${name}")
endfunction()

add_bus_fmu(CanBus)
# A transmission time of 1/3 s, which is no whole number of nanoseconds, and
# one of zero, which asks for the instant it was asked in.
add_bus_fmu(CanBusRaggedInterval FMI_BUS_RAGGED_INTERVAL=1)
add_bus_fmu(CanBusInstantInterval FMI_BUS_INSTANT_INTERVAL=1)

# --- the fixed-size array FMU (issue #190) ---
# Numeric arrays of each mapped type, a [2,3] matrix whose output depends on
# each element's indices, and accessors that refuse an nValues the references
# do not hold together. The Python tests write its description.
add_library(fmi_array SHARED fixtures/fmi_array.c)
set_target_properties(fmi_array PROPERTIES PREFIX "" OUTPUT_NAME "ArrayEcho")

# --- bench participants (routing baseline fixtures, issue #61) ---
# Payload size, publish period, and burst size come from manifest config, so
# the whole baseline matrix is driven without rebuilding.
add_library(bench_publisher MODULE fixtures/participants/bench_publisher.cpp)
add_library(bench_subscriber MODULE fixtures/participants/bench_subscriber.cpp)
foreach(bench bench_publisher bench_subscriber)
  target_include_directories(${bench} PRIVATE ${PROJECT_SOURCE_DIR}/include)
  target_link_libraries(${bench} PRIVATE nlohmann_json::nlohmann_json)
  set_target_properties(${bench} PROPERTIES PREFIX "" SUFFIX ".silp")
endforeach()

# --- toy participants (walking-skeleton fixtures) ---
add_library(toy_producer MODULE fixtures/participants/toy_producer.cpp)
add_library(toy_accumulator MODULE fixtures/participants/toy_accumulator.cpp)
# Throws across the C ABI seam on purpose, so exception containment is tested
# through a real shared library (issue #65).
add_library(toy_thrower MODULE fixtures/participants/toy_thrower.cpp)
foreach(toy toy_producer toy_accumulator toy_thrower)
  add_dependencies(${toy} toy_messages)
  target_include_directories(${toy} PRIVATE ${PROJECT_SOURCE_DIR}/include ${SIL_GEN_DIR})
  target_link_libraries(${toy} PRIVATE nlohmann_json::nlohmann_json)
  # Platform-neutral name so the same manifest works on macOS and Linux.
  set_target_properties(${toy} PROPERTIES PREFIX "" SUFFIX ".silp")
endforeach()
