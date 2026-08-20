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
   out of Git. Keep `AUTH_MODE=optional` when anonymous reads should remain
   available. Optional mode also works before Zitadel exists: leave
   `ZITADEL_ISSUER`, `ZITADEL_CLIENT_ID`, and `ZITADEL_CLIENT_SECRET` empty
   together. When any Zitadel value is present, all three plus the session
   signing secret (`AUTH_SECRET`, mapped to the web's `NEXTAUTH_SECRET`) must be
   present.
3. In the deployment shell, record the exact source revisions that any manual
   Compose build will stamp into its resulting images:

   ```sh
   export ONTOKIT_API_REVISION=$(git -C /opt/ontokit/ontokit-api rev-parse --verify HEAD)
   export ONTOKIT_WEB_REVISION=$(git -C /opt/ontokit/ontokit-web rev-parse --verify HEAD)
   ```

   Both values must be full 40-character commit SHAs. The automated deploy
   command supplies the approved pair itself; these exports are for manual
   bootstrap and recovery commands only.
4. Install `deploy/firewall/ontokit-firewall.sh` at
   `/usr/local/sbin/ontokit-firewall.sh` and the unit at
   `/etc/systemd/system/ontokit-firewall.service`. Make the script executable,
   run `systemctl daemon-reload`, then `systemctl enable --now ontokit-firewall`.
5. From `/opt/ontokit`, start the backing services, Zitadel, API, and worker,
   but do not build or start web yet. The API is expected to run with
   `AUTH_MODE=optional` before the OIDC client exists:

   ```sh
   docker compose up -d postgres redis minio mailpit zitadel login api worker
   ```
6. If the PostgreSQL volume already existed before `ZITADEL_DB_PASSWORD` or
   `ONTOKIT_DB_PASSWORD` was selected, update both existing roles explicitly:
   connect as the PostgreSQL administrator and run `ALTER ROLE` for `zitadel`
   and `ontokit` with their corresponding values from `.env`. Container
   environment changes do not alter roles already stored in the volume. Do not
   put either password in shell history or Git.
7. From `/opt/ontokit`, run the setup script with the deployed URL, Compose
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
8. Load the updated environment, retain the single effective `AUTH_MODE` from
   that file, build web with the same value, then recreate all credential
   consumers:

   ```sh
   set -a
   . /opt/ontokit/.env
   set +a
   docker compose build web
   docker compose up -d --force-recreate api worker web
   ```
9. On the hetzner-dev proxy box, install the validated contents of
   `deploy/traefik/ontokit-dev.yaml` at
   `/data/coolify/proxy/dynamic/ontokit-dev.yaml`. Rewrite and validate the
   complete file; never patch the live YAML by hand.

## Deploy

1. Push the intended API and web commits to their downstream forks.
2. On CPX41, in each repository, run `git fetch`, verify the intended commit,
   and check out its exact SHA.
3. From `/opt/ontokit`, load the deployment environment and export the exact
   checked-out revisions before rebuilding and restarting:

   ```sh
   set -a
   . /opt/ontokit/.env
   set +a
   export ONTOKIT_API_REVISION=$(git -C /opt/ontokit/ontokit-api rev-parse --verify HEAD)
   export ONTOKIT_WEB_REVISION=$(git -C /opt/ontokit/ontokit-web rev-parse --verify HEAD)
   docker compose build
   docker compose up -d --wait --wait-timeout 240
   ```

Use `docker compose build --no-cache web` when a clean web rebuild is needed.
Every web build receives the one effective `AUTH_MODE` plus any configured
`ZITADEL_ISSUER` and `ZITADEL_CLIENT_ID` through the Compose build arguments.
Next.js bakes these values into the image; bypassing Compose can produce a UI
whose auth behavior disagrees with the API. Compose also stamps each built API
and web image with its corresponding `org.opencontainers.image.revision`
label. A manual build therefore needs both `ONTOKIT_*_REVISION` exports above.

## Rollback

Prefer the forced command's `rollback` verb, which preserves the known-good
target if a rollback attempt fails and swaps the retry target only after a
successful deployment. For manual recovery, check out the previously known-good
API and web SHA pair, export those two revisions as described above, then run
`docker compose build` and `docker compose up -d --wait --wait-timeout 240`.
Rebuilding both images keeps the deployed pair internally consistent.

## Automated deploy

`.github/workflows/deploy-dev.yml` deploys after a push to `feat/pr-party` or a
manual workflow dispatch. Every run targets the `dev-deploy` GitHub Environment,
so GitHub pauses the deploy job until Damien approves it from the run's
**Review deployments** prompt. Both trigger forms read
`deploy/release-manifest.json`; there are no per-run SHA inputs and no branch-head
fallback. The validator accepts only the declared ALEA repositories and full
lowercase 40-character commit SHAs, and the workflow proves both commits are
fetchable before the forced command sees them. Updating DEV therefore means
reviewing and committing one matched API/web pair in the manifest.

### One-time: mint the deploy key (Damien only)

This is the only step an agent may not perform (credential material);
everything else is already installed and verified.

```bash
set -e; D=~/.config/ontokit-dev; mkdir -p $D; chmod 700 $D; if [ -f $D/deploy-key ]; then echo "REFUSED: $D/deploy-key already exists — rotate deliberately"; exit 1; fi; ssh-keygen -t ed25519 -N '' -C github-actions-deploy -f $D/deploy-key >/dev/null; chmod 600 $D/deploy-key; ssh-keyscan -t ed25519 178.156.208.239 2>/dev/null > $D/known_hosts; [ -s $D/known_hosts ] || { echo 'REFUSED: could not read box host key'; exit 1; }; ssh -i ~/.ssh/hetzner_dev root@178.156.208.239 "grep -q github-actions-deploy /root/.ssh/authorized_keys || echo 'restrict,command=\"/usr/local/sbin/ontokit-deploy\" $(cat $D/deploy-key.pub)' >> /root/.ssh/authorized_keys"; for r in ontokit-api ontokit-web; do gh secret set DEV_DEPLOY_SSH_KEY --repo alea-institute/$r < $D/deploy-key; gh secret set DEV_DEPLOY_KNOWN_HOSTS --repo alea-institute/$r < $D/known_hosts; done; ssh -i $D/deploy-key -o IdentitiesOnly=yes -o UserKnownHostsFile=$D/known_hosts -o StrictHostKeyChecking=yes root@178.156.208.239 status && echo 'SELF-CHECK OK — key works and is locked to the deploy command' || echo 'SELF-CHECK FAILED — key installed but deploy command did not answer; tell the session'
```

Setting `IdentitiesOnly` to `yes` is load-bearing here: `-i` only adds an identity, so without it an agent key can satisfy authentication and silently bypass the forced command, making the self-check prove nothing. This was found live on 2026-08-10.

It:

- generates an ed25519 key at `~/.config/ontokit-dev/deploy-key` (mode 600,
  outside every git tree)
- installs the public half into root's `authorized_keys` wrapped in `restrict`
  + `command="/usr/local/sbin/ontokit-deploy"`
- publishes `DEV_DEPLOY_SSH_KEY` and `DEV_DEPLOY_KNOWN_HOSTS` to both
  alea-institute forks
- ends with a self-check that prints OK without revealing key material
- refuses rather than overwrites an existing key

Until this runs, the Deploy DEV workflow's `preflight` job skips every push
cleanly, so no approval tap is consumed and nothing fails noisily.

The settled posture is a root key locked to one forced command. This was chosen
over a deploy-user account because the `deploy` account is in the **docker
group, which is root-equivalent** — so a shell-capable deploy account would grant
strictly MORE power than a key that can only invoke one validated script.
Decision: ask `ontokit-web-2026-08-10-u12-deploy-prereqs`, 2026-08-10.

The `dev-deploy` GitHub Environment with `damienriehl` as required reviewer
already exists on both forks.

`/usr/local/sbin/ontokit-deploy` was installed and verified on 2026-08-10: all
nine containers were healthy, and hostile inputs (`deploy; rm -rf /`, unknown
verb, malformed SHAs) were refused with rc=64. The original `status` output
called checkout heads "live" SHAs; that was not runtime proof. Install the
current script before relying on revision reporting: it reads immutable labels
from the images used by the running API, worker, and web containers, prints the
checkout revisions separately, and returns nonzero when either pair drifts. The
status path queries Docker's Compose labels directly, so missing or invalid
deployment auth configuration cannot hide the running revision truth.

To restore the pair recorded immediately before the last deploy, invoke the
same restricted key with the `rollback` verb. Each `status`, `deploy`, and
`rollback` invocation appends its verb, SHA pair, and outcome to
`/var/log/ontokit-deploy.log`; it never logs the server-side `.env` values.

## Dormant PROD promotion (Stage A)

`.github/workflows/promote-prod.yml` follows a successful **Deploy DEV** run. It
has no manual-dispatch path. It checks out the immutable workflow-run commit,
validates the same committed release manifest, and uses the GitHub Actions API
to require that the source run's `deploy` job actually succeeded rather than
being skipped. It then runs the DEV smoke and reports whether the PROD gate is
dormant. If `PROD_ENABLED` is not the exact string `true`, the final `promote`
job is skipped. If it is true, the job enters the `production` GitHub
Environment, refuses missing host/key prerequisites, and sends only
`deploy <api-sha> <web-sha>` through the restricted SSH key. There is no
separate approve button or manually supplied release pair; when activated,
green DEV plus the protected authority chain promotes automatically.

The smoke covers `/health`, the projects API, and the web `/projects` route. Its
write proof imports a new private `ci-smoke-*` project, creates a suggestion
session, adds a newly minted class, saves, submits, rejects HTTP 422 explicitly,
and deletes that project on success or failure. It never selects or changes the
seeded FOLIO project. Authenticated writes intentionally fail closed until the
credential design is installed:

- `DEV_SMOKE_BEARER_TOKEN` — a short-lived bearer credential for a dedicated
  smoke principal that may create/delete its own project and exercise its own
  suggestion path; store only in a branch-restricted `dev-smoke` GitHub
  Environment, never as a repository-wide secret or in the manifest or logs.
- Optional repository variables `DEV_API_URL` and `DEV_WEB_URL` override the
  current public DEV defaults.
- The credential must not grant repository-wide administration or access to
  seeded projects. Rotate it according to the eventual identity-provider
  policy; the scaffold does not mint or persist a token.

The public web probe deliberately omits the API bearer token, including when
the web and API currently share a hostname. Until the `dev-smoke` Environment
and its secret exist, the smoke refuses before its first HTTP request. This is
deliberate: public read checks alone did not catch the historical submit-422
failure and cannot authorize a write rehearsal.

### Parallel PROD activation checklist (Stage B; not yet executed)

Keep the current public PROD untouched while standing up the replacement on a
separate hostname. Complete every item before setting `PROD_ENABLED=true`:

1. Protect `feat/pr-party` (or its replacement deploy branch) with required CI,
   required review, and no direct pushes. Make `.github/workflows/**` owned by
   CODEOWNERS whose approval is required, so a release cannot weaken its own
   checks.
2. Create the `dev-smoke` GitHub Environment, restrict it to that protected
   deploy branch, and store `DEV_SMOKE_BEARER_TOKEN` only there. Then create the
   `production` GitHub Environment and restrict it to the same protected
   deploy branch, and place `PROD_DEPLOY_SSH_KEY` and
   `PROD_DEPLOY_KNOWN_HOSTS` only there. Set repository/environment variables
   `PROD_DEPLOY_HOST`, `PROD_DEPLOY_USER`, and finally `PROD_ENABLED`. Set the
   environment-only marker `PROD_ENVIRONMENT_READY=parallel-uat-v1` after its
   branch restrictions and secrets have been reviewed; the protected job
   refuses without it and re-validates the effective host/user values after
   entering the Environment. Do not add a workflow-level manual promotion input;
   the intended policy is automatic on green. If organizational policy requires
   an Environment reviewer, that is the sole allowed human authority gate.
3. Install the deploy key under the least-privilege viable account with
   `restrict,command="/usr/local/sbin/ontokit-deploy"`. From a clean client that
   cannot offer any other key, use `IdentitiesOnly=yes` to prove `status` works
   and hostile commands/full shells are refused. Do not enable the workflow for
   an unrestricted shell key.
4. Choose and record one clean data path before bootstrap: **seed a fresh
   replacement** or **migrate and explicitly purge demo gibberish**. Never blend
   the choices implicitly. Back up the current public service and retain its
   immutable rollback pair.
5. Commit a reviewed manifest containing the exact matched, fetchable API and
   web SHAs; deploy that pair to DEV and verify the running image revisions are
   identical to it. Install a scoped smoke credential and require the full
   write-path smoke to pass.
6. Build the parallel PROD host, run the same revision/status and smoke proofs
   against its pre-cutover hostname, and complete UAT. Only then flip
   `PROD_ENABLED=true` and observe an automatic green promotion.
7. DNS cutover is a separate, explicitly approved final action after UAT. Keep
   the old service and rollback DNS target available until the acceptance window
   closes.

Stage A changes no AWS host, GitHub Environment, secret, branch protection, or
DNS state. A local fake-endpoint rehearsal proves failure/gate behavior; live
writes and activation remain Stage B evidence.

## Demo repository refresh (prepared; not activated)

`deploy/refresh_demo_repositories.py` owns the U8 refresh contract for the two
approved routes in `deploy/demo-mirrors.json`:

- `alea-institute/FOLIO` → `alea-institute/ontokit-demo-folio`
- `CatholicOS/ontology-semantic-canon` →
  `alea-institute/ontokit-demo-semantic-canon`

The job refuses any other source/destination pair. It also refuses missing or
identical source/destination tokens. Git receives each token through a temporary
askpass helper rather than a URL or command argument. Each refresh clones only
the source default branch, adds `DEMO-README.md` with the exact source revision
and UTC refresh time, and force-updates only `HEAD:refs/heads/main` on the demo
target. It never uses `--mirror` or `--all`, so demo-authored non-default branches
remain untouched.

Both pushes finish before `deploy/resync_demo_projects.py` runs. The resync
child receives neither Git token in its environment; it reopens the same scoped
destination credential from a separate root-only token file. A nonblocking host lock refuses overlap,
so cron runs cannot interleave. If either repository refresh fails, project
resync does not start; the previously provisioned demo projects therefore never
read a partially refreshed pair.

Activation remains gated. Before installing `deploy/demo-refresh.cron.example`:

1. Create both private destination repositories and seed their branch
   protection/default-branch settings.
2. Mint `GITHUB_DEMO_SOURCE_TOKEN` with contents-read access only to the two
   source repositories. Prove that identity cannot push to either source.
3. Mint `GITHUB_DEMO_MIRROR_TOKEN` with contents-write access only to the two
   destination repositories. Prove that it cannot write anywhere else.
4. Install `deploy/resync_demo_projects.py`. It idempotently provisions one
   public demo Project per live source, refuses cross-target writes, swaps each
   bare clone with rollback on failed reindex, and rebuilds the PostgreSQL
   ontology index before accepting the new clone.
5. Store the refresh environment in root-owned mode-0600
   `/etc/ontokit/demo-refresh.env`. Also write the destination token value to
   root-owned mode-0600 `/etc/ontokit/demo-project-token`, set
   `ONTOKIT_DEMO_TOKEN_FILE` to that path, and set
   `DEMO_PROJECT_RESYNC_EXECUTABLE` to the checked-in resync script. The API and
   worker need the same scoped value as `GITHUB_DEMO_MIRROR_TOKEN`; never put a
   token value in cron, Git URLs, logs, or this repository.
6. Run one manual refresh, verify both default branches and README receipts,
   verify an existing non-default demo branch is unchanged, and record the
   negative token-scope push tests before enabling the daily cron.

The checked-in cron example is inert. No demo repository, token, host cron, or
project is created by committing these assets.

## Logs and health

Use `docker logs <container-name>` (optionally with `--tail` or `--follow`) for
service diagnostics. The login healthcheck must send the real
`Host: ontokit-auth.dev.openlegalstandard.org` header. Undici drops attempted
Host overrides, so the captured check deliberately uses Node's `http` module.

## Authentication mode

The API accepts only the literal enum values `required`, `optional`, or
`disabled` for `AUTH_MODE`. The deploy command validates this single server-side
value before its first fetch, checkout, rollback-record, or build mutation;
Compose supplies that same value to API runtime plus web build and runtime.
DEV's terminal state is `optional`: anonymous reads remain available while
protected operations use Zitadel when its issuer/client/client-secret set and
session signing secret are configured.
Optional mode without Zitadel remains supported. The former Traefik basic-auth
gate is intentionally absent; Zitadel is the sole configured authentication
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
