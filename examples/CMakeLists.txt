# --- shared-library adoption example (issue #184) ---
# library/speed_filter.c is an adopter's library with its own C API;
# the tests drive it through library/adapter.py. The deliberately
# incorrect build and the fault variants are the same library built once more,
# with the defect chosen by the preprocessor. The ".so" suffix on every
# platform keeps the tests' paths platform-neutral; dlopen does not care.
function(add_speed_filter name source)
  add_library(${name} SHARED ${source})
  target_include_directories(${name} PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/library)
  target_compile_definitions(${name} PRIVATE ${ARGN})
  set_target_properties(${name} PROPERTIES
    PREFIX ""
    SUFFIX ".so"
    RUNTIME_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}
    LIBRARY_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR})
endfunction()

# The runtime image builds from a source tree without examples/, so these
# targets exist only where the example does.
if(EXISTS ${CMAKE_SOURCE_DIR}/examples/library/speed_filter.c)
  add_speed_filter(speed_filter library/speed_filter.c)
  add_speed_filter(speed_filter_defect library/speed_filter.c
    SPEED_FILTER_DEFECT)
  add_speed_filter(speed_filter_crash ${PROJECT_SOURCE_DIR}/tests/fixtures/speed_filter_fault.c
    SPEED_FILTER_CRASH_AT_CYCLE=3)
  add_speed_filter(speed_filter_hang ${PROJECT_SOURCE_DIR}/tests/fixtures/speed_filter_fault.c
    SPEED_FILTER_HANG_AT_CYCLE=3)
  add_speed_filter(speed_filter_no_terminate ${PROJECT_SOURCE_DIR}/tests/fixtures/speed_filter_fault.c
    SPEED_FILTER_OMIT_TERMINATE)
  # Wrong only in the cycle of the Run's last Step, at 100 ms.
  add_speed_filter(speed_filter_wrong_last ${PROJECT_SOURCE_DIR}/tests/fixtures/speed_filter_fault.c
    SPEED_FILTER_WRONG_AT_CYCLE=11)
endif()

# --- recorded-data FMU example (issue #186) ---
# The shared library of fmu-replay/, which the tests package into an
# FMU with that example's package.py. Like the library example, it exists only
# where the example does.
if(EXISTS ${CMAKE_SOURCE_DIR}/examples/fmu-replay/ego_motion.c)
  add_library(ego_motion SHARED fmu-replay/ego_motion.c)
  set_target_properties(ego_motion PROPERTIES
    PREFIX ""
    SUFFIX ".so"
    OUTPUT_NAME "EgoMotion"
    RUNTIME_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}
    LIBRARY_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR})
endif()
