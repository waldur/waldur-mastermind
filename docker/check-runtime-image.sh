#!/bin/sh
# Check that a built mastermind image carries no build toolchain.
#
#   docker/check-runtime-image.sh <image>
#
# Fails when the image has a compiler, a *-dev package, git, ssh, wget, pip or
# uv, or when any of its layers holds a tests directory under /usr/src/waldur
# (a file deleted in a later layer is still in the layer that added it, so the
# layers are read one by one rather than the final filesystem).
#
# Tests directories inside installed third-party packages (/opt/venv) are not
# checked: they ship inside the locked wheels, and the image installs those as
# published rather than editing them after install.
#
# Uses buildah when it is installed (CI), docker otherwise; set
# CONTAINER_ENGINE to choose.
set -eu

IMAGE=${1:?usage: $0 <image>}
ENGINE=${CONTAINER_ENGINE:-$(command -v buildah >/dev/null 2>&1 && echo buildah || echo docker)}
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
FAILED=0

fail() {
    echo "FAIL: $1"
    FAILED=1
}

# Runs as the image's user; the checks only read.
FILESYSTEM_CHECK='
found=""
for tool in gcc cc c++ g++ ld git ssh wget pip pip3 uv; do
    command -v "$tool" >/dev/null 2>&1 && found="$found $tool"
done
[ -z "$found" ] || echo "tools on PATH:$found"
dev=$(dpkg-query -W -f "\${binary:Package}\n" | grep -E -- "-dev(:|$)" | tr "\n" " ")
[ -z "$dev" ] || echo "dev packages: $dev"
# Both interpreters: the venv does not see the base image site-packages, where
# the base image puts pip.
for python in python3 /usr/local/bin/python3; do
    "$python" -c "import pip" 2>/dev/null && echo "python module: pip ($python)"
done
true
'

echo "[+] Checking $IMAGE with $ENGINE"

case "$ENGINE" in
buildah)
    buildah push "$IMAGE" "docker-archive:$WORK/image.tar"
    container=$(buildah from "$IMAGE")
    problems=$(buildah run "$container" -- sh -c "$FILESYSTEM_CHECK")
    buildah rm "$container" >/dev/null
    ;;
docker)
    docker save -o "$WORK/image.tar" "$IMAGE"
    problems=$(docker run --rm --network none --entrypoint sh "$IMAGE" -c "$FILESYSTEM_CHECK")
    ;;
*)
    echo "unknown CONTAINER_ENGINE: $ENGINE" >&2
    exit 2
    ;;
esac

[ -z "$problems" ] || fail "$problems"

# The archive holds each layer as a tar of its own (docker-archive, or the OCI
# layout newer docker writes). Anything that is not a tar is skipped.
mkdir "$WORK/archive"
tar -xf "$WORK/image.tar" -C "$WORK/archive"
find "$WORK/archive" -type f | while read -r blob; do
    tar -tf "$blob" 2>/dev/null || true
done | sed 's|^\./||' >"$WORK/paths"
# Guards against reading nothing (an unreadable layer format) and passing.
if ! grep -q '^usr/src/waldur/' "$WORK/paths"; then
    fail "could not list the image layers: no /usr/src/waldur entries found"
fi
grep -E '^usr/src/waldur/(.*/)?tests(/|$)' "$WORK/paths" | sort -u >"$WORK/tests" || true
if [ -s "$WORK/tests" ]; then
    fail "tests directories in the image layers ($(wc -l <"$WORK/tests") paths):"
    head -20 "$WORK/tests"
fi

if [ "$FAILED" -ne 0 ]; then
    exit 1
fi
echo "[+] OK: no compiler, dev packages, git, ssh, wget, pip or uv; no tests under /usr/src/waldur in any layer"
