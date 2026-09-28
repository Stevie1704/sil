install(DIRECTORY include/sil/
  DESTINATION ${CMAKE_INSTALL_INCLUDEDIR}/sil)

# Native participants need the schema compiler as well as the C ABI headers.
# It is installed as a small, dependency-free command rather than as a
# development-only build-tree fixture.
install(PROGRAMS tools/silschema.py
  DESTINATION ${CMAKE_INSTALL_BINDIR}
  RENAME silschema)
install(FILES python/src/sil/_schema_types.py
  DESTINATION ${CMAKE_INSTALL_BINDIR}
  RENAME _sil_schema_types.py)

configure_file(cmake/release.json.in ${CMAKE_CURRENT_BINARY_DIR}/release.json
  @ONLY)
install(FILES ${CMAKE_CURRENT_BINARY_DIR}/release.json
  DESTINATION ${CMAKE_INSTALL_DATADIR}/sil)

# The staged prefix is what the release archives, so the grant travels with the
# binaries rather than staying behind in the checkout. The two header-only
# dependencies are compiled into sil-run, so their MIT notices ship with it.
set(SIL_LICENSE_DIR ${CMAKE_INSTALL_DATAROOTDIR}/licenses/sil)
install(FILES LICENSE NOTICE THIRD-PARTY-NOTICES.md
  DESTINATION ${SIL_LICENSE_DIR})
install(FILES ${mcap_SOURCE_DIR}/LICENSE
  DESTINATION ${SIL_LICENSE_DIR}/mcap)
install(FILES ${nlohmann_json_SOURCE_DIR}/LICENSE.MIT
  DESTINATION ${SIL_LICENSE_DIR}/nlohmann-json)
