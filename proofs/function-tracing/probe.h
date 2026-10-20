#pragma once
#include <stdint.h>

/* Proof-only logical source scopes. cleanup runs on every ordinary C return,
 * including the early returns in confirmed/check_object. */
typedef struct { uint16_t function; } trace_scope;
trace_scope trace_enter(uint16_t function);
void trace_leave(trace_scope *scope);
#define TRACE_SCOPE(id) \
  trace_scope trace_scope_ __attribute__((cleanup(trace_leave))) = trace_enter(id)
