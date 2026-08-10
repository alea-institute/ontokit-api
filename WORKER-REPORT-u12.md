# Worker report — U12 DEV IaC capture

## Result

Captured the verified 2026-08-10 CPX41 DEV environment as committed,
placeholder-parameterized infrastructure-as-code. The CI auto-deploy workflow
half remains deferred pending Damien's deploy keypair and U11 CI.

## Files

- Added `deploy/compose.dev.yaml`: live `/opt/ontokit/compose.yaml` structure,
  including API SHA `20cb6aa7`, web SHA `cfa91623`, and server-side `.env`
  provenance.
- Added `deploy/.env.example`: the 12 requested stack keys, placeholders only,
  with one explanatory comment per key and a 32-character master-key placeholder.
- Added `deploy/traefik/ontokit-dev.yaml`: live proxy file-provider config and
  the post-flip no-basic-auth decision.
- Added `deploy/firewall/ontokit-firewall.sh` and
  `deploy/firewall/ontokit-firewall.service`: the idempotent CPX41 ingress rules
  and systemd persistence unit.
- Added `deploy/RUNBOOK.md`: bootstrap, deploy, rollback, logs, authentication,
  and known-gotcha operations.
- Deleted `railway.json` under the KD4 topology ruling.

## Validation

`docker compose -f deploy/compose.dev.yaml config --quiet` was run with all 12
requested variables exported to placeholder values and with a temporary,
placeholder-only `deploy/.env` satisfying the captured `env_file` references.
The temporary file was removed immediately after validation.

```text
exit 0; no output
```

Additional checks:

```text
bash -n deploy/firewall/ontokit-firewall.sh
exit 0; no output

env-template: 12 expected keys; one comment each; masterkey placeholder length 32
git diff --check
exit 0; no output
traefik-gate-scan: PASS (no basic-auth middleware captured)
```

No application tests were added or run: this change captures configuration and
operations documentation without changing application behavior. Compose
rendering, shell syntax, exact environment-template assertions, targeted proxy
checks, and diff validation are the replacement evidence.

## Secret scan

The scan recursively covered `deploy/`, including hidden files, for these
value-shaped patterns:

- PEM private-key headers
- GitHub tokens matching `gh[pousr]_` plus 20 or more token characters
- AWS access-key IDs matching `AKIA` plus 16 uppercase alphanumeric characters
- three-segment JWT-shaped values beginning with `eyJ`
- hexadecimal strings of 48 or more characters
- base64-shaped strings of 48 or more characters
- non-placeholder assignments to sensitive `.env` names (`AUTH_SECRET`,
  `SECRET_KEY`, `ENCRYPTION_KEY`, `MASTERKEY`, `PASSWORD`, `CLIENT_SECRET`, and
  `SERVICE_TOKEN`)

```text
secret-scan: PASS (no private-key headers, GitHub tokens, AWS access keys, JWTs,
long hex strings, or long base64 strings)
placeholder-assignment-scan: PASS (all sensitive .env assignments are explicit
placeholders)
```

The known public hostnames, CPX41/proxy IP addresses, image names, development
database credentials present in the supplied live compose, and PAT expiration
dates are infrastructure facts rather than secret values.

## Deliberate deviations and deferrals

- Superseded the plan's KTD2 Traefik basic-auth gate: per Damien's 2026-08-10
  decision, the capture represents the post-flip state where Zitadel is the sole
  authentication system.
- Deferred the CI auto-deploy workflow half exactly as scoped; it remains blocked
  on Damien's deploy keypair and U11 CI.
- No other deviations from the supplied live compose, Traefik, firewall, or
  requested runbook facts.
