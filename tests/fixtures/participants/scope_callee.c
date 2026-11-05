/* The C side of tests/fixtures/participants/scope_exception.cpp (#372, Q6).
 * Built as C without -fexceptions: a C++ exception that passes through this
 * frame unwinds it without running the scope's cleanup, so the scope has an
 * entry and no return. */
#include <sil/source_scope.h>

void scoped_call(void (*callback)(void)) {
  SIL_SOURCE_SCOPE(12, "scoped_call");
  callback();
}
