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

# Build-tree paths are consumed by Manifests, tests, and developer commands.
# Keep them stable when targets are defined in subdirectories.
set(CMAKE_RUNTIME_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR})
set(CMAKE_LIBRARY_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR})
set(CMAKE_ARCHIVE_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR})

add_subdirectory(kernel)
add_subdirectory(shim)
add_subdirectory(tests)
# The runtime Docker build deliberately omits consumer examples.
if(EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/examples/CMakeLists.txt")
  add_subdirectory(examples)
endif()

include(cmake/install.cmake)
