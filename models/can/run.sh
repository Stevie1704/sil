#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."
mkdir -p build/can
docker build --platform linux/amd64 -t sil-can-qualification -f models/can/Dockerfile .
docker image inspect sil-can-qualification --format '{{.Id}}' > build/can/image.txt
git rev-parse HEAD > build/can/source-revision.txt
docker run --rm --platform linux/amd64 --network none \
    --user "$(id -u):$(id -g)" -e HOME=/tmp \
    -v "$PWD:/work" sil-can-qualification bash models/can/qualify.sh
