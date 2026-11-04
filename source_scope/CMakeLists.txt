# --- source-scope observer (#372) ---
# libsil_source_scope.so.1: a Native library built with SIL_SOURCE_SCOPES=1
# links it (include/sil/source_scope.h). It is not part of the runner: sil-run
# and the kernel sources have no symbol, hook or branch of it (ADR 0004,
# ADR 0007). The contract is Linux x86-64/glibc (ELF), so other hosts do not
# build it.
if(NOT CMAKE_SYSTEM_NAME STREQUAL "Linux")
  return()
endif()

add_library(sil_source_scope SHARED observer.c)
target_include_directories(sil_source_scope PRIVATE ${PROJECT_SOURCE_DIR}/include)
set_target_properties(sil_source_scope PROPERTIES
  C_STANDARD 11
  C_VISIBILITY_PRESET hidden
  VERSION 1
  SOVERSION 1)
target_link_options(sil_source_scope PRIVATE -Wl,--build-id=sha1)

install(TARGETS sil_source_scope
  LIBRARY DESTINATION ${CMAKE_INSTALL_LIBDIR})
