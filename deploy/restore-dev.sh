#!/usr/bin/env bash
# Restore a backup-dev.sh directory into a FRESH OntoKit DEV stack.
#
# Usage: restore-dev.sh <backup-dir>
#
# Run on the target host after copying compose.yaml, the overlay if any, .env and
# both checkouts into /opt/ontokit, and before anything has started there. It
# verifies SHA256SUMS, creates every container without starting the writers,
# restores PostgreSQL into a freshly initialised cluster, and unpacks each state
# volume. It refuses any target whose database or state volumes already hold
# data, so it can never overwrite a live site. api, worker and web stay stopped:
# start them only once the restore checks in deploy/MIGRATION.md pass.

set -euo pipefail

readonly DEPLOY_ROOT=/opt/ontokit
readonly VOLUMES=(git_repos minio_data zitadel_data redis_data)

refuse() {
    printf 'refused: %s\n' "$1" >&2
    exit 64
}

compose() {
    docker compose --project-directory "$DEPLOY_ROOT" "$@"
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

wait_for_postgres() {
    local attempt
    for attempt in $(seq 1 60); do
        compose exec -T postgres pg_isready -U postgres -q && return 0
        sleep 2
    done
    printf 'postgres did not become ready (%s attempts)\n' "$attempt" >&2
    return 1
}

main() {
    local backup=${1:-} postgres project helper_image volume target has_schema psql_log

    (($# == 1)) && [[ -d $backup ]] || refuse 'usage: restore-dev.sh <backup-dir>'
    backup=$(cd -- "$backup" && pwd)
    (cd -- "$backup" && sha256sum --check --quiet SHA256SUMS) ||
        refuse 'backup checksums do not match SHA256SUMS'

    if [[ -n $(compose ps -q --status running api worker web) ]]; then
        refuse 'api, worker or web is running; restore only into a stopped, fresh stack'
    fi

    # Images must already be built or pulled; never build during a restore.
    compose create --no-build
    compose start postgres
    wait_for_postgres
    has_schema=$(compose exec -T postgres psql -U postgres -tAc \
        "SELECT 1 FROM pg_database WHERE datname = 'ontokit'") || refuse 'cannot query postgres'
    if [[ $has_schema == 1 ]]; then
        has_schema=$(compose exec -T postgres psql -U postgres -d ontokit -tAc \
            "SELECT to_regclass('public.alembic_version') IS NOT NULL") ||
            refuse 'cannot inspect the ontokit database'
        [[ $has_schema != t ]] || refuse 'the target ontokit database already has a schema'
    fi

    postgres=$(compose ps -q postgres)
    project=$(docker inspect --format \
        '{{index .Config.Labels "com.docker.compose.project"}}' "$postgres")
    helper_image=$(docker inspect --format '{{.Image}}' "$postgres")
    for volume in "${VOLUMES[@]}"; do
        target=$(volume_for "$project" "$volume") || refuse "volume $volume is missing"
        docker run --rm --network none --user 0 --entrypoint sh -v "$target:/target:ro" \
            "$helper_image" -c '[ -z "$(ls -A /target)" ]' ||
            refuse "volume $volume is not empty"
    done

    psql_log=$(mktemp)
    # The dump drops and recreates every database and role it holds; errors for
    # the connected superuser and pre-existing roles are expected and harmless.
    gunzip -c "$backup/postgres.sql.gz" |
        compose exec -T postgres psql -U postgres -d postgres -q -o /dev/null 2>"$psql_log" || true
    has_schema=$(compose exec -T postgres psql -U postgres -d ontokit -tAc \
        'SELECT version_num FROM alembic_version') || refuse 'restored database has no schema'
    grep -qx "alembic_revision=$has_schema" "$backup/MANIFEST" ||
        refuse "restored alembic revision $has_schema does not match the backup"

    compose stop redis 2>/dev/null || true
    for volume in "${VOLUMES[@]}"; do
        target=$(volume_for "$project" "$volume")
        docker run --rm --network none --user 0 --entrypoint tar \
            -v "$target:/target" -v "$backup:/backup:ro" "$helper_image" \
            -xzpf "/backup/$volume.tar.gz" -C /target
    done

    printf 'restore complete: alembic_revision=%s; api, worker and web remain stopped\n' \
        "$has_schema"
    printf 'psql notices: %s\n' "$psql_log"
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
    main "$@"
fi
