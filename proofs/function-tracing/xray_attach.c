#include <dlfcn.h>

/* Retry patching after the Native DSO has actually loaded, rather than
 * mistaking a pre-main patch attempt for a test of loaded-DSO coverage. */
__attribute__((constructor, xray_never_instrument)) static void attach(void) {
  void (*hook)(void) = dlsym(RTLD_DEFAULT, "trace_xray_attach");
  if (hook) hook();
}
