# OntoKit DEV host migration: inventory, backup, rehearsal, cutover

Prepares the move from the Hetzner EU host to an ALEA-controlled AWS account
(`docs/plans/2026-09-30-chore-alea-aws-migration-readiness-plan.md`). Nothing
here provisions, bills or changes routing. The move stays blocked until the
gates below hold. `tests/unit/test_migration_inventory.py` fails if
`deploy/compose.dev.yaml` gains a service, volume, port or variable this file
does not list.

## Gates (all external; none is met by this repository)

| Gate | Owner | Evidence required |
|---|---|---|
| ALEA AWS account, region and operator role | Mike / ALEA | Read-only `aws sts get-caller-identity` from the approved profile matches the approved account |
| Budget and instance sizing | Damien | Written approval of size and monthly ceiling before any resource exists |
| Staging hostname for the rehearsal | Damien | DNS name plus a Zitadel redirect URI registered for it |
| Accepted EU release pair | Operator | `ontokit-deploy status` output with no drift |

Sizing input: EU runs on a Hetzner CPX32 (RUNBOOK, 2026-09-21). The compose
memory limits below add up to 11.25 GiB, which no host this size can give every
service at once. Size the target from measured EU usage (`docker stats
--no-stream`, `docker system df -v`), not from the limits.

## Inventory (from `deploy/compose.dev.yaml`)

### Services

| Service | Image source | Memory limit | State it owns |
|---|---|---|---|
| `postgres` | `pgvector/pgvector` pg17, digest-pinned | 2g | `postgres_data` (databases `ontokit`, `zitadel`) |
| `redis` | `redis:7-alpine`, digest-pinned | 512m | `redis_data` (ARQ queue) |
| `minio` | `minio/minio`, digest-pinned | 1g | `minio_data` |
| `api` | built from `ontokit-api`, or the published GHCR digest | 2g | `git_repos` (shared with worker) |
| `worker` | same image as `api` | 3g | none of its own |
| `web` | built from `ontokit-web`, or the published GHCR digest | 1g | none |
| `mailpit` | `axllent/mailpit`, digest-pinned | 256m | none (DEV mail sink) |
| `zitadel` | `ghcr.io/zitadel/zitadel`, digest-pinned | 1g | `zitadel_data` (PATs) plus the `zitadel` database |
| `login` | `ghcr.io/zitadel/zitadel-login`, digest-pinned | 512m | reads `zitadel_data` |

### Named volumes

`postgres_data`, `redis_data`, `minio_data`, `git_repos`, `zitadel_data`.
`deploy/backup-dev.sh` captures `postgres_data` as a `pg_dumpall` and the other
four as tarballs.

### Published ports (reachable only from the ingress proxy; see `deploy/firewall/`)

| Port | Service | Routed hostname |
|---|---|---|
| `3000` | `web` | `ontokit.dev.openlegalstandard.org` |
| `8000` | `api` | `ontokit.dev.openlegalstandard.org` paths `/api/v1`, `/ws`, `/health`, `/openapi.json` |
| `8080` | `zitadel` | `ontokit-auth.dev.openlegalstandard.org` |
| `8081` | `login` (container port 3000) | `ontokit-auth.dev.openlegalstandard.org/ui/v2/login` |

### Variables in `/opt/ontokit/.env` (names only; values never leave the host)

`POSTGRES_PASSWORD`, `ZITADEL_DB_PASSWORD`, `ONTOKIT_DB_PASSWORD`,
`MINIO_ROOT_USER`, `MINIO_ROOT_PASSWORD`, `AUTH_MODE`, `AUTH_SECRET`,
`NEXTAUTH_SECRET`, `SECRET_KEY`, `GITHUB_TOKEN_ENCRYPTION_KEY`, `ZITADEL_ISSUER`,
`ZITADEL_CLIENT_ID`, `NEXT_PUBLIC_ZITADEL_CLIENT_ID`, `ZITADEL_CLIENT_SECRET`,
`ZITADEL_SERVICE_TOKEN`, `ZITADEL_MASTERKEY`, `ZITADEL_ADMIN_PASSWORD`,
`SUPERADMIN_USER_IDS`. The deploy command sets `ONTOKIT_API_REVISION` and
`ONTOKIT_WEB_REVISION`, and for image deploys `ONTOKIT_API_IMAGE` and
`ONTOKIT_WEB_IMAGE`.

Two values must travel unchanged with the data. `ZITADEL_MASTERKEY` encrypts
Zitadel's data, so a restore without it cannot read the `zitadel` database.
`GITHUB_TOKEN_ENCRYPTION_KEY` encrypts the GitHub tokens stored in `ontokit`,
so a new value orphans every connected integration.

### Host files outside Docker

| Path | Source in this repository | Holds secrets |
|---|---|---|
| `/opt/ontokit/compose.yaml` | `deploy/compose.dev.yaml` | no |
| `/opt/ontokit/compose.images.yaml` | `deploy/compose.dev.images.yaml` (image deploys) | no |
| `/opt/ontokit/.env` | `deploy/.env.example` | yes |
| `/opt/ontokit/ontokit-api`, `/opt/ontokit/ontokit-web` | Git checkouts | no |
| `/opt/ontokit/.deploy-current`, `.deploy-previous` | written by the forced command | no |
| `/usr/local/sbin/ontokit-deploy` | `deploy/ontokit-deploy.sh` | no |
| `/root/.ssh/authorized_keys` forced-command entry | RUNBOOK "Automated deploy" | public key only |
| `/usr/local/sbin/ontokit-firewall.sh`, its systemd unit | `deploy/firewall/` | no |
| `/etc/ontokit/firewall.env` | RUNBOOK bootstrap step 4 | no (addresses) |
| `/etc/ontokit/demo-refresh.env`, `/etc/ontokit/demo-project-token` | not yet installed | yes |
| `/var/log/ontokit-deploy.log` | written by the forced command | no |

### External dependencies

- The ingress proxy (Traefik, `deploy/traefik/ontokit-dev.yaml`) runs on a
  separate box. It terminates TLS with Let's Encrypt and routes to this host's
  ports. A move changes its upstream address, not its hostnames.
- DNS for `ontokit.dev.openlegalstandard.org` and
  `ontokit-auth.dev.openlegalstandard.org` points at that proxy.
- GitHub: both checkouts fetch from `alea-institute`, and the `dev-deploy`
  environment holds `DEV_DEPLOY_HOST`, `DEV_DEPLOY_SSH_KEY` and
  `DEV_DEPLOY_KNOWN_HOSTS`. All three change with the host.
- GHCR, for image deploys.

## Backup (EU, no downtime)

```sh
sudo bash /opt/ontokit/ontokit-api/deploy/backup-dev.sh /root/ontokit-backup-$(date +%Y%m%d-%H%M)
```

The backup leaves the site serving. Each database dump is consistent, but a write
that lands between steps can appear in one artifact and not another. For the
cutover snapshot, add `--stop-writers`: the script stops `web`, `worker` and
`api` (never the data services) and restarts them however it ends. The output
directory is mode 0700. It holds database contents and Zitadel PATs, so move it
only through the operator's secret transfer mechanism, together with `.env`.
`MANIFEST` records the running revisions and the alembic head, and `SHA256SUMS`
covers every artifact.

## Rehearsal on the target (after the gates)

1. Install Docker and Compose 2.24 or later. Copy `compose.yaml` and, for image
   deploys, `compose.images.yaml`. Clone both repositories at the `MANIFEST`
   revisions and copy `.env` through the secret mechanism. In `.env`, keep every
   value except the hostnames the staging name replaces.
2. Build or pull the API and web images at the `MANIFEST` revisions. Image
   deploys need GHCR pull access.
3. `sudo bash deploy/restore-dev.sh <backup-dir>`. It verifies `SHA256SUMS`,
   creates the containers, and restores Postgres into a fresh cluster. It
   refuses unless the restored alembic revision equals `MANIFEST`. It then
   unpacks the four volumes and leaves `api`, `worker` and `web` stopped. It
   refuses any target whose database or volumes already hold data.
4. Start `api` alone with `RUN_MIGRATIONS=1`; confirm `/health` and that
   `alembic current` equals the manifest head. Start `web`, then `zitadel` and
   `login`. Start `worker` last, and only after confirming that no outbound
   integration (GitHub sync, demo refresh, PR Party) points at production
   repositories from both sites at once.
5. Check: log in and log out through the staging hostname; FOLIO project
   counts and a class-tree read match EU; `git -C` shows the same refs for a
   sample of bare repositories in `git_repos`; an uploaded object downloads
   from MinIO; the ARQ queue length matches.
6. Record the timings and every failure. Then rehearse the return: point the
   staging name back at EU and confirm EU served unchanged throughout.

`deploy/tests/backup-restore-test.sh` runs steps 3 to 5 against a disposable
stack in CI.

## Cutover and return

1. Announce a write freeze. Run `backup-dev.sh --stop-writers` on EU and leave
   the EU writers stopped. EU is then read-only at the network edge.
2. Restore on AWS (rehearsal steps 3 and 4) and run the step 5 checks.
3. Repoint the proxy upstream (or DNS) to AWS, then update the three
   `dev-deploy` values and re-pin `DEV_DEPLOY_KNOWN_HOSTS`. Run
   `ontokit-deploy status` over the new key.
4. Return path, if AWS has accepted no writes: point the proxy back and start
   the EU writers. If AWS has accepted writes: freeze AWS, take
   `backup-dev.sh --stop-writers` there, restore it on a clean EU stack, then
   reopen EU. Reverting DNS alone would drop those writes.
5. Keep the EU host, its volumes and the cutover backup intact through the
   agreed acceptance window. Decommissioning needs its own approval and a
   verified backup.
