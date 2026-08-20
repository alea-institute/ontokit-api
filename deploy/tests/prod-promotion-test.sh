#!/usr/bin/env bash

set -euo pipefail

readonly TEST_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
readonly SMOKE_SCRIPT="$TEST_ROOT/deploy/smoke-release.sh"
readonly GATE_SCRIPT="$TEST_ROOT/deploy/check-prod-gate.sh"

failures=0

pass() { printf 'PASS: %s\n' "$1"; }
fail() { printf 'FAIL: %s\n' "$1" >&2; return 1; }

test_disabled_gate_refuses_promotion() (
    local output rc
    set +e
    output=$(PROD_ENABLED=false PROD_DEPLOY_HOST=prod.example.test \
        PROD_DEPLOY_USER=deploy "$GATE_SCRIPT" 2>&1)
    rc=$?
    set -e
    [[ $rc == 78 ]] || { fail "disabled gate returned $rc instead of 78"; return; }
    [[ $output == *"PROD_ENABLED is not true"* ]] || {
        fail "disabled reason was not explicit"
        return
    }
    pass "PROD gate is visibly disabled"
)

test_enabled_gate_requires_host_contract() (
    local output rc
    set +e
    output=$(PROD_ENABLED=true "$GATE_SCRIPT" 2>&1)
    rc=$?
    set -e
    [[ $rc != 0 ]] || { fail "enabled gate accepted missing host prerequisites"; return; }
    [[ $output == *"PROD_DEPLOY_HOST"* ]] || {
        fail "missing host contract was not named"
        return
    }
    pass "enabled gate fails closed without host prerequisites"
)

test_enabled_gate_accepts_complete_non_secret_contract() (
    local output
    output=$(PROD_ENABLED=true PROD_DEPLOY_HOST=prod.example.test \
        PROD_DEPLOY_USER=ontokit-deploy "$GATE_SCRIPT") || return
    [[ $output == enabled:* ]] || {
        fail "complete non-secret gate contract was not accepted"
        return
    }
    pass "complete non-secret gate contract reaches the protected job boundary"
)

test_write_smoke_requires_auth_contract_before_network() (
    local output rc network_marker
    network_marker=$(mktemp)
    http_request() { printf touched >"$network_marker"; }
    public_http_request() { printf touched >"$network_marker"; }
    export -f http_request
    export -f public_http_request
    set +e
    output=$(SMOKE_BASE_URL=https://api.example.test \
        SMOKE_WEB_URL=https://web.example.test \
        SMOKE_RUN_ID=test-run \
        SMOKE_TEST_OVERRIDE=1 \
        bash -c 'source "$1"; main' bash "$SMOKE_SCRIPT" 2>&1)
    rc=$?
    set -e
    [[ $rc != 0 ]] || { fail "write smoke accepted a missing bearer token"; return; }
    [[ $output == *"SMOKE_BEARER_TOKEN"* ]] || {
        fail "credential contract was not named"
        return
    }
    [[ ! -s $network_marker ]] || {
        fail "network was touched before credential refusal"
        return
    }
    rm -f -- "$network_marker"
    pass "write smoke refuses missing credentials before network access"
)

test_submit_422_blocks_and_cleans_up() (
    local calls output rc
    calls=$(mktemp)
    export CALLS_FILE=$calls
    http_request() {
        local method=$1 url=$2 output_file=$3
        shift 3
        if [[ " $* " == *' --data-binary @- '* ]]; then
            cat >/dev/null
        fi
        printf '%s %s\n' "$method" "$url" >>"$CALLS_FILE"
        case "$url" in
            */health) printf '{"status":"healthy"}' >"$output_file"; printf 200 ;;
            */api/v1/projects\?*) printf '{"items":[]}' >"$output_file"; printf 200 ;;
            https://web.example.test/projects) printf '<html></html>' >"$output_file"; printf 200 ;;
            */api/v1/projects/import)
                printf '{"id":"11111111-1111-1111-1111-111111111111"}' >"$output_file"; printf 201 ;;
            */suggestions/sessions)
                printf '{"session_id":"s_test","branch":"suggest/test"}' >"$output_file"; printf 201 ;;
            */suggestions/sessions/s_test/save)
                printf '{"commit_hash":"abc","branch":"suggest/test","changes_count":1}' >"$output_file"; printf 200 ;;
            */suggestions/sessions/s_test/submit)
                printf '{"detail":"validation failed"}' >"$output_file"; printf 422 ;;
            */api/v1/projects/11111111-1111-1111-1111-111111111111)
                : >"$output_file"; printf 204 ;;
            *) printf '{}' >"$output_file"; printf 500 ;;
        esac
    }
    public_http_request() {
        local method=$1 url=$2 output_file=$3
        printf '%s %s public\n' "$method" "$url" >>"$CALLS_FILE"
        printf '<html></html>' >"$output_file"
        printf 200
    }
    export -f http_request
    export -f public_http_request
    set +e
    output=$(SMOKE_BASE_URL=https://api.example.test \
        SMOKE_WEB_URL=https://web.example.test \
        SMOKE_BEARER_TOKEN=test-only-token \
        SMOKE_RUN_ID=test-run \
        SMOKE_TEST_OVERRIDE=1 \
        bash -c 'source "$1"; main' bash "$SMOKE_SCRIPT" 2>&1)
    rc=$?
    set -e
    [[ $rc != 0 ]] || { fail "422 submit did not block the smoke"; return; }
    [[ $output == *"submit returned HTTP 422"* ]] || {
        fail "422 cause was not reported"
        return
    }
    grep -q 'DELETE.*/api/v1/projects/11111111-1111-1111-1111-111111111111' "$calls" || \
        { fail "throwaway project was not cleaned up after failed submit"; return; }
    ! grep -q 'https://web.example.test/projects$' "$calls" || {
        fail "authenticated request helper sent the bearer token to the web origin"
        return
    }
    grep -q 'https://web.example.test/projects public$' "$calls" || {
        fail "web route did not use the public request helper"
        return
    }
    rm -f -- "$calls"
    pass "failed write smoke blocks promotion and cleans up"
)

test_green_write_smoke_passes_and_cleans_up() (
    local calls output
    calls=$(mktemp)
    export CALLS_FILE=$calls
    http_request() {
        local method=$1 url=$2 output_file=$3
        shift 3
        if [[ " $* " == *' --data-binary @- '* ]]; then
            cat >/dev/null
        fi
        printf '%s %s\n' "$method" "$url" >>"$CALLS_FILE"
        case "$url" in
            */health) printf '{"status":"healthy"}' >"$output_file"; printf 200 ;;
            */api/v1/projects\?*) printf '{"items":[]}' >"$output_file"; printf 200 ;;
            https://web.example.test/projects) printf '<html></html>' >"$output_file"; printf 200 ;;
            */api/v1/projects/import)
                printf '{"id":"22222222-2222-2222-2222-222222222222"}' >"$output_file"; printf 201 ;;
            */suggestions/sessions)
                printf '{"session_id":"s_green","branch":"suggest/green"}' >"$output_file"; printf 201 ;;
            */suggestions/sessions/s_green/save)
                printf '{"commit_hash":"abc","branch":"suggest/green","changes_count":1}' >"$output_file"; printf 200 ;;
            */suggestions/sessions/s_green/submit)
                printf '{"pr_number":1,"pr_url":null,"status":"pending"}' >"$output_file"; printf 200 ;;
            */api/v1/projects/22222222-2222-2222-2222-222222222222)
                : >"$output_file"; printf 204 ;;
            *) printf '{}' >"$output_file"; printf 500 ;;
        esac
    }
    public_http_request() {
        local method=$1 url=$2 output_file=$3
        printf '%s %s public\n' "$method" "$url" >>"$CALLS_FILE"
        printf '<html></html>' >"$output_file"
        printf 200
    }
    export -f http_request
    export -f public_http_request
    output=$(SMOKE_BASE_URL=https://api.example.test \
        SMOKE_WEB_URL=https://web.example.test \
        SMOKE_BEARER_TOKEN=test-only-token \
        SMOKE_RUN_ID=test-run \
        SMOKE_TEST_OVERRIDE=1 \
        bash -c 'source "$1"; main' bash "$SMOKE_SCRIPT") || return
    [[ $output == *'smoke=passed'* ]] || {
        fail "green write path did not report success"
        return
    }
    grep -q 'DELETE.*/api/v1/projects/22222222-2222-2222-2222-222222222222' "$calls" || {
        fail "green smoke did not clean up its throwaway project"
        return
    }
    rm -f -- "$calls"
    pass "green isolated write smoke passes and cleans up"
)

test_disabled_gate_refuses_promotion || failures=$((failures + 1))
test_enabled_gate_requires_host_contract || failures=$((failures + 1))
test_enabled_gate_accepts_complete_non_secret_contract || failures=$((failures + 1))
test_write_smoke_requires_auth_contract_before_network || failures=$((failures + 1))
test_submit_422_blocks_and_cleans_up || failures=$((failures + 1))
test_green_write_smoke_passes_and_cleans_up || failures=$((failures + 1))
exit "$failures"
