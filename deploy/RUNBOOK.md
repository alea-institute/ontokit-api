# OntoKit DEV deployment runbook

This directory captures the post-auth-flip DEV environment on CPX41. The
UAT-history companion is `ontokit-web/docs/roundup-2026-08/DEV-RUNBOOK.md`.
The CI auto-deploy workflow and its host-side forced command are maintained in
this repository; deployment credentials remain outside Git.

## Bootstrap from scratch

Run these steps in order:

1. Place the API and web repositories at `/opt/ontokit/ontokit-api` and
   `/opt/ontokit/ontokit-web`. Copy `deploy/compose.dev.yaml` to
   `/opt/ontokit/compose.yaml`.
2. Create `/opt/ontokit/.env` from `deploy/.env.example`. Generate a distinct,
   high-entropy value for every secret and replace every placeholder;
   `ZITADEL_MASTERKEY` must be exactly 32 characters. Keep this server-side file
   out of Git.
3. Install `deploy/firewall/ontokit-firewall.sh` at
   `/usr/local/sbin/ontokit-firewall.sh` and the unit at
   `/etc/systemd/system/ontokit-firewall.service`. Make the script executable,
   run `systemctl daemon-reload`, then `systemctl enable --now ontokit-firewall`.
4. From `/opt/ontokit`, start the backing services, Zitadel, API, and worker,
   but do not build or start web yet. The API is expected to run with
   `AUTH_MODE=optional` before the OIDC client exists:

   ```sh
   docker compose up -d postgres redis minio mailpit zitadel login api worker
   ```
5. If the PostgreSQL volume already existed before `ZITADEL_DB_PASSWORD` or
   `ONTOKIT_DB_PASSWORD` was selected, update both existing roles explicitly:
   connect as the PostgreSQL administrator and run `ALTER ROLE` for `zitadel`
   and `ontokit` with their corresponding values from `.env`. Container
   environment changes do not alter roles already stored in the volume. Do not
   put either password in shell history or Git.
6. From `/opt/ontokit`, run the setup script with the deployed URL, Compose
   volume, and shared deployment env file explicitly selected. `UPDATE_ENV=true`
   writes the generated OIDC values to `/opt/ontokit/.env` for both consumers:

   ```sh
   set -a
   . /opt/ontokit/.env
   set +a
   ZITADEL_URL=https://ontokit-auth.dev.openlegalstandard.org \
   ZITADEL_DATA_VOLUME=ontokit_zitadel_data \
   API_ENV_FILE=/opt/ontokit/.env \
   WEB_ENV_FILE=/opt/ontokit/.env \
   UPDATE_ENV=true \
   ./ontokit-api/scripts/setup-zitadel.sh
   ```

   Generated values belong only in the server-side `.env`.
7. Load the updated environment, build web with every auth-sensitive build
   argument stated explicitly, then recreate all credential consumers:

   ```sh
   set -a
   . /opt/ontokit/.env
   set +a
   docker compose build \
     --build-arg AUTH_MODE=optional \
     --build-arg ZITADEL_ISSUER="$ZITADEL_ISSUER" \
     --build-arg ZITADEL_CLIENT_ID="$ZITADEL_CLIENT_ID" \
     web
   docker compose up -d --force-recreate api worker web
   ```
8. On the hetzner-dev proxy box, install the validated contents of
   `deploy/traefik/ontokit-dev.yaml` at
   `/data/coolify/proxy/dynamic/ontokit-dev.yaml`. Rewrite and validate the
   complete file; never patch the live YAML by hand.

## Deploy

1. Push the intended API and web commits to their downstream forks.
2. On CPX41, in each repository, run `git fetch`, verify the intended commit,
   and check out its exact SHA.
3. From `/opt/ontokit`, rebuild and restart:

   ```sh
   docker compose build
   docker compose up -d
   ```

Use `docker compose build --no-cache web` when a clean web rebuild is needed.
Every web build must receive `AUTH_MODE`, `ZITADEL_ISSUER`, and
`ZITADEL_CLIENT_ID` through the compose build arguments. Next.js bakes these
values into the image; omitting them can produce a UI that behaves as auth-off
even when the runtime environment is correct.

## Rollback

Check out the previously known-good API and web SHA pair on CPX41, then run
`docker compose build` and `docker compose up -d`. Rebuilding both images keeps
the deployed pair internally consistent. Record both SHAs together before each
deployment so the rollback target is unambiguous.

## Automated deploy

`.github/workflows/deploy-dev.yml` deploys after a push to `feat/pr-party` or a
manual workflow dispatch. Every run targets the `dev-deploy` GitHub Environment,
so GitHub pauses the deploy job until Damien approves it from the run's
**Review deployments** prompt. A manual run may supply either exact SHA; an
omitted API SHA uses the workflow commit, while an omitted web SHA resolves the
current `alea-institute/ontokit-web` `feat/pr-party` head and fails closed if
that head cannot be resolved.

The CI key is restricted to the forced command installed from
`deploy/ontokit-deploy.sh` at `/usr/local/sbin/ontokit-deploy` (root-owned,
mode `0755`). As a one-time provisioning step, the orchestrator installs that
script and adds the CI public key to root's `authorized_keys` with a forced
`command="/usr/local/sbin/ontokit-deploy"` plus SSH forwarding/PTY restrictions.
The private key and pinned host keys are stored only in GitHub secrets named
`DEV_DEPLOY_SSH_KEY` and `DEV_DEPLOY_KNOWN_HOSTS`.

To restore the pair recorded immediately before the last deploy, invoke the
same restricted key with the `rollback` verb. Each `status`, `deploy`, and
`rollback` invocation appends its verb, SHA pair, and outcome to
`/var/log/ontokit-deploy.log`; it never logs the server-side `.env` values.

KTD10's matched-pair release manifest remains known residual work. Today's
automatic pairing uses the two integration-branch heads (or explicitly
dispatched SHAs), not a manifest-defined release pair.

## Logs and health

Use `docker logs <container-name>` (optionally with `--tail` or `--follow`) for
service diagnostics. The login healthcheck must send the real
`Host: ontokit-auth.dev.openlegalstandard.org` header. Undici drops attempted
Host overrides, so the captured check deliberately uses Node's `http` module.

## Authentication mode

The API accepts only the literal enum values `required`, `optional`, or
`disabled` for `AUTH_MODE`. DEV's terminal state is `optional`: anonymous reads
remain available while protected operations use Zitadel. The former Traefik
basic-auth gate is intentionally absent; Zitadel is the sole authentication
system after the 2026-08-10 flip, superseding KTD2.

## Known gotchas

- The previously deployed `zitadel-login:latest` image (now pinned by digest)
  dropped effective
  `ZITADEL_SERVICE_USER_TOKEN_FILE` support. The compose entrypoint wrapper reads
  the PAT file and exports its value as `ZITADEL_SERVICE_USER_TOKEN` before
  starting Login V2.
- A pre-existing PostgreSQL volume preserves old `zitadel` and `ontokit` role
  passwords; use the `ALTER ROLE` bootstrap step when changing either one.
- The login healthcheck requires the production Host header and cannot rely on
  undici to preserve a Host override.
- Both first-instance Zitadel PATs expire at `2026-09-15T00:00:00Z`. Rotate them
  before that date and update the affected server-side credentials.
