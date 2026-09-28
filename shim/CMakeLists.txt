# --- virtual clock shim (preload library + probe, issue #26) ---
# One extra shared library built alongside the runner. Opaque POSIX process
# participants preload it (LD_PRELOAD / DYLD_INSERT_LIBRARIES) so their own
# clock reads return stepped virtual time. It knows nothing of the kernel.
# SHARED (not MODULE): macOS DYLD_INSERT_LIBRARIES rejects a Mach-O bundle and
# requires a real dylib; LD_PRELOAD accepts a shared object on Linux.
add_library(sil_clock_shim SHARED clock_shim.c)
target_include_directories(sil_clock_shim PRIVATE ${PROJECT_SOURCE_DIR}/include)
# The root output directories place it next to sil-run, so the runner can find
# it relative to itself.
if(NOT APPLE)
  # Linux: the region-absent fallback resolves the real libc symbol via
  # dlsym(RTLD_NEXT, ...), which needs libdl.
  target_link_libraries(sil_clock_shim PRIVATE dl)
endif()

install(TARGETS sil_clock_shim
  RUNTIME DESTINATION ${CMAKE_INSTALL_BINDIR}
  LIBRARY DESTINATION ${CMAKE_INSTALL_LIBDIR}
  ARCHIVE DESTINATION ${CMAKE_INSTALL_LIBDIR})
