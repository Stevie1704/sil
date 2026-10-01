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
    SUFFIX ".so")
endfunction()

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

# --- recorded-data FMU example (issue #186) ---
# The shared library of fmu-replay/, which the tests package into an
# FMU with that example's package.py.
add_library(ego_motion SHARED fmu-replay/ego_motion.c)
set_target_properties(ego_motion PROPERTIES
  PREFIX ""
  SUFFIX ".so"
  OUTPUT_NAME "EgoMotion")

# --- Native ADAS reference application (issues #222, #223) ---
# adas-reference/adas_reference.c is a C application with its own API and no
# SiL include; sil_adapter.c is the Native participant around it, built
# against the public ABI header and the layouts silschema generates from
# schemas.json. The wrong-sign build is the same sources with one defect
# chosen by the preprocessor, so the comparison can be shown to fail.
set(ADAS_DIR ${CMAKE_CURRENT_SOURCE_DIR}/adas-reference)
set(ADAS_GEN_DIR ${CMAKE_BINARY_DIR}/generated/adas-reference)
add_custom_command(
  OUTPUT ${ADAS_GEN_DIR}/adas_messages.h
  COMMAND python3 ${PROJECT_SOURCE_DIR}/tools/silschema.py
          ${ADAS_DIR}/schemas.json ${ADAS_GEN_DIR}/adas_messages.h
  DEPENDS ${PROJECT_SOURCE_DIR}/tools/silschema.py
          ${PROJECT_SOURCE_DIR}/python/src/sil/_schema_types.py
          ${ADAS_DIR}/schemas.json)
add_custom_target(adas_messages DEPENDS ${ADAS_GEN_DIR}/adas_messages.h)

function(add_adas_reference name)
  add_library(${name} MODULE ${ADAS_DIR}/adas_reference.c
    ${ADAS_DIR}/sil_adapter.c)
  add_dependencies(${name} adas_messages)
  target_include_directories(${name} PRIVATE
    ${PROJECT_SOURCE_DIR}/include ${ADAS_DIR} ${ADAS_GEN_DIR})
  target_compile_definitions(${name} PRIVATE ${ARGN})
  # The reference arithmetic is specified without contracted floating-point
  # expressions; Clang contracts by default, also in ISO C mode.
  target_compile_options(${name} PRIVATE -ffp-contract=off)
  set_target_properties(${name} PROPERTIES PREFIX "" SUFFIX ".so"
    C_STANDARD 11 C_STANDARD_REQUIRED ON C_EXTENSIONS OFF)
endfunction()

add_adas_reference(adas_reference)
add_adas_reference(adas_reference_wrong_sign ADAS_REFERENCE_WRONG_SIGN)

# The application's lifecycle without SiL: init, reset, terminate and the
# rejections a Run cannot observe.
add_executable(adas_reference_test
  ${PROJECT_SOURCE_DIR}/tests/adas_reference_test.c ${ADAS_DIR}/adas_reference.c)
target_include_directories(adas_reference_test PRIVATE ${ADAS_DIR})
set_target_properties(adas_reference_test PROPERTIES
  C_STANDARD 11 C_STANDARD_REQUIRED ON C_EXTENSIONS OFF)
if(NOT APPLE)
  target_link_libraries(adas_reference_test PRIVATE m)
  target_link_libraries(adas_reference PRIVATE m)
  target_link_libraries(adas_reference_wrong_sign PRIVATE m)
endif()
add_test(NAME adas_reference_application COMMAND adas_reference_test
  WORKING_DIRECTORY ${CMAKE_BINARY_DIR})
