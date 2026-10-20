#!/usr/bin/env bash
# The container side of the profiling qualification (#319).
#
#     container.sh images IDS                      # build the three images
#     container.sh build IDS WORK                  # build the workload
#     container.sh run IDS WORK EVIDENCE [DOCKER-OPTION...] -- QUALIFY-ARG...
#     container.sh render IDS SETS EVIDENCE        # offline render
#
# `images` builds the runtime image with the command of docs/container.md
# ("Build an image from a checkout"), the profiling image from
# profile.Dockerfile (the documented one) and the workload builder. It writes
# their IDs to IDS/. `run` starts the profiling image as docs/container.md
# documents it: the host user, `--network none`, and the given options (for
# example `--cap-add PERFMON`). `render` uses the plain runtime image, which
# has no perf and no Heaptrack. The checkout is mounted read-only at /src,
# WORK at /work, EVIDENCE and SETS at /evidence and /sets.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/../.." && pwd)
action=$1
ids=$2
mkdir -p "$ids"
ids=$(cd "$ids" && pwd)
user=(--user "$(id -u):$(id -g)")
src=(--mount "type=bind,src=$root,dst=/src,readonly")
qualify=/src/proofs/profiling-qualification/qualify.py
case $action in
images)
    platform=${SIL_QUALIFY_PLATFORM:-linux/amd64}
    version=$(python3 "$root/tools/release.py" project-version --root "$root")
    revision=${SIL_SOURCE_REVISION:-$(git -C "$root" rev-parse HEAD)}
    build_args=()
    if [[ -n "${SIL_QUALIFY_PYTHON_IMAGE:-}" ]]; then
        build_args=(--build-arg "PYTHON_IMAGE=$SIL_QUALIFY_PYTHON_IMAGE")
    fi
    docker build --platform "$platform" --target runtime -t sil:local \
        "${build_args[@]+"${build_args[@]}"}" \
        --build-arg SIL_VERSION="$version" \
        --build-arg SIL_SOURCE_REVISION="$revision" \
        --iidfile "$ids/runtime.txt" "$root"
    docker build --platform "$platform" -t sil-profile:local \
        -f "$here/profile.Dockerfile" --iidfile "$ids/profile.txt" "$here"
    docker build --platform "$platform" -f "$here/builder.Dockerfile" \
        "${build_args[@]+"${build_args[@]}"}" \
        --iidfile "$ids/builder.txt" "$root"
    ;;
build)
    work=$3
    mkdir -p "$work"
    work=$(cd "$work" && pwd)
    docker run --rm --network none "${user[@]}" "${src[@]}" \
        --mount "type=bind,src=$work,dst=/work" \
        "$(cat "$ids/builder.txt")" python "$qualify" build /work
    ;;
run)
    work=$3
    evidence=$4
    shift 4
    options=()
    while [[ $# -gt 0 && $1 != -- ]]; do options+=("$1"); shift; done
    shift
    mkdir -p "$evidence"
    work=$(cd "$work" && pwd)
    evidence=$(cd "$evidence" && pwd)
    printf '%s\n' "${options[@]+"${options[@]}"}" > "$evidence/docker-options.txt"
    cp "$ids/profile.txt" "$evidence/image-id.txt"
    docker run --rm --network none "${user[@]}" "${src[@]}" \
        --mount "type=bind,src=$work,dst=/work" \
        --mount "type=bind,src=$evidence,dst=/evidence" \
        "${options[@]+"${options[@]}"}" --entrypoint python \
        "$(cat "$ids/profile.txt")" "$qualify" run /work /evidence "$@"
    ;;
render)
    sets=$(cd "$3" && pwd)
    mkdir -p "$4"
    evidence=$(cd "$4" && pwd)
    cp "$ids/runtime.txt" "$evidence/image-id.txt"
    docker run --rm --network none "${user[@]}" "${src[@]}" \
        --mount "type=bind,src=$sets,dst=/sets,readonly" \
        --mount "type=bind,src=$evidence,dst=/evidence" \
        --entrypoint python "$(cat "$ids/runtime.txt")" \
        "$qualify" render /sets /evidence/result
    ;;
*)
    echo "usage: container.sh images|build|run|render IDS ..." >&2
    exit 2
    ;;
esac
