#!/bin/sh
# Run-boundary adapter: the same Manifest is used for baseline and capture.
set -eu
case ${SILPROOF_MODE:?} in
    preload) LD_PRELOAD=$SILPROOF_HEAPTRACK${LD_PRELOAD:+:$LD_PRELOAD} ;;
    preserve)
        SILPROOF_SHIM_PRELOAD=${LD_PRELOAD:?runner must install the Clock shim}
        export SILPROOF_SHIM_PRELOAD
        LD_PRELOAD=$SILPROOF_PRESERVE:$SILPROOF_HEAPTRACK:$LD_PRELOAD ;;
    *) exit 91 ;;
esac
export LD_PRELOAD
exec "$@"
