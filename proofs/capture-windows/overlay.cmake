# Replace only the diagnostics translation unit in a proof build. No edit to
# kernel sources, installation rules or the ordinary runner's target.
function(window_proof_overlay)
  get_target_property(sources sil-run-diagnostics SOURCES)
  list(REMOVE_ITEM sources src/run_diagnostics.cpp)
  list(APPEND sources "${WINDOW_PROOF_SOURCE}")
  set_property(TARGET sil-run-diagnostics PROPERTY SOURCES "${sources}")
  target_include_directories(sil-run-diagnostics PRIVATE
    "${CMAKE_SOURCE_DIR}/kernel/src" "${CMAKE_SOURCE_DIR}/proofs/capture-windows")
endfunction()
cmake_language(DEFER CALL window_proof_overlay)
