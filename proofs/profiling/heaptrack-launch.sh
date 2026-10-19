#!/bin/sh
# The disposable Heaptrack launch adapter of the profiling proof (#314).
#
#     heaptrack-launch.sh live|raw NAME COMMAND [ARG...]
#
# preloads Heaptrack into COMMAND and then execs it, so COMMAND keeps the PID,
# file descriptors (the Step protocol pipes), working directory, arguments and
# environment that it got. Heaptrack is put before an LD_PRELOAD that is
# already set, so the Clock shim of a shimmed participant stays loaded.
#
# live: Heaptrack writes to the FIFO $SILPROOF_HEAPTRACK_DIR/NAME.fifo. The
#       proof driver starts heaptrack_interpret on it before the Run, outside
#       the Run's process group, so symbols resolve while the Run directory
#       exists and the collector is not part of the participant's cleanup.
# raw:  Heaptrack writes its raw data to $SILPROOF_HEAPTRACK_DIR/NAME.raw.
#       The driver interprets it after the Run.
set -eu
mode=$1
name=$2
shift 2
dir=${SILPROOF_HEAPTRACK_DIR:?SILPROOF_HEAPTRACK_DIR is not set}
preload=${SILPROOF_HEAPTRACK_PRELOAD:?SILPROOF_HEAPTRACK_PRELOAD is not set}
case $mode in
    live) DUMP_HEAPTRACK_OUTPUT=$dir/$name.fifo ;;
    raw) DUMP_HEAPTRACK_OUTPUT=$dir/$name.raw ;;
    *) echo "heaptrack-launch.sh: unknown mode $mode" >&2; exit 2 ;;
esac
export DUMP_HEAPTRACK_OUTPUT
LD_PRELOAD=$preload${LD_PRELOAD:+:$LD_PRELOAD}
export LD_PRELOAD
exec "$@"
