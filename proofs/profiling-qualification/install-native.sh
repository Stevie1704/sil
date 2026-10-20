#!/usr/bin/env bash
# Installs SiL natively as a release user does (#319): the wheel and the
# native development archive of this checkout, built as the release workflow
# builds them (.github/workflows/release.yml), served from a local mirror of
# the release download layout and installed with the release's install.sh.
#
#     install-native.sh PREFIX OUT
#
# OUT receives the release files. PREFIX/env puts the installed version on
# PATH. Needs uv, docker (the native prefix uses the runtime image's pinned
# bookworm toolchain) and python3.
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
prefix=$1
out=$2
mkdir -p "$out/dist"
out=$(cd "$out" && pwd)
version=$(python3 "$root/tools/release.py" project-version --root "$root")
revision=${SIL_SOURCE_REVISION:-$(git -C "$root" rev-parse HEAD)}
python3 "$root/tools/release.py" stamp-python --root "$root" \
    --output-dir "$out/python-release" --version "$version" \
    --source-revision "$revision"
uv build --wheel --out-dir "$out/dist" "$out/python-release"
docker build --target build --tag "sil-native-build:qualify" \
    --build-arg SIL_VERSION="$version" \
    --build-arg SIL_SOURCE_REVISION="$revision" "$root"
container=$(docker create sil-native-build:qualify)
trap 'docker rm "$container" >/dev/null' EXIT
rm -rf "$out/native"
docker cp "$container:/opt/sil/native" "$out/native"
python3 "$root/tools/release.py" make-native-archive --prefix "$out/native" \
    --output "$out/dist/sil-native-dev-${version}-linux-x86_64.tar.gz" \
    --version "$version"
python3 "$root/tools/release.py" stamp-installer --root "$root" \
    --output "$out/dist/install.sh" --version "$version"
mirror="$out/mirror"
mkdir -p "$mirror/v$version"
cp "$out/dist"/* "$mirror/v$version/"
(cd "$mirror/v$version" && sha256sum -- * > SHA256SUMS)
SIL_RELEASE_URL="file://$mirror" sh "$out/dist/install.sh" --prefix "$prefix"
