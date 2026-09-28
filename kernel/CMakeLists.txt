set(SIL_KERNEL_SOURCES
  src/main.cpp
  src/manifest.cpp
  src/interceptor.cpp
  src/engine.cpp
  src/clock_shim.cpp
  src/mapped_region.cpp
  src/owned_directory.cpp
  src/native_participant.cpp
  src/channel_arenas.cpp
  src/process_participant.cpp
  src/provenance.cpp
  src/run_signal.cpp
  src/mcap_recording.cpp
  src/recording_file.cpp
  src/replayer.cpp)

add_executable(sil-run ${SIL_KERNEL_SOURCES})
target_include_directories(sil-run PRIVATE ${PROJECT_SOURCE_DIR}/include)
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
  ${SIL_KERNEL_SOURCES} src/copy_counters.cpp)
target_include_directories(sil-run-instrumented PRIVATE ${PROJECT_SOURCE_DIR}/include)
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
