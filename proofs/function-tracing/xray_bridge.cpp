#include <xray/xray_interface.h>

/* compiler-rt localizes its own API symbols. These proof-owned executable
 * wrappers are exported explicitly; a DSO never links its own runtime. */
extern "C" __attribute__((xray_never_instrument))
int sil_proof_xray_attach(void (*handler)(int32_t, XRayEntryType)) {
  __xray_init();
  if (!__xray_set_handler(handler)) return -2;
  return __xray_patch();
}
extern "C" __attribute__((xray_never_instrument))
size_t sil_proof_xray_max() { return __xray_max_function_id(); }
