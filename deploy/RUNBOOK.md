# OntoKit DEV deployment runbook

> **T10 synthesis scope:** this branch carries only the inert demo-refresh and
> demo-project assets described under “Demo repository refresh.” The earlier
> deployment sections document prerequisites owned by the separate T2
> deployment tranche; paths absent from this branch must not be installed or
> executed from this branch alone.

This directory captures the DEV deployment contract. The application host moved
to EU CPX32 on 2026-09-21; hosted identity acceptance remains separate. The
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
   `/etc/systemd/system/ontokit-firewall.service`. Before starting the unit,
   create root-owned `/etc/ontokit/firewall.env` with `ONTOKIT_PROXY_IPV4` set
   to the verified ingress proxy's IPv4 and `ONTOKIT_HOST_IPV4` set to this
   application host's IPv4. Both must be canonical dotted-decimal addresses;
   there are no defaults. Missing or invalid settings fail before any rule
   changes. Preserve an independent provider firewall while installing and
   verifying these host rules. Make the script executable,
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
2. On the explicitly selected DEV host, in each repository, run `git fetch`, verify the intended commit,
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

The host's installed forced-command script at `/usr/local/sbin/ontokit-deploy`
is a separate copy. Refresh it from `deploy/ontokit-deploy.sh` for the checkout
umask change to take effect on the host. Until it is refreshed, the Dockerfile's
explicit `COPY --chmod` settings still protect image builds from restrictive
source-file modes.

## Rollback

Prefer the forced command's `rollback` verb, which preserves the known-good
target if a rollback attempt fails and swaps the retry target only after a
successful deployment. For manual recovery, check out the previously known-good
API and web SHA pair, export those two revisions as described above, then run
`docker compose build` and `docker compose up -d --wait --wait-timeout 240`.
Rebuilding both images keeps the deployed pair internally consistent.

## Automated deploy

`.github/workflows/deploy-dev.yml` triggers on pushes to `dev` that change
`deploy/release-manifest.json`. `workflow_dispatch` re-runs the manifest-declared
API/web pair and consumes no inputs. Every run targets the `dev-deploy` GitHub Environment,
so GitHub pauses the deploy job until Damien approves it from the run's
**Review deployments** prompt. Both trigger forms read
`deploy/release-manifest.json`; there are no per-run SHA inputs and no branch-head
fallback. The validator accepts only the declared ALEA repositories and full
lowercase 40-character commit SHAs, and the workflow proves both commits are
fetchable before the forced command sees them. Updating DEV therefore means
reviewing and committing one matched API/web pair in the manifest.

The `dev-deploy` Environment permits only the exact `dev` branch as of
2026-09-21. The frozen `feat/pr-party` workflow also uses this Environment,
so its deployment job is denied by the branch policy. Required reviewers and
self-review protection were preserved.

### Explicit migration target

The deploy job requires `DEV_DEPLOY_HOST`, resolved inside the protected
`dev-deploy` Environment. Set it to the approved destination's IP address or
DNS hostname; there is no default or fallback to the historical US server.
Missing, empty, whitespace, and malformed values fail before credentials are
installed or SSH runs. Do not include a username, port, URL, or SSH options.
IPv4, IPv6 without a zone identifier, and DNS names are accepted.

Before moving this variable, verify the destination's host key independently
and update `DEV_DEPLOY_KNOWN_HOSTS` for that same destination. Keep strict host
key checking and the restricted forced-command deploy credential. A target
change does not change the immutable API/web manifest pair, the protected
Environment, or the dormant PROD gate; keep `PROD_ENABLED` disabled.

For the EU migration, Damien confirmed on 2026-09-21 that no other operator is
changing DEV and authorized autonomous migration work. Establish the target's
restore and capacity evidence under that ownership; repairing the historical
US deployment first is not a prerequisite. Retiring US resources still requires
proof that their data is independently recoverable and their dependencies are
no longer needed. The US server has been retired; do not reuse its historical address. The current
EU application host is `77.42.71.53`, behind the preserved proxy
`204.168.246.227`. The tracked Traefik file includes all four backend URLs and
the internal identity-call source condition. When changing hosts, update these
together with `/etc/ontokit/firewall.env` (`ONTOKIT_HOST_IPV4` and, if the proxy
moves, `ONTOKIT_PROXY_IPV4`), `DEV_DEPLOY_HOST`, and `DEV_DEPLOY_KNOWN_HOSTS`.
Verify that the installed IPv4 rules match the new destination (`--ctorigdst`)
and that ports 3000, 8000, 8080 and 8081 are unreachable from a non-proxy
network. Keep the independent provider firewall while checking host rules.

### Deployment key and host verification

The existing restricted deployment public key was preserved on EU; migration did
not require minting a replacement private key. Keep credential material outside
Git. For a fresh bootstrap, generate a distinct key once without overwriting an
existing key, and install its public half with both `restrict` and
`command="/usr/local/sbin/ontokit-deploy"`. Publish the private key only through
`DEV_DEPLOY_SSH_KEY`; use `DEV_DEPLOY_KNOWN_HOSTS` for the verified public host key.
These secrets must be available to the workflow's repository-level preflight.

Obtain the expected host key through the recorded host-verification process.
An unverified `ssh-keyscan` response alone does not establish trust. Set the
explicit destination and local paths to the restricted key and pinned known-hosts
file, then verify the forced command from an allowed network:

```bash
: "${DEV_DEPLOY_HOST:?Set the explicitly approved destination}"
: "${ONTOKIT_DEPLOY_KEY:?Set the restricted deployment key path}"
: "${ONTOKIT_KNOWN_HOSTS:?Set the verified known-hosts path}"
ssh -F /dev/null -i "$ONTOKIT_DEPLOY_KEY" \
  -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes \
  -o GlobalKnownHostsFile=/dev/null -o UserKnownHostsFile="$ONTOKIT_KNOWN_HOSTS" \
  "root@$DEV_DEPLOY_HOST" status
```

`status` output can report unhealthy services or revision drift with a nonzero
exit status even when SSH authentication and command dispatch worked. Diagnose
that output separately from SSH transport, host-key or authentication errors
(commonly exit 255); do not rotate credentials solely because health is red.
Using the operator credential, separately verify that the installed deployment
public-key entry retains both `restrict` and the forced-command option.

`IdentitiesOnly=yes` prevents a different agent key from silently satisfying
authentication and making the forced-command check misleading. A successful
operator-side connection does not establish GitHub runner reachability. Before
re-enabling DEV deployment, verify runner ingress, the matched manifest, healthy
services and rollback behavior under the protected environment. During EU
recovery the workflow remains disabled and Login acceptance is still open.

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

A rebuild of the already checked-out pair preserves the older rollback target,
including when the rebuild or health checks fail. If no saved pair exists,
a same-pair rebuild creates no rollback history and `rollback` refuses with
exit 64. This is expected on a fresh host or when unaccepted source history
has been archived; independent restore evidence remains the recovery path.
A different-pair deploy recording checkout history does not itself prove that
the recorded pair was healthy or accepted.

Partial checkout failure followed by retry, and retry after a failed rollback,
still have pre-existing history limitations. Verify these recovery paths before
normal deployment activation; the same-pair guard is not full rollback proof.
To restore an accepted saved pair, invoke the
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

## Demo retention

The worker runs demo retention daily at **04:30** in the worker's timezone,
after the host refresh scheduled at **03:17**, leaving more than an hour for
refresh preparation. Keep the worker and host schedule timezones aligned.
The refresh lease prevents overlap even when a refresh runs late. Retention
acquires that lease for one generation at a time and releases it between
generations; it stops starting generations when its run budget expires.

Configuration (setting name, environment variable, default):

- `demo_retention_keep_retired` / `DEMO_RETENTION_KEEP_RETIRED`: **1** retired generation.
- `demo_retention_min_age_days` / `DEMO_RETENTION_MIN_AGE_DAYS`: **7** days.
- `demo_retention_run_budget_seconds` / `DEMO_RETENTION_RUN_BUDGET_SECONDS`: **600** seconds.

From the API repository with its configured runtime environment:

```bash
uv run python deploy/purge_demo_generations.py --dry-run
uv run python deploy/purge_demo_generations.py --status
uv run python deploy/purge_demo_generations.py --apply --env development
```

`--dry-run` prints eligible and retained generations with reasons without
changes. `--status` reads persisted generation markers and purge receipts:
retained count means generations whose content has not been purged (including
eligible generations), and purged count means generations with `purged_at`.
It also reports the latest `purged_at` and the latest failed purge attempt,
including failures followed by a successful retry. Generation identities remain
after content is purged.

`--apply` prints the service summary and persisted receipts. It refuses with
exit code 64 unless `--env` exactly matches `settings.app_env` / `APP_ENV`
(`development`, `staging`, or `production`). A yielded lease exits zero;
recorded purge failures exit one. The development apply command above is for
the U10 acceptance pass only. This section installs no host cron and authorizes
no DEV or PROD mutation; the arq cron is code shipped with the worker deploy.

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

## Seam status on dev (2026-09-06)

- The DEV host has no database-reset verb; the reset before the first `dev`
  deploy is a separate gated operation (U9/B11).
- Required status checks and rulesets on `dev` are repository settings, not
  workflow content (gate B12).
- This workflow copy triggers on pushes to `dev` that change
  `deploy/release-manifest.json`; the API/web pair is manifest-declared.
  `workflow_dispatch` re-runs the manifest pair and consumes no inputs.
- The `dev-deploy` Environment now permits only the exact `dev` branch. Its
  branch policy blocks deployment from the frozen `feat/pr-party` workflow.
