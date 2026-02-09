-- Initialize Zitadel database user
CREATE USER zitadel WITH PASSWORD 'zitadel';
GRANT ALL PRIVILEGES ON DATABASE zitadel TO zitadel;
