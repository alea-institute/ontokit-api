#!/bin/bash
# Deploy-owned PostgreSQL entrypoint initialization for fresh DEV volumes.
# Compose passes the application role passwords into the postgres container;
# the official entrypoint preserves that environment while running this mount.

set -e

: "${ZITADEL_DB_PASSWORD:?ZITADEL_DB_PASSWORD must be set}"
: "${ONTOKIT_DB_PASSWORD:?ONTOKIT_DB_PASSWORD must be set}"

psql -v ON_ERROR_STOP=1 \
    --username "$POSTGRES_USER" \
    --dbname "$POSTGRES_DB" \
    --set=zitadel_password="$ZITADEL_DB_PASSWORD" \
    --set=ontokit_password="$ONTOKIT_DB_PASSWORD" <<-'EOSQL'
    CREATE USER zitadel WITH PASSWORD :'zitadel_password';
    CREATE DATABASE zitadel OWNER zitadel;
    GRANT ALL PRIVILEGES ON DATABASE zitadel TO zitadel;

    CREATE USER ontokit WITH PASSWORD :'ontokit_password';
    CREATE DATABASE ontokit OWNER ontokit;
    GRANT ALL PRIVILEGES ON DATABASE ontokit TO ontokit;
EOSQL

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname ontokit <<-'EOSQL'
    CREATE EXTENSION IF NOT EXISTS vector;
EOSQL

echo "Databases initialized: zitadel, ontokit (with pgvector)"
