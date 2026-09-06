#!/usr/bin/env bash

set -euo pipefail

readonly TEST_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
readonly DEPLOY_SCRIPT="$TEST_DIR/../ontokit-deploy.sh"
readonly API_SHA=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
readonly WEB_SHA=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
readonly OLD_API_SHA=1111111111111111111111111111111111111111
readonly OLD_WEB_SHA=2222222222222222222222222222222222222222
readonly TMP_ROOT=$(mktemp -d)
readonly TEST_DEPLOY_ROOT="$TMP_ROOT/ontokit"
readonly TEST_SCRIPT="$TMP_ROOT/ontokit-deploy.sh"

trap 'rm -rf -- "$TMP_ROOT"' EXIT

fail() {
    printf 'not ok - %s\n' "$1" >&2
    return 1
}

pass() {
    printf 'ok - %s\n' "$1"
}

assert_contains() {
    local haystack=$1
    local needle=$2
    local message=$3

    [[ $haystack == *"$needle"* ]] || fail "$message (missing: $needle)"
}

assert_empty_file() {
    local path=$1
    local message=$2

    [[ ! -s $path ]] || fail "$message (contents: $(<"$path"))"
}

prepare_test_script() {
    mkdir -p "$TEST_DEPLOY_ROOT/ontokit-api" "$TEST_DEPLOY_ROOT/ontokit-web"
    sed \
        -e "s|readonly DEPLOY_ROOT=/opt/ontokit|readonly DEPLOY_ROOT=$TEST_DEPLOY_ROOT|" \
        -e "s|readonly LOCK_FILE=/var/lock/ontokit-deploy.lock|readonly LOCK_FILE=$TMP_ROOT/ontokit-deploy.lock|" \
        -e "s|readonly LOG_FILE=/var/log/ontokit-deploy.log|readonly LOG_FILE=$TMP_ROOT/ontokit-deploy.log|" \
        "$DEPLOY_SCRIPT" >"$TEST_SCRIPT"
}

test_invalid_config_refuses_before_checkout() (
    local call_log="$TMP_ROOT/config-order.log"
    local deployed=0
    : >"$call_log"

    cat >"$TEST_DEPLOY_ROOT/.env" <<'EOF'
AUTH_MODE=bogus
ZITADEL_ISSUER=https://issuer.example.test
ZITADEL_CLIENT_ID=test-client
EOF

    # shellcheck source=../ontokit-deploy.sh
    source "$TEST_SCRIPT"

    fetch_and_verify() { :; }
    record_previous_pair() { :; }
    git() {
        if [[ $* == *' rev-parse --verify HEAD' ]]; then
            if [[ $* == *ontokit-api* ]]; then
                printf '%s\n' "$OLD_API_SHA"
            else
                printf '%s\n' "$OLD_WEB_SHA"
            fi
        else
            printf '%s\n' "$*" >>"$call_log"
        fi
    }
    docker() { :; }
    timeout() { shift; "$@"; }
    compose_status() { :; }

    if deploy_pair "$API_SHA" "$WEB_SHA" >/dev/null 2>&1; then
        deployed=1
    fi
    [[ $deployed == 0 ]] || {
        fail 'deployment unexpectedly accepted invalid AUTH_MODE'
        return
    }
    assert_empty_file "$call_log" 'configuration refusal occurred after a git mutation' || return
    pass 'invalid configuration refuses before checkout'
)

test_status_uses_running_image_revisions_and_flags_drift() (
    local output rc

    # shellcheck source=../ontokit-deploy.sh
    source "$TEST_SCRIPT"

    repo_sha() {
        if [[ $1 == *ontokit-api ]]; then
            printf '%s\n' "$OLD_API_SHA"
        else
            printf '%s\n' "$OLD_WEB_SHA"
        fi
    }
    running_image_revision() {
        case "$1" in
            api) printf '%s\n' "$API_SHA" ;;
            web) printf '%s\n' "$WEB_SHA" ;;
            *) return 1 ;;
        esac
    }
    compose_status() { :; }

    set +e
    output=$(show_status 2>&1)
    rc=$?
    set -e

    [[ $rc -ne 0 ]] || {
        fail 'status accepted checkout/runtime drift'
        return
    }
    assert_contains "$output" "api_runtime=$API_SHA" 'status omitted running API revision' || return
    assert_contains "$output" "web_runtime=$WEB_SHA" 'status omitted running web revision' || return
    assert_contains "$output" "api_checkout=$OLD_API_SHA" 'status omitted API checkout revision' || return
    assert_contains "$output" "web_checkout=$OLD_WEB_SHA" 'status omitted web checkout revision' || return
    assert_contains "$output" 'revision_drift=detected' 'status did not flag revision drift' || return
    pass 'status reports running image revisions and flags drift'
)

test_optional_auth_without_zitadel_is_valid() (
    local compose_calls="$TMP_ROOT/optional-compose.log"

    cat >"$TEST_DEPLOY_ROOT/.env" <<'EOF'
AUTH_MODE=optional
ZITADEL_ISSUER=
ZITADEL_CLIENT_ID=
EOF
    : >"$compose_calls"

    # shellcheck source=../ontokit-deploy.sh
    source "$TEST_SCRIPT"
    docker() {
        printf '%s\n' "$*" >>"$compose_calls"
    }

    load_and_validate_config "$API_SHA" "$WEB_SHA" || return
    [[ $EFFECTIVE_AUTH_MODE == optional ]] || {
        fail 'optional mode was not retained as the effective auth mode'
        return
    }
    [[ $AUTH_MODE == optional ]] || {
        fail 'optional mode was not exported coherently'
        return
    }
    assert_contains "$(<"$compose_calls")" 'config --quiet' \
        'compose configuration was not preflighted' || return
    pass 'optional auth without Zitadel remains valid'
)

test_configured_zitadel_requires_full_server_set() (
    local compose_calls="$TMP_ROOT/incomplete-compose.log"

    cat >"$TEST_DEPLOY_ROOT/.env" <<'EOF'
AUTH_MODE=optional
AUTH_SECRET=session-secret
ZITADEL_ISSUER=https://issuer.example.test
ZITADEL_CLIENT_ID=test-client
ZITADEL_CLIENT_SECRET=
EOF
    : >"$compose_calls"

    # shellcheck source=../ontokit-deploy.sh
    source "$TEST_SCRIPT"
    docker() {
        printf '%s\n' "$*" >>"$compose_calls"
    }

    if load_and_validate_config "$API_SHA" "$WEB_SHA" >/dev/null 2>&1; then
        fail 'incomplete Zitadel server configuration was accepted'
        return
    fi
    assert_empty_file "$compose_calls" \
        'incomplete Zitadel configuration reached Compose before refusal' || return
    pass 'configured Zitadel requires the full server-side credential set'
)

test_auth_secret_maps_to_nextauth_contract() (
    cat >"$TEST_DEPLOY_ROOT/.env" <<'EOF'
AUTH_MODE=optional
AUTH_SECRET=session-secret
ZITADEL_ISSUER=https://issuer.example.test
ZITADEL_CLIENT_ID=test-client
ZITADEL_CLIENT_SECRET=test-client-secret
EOF

    # shellcheck source=../ontokit-deploy.sh
    source "$TEST_SCRIPT"
    docker() { :; }

    load_and_validate_config "$API_SHA" "$WEB_SHA" || return
    [[ $NEXTAUTH_SECRET == session-secret ]] || {
        fail 'AUTH_SECRET was not mapped to the web NEXTAUTH_SECRET contract'
        return
    }
    pass 'AUTH_SECRET maps to the web session-secret contract'
)

test_forced_command_rejects_hostile_input() (
    local mutated=0

    # shellcheck source=../ontokit-deploy.sh
    source "$TEST_SCRIPT"
    deploy_pair() { mutated=1; }

    if dispatch_request 'deploy; rm -rf /' >/dev/null 2>&1; then
        fail 'hostile forced-command input was accepted'
        return
    fi
    [[ $mutated == 0 ]] || {
        fail 'hostile forced-command input reached deployment'
        return
    }
    pass 'forced-command input hardening is preserved'
)

test_failed_rollback_preserves_retryable_pair() (
    local before after

    # shellcheck source=../ontokit-deploy.sh
    source "$TEST_SCRIPT"
    printf '%s %s\n' "$API_SHA" "$WEB_SHA" >"$PREVIOUS_FILE"
    before=$(<"$PREVIOUS_FILE")
    repo_sha() {
        if [[ $1 == *ontokit-api ]]; then
            printf '%s\n' "$OLD_API_SHA"
        else
            printf '%s\n' "$OLD_WEB_SHA"
        fi
    }
    deploy_pair() { return 1; }

    if rollback_pair >/dev/null 2>&1; then
        fail 'failed rollback unexpectedly reported success'
        return
    fi
    after=$(<"$PREVIOUS_FILE")
    [[ $after == "$before" ]] || {
        fail 'failed rollback replaced the retryable previous pair'
        return
    }
    pass 'failed rollback preserves the retryable pair'
)

test_matching_runtime_pair_has_no_drift() (
    local output rc

    rm -f -- "$TEST_DEPLOY_ROOT/.env"
    # shellcheck source=../ontokit-deploy.sh
    source "$TEST_SCRIPT"
    repo_sha() {
        if [[ $1 == *ontokit-api ]]; then
            printf '%s\n' "$API_SHA"
        else
            printf '%s\n' "$WEB_SHA"
        fi
    }
    running_image_revision() {
        case "$1" in
            api | worker) printf '%s\n' "$API_SHA" ;;
            web) printf '%s\n' "$WEB_SHA" ;;
            *) return 1 ;;
        esac
    }
    compose_status() { :; }

    set +e
    output=$(show_status 2>&1)
    rc=$?
    set -e

    [[ $rc == 0 ]] || {
        fail 'status rejected a matching healthy runtime pair'
        return
    }
    assert_contains "$output" 'revision_drift=none' \
        'status falsely reported checkout/runtime drift' || return
    pass 'matching runtime pair reports no drift without deployment config'
)

prepare_test_script
failures=0
test_invalid_config_refuses_before_checkout || failures=$((failures + 1))
test_status_uses_running_image_revisions_and_flags_drift || failures=$((failures + 1))
test_optional_auth_without_zitadel_is_valid || failures=$((failures + 1))
test_configured_zitadel_requires_full_server_set || failures=$((failures + 1))
test_auth_secret_maps_to_nextauth_contract || failures=$((failures + 1))
test_forced_command_rejects_hostile_input || failures=$((failures + 1))
test_failed_rollback_preserves_retryable_pair || failures=$((failures + 1))
test_matching_runtime_pair_has_no_drift || failures=$((failures + 1))
exit "$failures"
