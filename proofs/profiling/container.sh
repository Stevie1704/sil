#!/usr/bin/env bash
# The container side of the profiling proof (issue #314).
#
#     container.sh image EVIDENCE                    # build, write image-id.txt
#     container.sh probe EVIDENCE NAME [RUN-OPTION...]
#     container.sh run   EVIDENCE NAME [RUN-OPTION...]
#
# probe and run start the image built by `image` with `--init --network none`
# and the given `docker run` options, for example `--cap-add PERFMON`. They
# copy /work/evidence out to EVIDENCE/NAME, also after a failure, and write
# the options to EVIDENCE/NAME/docker-options.txt. The container is removed.
# SIL_PROFILING_PYTHON_IMAGE overrides the base image, as in proofs/adas-cost.
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
action=$1
evidence=$2
mkdir -p "$evidence"
evidence=$(cd "$evidence" && pwd)
case $action in
image)
    build_args=()
    if [[ -n "${SIL_PROFILING_PYTHON_IMAGE:-}" ]]; then
        build_args=(--build-arg "PYTHON_IMAGE=$SIL_PROFILING_PYTHON_IMAGE")
    fi
    platform=${SIL_PROFILING_PLATFORM:-linux/amd64}
    docker build --platform "$platform" -f "$root/proofs/profiling/Dockerfile" \
        "${build_args[@]+"${build_args[@]}"}" \
        --iidfile "$evidence/image-id.txt" "$root"
    ;;
probe | run)
    name=$3
    shift 3
    image_id=$(cat "$evidence/image-id.txt")
    out="$evidence/$name"
    mkdir -p "$out"
    printf '%s\n' "$@" > "$out/docker-options.txt"
    container=$(docker create --init --network none "$@" "$image_id" \
        python /src/proofs/profiling/proof.py "$action" /work/evidence)
    trap 'docker rm -f "$container" >/dev/null' EXIT
    status=0
    docker start -a "$container" || status=$?
    docker cp "$container:/work/evidence/." "$out/" || true
    exit "$status"
    ;;
*)
    echo "usage: container.sh image|probe|run EVIDENCE [NAME RUN-OPTION...]" >&2
    exit 2
    ;;
esac
