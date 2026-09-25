#!/usr/bin/env bash
# Validate the CAN FMU as a consumer gets it (issue #158). Run models/can/run.sh
# first; it builds the FMU and the qualification image this script reuses.
#
# 1. Installed SiL: the unchanged production runtime image (installed wheel
#    and `sil-run`, no source tree, no network) Runs the documented example
#    twice from its configuration file.
# 2. Independent FMI path: FMPy alone drives the same archive with the same
#    configuration in the qualification image, where `sil` is not importable.
#
# The traces must agree. Each container has a deadline (CAN_BUNDLE_DEADLINE_S,
# default 600 s); inside, every Run and Participant response has its own.
set -euo pipefail
cd "$(dirname "$0")/../.."
platform=linux/amd64
runtime=sil-can-runtime:local
qualification=sil-can-qualification
deadline=${CAN_BUNDLE_DEADLINE_S:-600}
fmu=build/can/SilCanBus.fmu
out=build/can/bundle
if [[ ! -f "$fmu" ]]; then
    echo "no $fmu; run models/can/run.sh first" >&2
    exit 2
fi
rm -rf "$out"
mkdir -p "$out"

container=
cleanup() { if [[ -n "$container" ]]; then docker rm -f "$container" >/dev/null; fi; }
trap cleanup EXIT

# A host whose legacy builder cannot select a platform from the pinned index
# may set CAN_RUNTIME_PYTHON_IMAGE to that index's linux/amd64 manifest,
# python@sha256:781449467ffb6f04218f09b1ecdcdc7d22b289ee5da9ec498b024e24ad7a6db7.
docker build --platform "$platform" --target runtime -t "$runtime" \
    ${CAN_RUNTIME_PYTHON_IMAGE:+--build-arg PYTHON_IMAGE="$CAN_RUNTIME_PYTHON_IMAGE"} \
    --build-arg SIL_VERSION="$(python3 tools/release.py project-version --root .)" \
    --build-arg SIL_SOURCE_REVISION="$(git rev-parse HEAD)" .
docker image inspect "$runtime" --format '{{.Id}}' > "$out/runtime-image.txt"
docker image inspect "$qualification" --format '{{.Id}}' > "$out/qualification-image.txt"

# Inputs are copied in and evidence out, so no host directory is mounted.
container=$(docker create --platform "$platform" --network none \
    --entrypoint python "$runtime" /workspace/example/sil_run.py \
    /workspace/example/example.json /workspace/example/SilCanBus.fmu /workspace/sil)
docker cp models/can/example "$container:/workspace/example"
docker cp "$fmu" "$container:/workspace/example/SilCanBus.fmu"
status=0
timeout "$deadline" docker start -a "$container" || status=$?
docker cp "$container:/workspace/sil" "$out/sil" || true
cleanup
container=
if [[ $status -ne 0 ]]; then
    echo "installed SiL Run failed or missed its deadline ($status)" >&2
    exit 1
fi

sil_absent='import importlib.util, sys; sys.exit(importlib.util.find_spec("sil") is not None)'
# A created container, so the deadline also removes it and not only the client.
container=$(docker create --platform "$platform" --network none \
    --user "$(id -u):$(id -g)" -e HOME=/tmp -v "$PWD:/work" "$qualification" \
    sh -c "python -c '$sil_absent' && python models/can/example/independent.py \
        models/can/example/example.json $fmu $out/independent.json")
status=0
timeout "$deadline" docker start -a "$container" || status=$?
cleanup
container=
if [[ $status -ne 0 ]]; then
    echo "independent FMI path failed or missed its deadline ($status)" >&2
    exit 1
fi

python3 models/can/bundle_verdict.py "$out"
