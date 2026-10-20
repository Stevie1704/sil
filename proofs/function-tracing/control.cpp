#include <thread>
#include <stdexcept>
#include <cstdio>
#include <dlfcn.h>
#include <xray/xray_interface.h>

__attribute__((noinline)) int control_leaf(int n) {
  volatile int result = n;
  if (n) result += control_leaf(n - 1);
  return result;
}
__attribute__((noinline)) int control_throw() {
  throw std::runtime_error("proof");
}
__attribute__((noinline)) int control_tail(int n) { return control_leaf(n); }
int main() {
  const size_t maximum = __xray_max_function_id();
  if (maximum > 256) return 2;
  for (size_t id = 1; id <= maximum; ++id) {
    Dl_info info = {};
    if (dladdr(reinterpret_cast<void *>(__xray_function_address(id)), &info)
        && info.dli_sname)
      std::printf("%zu %s\n", id, info.dli_sname);
  }
  control_leaf(3);
  std::thread worker([] { control_leaf(2); });
  worker.join();
  control_tail(1);
  try { control_throw(); } catch (const std::runtime_error &) {}
}
