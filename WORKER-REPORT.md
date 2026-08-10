# Worker Report — u7-zitadel-script

## Files changed

- `compose.yaml`
  - Parameterized the Zitadel master key, external domain/port/secure mode, Login V2 base, admin password, PAT expiry, and log level with local-development-preserving defaults.
- `scripts/setup-zitadel.sh`
  - Added `WEB_BASE_URL`, `API_ENV_FILE`, `WEB_ENV_FILE`, `ADMIN_USERNAME`, `ZITADEL_ADMIN_PASSWORD`, and `MAILPIT_URL` handling.
  - Added `--show-secrets`; client secrets and PATs are masked to four characters plus `…` by default, while `--update-env` continues writing full values.
  - Escaped the parameterized admin username using the existing `jq` dependency.
- `WORKER-REPORT.md`
  - Records validation and deviations for the orchestrator.

## Rendered Compose validation

### No overrides

`docker compose config` rendered output was compared with the pre-change rendering. The diff is empty (0 lines): every rendered default and output path is identical.

### Full overrides

The override rendering changed the intended keys as follows:

- command master key: `MasterkeyNeedsToHave32Characters` → `xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`
- `ZITADEL_EXTERNALDOMAIN`: `localhost` → `auth.example.org`
- `ZITADEL_EXTERNALPORT`: `8080` → `443`
- `ZITADEL_EXTERNALSECURE`: `false` → `true`
- `ZITADEL_DEFAULTINSTANCE_FEATURES_LOGINV2_BASEURI`: `http://localhost:8081/ui/v2/login` → `https://auth.example.org/ui/v2/login`
- `ZITADEL_OIDC_DEFAULTLOGINURLV2`: `http://localhost:8081/ui/v2/login/login?authRequest=` → `https://auth.example.org/ui/v2/login/login?authRequest=`
- `ZITADEL_OIDC_DEFAULTLOGOUTURLV2`: `http://localhost:8081/ui/v2/login/logout?post_logout_redirect=` → `https://auth.example.org/ui/v2/login/logout?post_logout_redirect=`
- `ZITADEL_SAML_DEFAULTLOGINURLV2`: `http://localhost:8081/ui/v2/login/login?samlRequest=` → `https://auth.example.org/ui/v2/login/login?samlRequest=`
- `ZITADEL_FIRSTINSTANCE_ORG_HUMAN_PASSWORD`: `Admin123!` → `Str0ng!Pass`
- both PAT expiration keys: `2030-01-01T00:00:00Z` → `2026-09-15T00:00:00Z`
- `ZITADEL_LOG_LEVEL`: `debug` → `info`

The Docker Compose command returned success for both renders. This host emitted three non-fatal `Failed to create stream fd: Operation not permitted` diagnostics per invocation; the rendered YAML was still complete and parseable.

## Script and lint validation

- `bash -n scripts/setup-zitadel.sh`: passed.
- Focused shell checks: default `abcdefgh` display rendered as `abcd…`; `--show-secrets` behavior rendered it as `abcdefgh`; unset and empty admin-password overrides retained `Admin123!` and displayed the known-default mode; non-empty override selected the external-password message mode.
- `git diff --check`: passed.
- `shellcheck scripts/setup-zitadel.sh`: not run because `shellcheck` is not installed. No shellcheck findings are therefore available to classify as new or pre-existing.

## Review and deviations

- Review found and fixed an empty-password edge case: Compose's `${ZITADEL_ADMIN_PASSWORD:-Admin123!}` treats an empty value as the default, so the script now does the same when deciding whether to reveal the known default. This is the only place where the initial wording “set externally” needed interpretation to stay consistent with actual Compose behavior.
- No automated tests were added because the task forbids changes outside the script, Compose file, and this report. Secret-display branches and the Compose override matrix were instead verified directly with focused shell checks and rendered-config comparisons.
- Base URL overrides are concatenated with fixed suffixes as requested. Operators should provide `WEB_BASE_URL` and `ZITADEL_LOGIN_EXTERNAL_BASE` without a trailing slash to avoid doubled slashes.
- No network access, push, or `docker compose up` was used.
