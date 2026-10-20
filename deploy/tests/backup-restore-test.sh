#!/usr/bin/env bash
# Round trip: seed a disposable stack shaped like compose.dev.yaml, back it up
# with backup-dev.sh --stop-writers, restore into a second fresh stack with
# restore-dev.sh, and compare every kind of state. Needs Docker; skips without it.
# BACKUP_TEST_POSTGRES_IMAGE / BACKUP_TEST_REDIS_IMAGE override the images.

set -euo pipefail

readonly TEST_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
readonly POSTGRES_IMAGE=${BACKUP_TEST_POSTGRES_IMAGE:-postgres:17}
readonly REDIS_IMAGE=${BACKUP_TEST_REDIS_IMAGE:-redis:7-alpine}
readonly TMP_ROOT=$(mktemp -d)
readonly SOURCE_ROOT="$TMP_ROOT/source"
readonly TARGET_ROOT="$TMP_ROOT/target"
readonly BACKUP_DIR="$TMP_ROOT/backup"
readonly ALEMBIC_REVISION=feedfacecafe

if ! docker info >/dev/null 2>&1; then
    printf 'ok - backup/restore round trip # SKIP docker unavailable\n'
    exit 0
fi

cleanup() {
    local root
    for root in "$SOURCE_ROOT" "$TARGET_ROOT"; do
        [[ -f $root/compose.yaml ]] &&
            docker compose --project-directory "$root" down -v --remove-orphans >/dev/null 2>&1
    done
    rm -rf -- "$TMP_ROOT"
}
trap cleanup EXIT

fail() {
    printf 'not ok - %s\n' "$1" >&2
    exit 1
}

write_stack() {
    local root=$1 name=$2
    mkdir -p "$root"
    cat >"$root/compose.yaml" <<EOF
name: $name
services:
  postgres:
    image: $POSTGRES_IMAGE
    environment: {POSTGRES_PASSWORD: rehearsal}
    volumes: [postgres_data:/var/lib/postgresql/data]
  redis:
    image: $REDIS_IMAGE
    volumes: [redis_data:/data]
  api:
    image: $REDIS_IMAGE
    command: ["sleep", "infinity"]
    volumes: [git_repos:/data/repos, minio_data:/data/minio, zitadel_data:/zitadel-data]
  worker:
    image: $REDIS_IMAGE
    command: ["sleep", "infinity"]
  web:
    image: $REDIS_IMAGE
    command: ["sleep", "infinity"]
volumes:
  postgres_data: {}
  redis_data: {}
  git_repos: {}
  minio_data: {}
  zitadel_data: {}
EOF
}

install_script() {
    local script=$1 root=$2
    sed -e "s|readonly DEPLOY_ROOT=/opt/ontokit|readonly DEPLOY_ROOT=$root|" \
        "$TEST_DIR/../$script" >"$TMP_ROOT/$script"
}

dc() {
    docker compose --project-directory "$1" "${@:2}"
}

psql_in() {
    dc "$1" exec -T postgres psql -U postgres -v ON_ERROR_STOP=1 -tA "${@:2}"
}

wait_ready() {
    local attempt
    for attempt in $(seq 1 60); do
        dc "$1" exec -T postgres pg_isready -U postgres -q 2>/dev/null && return 0
        sleep 1
    done
    fail "postgres in $1 never became ready"
}

write_stack "$SOURCE_ROOT" "ontokit-rehearsal-src-$$"
write_stack "$TARGET_ROOT" "ontokit-rehearsal-dst-$$"
install_script backup-dev.sh "$SOURCE_ROOT"
install_script restore-dev.sh "$TARGET_ROOT"

dc "$SOURCE_ROOT" up -d --quiet-pull >/dev/null 2>&1 || fail 'source stack did not start'
wait_ready "$SOURCE_ROOT"
psql_in "$SOURCE_ROOT" -c "CREATE ROLE ontokit LOGIN PASSWORD 'x'" \
    -c 'CREATE DATABASE ontokit OWNER ontokit' -c 'CREATE DATABASE zitadel' >/dev/null
psql_in "$SOURCE_ROOT" -d ontokit \
    -c 'CREATE TABLE alembic_version (version_num varchar(32) PRIMARY KEY)' \
    -c "INSERT INTO alembic_version VALUES ('$ALEMBIC_REVISION')" \
    -c 'CREATE TABLE projects (name text)' \
    -c "INSERT INTO projects VALUES ('FOLIO'), ('Catholic OS')" >/dev/null
psql_in "$SOURCE_ROOT" -d zitadel -c 'CREATE TABLE instances (id int)' \
    -c 'INSERT INTO instances VALUES (42)' >/dev/null
dc "$SOURCE_ROOT" exec -T api sh -c \
    'mkdir -p /data/repos/folio.git /data/minio/ontokit && echo ref > /data/repos/folio.git/HEAD &&
     echo object > /data/minio/ontokit/blob && echo pat > /zitadel-data/admin.pat'
dc "$SOURCE_ROOT" exec -T redis redis-cli SET arq:queued job-1 >/dev/null

bash "$TMP_ROOT/backup-dev.sh" "$BACKUP_DIR" --stop-writers >/dev/null ||
    fail 'backup-dev.sh failed'
[[ -n $(dc "$SOURCE_ROOT" ps -q --status running api worker web | sort -u) ]] ||
    fail 'writers were not restarted after the backup'
[[ $(dc "$SOURCE_ROOT" ps -q --status running api worker web | wc -l) == 3 ]] ||
    fail 'not every writer is running after the backup'
(cd "$BACKUP_DIR" && sha256sum --check --quiet SHA256SUMS) || fail 'SHA256SUMS does not verify'
grep -qx "alembic_revision=$ALEMBIC_REVISION" "$BACKUP_DIR/MANIFEST" ||
    fail 'MANIFEST lacks the alembic revision'
grep -qx 'mode=writers-stopped' "$BACKUP_DIR/MANIFEST" || fail 'MANIFEST lacks the backup mode'
[[ $(stat -c %a "$BACKUP_DIR") == 700 ]] || fail 'backup directory is not private'
if bash "$TMP_ROOT/backup-dev.sh" "$BACKUP_DIR" >/dev/null 2>&1; then
    fail 'backup overwrote an existing directory'
fi
printf 'ok - backup-dev.sh captures every store, restarts writers and refuses reuse\n'

bash "$TMP_ROOT/restore-dev.sh" "$BACKUP_DIR" >/dev/null || fail 'restore-dev.sh failed'
[[ -z $(dc "$TARGET_ROOT" ps -q --status running api worker web) ]] ||
    fail 'restore started a writer'
[[ $(psql_in "$TARGET_ROOT" -d ontokit \
    -c "SELECT string_agg(name, ',' ORDER BY name) FROM projects") == 'Catholic OS,FOLIO' ]] || fail 'ontokit rows did not survive the round trip'
[[ $(psql_in "$TARGET_ROOT" -d zitadel -c 'SELECT id FROM instances') == 42 ]] ||
    fail 'zitadel rows did not survive the round trip'
[[ $(psql_in "$TARGET_ROOT" -d ontokit -c 'SELECT tableowner FROM pg_tables WHERE tablename = $$projects$$') == postgres ]] ||
    fail 'table ownership changed'
dc "$TARGET_ROOT" start api >/dev/null 2>&1
[[ $(dc "$TARGET_ROOT" exec -T api sh -c \
    'cat /data/repos/folio.git/HEAD /data/minio/ontokit/blob /zitadel-data/admin.pat') == \
    $'ref\nobject\npat' ]] || fail 'volume contents did not survive the round trip'
dc "$TARGET_ROOT" start redis >/dev/null 2>&1
sleep 1
[[ $(dc "$TARGET_ROOT" exec -T redis redis-cli GET arq:queued) == job-1 ]] ||
    fail 'redis queue state did not survive the round trip'
dc "$TARGET_ROOT" stop api >/dev/null 2>&1
printf 'ok - restore-dev.sh rebuilds every store in a fresh stack without starting writers\n'

if bash "$TMP_ROOT/restore-dev.sh" "$BACKUP_DIR" >/dev/null 2>&1; then
    fail 'restore overwrote a target that already holds data'
fi
printf 'ok - restore-dev.sh refuses a target that already holds data\n'
