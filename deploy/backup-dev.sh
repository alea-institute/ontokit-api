#!/usr/bin/env bash
# Back up the OntoKit DEV stack's state to a new local directory.
#
# Usage: backup-dev.sh <new-target-dir> [--stop-writers]
#
# Writes postgres.sql.gz (pg_dumpall of every database and role), one tarball per
# stateful named volume, MANIFEST (non-secret provenance) and SHA256SUMS. Without
# --stop-writers the stack keeps serving: each database dump is consistent, but a
# write between steps can land in one artifact and not another. Use
# --stop-writers for a cutover snapshot; it stops web, worker and api (never the
# data services) and starts them again however the backup ends.
#
# The output holds credentials (database contents, Zitadel PATs): it is created
# 0700 and must travel only through the operator's secret transfer mechanism.
# /opt/ontokit/.env is deliberately not copied.

set -euo pipefail

readonly DEPLOY_ROOT=/opt/ontokit
# Named volumes whose contents are state; postgres_data is captured by pg_dumpall.
readonly VOLUMES=(git_repos minio_data zitadel_data redis_data)
readonly WRITERS=(web worker api)

refuse() {
    printf 'refused: %s\n' "$1" >&2
    exit 64
}

compose() {
    docker compose --project-directory "$DEPLOY_ROOT" "$@"
}

container_for() {
    local ids
    ids=$(compose ps -q "$1") || return 1
    [[ -n $ids && $ids != *$'\n'* ]] || {
        printf 'service %s must have exactly one container\n' "$1" >&2
        return 1
    }
    printf '%s\n' "$ids"
}

label_of() {
    docker inspect --format "{{index .Config.Labels \"$2\"}}" "$1"
}

volume_for() {
    local project=$1 volume=$2 names
    names=$(docker volume ls -q \
        --filter "label=com.docker.compose.project=$project" \
        --filter "label=com.docker.compose.volume=$volume") || return 1
    [[ -n $names && $names != *$'\n'* ]] || {
        printf 'volume %s must exist exactly once in project %s\n' "$volume" "$project" >&2
        return 1
    }
    printf '%s\n' "$names"
}

image_revision() {
    local container
    container=$(container_for "$1") || {
        printf 'unavailable\n'
        return 0
    }
    docker image inspect --format \
        '{{index .Config.Labels "org.opencontainers.image.revision"}}' \
        "$(docker inspect --format '{{.Image}}' "$container")" || printf 'unavailable\n'
}

restart_writers() {
    # Reverse order: api first, so the worker and web find it healthy.
    compose start api worker web || printf 'WARNING: restart writers manually\n' >&2
}

main() {
    local target=${1:-} stop_writers=false
    local postgres project helper_image volume source alembic_revision mode=live

    case "${2:-}" in
        "") ;;
        --stop-writers) stop_writers=true mode=writers-stopped ;;
        *) refuse 'usage: backup-dev.sh <new-target-dir> [--stop-writers]' ;;
    esac
    (($# <= 2)) || refuse 'usage: backup-dev.sh <new-target-dir> [--stop-writers]'
    [[ -n $target ]] || refuse 'usage: backup-dev.sh <new-target-dir> [--stop-writers]'
    [[ ! -e $target ]] || refuse "$target already exists; choose a new directory"

    umask 077
    postgres=$(container_for postgres) || refuse 'postgres is not running'
    project=$(label_of "$postgres" com.docker.compose.project)
    [[ -n $project ]] || refuse 'cannot read the compose project name'
    # The pinned PostgreSQL image is already local and has tar; nothing is pulled.
    helper_image=$(docker inspect --format '{{.Image}}' "$postgres")
    for volume in "${VOLUMES[@]}"; do
        volume_for "$project" "$volume" >/dev/null || refuse "volume $volume is missing"
    done

    mkdir -m 0700 -- "$target"
    target=$(cd -- "$target" && pwd)

    {
        printf 'created_at=%s\n' "$(date --utc '+%Y-%m-%dT%H:%M:%SZ')"
        printf 'mode=%s\n' "$mode"
        printf 'compose_project=%s\n' "$project"
        printf 'api_revision=%s\n' "$(image_revision api)"
        printf 'worker_revision=%s\n' "$(image_revision worker)"
        printf 'web_revision=%s\n' "$(image_revision web)"
        printf 'postgres_image=%s\n' "$(docker inspect --format '{{.Config.Image}}' "$postgres")"
    } >"$target/MANIFEST"

    if [[ $stop_writers == true ]]; then
        trap restart_writers EXIT
        compose stop "${WRITERS[@]}"
    fi

    alembic_revision=$(compose exec -T postgres \
        psql -U postgres -d ontokit -tAc 'SELECT version_num FROM alembic_version')
    printf 'alembic_revision=%s\n' "$alembic_revision" >>"$target/MANIFEST"
    compose exec -T postgres pg_dumpall -U postgres --clean --if-exists \
        | gzip >"$target/postgres.sql.gz"

    # Persist Redis (ARQ job queue) before copying its volume.
    compose exec -T redis redis-cli SAVE >/dev/null

    for volume in "${VOLUMES[@]}"; do
        source=$(volume_for "$project" "$volume")
        # The helper reads volumes as root; hand each archive to the caller.
        docker run --rm --network none --user 0 --entrypoint sh \
            -v "$source:/source:ro" -v "$target:/backup" "$helper_image" -c \
            'tar -C /source -czf "/backup/$1.tar.gz" . && chown "$2" "/backup/$1.tar.gz" &&
             chmod 0600 "/backup/$1.tar.gz"' \
            sh "$volume" "$(id -u):$(id -g)"
    done

    (cd -- "$target" && sha256sum -- MANIFEST postgres.sql.gz ./*.tar.gz >SHA256SUMS)
    chmod 0600 -- "$target"/*
    printf 'backup complete: %s (%s)\n' "$target" "$mode"
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
    main "$@"
fi
