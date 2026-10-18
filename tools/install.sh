#!/bin/sh
# Install one released SiL version for Linux x86-64 from its GitHub release.
#
# Each version goes into its own directory, PREFIX/VERSION, with the Python
# package in a virtual environment (venv/) and the native runners (native/).
# PREFIX/current points at the version installed last, and PREFIX/env puts
# both bin directories on PATH. The venv's python3 must come first on PATH,
# because Manifests start Python Participants with `python3 -m sil.participant`.
#
# Every download is checked against the release's SHA256SUMS before anything
# is installed. An installed version is never changed or replaced.

set -eu

# The release build writes the version of its release here (tools/release.py
# stamp-installer); in the repository it is empty and --version is required.
DEFAULT_VERSION=""
RELEASE_URL="${SIL_RELEASE_URL:-https://github.com/Stevie1704/sil/releases/download}"
EXIT_FAIL=1
EXIT_USAGE=2

usage() {
    cat <<EOF
usage: install.sh [--version VERSION] [--prefix DIR]

Install the SiL release VERSION for Linux x86-64: the sil command and the
Python package in a virtual environment, and the native runners. Every
download is checked against the release's SHA256SUMS first.

options:
  --version VERSION  the release to install, for example 0.2.0${DEFAULT_VERSION:+
                     (default $DEFAULT_VERSION)}
  --prefix DIR       the directory for all versions (default ~/.local/share/sil)
  -h, --help         show this help

SIL_RELEASE_URL replaces the download base URL, for a mirror.
EOF
}

fail() {
    echo "install.sh: error: $*" >&2
    exit "$EXIT_FAIL"
}

usage_error() {
    echo "install.sh: error: $*" >&2
    usage >&2
    exit "$EXIT_USAGE"
}

version="$DEFAULT_VERSION"
prefix="${HOME:?HOME is not set}/.local/share/sil"
while [ $# -gt 0 ]; do
    case "$1" in
        --version)
            [ $# -ge 2 ] || usage_error "--version needs a value"
            version="$2"
            shift 2
            ;;
        --prefix)
            [ $# -ge 2 ] || usage_error "--prefix needs a value"
            prefix="$2"
            shift 2
            ;;
        -h | --help)
            usage
            exit 0
            ;;
        *)
            usage_error "unknown argument '$1'"
            ;;
    esac
done

[ -n "$version" ] || usage_error "give the release with --version, for example --version 0.2.0"
case "$version" in
    *[!0-9.]* | .* | *. | *..*) usage_error "'$version' is not a release version such as 0.2.0" ;;
esac
[ "$(echo "$version" | tr -cd . | wc -c | tr -d ' ')" = 2 ] ||
    usage_error "'$version' is not a release version such as 0.2.0"

system="$(uname -s)"
machine="$(uname -m)"
if [ "$system" != Linux ] || [ "$machine" != x86_64 ]; then
    fail "SiL releases support Linux x86-64 only, not $system $machine. On Windows, run this script in WSL2 (Ubuntu 24.04). Otherwise use the runtime image with Docker, on a Mac with --platform linux/amd64: the release notes give its digest, and 'Windows and macOS' in docs/getting-started.md the commands"
fi

for tool in curl tar python3; do
    command -v "$tool" >/dev/null 2>&1 || fail "'$tool' is not on PATH"
done
python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' ||
    fail "SiL needs Python 3.11 or newer; python3 is $(python3 -c 'import platform; print(platform.python_version())')"

if command -v sha256sum >/dev/null 2>&1; then
    sha256() { sha256sum "$1" | cut -d' ' -f1; }
elif command -v shasum >/dev/null 2>&1; then
    sha256() { shasum -a 256 "$1" | cut -d' ' -f1; }
else
    fail "neither 'sha256sum' nor 'shasum' is on PATH"
fi

wheel="sil-$version-py3-none-any.whl"
archive="sil-native-dev-$version-linux-x86_64.tar.gz"
target="$prefix/$version"
complete="$target/.complete"

write_env() {
    cat >"$prefix/env.tmp" <<EOF
# Puts the current SiL version on PATH. Source it: . "$prefix/env"
PATH="$prefix/current/venv/bin:$prefix/current/native/bin:\$PATH"
export PATH
EOF
    mv "$prefix/env.tmp" "$prefix/env"
}

make_current() {
    # Not `mv` over the old link: mv moves into a directory a link points at.
    rm -f "$prefix/current"
    ln -s "$version" "$prefix/current"
}

finish() {
    make_current
    write_env
    cat <<EOF
SiL $version is in $target and is the current version.
Put it on PATH in this shell and in your shell profile:

  . "$prefix/env"

This puts the SiL virtual environment first on PATH, so python3 is its
Python, which has the sil package. Then run 'sil docs agent-guide', and
'sil skill install' in a project that a coding agent works on.
EOF
}

mkdir -p "$prefix"
if [ -e "$complete" ]; then
    echo "SiL $version is already installed in $target; nothing was downloaded."
    finish
    exit 0
fi
[ -e "$target" ] &&
    fail "$target exists but is not a complete installation; remove it and run install.sh again"

download="$(mktemp -d "$prefix/.download-$version.XXXXXX")"
installed=false
cleanup() {
    rm -rf "$download"
    if [ "$installed" = false ]; then rm -rf "$target"; fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

base="$RELEASE_URL/v$version"
for file in SHA256SUMS "$wheel" "$archive"; do
    curl --fail --silent --show-error --location --output "$download/$file" "$base/$file" ||
        fail "could not download $base/$file"
done

for file in "$wheel" "$archive"; do
    expected="$(awk -v name="$file" '$2 == name || $2 == "*" name { print $1 }' "$download/SHA256SUMS")"
    [ -n "$expected" ] || fail "SHA256SUMS of release $version has no entry for $file"
    actual="$(sha256 "$download/$file")"
    [ "$actual" = "$expected" ] ||
        fail "$file does not match SHA256SUMS (expected $expected, got $actual); nothing was installed"
done

mkdir -p "$target/native"
tar -xzf "$download/$archive" -C "$target/native" --strip-components=1 ||
    fail "could not unpack $archive"
python3 -m venv "$target/venv" ||
    fail "python3 could not create a virtual environment; on Debian or Ubuntu, install the python3-venv package"
"$target/venv/bin/python" -m pip install --quiet --disable-pip-version-check "$download/$wheel" ||
    fail "pip could not install $wheel"

runner_version="$("$target/native/bin/sil-run" --version)" ||
    fail "the installed sil-run does not start"
[ "$runner_version" = "$version" ] ||
    fail "the installed sil-run reports version '$runner_version', not $version"
"$target/venv/bin/sil" --help >/dev/null || fail "the installed sil command does not start"

touch "$complete"
installed=true
finish
