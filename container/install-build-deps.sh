#!/bin/sh
# Install packages from the snapshot sources configured by the build stage.
set -eu

# Snapshot/CDN failures can include transient 404s as well as 503/504s.
# Retry the fetch phase as a whole, including errors APT does not retry itself.
retry_fetch() {
    attempt=1
    while :; do
        if apt-get \
            -o Acquire::Check-Valid-Until=false \
            -o Acquire::http::Timeout=30 \
            -o Acquire::https::Timeout=30 \
            -o Acquire::Retries=0 "$@"; then
            return 0
        else
            status=$?
        fi
        if [ "$attempt" -ge 3 ]; then
            return "$status"
        fi
        echo "APT fetch failed (attempt $attempt/3, exit $status); retrying" >&2
        sleep "$((attempt * 5))"
        attempt=$((attempt + 1))
    done
}

# A partial index must fail so it is retried rather than silently accepted.
retry_fetch --error-on=any update
retry_fetch --download-only install -y --no-install-recommends "$@"
# Downloads are complete. Do not retry dpkg failures or fetch during install.
apt-get --no-download install -y --no-install-recommends "$@"
