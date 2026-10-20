#!/bin/sh
# The shim belongs to the inferior, never to GDB's own host clock.
export SIL_DEBUG_SHIM="${LD_PRELOAD-}"
unset LD_PRELOAD
exec gdb -nx -q -batch -x "$SIL_DEBUG_COMMANDS" --args "$@"
