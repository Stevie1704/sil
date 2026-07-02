cmake_minimum_required(VERSION 3.24)
project(sil LANGUAGES C CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

include(FetchContent)

FetchContent_Declare(nlohmann_json
  URL https://github.com/nlohmann/json/releases/download/v3.12.0/json.tar.xz
  DOWNLOAD_EXTRACT_TIMESTAMP ON)
FetchContent_MakeAvailable(nlohmann_json)

FetchContent_Declare(mcap
  URL https://github.com/foxglove/mcap/archive/refs/tags/releases/cpp/v2.1.3.tar.gz
  DOWNLOAD_EXTRACT_TIMESTAMP ON)
FetchContent_MakeAvailable(mcap)

add_library(mcap_headers INTERFACE)
target_include_directories(mcap_headers INTERFACE ${mcap_SOURCE_DIR}/cpp/mcap/include)
# Recording is uncompressed in v1: no lz4/zstd dependency in the kernel.
target_compile_definitions(mcap_headers INTERFACE
  MCAP_COMPRESSION_NO_LZ4 MCAP_COMPRESSION_NO_ZSTD)

add_executable(sil-run
  kernel/src/main.cpp
  kernel/src/manifest.cpp
  kernel/src/engine.cpp
  kernel/src/native_participant.cpp
  kernel/src/process_participant.cpp
  kernel/src/recorder.cpp)
target_include_directories(sil-run PRIVATE include)
target_link_libraries(sil-run PRIVATE nlohmann_json::nlohmann_json mcap_headers)

# --- generated message headers (single typed contract per DESIGN #9) ---
set(SIL_GEN_DIR ${CMAKE_BINARY_DIR}/generated)
add_custom_command(
  OUTPUT ${SIL_GEN_DIR}/toy_messages.h
  COMMAND python3 ${CMAKE_SOURCE_DIR}/tools/silschema.py
          ${CMAKE_SOURCE_DIR}/schemas/toy.json ${SIL_GEN_DIR}/toy_messages.h
  DEPENDS tools/silschema.py schemas/toy.json)
add_custom_target(toy_messages DEPENDS ${SIL_GEN_DIR}/toy_messages.h)

# --- toy participants (walking-skeleton fixtures) ---
add_library(toy_producer MODULE participants/toy_producer.cpp)
add_library(toy_accumulator MODULE participants/toy_accumulator.cpp)
foreach(toy toy_producer toy_accumulator)
  add_dependencies(${toy} toy_messages)
  target_include_directories(${toy} PRIVATE include ${SIL_GEN_DIR})
  target_link_libraries(${toy} PRIVATE nlohmann_json::nlohmann_json)
  # Platform-neutral name so the same manifest works on macOS and Linux.
  set_target_properties(${toy} PROPERTIES PREFIX "" SUFFIX ".silp")
endforeach()
