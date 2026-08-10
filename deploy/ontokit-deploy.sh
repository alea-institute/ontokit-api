#!/usr/bin/env bash

set -euo pipefail

readonly DEPLOY_ROOT=/opt/ontokit
readonly API_REPO="$DEPLOY_ROOT/ontokit-api"
readonly WEB_REPO="$DEPLOY_ROOT/ontokit-web"
readonly ENV_FILE="$DEPLOY_ROOT/.env"
readonly PREVIOUS_FILE="$DEPLOY_ROOT/.deploy-previous"
readonly LOCK_FILE=/var/lock/ontokit-deploy.lock
readonly LOG_FILE=/var/log/ontokit-deploy.log
readonly SHA_PATTERN='^[0-9a-f]{40}$'

LOG_VERB=refused
LOG_API_SHA=-
LOG_WEB_SHA=-

log_outcome() {
    local exit_status=$?
    local outcome=success

    if ((exit_status != 0)); then
        outcome="failure($exit_status)"
    fi

    printf '%s verb=%s api=%s web=%s outcome=%s\n' \
        "$(date --utc '+%Y-%m-%dT%H:%M:%SZ')" \
        "$LOG_VERB" "$LOG_API_SHA" "$LOG_WEB_SHA" "$outcome" >>"$LOG_FILE"
}

refuse() {
    printf 'refused: %s\n' "$1" >&2
    return 64
}

valid_sha() {
    [[ $1 =~ $SHA_PATTERN ]]
}

repo_sha() {
    git -C "$1" rev-parse --verify HEAD
}

compose_status() {
    local -a services=()
    local service_output service container_id state health summary
    local failed=0

    service_output=$(docker compose --project-directory "$DEPLOY_ROOT" config --services) || {
        printf 'unable to discover compose services\n' >&2
        return 1
    }
    [[ -n $service_output ]] || {
        printf 'compose service list is empty\n' >&2
        return 1
    }
    mapfile -t services <<<"$service_output"
    for service in "${services[@]}"; do
        container_id=$(docker compose --project-directory "$DEPLOY_ROOT" ps -q "$service")
        if [[ -z $container_id ]]; then
            printf '%s=missing\n' "$service"
            failed=1
            continue
        fi

        state=$(docker inspect --format '{{.State.Status}}' "$container_id")
        health=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$container_id")
        summary=$state
        if [[ -n $health ]]; then
            summary="$state/$health"
        fi
        printf '%s=%s\n' "$service" "$summary"

        if [[ $state != running || (-n $health && $health != healthy) ]]; then
            failed=1
        fi
    done

    return "$failed"
}

show_status() {
    local api_sha web_sha

    api_sha=$(repo_sha "$API_REPO")
    web_sha=$(repo_sha "$WEB_REPO")
    LOG_API_SHA=$api_sha
    LOG_WEB_SHA=$web_sha
    printf 'api=%s web=%s\n' "$api_sha" "$web_sha"
    compose_status
}

fetch_and_verify() {
    local repo=$1
    local sha=$2
    local remote_refs

    git -C "$repo" fetch --prune origin
    remote_refs=$(git -C "$repo" for-each-ref --contains="$sha" \
        --format='%(refname)' refs/remotes/origin/) || {
        printf 'refused: cannot inspect fetched refs in %s\n' "$repo" >&2
        return 65
    }
    if [[ -z $remote_refs ]]; then
        printf 'refused: SHA %s is not fetchable in %s\n' "$sha" "$repo" >&2
        return 65
    fi
}

write_previous_pair() {
    local api_sha=$1
    local web_sha=$2
    local previous_tmp

    previous_tmp="$PREVIOUS_FILE.tmp.$$"
    printf '%s %s\n' "$api_sha" "$web_sha" >"$previous_tmp"
    chmod 600 "$previous_tmp"
    mv "$previous_tmp" "$PREVIOUS_FILE"
}

record_previous_pair() {
    local api_sha web_sha

    api_sha=$(repo_sha "$API_REPO")
    web_sha=$(repo_sha "$WEB_REPO")
    valid_sha "$api_sha" || refuse 'current API checkout is not a full SHA'
    valid_sha "$web_sha" || refuse 'current web checkout is not a full SHA'
    write_previous_pair "$api_sha" "$web_sha"
}

deploy_pair() {
    local api_sha=$1
    local web_sha=$2
    local record_previous=${3:-true}

    LOG_API_SHA=$api_sha
    LOG_WEB_SHA=$web_sha

    fetch_and_verify "$API_REPO" "$api_sha"
    fetch_and_verify "$WEB_REPO" "$web_sha"
    if [[ $record_previous == true ]]; then
        record_previous_pair
    fi
    git -C "$API_REPO" checkout --detach "$api_sha"
    git -C "$WEB_REPO" checkout --detach "$web_sha"

    set -a
    # shellcheck disable=SC1091 # This root-owned, server-local file is intentionally not in Git.
    source "$ENV_FILE"
    set +a
    : "${AUTH_MODE:?AUTH_MODE must be set in $ENV_FILE}"
    : "${ZITADEL_ISSUER:?ZITADEL_ISSUER must be set in $ENV_FILE}"
    : "${ZITADEL_CLIENT_ID:?ZITADEL_CLIENT_ID must be set in $ENV_FILE}"

    docker compose --project-directory "$DEPLOY_ROOT" build api
    docker compose --project-directory "$DEPLOY_ROOT" build --no-cache \
        --build-arg "AUTH_MODE=$AUTH_MODE" \
        --build-arg "ZITADEL_ISSUER=$ZITADEL_ISSUER" \
        --build-arg "ZITADEL_CLIENT_ID=$ZITADEL_CLIENT_ID" \
        web

    # Do not auto-rollback here: a transient healthcheck failure is safer held for inspection.
    if ! timeout 300 docker compose --project-directory "$DEPLOY_ROOT" \
        up -d --wait --wait-timeout 240; then
        printf 'deploy failed: compose did not become ready; rollback was not automatic\n' >&2
        return 1
    fi

    if ! compose_status; then
        printf 'deploy failed: one or more containers are not ready\n' >&2
        return 1
    fi
    printf 'deployed api=%s web=%s\n' "$api_sha" "$web_sha"
}

rollback_pair() {
    local api_sha web_sha extra current_api_sha current_web_sha

    if [[ ! -r $PREVIOUS_FILE ]]; then
        refuse 'no previous deployment pair is recorded'
        return
    fi
    read -r api_sha web_sha extra <"$PREVIOUS_FILE"
    if [[ -n ${extra:-} ]] || ! valid_sha "${api_sha:-}" || ! valid_sha "${web_sha:-}"; then
        refuse 'recorded previous deployment pair is invalid'
        return
    fi

    current_api_sha=$(repo_sha "$API_REPO")
    current_web_sha=$(repo_sha "$WEB_REPO")
    valid_sha "$current_api_sha" || refuse 'current API checkout is not a full SHA'
    valid_sha "$current_web_sha" || refuse 'current web checkout is not a full SHA'

    # Preserve the known-good target if this rollback attempt fails so it remains retryable.
    deploy_pair "$api_sha" "$web_sha" false
    write_previous_pair "$current_api_sha" "$current_web_sha"
}

dispatch_request() {
    local request_text=$1
    local -a request=()

    if [[ $request_text == *$'\n'* || $request_text == *$'\r'* ]]; then
        refuse 'command must be a single line'
        return
    fi
    read -r -a request <<<"$request_text"
    case "${request[0]:-}" in
        status)
            ((${#request[@]} == 1)) || {
                refuse 'status takes no arguments'
                return
            }
            LOG_VERB=status
            show_status
            ;;
        deploy)
            ((${#request[@]} == 3)) || {
                refuse 'deploy requires exactly two full SHAs'
                return
            }
            valid_sha "${request[1]}" && valid_sha "${request[2]}" || {
                refuse 'deploy SHAs must be 40 lowercase hexadecimal characters'
                return
            }
            LOG_VERB=deploy
            deploy_pair "${request[1]}" "${request[2]}"
            ;;
        rollback)
            ((${#request[@]} == 1)) || {
                refuse 'rollback takes no arguments'
                return
            }
            LOG_VERB=rollback
            rollback_pair
            ;;
        *)
            refuse 'allowed commands are status, deploy <api-sha> <web-sha>, and rollback'
            ;;
    esac
}

main() {
    umask 077
    exec 9>"$LOCK_FILE"
    flock 9
    trap log_outcome EXIT
    dispatch_request "${SSH_ORIGINAL_COMMAND:-}"
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
    main "$@"
fi
