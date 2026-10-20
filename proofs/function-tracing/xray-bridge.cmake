# Only the proof-only Clang executable gets the bridge. Defer until the root
# has declared its targets; do not apply to dependency project() calls.
if(PROJECT_NAME STREQUAL "sil")
  set(SIL_FUNCTION_TRACING_BRIDGE "${CMAKE_CURRENT_LIST_DIR}/xray_bridge.cpp")
  cmake_language(DEFER CALL target_sources sil-run-diagnostics PRIVATE
    "${SIL_FUNCTION_TRACING_BRIDGE}")
endif()
