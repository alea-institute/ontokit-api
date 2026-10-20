#!/usr/bin/env bash

set -euo pipefail

readonly DEPLOY_ROOT=/opt/ontokit
readonly API_REPO="$DEPLOY_ROOT/ontokit-api"
readonly WEB_REPO="$DEPLOY_ROOT/ontokit-web"
readonly ENV_FILE="$DEPLOY_ROOT/.env"
readonly PREVIOUS_FILE="$DEPLOY_ROOT/.deploy-previous"
readonly CURRENT_FILE="$DEPLOY_ROOT/.deploy-current"
readonly COMPOSE_FILE="$DEPLOY_ROOT/compose.yaml"
readonly COMPOSE_IMAGES_FILE="$DEPLOY_ROOT/compose.images.yaml"
readonly API_IMAGE_REPOSITORY=ghcr.io/alea-institute/ontokit
readonly WEB_IMAGE_REPOSITORY=ghcr.io/alea-institute/ontokit-web
readonly LOCK_FILE=/var/lock/ontokit-deploy.lock
readonly LOG_FILE=/var/log/ontokit-deploy.log
readonly SHA_PATTERN='^[0-9a-f]{40}$'
readonly DIGEST_PATTERN='^sha256:[0-9a-f]{64}$'

LOG_VERB=refused
LOG_API_SHA=-
LOG_WEB_SHA=-
EFFECTIVE_AUTH_MODE=
# Source deploys use compose.yaml alone; image deploys add the image overlay.
COMPOSE_ARGS=(--project-directory "$DEPLOY_ROOT")

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

valid_digest() {
    [[ $1 =~ $DIGEST_PATTERN ]]
}

compose() {
    docker compose "${COMPOSE_ARGS[@]}" "$@"
}

# A release record is "<api-sha> <web-sha>" for a source build, or
# "<api-sha> <web-sha> <api-digest> <web-digest>" for published images.
valid_release_record() {
    local api_sha web_sha api_digest web_digest extra
    read -r api_sha web_sha api_digest web_digest extra <<<"$1"
    [[ -z ${extra:-} ]] && valid_sha "${api_sha:-}" && valid_sha "${web_sha:-}" || return 1
    [[ -z ${api_digest:-} && -z ${web_digest:-} ]] && return 0
    valid_digest "${api_digest:-}" && valid_digest "${web_digest:-}"
}

write_release_record() {
    local path=$1 record=$2
    local record_tmp="$path.tmp.$$"

    printf '%s\n' "$record" >"$record_tmp"
    chmod 600 "$record_tmp"
    mv "$record_tmp" "$path"
}

repo_sha() {
    git -C "$1" rev-parse --verify HEAD
}

container_id_for_service() {
    local service=$1
    local container_output
    local -a container_ids=()

    container_output=$(docker ps -aq \
        --filter "label=com.docker.compose.project.working_dir=$DEPLOY_ROOT" \
        --filter "label=com.docker.compose.service=$service") || return 1
    [[ -n $container_output ]] || return 1
    mapfile -t container_ids <<<"$container_output"
    ((${#container_ids[@]} == 1)) || {
        printf 'unable to identify %s container unambiguously\n' "$service" >&2
        return 1
    }
    printf '%s\n' "${container_ids[0]}"
}

running_image_revision() {
    local service=$1
    local container_id image_id revision

    if ! container_id=$(container_id_for_service "$service"); then
        printf 'unable to read %s revision: container is missing\n' "$service" >&2
        return 1
    fi
    image_id=$(docker inspect --format '{{.Image}}' "$container_id") || {
        printf 'unable to read %s revision: image lookup failed\n' "$service" >&2
        return 1
    }
    [[ -n $image_id ]] || {
        printf 'unable to read %s revision: container image is missing\n' "$service" >&2
        return 1
    }
    revision=$(docker image inspect --format \
        '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$image_id") || {
        printf 'unable to read %s revision: image inspection failed\n' "$service" >&2
        return 1
    }
    valid_sha "$revision" || {
        printf 'unable to read %s revision: image label is missing or invalid\n' "$service" >&2
        return 1
    }
    printf '%s\n' "$revision"
}

compose_status() {
    local -a services=(postgres redis minio api worker web mailpit zitadel login)
    local service container_id state health summary
    local failed=0

    for service in "${services[@]}"; do
        if ! container_id=$(container_id_for_service "$service"); then
            printf '%s=missing\n' "$service"
            failed=1
            continue
        fi

        state=$(docker inspect --format '{{.State.Status}}' "$container_id") || {
            printf '%s=inspection-failed\n' "$service"
            failed=1
            continue
        }
        health=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}' \
            "$container_id") || {
            printf '%s=inspection-failed\n' "$service"
            failed=1
            continue
        }
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
    local api_checkout web_checkout api_runtime web_runtime worker_runtime
    local revision_drift=none
    local failed=0

    if ! api_checkout=$(repo_sha "$API_REPO"); then
        api_checkout=unavailable
        failed=1
    fi
    if ! web_checkout=$(repo_sha "$WEB_REPO"); then
        web_checkout=unavailable
        failed=1
    fi
    if ! api_runtime=$(running_image_revision api); then
        api_runtime=unavailable
        failed=1
    fi
    if ! worker_runtime=$(running_image_revision worker); then
        worker_runtime=unavailable
        failed=1
    fi
    if ! web_runtime=$(running_image_revision web); then
        web_runtime=unavailable
        failed=1
    fi

    if valid_sha "$api_runtime"; then
        LOG_API_SHA=$api_runtime
    fi
    if valid_sha "$web_runtime"; then
        LOG_WEB_SHA=$web_runtime
    fi
    if [[ $api_checkout != "$api_runtime" || $api_runtime != "$worker_runtime" || \
        $web_checkout != "$web_runtime" ]]; then
        revision_drift=detected
        failed=1
    fi

    printf 'api_runtime=%s web_runtime=%s worker_runtime=%s api_checkout=%s web_checkout=%s revision_drift=%s\n' \
        "$api_runtime" "$web_runtime" "$worker_runtime" "$api_checkout" "$web_checkout" \
        "$revision_drift"
    compose_status || failed=1
    return "$failed"
}

load_and_validate_config() {
    local api_sha=$1
    local web_sha=$2
    local issuer client_id client_secret session_secret
    local zitadel_configured=false

    [[ -r $ENV_FILE ]] || {
        refuse "$ENV_FILE must exist and be readable"
        return
    }

    set -a
    # shellcheck disable=SC1090 # This root-owned, server-local file is intentionally not in Git.
    source "$ENV_FILE" || {
        set +a
        refuse "$ENV_FILE could not be loaded"
        return
    }
    set +a

    EFFECTIVE_AUTH_MODE=${AUTH_MODE:-}
    case "$EFFECTIVE_AUTH_MODE" in
        required | optional | disabled) ;;
        *)
            refuse 'AUTH_MODE must be required, optional, or disabled'
            return
            ;;
    esac

    issuer=${ZITADEL_ISSUER:-}
    client_id=${ZITADEL_CLIENT_ID:-}
    client_secret=${ZITADEL_CLIENT_SECRET:-}
    session_secret=${NEXTAUTH_SECRET:-${AUTH_SECRET:-}}
    if [[ -n $issuer || -n $client_id || -n $client_secret ]]; then
        zitadel_configured=true
    fi
    if [[ $EFFECTIVE_AUTH_MODE == required ]]; then
        zitadel_configured=true
    fi
    if [[ $zitadel_configured == true && \
        (-z $issuer || -z $client_id || -z $client_secret || -z $session_secret) ]]; then
        refuse 'configured or required auth needs ZITADEL_ISSUER, ZITADEL_CLIENT_ID, ZITADEL_CLIENT_SECRET, and AUTH_SECRET (or NEXTAUTH_SECRET)'
        return
    fi

    AUTH_MODE=$EFFECTIVE_AUTH_MODE
    NEXTAUTH_SECRET=$session_secret
    ONTOKIT_API_REVISION=$api_sha
    ONTOKIT_WEB_REVISION=$web_sha
    export AUTH_MODE NEXTAUTH_SECRET ONTOKIT_API_REVISION ONTOKIT_WEB_REVISION

    if ! compose config --quiet; then
        refuse 'compose configuration is invalid'
        return
    fi
}

fetch_and_verify() {
    local repo=$1
    local sha=$2
    local remote_refs

    git -C "$repo" fetch --prune origin || {
        printf 'refused: cannot fetch origin in %s\n' "$repo" >&2
        return 65
    }
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

# The last release this script verified, falling back to the checkouts for hosts
# that predate .deploy-current (always a source build there).
current_release_record() {
    local record api_sha web_sha

    if [[ -r $CURRENT_FILE ]]; then
        record=$(<"$CURRENT_FILE")
        valid_release_record "$record" || {
            refuse 'recorded current release is invalid'
            return
        }
        printf '%s\n' "$record"
        return 0
    fi
    api_sha=$(repo_sha "$API_REPO")
    web_sha=$(repo_sha "$WEB_REPO")
    valid_sha "$api_sha" || {
        refuse 'current API checkout is not a full SHA'
        return
    }
    valid_sha "$web_sha" || {
        refuse 'current web checkout is not a full SHA'
        return
    }
    printf '%s %s\n' "$api_sha" "$web_sha"
}

record_previous_pair() {
    local target_record=$*
    local current_record

    current_record=$(current_release_record) || return
    # A rebuild/retry of the current release must retain the older rollback target.
    # This also preserves that target after a failed attempt moved both checkouts.
    if [[ $current_record == "$target_record" ]]; then
        return 0
    fi
    write_release_record "$PREVIOUS_FILE" "$current_record"
}

deploy_pair() {
    local api_sha=$1
    local web_sha=$2
    local record_previous=${3:-true}

    LOG_API_SHA=$api_sha
    LOG_WEB_SHA=$web_sha

    # Validate local configuration before fetch, checkout, rollback-record, or build mutations.
    load_and_validate_config "$api_sha" "$web_sha" || return
    fetch_and_verify "$API_REPO" "$api_sha" || return
    fetch_and_verify "$WEB_REPO" "$web_sha" || return
    if [[ $record_previous == true ]]; then
        record_previous_pair "$api_sha" "$web_sha" || return
    fi
    ( umask 022; git -C "$API_REPO" checkout --detach "$api_sha" ) || return
    ( umask 022; git -C "$WEB_REPO" checkout --detach "$web_sha" ) || return

    compose build api || return
    compose build --no-cache web || return

    start_and_verify "$api_sha" "$web_sha" || return
    write_release_record "$CURRENT_FILE" "$api_sha $web_sha"
    printf 'deployed api=%s web=%s\n' "$api_sha" "$web_sha"
}

start_and_verify() {
    local api_sha=$1
    local web_sha=$2

    # Do not auto-rollback here: a transient healthcheck failure is safer held for inspection.
    if ! timeout 300 docker compose "${COMPOSE_ARGS[@]}" \
        up -d --no-build --wait --wait-timeout 240; then
        printf 'deploy failed: compose did not become ready; rollback was not automatic\n' >&2
        return 1
    fi

    if ! compose_status; then
        printf 'deploy failed: one or more containers are not ready\n' >&2
        return 1
    fi
    if ! verify_runtime_pair "$api_sha" "$web_sha"; then
        printf 'deploy failed: running images do not match the approved pair\n' >&2
        return 1
    fi
}

image_revision() {
    docker image inspect --format \
        '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$1"
}

verify_image_revision() {
    local image=$1
    local expected_sha=$2
    local revision

    revision=$(image_revision "$image") || {
        printf 'refused: cannot inspect pulled image %s\n' "$image" >&2
        return 65
    }
    if [[ $revision != "$expected_sha" ]]; then
        printf 'refused: image %s is labelled revision %s, expected %s\n' \
            "$image" "${revision:-none}" "$expected_sha" >&2
        return 65
    fi
}

deploy_images() {
    local api_sha=$1
    local web_sha=$2
    local api_digest=$3
    local web_digest=$4
    local record_previous=${5:-true}

    LOG_API_SHA=$api_sha
    LOG_WEB_SHA=$web_sha

    [[ -r $COMPOSE_IMAGES_FILE ]] || {
        refuse "$COMPOSE_IMAGES_FILE must exist and be readable"
        return
    }
    COMPOSE_ARGS=(--project-directory "$DEPLOY_ROOT" -f "$COMPOSE_FILE" -f "$COMPOSE_IMAGES_FILE")
    local api_image="$API_IMAGE_REPOSITORY@$api_digest"
    local web_image="$WEB_IMAGE_REPOSITORY@$web_digest"
    ONTOKIT_API_IMAGE=$api_image
    ONTOKIT_WEB_IMAGE=$web_image
    export ONTOKIT_API_IMAGE ONTOKIT_WEB_IMAGE

    # Validate local configuration before fetch, checkout, rollback-record, or pull mutations.
    load_and_validate_config "$api_sha" "$web_sha" || return
    # .env is sourced with allexport; it must not swap the validated images.
    [[ $ONTOKIT_API_IMAGE == "$api_image" && $ONTOKIT_WEB_IMAGE == "$web_image" ]] || {
        refuse "$ENV_FILE must not set ONTOKIT_API_IMAGE or ONTOKIT_WEB_IMAGE"
        return
    }
    # The checkouts still supply deploy-owned bootstrap files and the status baseline.
    fetch_and_verify "$API_REPO" "$api_sha" || return
    fetch_and_verify "$WEB_REPO" "$web_sha" || return
    compose pull api worker web || return
    verify_image_revision "$ONTOKIT_API_IMAGE" "$api_sha" || return
    verify_image_revision "$ONTOKIT_WEB_IMAGE" "$web_sha" || return
    if [[ $record_previous == true ]]; then
        record_previous_pair "$api_sha" "$web_sha" "$api_digest" "$web_digest" || return
    fi
    ( umask 022; git -C "$API_REPO" checkout --detach "$api_sha" ) || return
    ( umask 022; git -C "$WEB_REPO" checkout --detach "$web_sha" ) || return

    start_and_verify "$api_sha" "$web_sha" || return
    write_release_record "$CURRENT_FILE" "$api_sha $web_sha $api_digest $web_digest"
    printf 'deployed api=%s web=%s api_image=%s web_image=%s\n' \
        "$api_sha" "$web_sha" "$ONTOKIT_API_IMAGE" "$ONTOKIT_WEB_IMAGE"
}

verify_runtime_pair() {
    local expected_api_sha=$1
    local expected_web_sha=$2
    local api_runtime worker_runtime web_runtime

    api_runtime=$(running_image_revision api) || return 1
    worker_runtime=$(running_image_revision worker) || return 1
    web_runtime=$(running_image_revision web) || return 1
    if [[ $api_runtime != "$expected_api_sha" || $worker_runtime != "$expected_api_sha" || \
        $web_runtime != "$expected_web_sha" ]]; then
        printf 'revision mismatch: api=%s worker=%s web=%s expected_api=%s expected_web=%s\n' \
            "$api_runtime" "$worker_runtime" "$web_runtime" \
            "$expected_api_sha" "$expected_web_sha" >&2
        return 1
    fi
}

rollback_pair() {
    local previous_record current_record api_sha web_sha api_digest web_digest

    if [[ ! -r $PREVIOUS_FILE ]]; then
        refuse 'no previous deployment pair is recorded'
        return
    fi
    previous_record=$(<"$PREVIOUS_FILE")
    valid_release_record "$previous_record" || {
        refuse 'recorded previous deployment pair is invalid'
        return
    }
    read -r api_sha web_sha api_digest web_digest <<<"$previous_record"
    current_record=$(current_release_record) || return

    # Preserve the known-good target if this rollback attempt fails so it remains retryable.
    if [[ -n ${api_digest:-} ]]; then
        deploy_images "$api_sha" "$web_sha" "$api_digest" "$web_digest" false || return
    else
        deploy_pair "$api_sha" "$web_sha" false || return
    fi
    write_release_record "$PREVIOUS_FILE" "$current_record"
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
        deploy-images)
            ((${#request[@]} == 5)) || {
                refuse 'deploy-images requires two full SHAs and two sha256 digests'
                return
            }
            valid_sha "${request[1]}" && valid_sha "${request[2]}" || {
                refuse 'deploy SHAs must be 40 lowercase hexadecimal characters'
                return
            }
            valid_digest "${request[3]}" && valid_digest "${request[4]}" || {
                refuse 'image digests must be sha256: followed by 64 lowercase hexadecimal characters'
                return
            }
            LOG_VERB=deploy-images
            deploy_images "${request[1]}" "${request[2]}" "${request[3]}" "${request[4]}"
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
            refuse 'allowed commands are status, deploy <api-sha> <web-sha>, deploy-images <api-sha> <web-sha> <api-digest> <web-digest>, and rollback'
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
