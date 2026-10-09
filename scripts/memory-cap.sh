#!/usr/bin/env bash
# Run a command with a hard memory ceiling; never fall back to an uncapped run.
set -euo pipefail

export ONTOKIT_TEST_MEMORY_MIB="${ONTOKIT_TEST_MEMORY_MIB:-6144}"
if [[ ! "$ONTOKIT_TEST_MEMORY_MIB" =~ ^[1-9][0-9]{0,5}$ ]] || (( $# == 0 )); then
    echo 'Usage: ONTOKIT_TEST_MEMORY_MIB=<positive MiB> bash scripts/memory-cap.sh command [args...]' >&2
    exit 2
fi
memory_bytes=$((ONTOKIT_TEST_MEMORY_MIB * 1024 * 1024))

if command -v systemd-run >/dev/null &&
    systemd-run --user --scope -p "MemoryMax=$memory_bytes" -p MemorySwapMax=0 true >/dev/null 2>&1; then
    exec systemd-run --user --scope -p "MemoryMax=$memory_bytes" -p MemorySwapMax=0 "$@"
fi

echo "systemd user scope unavailable; using prlimit address-space cap ($ONTOKIT_TEST_MEMORY_MIB MiB)" >&2
exec prlimit --as="$memory_bytes:$memory_bytes" -- "$@"
