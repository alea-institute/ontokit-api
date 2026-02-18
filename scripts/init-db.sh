#!/bin/bash
# Initialize PostgreSQL with multiple databases for OntoKit stack
# This script runs as the postgres superuser during container startup

set -e

# Create Zitadel database and user
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    -- Create Zitadel user and database
    CREATE USER zitadel WITH PASSWORD 'zitadel';
    CREATE DATABASE zitadel OWNER zitadel;
    GRANT ALL PRIVILEGES ON DATABASE zitadel TO zitadel;

    -- Create OntoKit user and database
    CREATE USER ontokit WITH PASSWORD 'ontokit';
    CREATE DATABASE ontokit OWNER ontokit;
    GRANT ALL PRIVILEGES ON DATABASE ontokit TO ontokit;
EOSQL

echo "Databases initialized: zitadel, ontokit"
