#pragma once
// Proof-only diagnostics build. No allocation, retries or collector wait in
// an Activation. A full/missing receiver drops the notification explicitly.
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>
#include <cstdlib>
#include <cstdio>
#include <cstdint>
#include <cstring>
#include <time.h>

namespace window_proof {
struct Notice { uint64_t host_ns, virtual_ns, task, edge; };
static_assert(sizeof(Notice) == 32);
struct Observer {
  int fd = -1;
  uint64_t sent = 0, dropped = 0;
  Observer() {
    const char *path = std::getenv("SIL_WINDOW_SOCKET");
    if (!path) return;
    sockaddr_un address{};
    address.sun_family = AF_UNIX;
    if (std::strlen(path) >= sizeof(address.sun_path)) return;
    std::strcpy(address.sun_path, path);
    fd = socket(AF_UNIX, SOCK_DGRAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
    if (fd >= 0 && connect(fd, reinterpret_cast<sockaddr *>(&address),
                           sizeof address) != 0) { close(fd); fd = -1; }
  }
  ~Observer() {
    if (fd >= 0) close(fd);
    const char *path = std::getenv("SIL_WINDOW_OBSERVER");
    if (!path) return;
    FILE *out = std::fopen(path, "wx");
    if (out) {
      std::fprintf(out, "{\"sent\":%llu,\"dropped\":%llu}\n",
                   (unsigned long long)sent, (unsigned long long)dropped);
      std::fclose(out);
    }
  }
  void emit(uint64_t task, uint64_t vt, uint64_t edge) {
    timespec time{};
    if (clock_gettime(CLOCK_MONOTONIC, &time) != 0) { ++dropped; return; }
    Notice notice{uint64_t(time.tv_sec)*1000000000 + uint64_t(time.tv_nsec),
                  vt, task, edge};
    if (fd >= 0 && send(fd, &notice, sizeof notice, MSG_DONTWAIT | MSG_NOSIGNAL)
                       == sizeof notice) ++sent;
    else ++dropped;
  }
};
inline void notify(uint64_t task, uint64_t vt, uint64_t edge) {
  static Observer observer;
  observer.emit(task, vt, edge);
}
}
