#!/usr/bin/env bash

set -euo pipefail

readonly TEST_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
readonly DEPLOY_SCRIPT="$TEST_DIR/../ontokit-deploy.sh"
readonly API_SHA=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
readonly WEB_SHA=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
readonly OLD_API_SHA=1111111111111111111111111111111111111111
readonly OLD_WEB_SHA=2222222222222222222222222222222222222222
readonly API_DIGEST=sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc
readonly WEB_DIGEST=sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd
readonly OLD_API_DIGEST=sha256:3333333333333333333333333333333333333333333333333333333333333333
readonly OLD_WEB_DIGEST=sha256:4444444444444444444444444444444444444444444444444444444444444444
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

test_same_pair_retry_preserves_previous() (
    local outcome before result call_log="$TMP_ROOT/retry-calls.log"
    source "$TEST_SCRIPT"
    load_and_validate_config() { :; }
    fetch_and_verify() { :; }
    repo_sha() {
        if [[ $1 == *ontokit-api ]]; then printf '%s\n' "$API_SHA";
        else printf '%s\n' "$WEB_SHA"; fi
    }
    git() { :; }
    docker() {
        printf '%s\n' "$*" >>"$call_log"
        [[ $outcome != build || $* != *'build api'* ]] || return 1
        [[ $outcome != up || $* != *'up -d'* ]] || return 1
    }
    timeout() { shift; "$@"; }
    compose_status() { [[ $outcome != health ]]; }
    verify_runtime_pair() { [[ $outcome != revision ]]; }

    for outcome in build up health revision success; do
        printf '%s %s\n' "$OLD_API_SHA" "$OLD_WEB_SHA" >"$PREVIOUS_FILE"
        before=$(<"$PREVIOUS_FILE")
        : >"$call_log"
        result=0
        deploy_pair "$API_SHA" "$WEB_SHA" >/dev/null 2>&1 || result=$?
        if [[ $outcome == success ]]; then
            [[ $result == 0 ]] || { fail 'same-pair retry did not succeed'; return; }
            assert_contains "$(<"$call_log")" 'build --no-cache web' 'same-pair retry skipped rebuilding' || return
        else
            [[ $result != 0 ]] || { fail "same-pair retry hid $outcome failure"; return; }
        fi
        [[ $(<"$PREVIOUS_FILE") == "$before" ]] || {
            fail "same-pair $outcome retry replaced the saved rollback pair"
            return
        }
    done
    pass 'same-pair retries retain rollback through build, startup, health, revision and success outcomes'
)

test_previous_pair_recording_distinguishes_real_change() (
    source "$TEST_SCRIPT"
    repo_sha() {
        if [[ $1 == *ontokit-api ]]; then printf '%s\n' "$API_SHA";
        else printf '%s\n' "$WEB_SHA"; fi
    }
    rm -f -- "$PREVIOUS_FILE"
    record_previous_pair "$API_SHA" "$WEB_SHA" || return
    [[ ! -e $PREVIOUS_FILE ]] || { fail 'same-pair recording fabricated a rollback target'; return; }
    record_previous_pair "$OLD_API_SHA" "$WEB_SHA" || return
    [[ $(<"$PREVIOUS_FILE") == "$API_SHA $WEB_SHA" ]] || {
        fail 'API-only change did not retain the previous complete pair'; return;
    }
    rm -f -- "$PREVIOUS_FILE"
    record_previous_pair "$API_SHA" "$OLD_WEB_SHA" || return
    [[ $(<"$PREVIOUS_FILE") == "$API_SHA $WEB_SHA" ]] || {
        fail 'web-only change did not retain the previous complete pair'; return;
    }
    pass 'same-pair recording creates no fake history; either component change retains the prior pair'
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

test_deploy_images_rejects_malformed_requests() (
    local request mutated=0

    # shellcheck source=../ontokit-deploy.sh
    source "$TEST_SCRIPT"
    deploy_images() { mutated=1; }
    deploy_pair() { mutated=1; }

    for request in \
        "deploy-images $API_SHA $WEB_SHA $API_DIGEST" \
        "deploy-images $API_SHA $WEB_SHA $API_DIGEST $WEB_DIGEST extra" \
        "deploy-images $API_SHA $WEB_SHA ${API_DIGEST#sha256:} $WEB_DIGEST" \
        "deploy-images $API_SHA $WEB_SHA ${API_DIGEST^^} $WEB_DIGEST" \
        "deploy-images $API_SHA $WEB_SHA latest $WEB_DIGEST" \
        "deploy-images $API_SHA $WEB_SHA $API_DIGEST;reboot $WEB_DIGEST" \
        "deploy-images ${API_SHA:0:12} $WEB_SHA $API_DIGEST $WEB_DIGEST" \
        "deploy $API_SHA $WEB_SHA $API_DIGEST $WEB_DIGEST"; do
        if dispatch_request "$request" >/dev/null 2>&1; then
            fail "malformed request was accepted: $request"
            return
        fi
    done
    [[ $mutated == 0 ]] || {
        fail 'malformed image request reached deployment'
        return
    }
    pass 'deploy-images refuses malformed SHAs, digests and argument counts'
)

# Stubs for an image deploy whose external effects are logged to $1.
stub_image_deploy() {
    local call_log=$1

    : >"$call_log"
    : >"$COMPOSE_IMAGES_FILE"
    load_and_validate_config() { printf 'config %s\n' "${COMPOSE_ARGS[*]}" >>"$call_log"; }
    fetch_and_verify() { :; }
    repo_sha() {
        if [[ $1 == *ontokit-api ]]; then printf '%s\n' "$OLD_API_SHA";
        else printf '%s\n' "$OLD_WEB_SHA"; fi
    }
    git() { printf 'git %s\n' "$*" >>"$call_log"; }
    docker() { printf 'docker %s\n' "$*" >>"$call_log"; }
    image_revision() {
        case "$1" in
            ghcr.io/alea-institute/ontokit@*) printf '%s\n' "$API_SHA" ;;
            ghcr.io/alea-institute/ontokit-web@*) printf '%s\n' "$WEB_SHA" ;;
        esac
    }
    timeout() { shift; "$@"; }
    compose_status() { :; }
    verify_runtime_pair() { :; }
}

test_deploy_images_pulls_verifies_and_records() (
    local call_log="$TMP_ROOT/images.log" calls

    # shellcheck source=../ontokit-deploy.sh
    source "$TEST_SCRIPT"
    rm -f -- "$PREVIOUS_FILE" "$CURRENT_FILE"
    stub_image_deploy "$call_log"

    deploy_images "$API_SHA" "$WEB_SHA" "$API_DIGEST" "$WEB_DIGEST" >/dev/null || {
        fail 'image deploy failed with healthy stubs'
        return
    }
    calls=$(<"$call_log")
    assert_contains "$calls" "-f $TEST_DEPLOY_ROOT/compose.yaml -f $COMPOSE_IMAGES_FILE" \
        'image deploy did not apply the image overlay' || return
    assert_contains "$calls" 'pull api worker web' 'image deploy did not pull its images' || return
    assert_contains "$calls" 'up -d --no-build --wait' 'image deploy could build on the host' || return
    [[ $calls != *' build '* ]] || {
        fail 'image deploy invoked a host build'
        return
    }
    [[ $ONTOKIT_API_IMAGE == "ghcr.io/alea-institute/ontokit@$API_DIGEST" ]] || {
        fail 'API image reference was not pinned to the requested digest'
        return
    }
    [[ $(<"$CURRENT_FILE") == "$API_SHA $WEB_SHA $API_DIGEST $WEB_DIGEST" ]] || {
        fail 'image deploy did not record the accepted image release'
        return
    }
    [[ $(<"$PREVIOUS_FILE") == "$OLD_API_SHA $OLD_WEB_SHA" ]] || {
        fail 'first image deploy did not keep the source checkout pair as rollback'
        return
    }
    pass 'deploy-images pulls, verifies, starts without building and records both pairs'
)

test_deploy_images_refuses_mislabelled_image_before_mutation() (
    local call_log="$TMP_ROOT/mislabelled.log" calls

    # shellcheck source=../ontokit-deploy.sh
    source "$TEST_SCRIPT"
    rm -f -- "$PREVIOUS_FILE" "$CURRENT_FILE"
    stub_image_deploy "$call_log"
    image_revision() { printf '%s\n' "$OLD_API_SHA"; }

    if deploy_images "$API_SHA" "$WEB_SHA" "$API_DIGEST" "$WEB_DIGEST" >/dev/null 2>&1; then
        fail 'image with a foreign revision label was deployed'
        return
    fi
    calls=$(<"$call_log")
    [[ $calls != *'git '* && $calls != *' up '* ]] || {
        fail 'mislabelled image refusal happened after checkout or startup'
        return
    }
    [[ ! -e $PREVIOUS_FILE && ! -e $CURRENT_FILE ]] || {
        fail 'mislabelled image refusal changed the release records'
        return
    }
    pass 'deploy-images refuses an image whose revision label differs before mutation'
)

test_deploy_images_requires_overlay() (
    local call_log="$TMP_ROOT/overlay.log"

    # shellcheck source=../ontokit-deploy.sh
    source "$TEST_SCRIPT"
    stub_image_deploy "$call_log"
    rm -f -- "$COMPOSE_IMAGES_FILE"

    if deploy_images "$API_SHA" "$WEB_SHA" "$API_DIGEST" "$WEB_DIGEST" >/dev/null 2>&1; then
        fail 'image deploy ran without the image overlay'
        return
    fi
    assert_empty_file "$call_log" 'missing overlay refusal happened after other work' || return
    pass 'deploy-images refuses when the host lacks the image overlay'
)

test_new_deploy_after_failure_keeps_accepted_rollback() (
    local call_log="$TMP_ROOT/accepted.log"

    # shellcheck source=../ontokit-deploy.sh
    source "$TEST_SCRIPT"
    stub_image_deploy "$call_log"
    printf '%s %s %s %s\n' "$OLD_API_SHA" "$OLD_WEB_SHA" "$OLD_API_DIGEST" "$OLD_WEB_DIGEST" \
        >"$CURRENT_FILE"
    rm -f -- "$PREVIOUS_FILE"

    verify_runtime_pair() { return 1; }
    if deploy_images "$API_SHA" "$WEB_SHA" "$API_DIGEST" "$WEB_DIGEST" >/dev/null 2>&1; then
        fail 'unhealthy image deploy reported success'
        return
    fi
    verify_runtime_pair() { :; }
    image_revision() {
        case "$1" in
            *"@$OLD_WEB_DIGEST") printf '%s\n' "$OLD_WEB_SHA" ;;
            *) printf '%s\n' "$API_SHA" ;;
        esac
    }
    deploy_images "$API_SHA" "$OLD_WEB_SHA" "$API_DIGEST" "$OLD_WEB_DIGEST" >/dev/null || {
        fail 'follow-up image deploy failed'
        return
    }
    [[ $(<"$PREVIOUS_FILE") == "$OLD_API_SHA $OLD_WEB_SHA $OLD_API_DIGEST $OLD_WEB_DIGEST" ]] || {
        fail "rollback target became a failed release: $(<"$PREVIOUS_FILE")"
        return
    }
    pass 'a failed release never becomes the rollback target'
)

test_rollback_follows_recorded_mode() (
    local mode_log="$TMP_ROOT/rollback-mode.log"

    # shellcheck source=../ontokit-deploy.sh
    source "$TEST_SCRIPT"
    deploy_images() { printf 'images %s\n' "$*" >"$mode_log"; }
    deploy_pair() { printf 'source %s\n' "$*" >"$mode_log"; }

    printf '%s %s %s %s\n' "$OLD_API_SHA" "$OLD_WEB_SHA" "$OLD_API_DIGEST" "$OLD_WEB_DIGEST" \
        >"$PREVIOUS_FILE"
    printf '%s %s\n' "$API_SHA" "$WEB_SHA" >"$CURRENT_FILE"
    rollback_pair >/dev/null || {
        fail 'image rollback failed'
        return
    }
    [[ $(<"$mode_log") == "images $OLD_API_SHA $OLD_WEB_SHA $OLD_API_DIGEST $OLD_WEB_DIGEST false" ]] || {
        fail "image rollback used the wrong path: $(<"$mode_log")"
        return
    }
    [[ $(<"$PREVIOUS_FILE") == "$API_SHA $WEB_SHA" ]] || {
        fail 'image rollback did not keep the replaced release for roll-forward'
        return
    }

    printf '%s %s\n' "$OLD_API_SHA" "$OLD_WEB_SHA" >"$PREVIOUS_FILE"
    rollback_pair >/dev/null || {
        fail 'source rollback failed'
        return
    }
    [[ $(<"$mode_log") == "source $OLD_API_SHA $OLD_WEB_SHA false" ]] || {
        fail "source rollback used the wrong path: $(<"$mode_log")"
        return
    }

    printf '%s %s %s\n' "$OLD_API_SHA" "$OLD_WEB_SHA" "$OLD_API_DIGEST" >"$PREVIOUS_FILE"
    : >"$mode_log"
    if rollback_pair >/dev/null 2>&1; then
        fail 'rollback accepted a half image record'
        return
    fi
    assert_empty_file "$mode_log" 'half image record reached deployment' || return
    pass 'rollback restores the recorded source or image release and refuses partial records'
)

test_deploy_images_refuses_env_image_override() (
    local call_log="$TMP_ROOT/env-override.log" calls

    # shellcheck source=../ontokit-deploy.sh
    source "$TEST_SCRIPT"
    rm -f -- "$PREVIOUS_FILE" "$CURRENT_FILE"
    stub_image_deploy "$call_log"
    load_and_validate_config() {
        ONTOKIT_WEB_IMAGE=ghcr.io/alea-institute/ontokit-web:latest
    }

    if deploy_images "$API_SHA" "$WEB_SHA" "$API_DIGEST" "$WEB_DIGEST" >/dev/null 2>&1; then
        fail 'an .env image override replaced the validated digest'
        return
    fi
    calls=$(<"$call_log")
    [[ $calls != *'pull'* && $calls != *'git '* ]] || {
        fail 'image override refusal happened after pull or checkout'
        return
    }
    pass 'deploy-images refuses an .env that overrides the validated images'
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
test_same_pair_retry_preserves_previous || failures=$((failures + 1))
test_previous_pair_recording_distinguishes_real_change || failures=$((failures + 1))
test_matching_runtime_pair_has_no_drift || failures=$((failures + 1))
test_deploy_images_rejects_malformed_requests || failures=$((failures + 1))
test_deploy_images_pulls_verifies_and_records || failures=$((failures + 1))
test_deploy_images_refuses_mislabelled_image_before_mutation || failures=$((failures + 1))
test_deploy_images_requires_overlay || failures=$((failures + 1))
test_new_deploy_after_failure_keeps_accepted_rollback || failures=$((failures + 1))
test_rollback_follows_recorded_mode || failures=$((failures + 1))
test_deploy_images_refuses_env_image_override || failures=$((failures + 1))
exit "$failures"
