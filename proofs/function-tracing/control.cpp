#include <thread>
#include <stdexcept>

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
  control_leaf(3);
  std::thread worker([] { control_leaf(2); });
  worker.join();
  control_tail(1);
  try { control_throw(); } catch (const std::runtime_error &) {}
}
