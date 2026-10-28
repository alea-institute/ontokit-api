# oa-6 suggestion and review security audit

Observed **2026-10-28 UTC** for the October 27 plan. The full lockfile scan found **seven distinct vulnerabilities across five packages**; none is suppressed or remediated by this hygiene item. The completed Semgrep OSS scan found **zero findings** in its recorded scope. The current Semgrep release could not complete here, and the successful older-engine run is supplemental evidence, not authenticated CI/Pro equivalence.

Authority: `docs/plans/2026-10-27-2050-test-suggestion-review-audit-plan.md`, U5/R5–R8/KTD4–KTD5. Starting build revision: `241a430b`; baseline evidence commit: `c062f5bc`; final application scan revision: `fbb520e50eb877ce3b29e9f1dda23d5a7d1d1a57`. The controller owns fresh upstream reconciliation, required reviews and publication. This audit neither establishes DEV acceptance nor completes oa-0 dependency remediation.

## Evidence and outcomes

| Check | Outcome | Evidence and limitation |
|---|---|---|
| Full `uv.lock` OSV scan | Completed, findings remain; exit 1 | OSV-Scanner 2.3.5, matching the pinned workflow release; 164 lock entries retained in JSON, including the unversioned local project |
| Semgrep 1.182.1 | BLOCKED; exit 2 | Core rule-validation RPC subprocess exited 1. No valid scan JSON; findings and parsing coverage are unknown |
| Semgrep 1.136.0 OSS | PASS for recorded scope; exit 0 | Current snapshots of all five fallback packs; 361 applicable rules reported, 194 targets, zero findings, zero reported parse errors/skipped rules/targets |
| Manual authorization trace | Completed within scope | The missing private-project comment-review view check was reproduced and corrected in U3; no broader authorization policy change |
| Baseline tests and type checks | Recorded separately | `docs/plans/evidence/oa-6/baseline.md`; the original Python 3.13 mypy failure is not a new code regression |
| Final tests, persistence and coverage | See final verification receipt | `docs/plans/evidence/oa-6/final-verification.md` is the build owner's authoritative result; this scan is not a substitute for those checks |
| Authenticated CI policy/Pro and deployment acceptance | NOT RUN locally | Controller-owned gates; no credentials, remote, hosted account, provider or deployment was accessed |

Machine-readable metadata, exact source hashes, invocations and timestamps are in `docs/plans/evidence/oa-6/audit-scan-metadata.json` and `audit-source-manifest.json`. Machine-local paths in scanner output were replaced with repo-relative paths or symbolic cache/toolchain placeholders. Package/advisory payloads and scan errors were retained. Compressed artifacts are gzip JSON/text, readable with `gzip -dc`.

## Dependency findings

Lock SHA256: `430fec1e7523f4a4a034a290c45eb29c0e3a86978cc7154bf027951d3eb84d5c`.

The scanner extracted all **164 entries / 162 package names**. There are **163 versioned published entries** and the local editable `ontokit` entry, which has no registry version and is explicitly unscannable. The two alternative-version package entries are preserved. The JSON inventory matches every lock entry; the local project's missing version is represented by an empty string. No environment-marker, runtime or development filtering was applied to the vulnerability scan.

The result contains eight advisory records representing seven distinct vulnerabilities after alias grouping. Severities below are the advisory's supplied labels, not a new risk score. Full CVSS vectors, aliases, affected ranges, references, publication/modification times and package inventory remain in `docs/plans/evidence/oa-6/audit-osv.json`.

| Package and locked version | Advisory / aliases | Supplied severity | First fixed version | Applicability and disposition |
|---|---|---|---|---|
| certifi `2026.7.22` | [PYSEC-2026-4320](https://osv.dev/vulnerability/PYSEC-2026-4320), alias [GHSA-6q48-6c7w-9gw2](https://osv.dev/vulnerability/GHSA-6q48-6c7w-9gw2) | CRITICAL on GHSA; CVSS v4 supplied by both records | PYSEC: `2026.10.19`; GHSA: `2026.10.18` | Runtime transitive dependency of HTTP/storage clients. Advisory concerns compromised CA intermediates. **oa-0:** upgrade to at least `2026.10.19` to satisfy both records and rerun the full scan |
| filelock `3.32.3` | [GHSA-567q-5qvw-pf92](https://osv.dev/vulnerability/GHSA-567q-5qvw-pf92); CVE-2026-112907 | LOW | `4.2.1` | Windows SoftFileLock timeout/stale-break issue. Runtime and dev dependency reachability; Windows-specific applicability is not a reason to erase the lock finding. **oa-0:** upgrade/retest |
| filelock `3.32.3` | [GHSA-9vhv-jxww-xfm5](https://osv.dev/vulnerability/GHSA-9vhv-jxww-xfm5); CVE-2026-112913 | LOW | `4.2.1` | Windows ReadWriteLock SQLite side-file interference. No direct application use of this API was found. **oa-0:** upgrade/retest; retain package-level finding |
| filelock `3.32.3` | [GHSA-v4x8-3wr5-3wq8](https://osv.dev/vulnerability/GHSA-v4x8-3wr5-3wq8); CVE-2026-112940 | HIGH | `4.2.1` | ReadWriteLock symlink/journal-file attack by a local actor. No direct application use of this API was found; transitive call reachability was not proved absent. **oa-0:** upgrade/retest |
| gitpython `3.2.0` | [GHSA-83vg-56qc-22m7](https://osv.dev/vulnerability/GHSA-83vg-56qc-22m7); CVE-2026-114145 | HIGH | `3.2.1` | Direct runtime dependency; advisory concerns submodule cloning before path validation. Current application Git implementation uses pygit2; no GitPython imports or submodule clone call was found under `ontokit/`. This reduces demonstrated route reachability but does not clear the dependency. **oa-0:** upgrade/retest |
| setuptools `84.0.0` | [GHSA-xmj3-9gf7-h52w](https://osv.dev/vulnerability/GHSA-xmj3-9gf7-h52w); no alias supplied | LOW | `84.1.0` | Runtime dependency through torch. Advisory concerns Windows legacy wheel installation and namespace-package path escape. Runtime request exploitability was not demonstrated. **oa-0:** upgrade/retest |
| virtualenv `21.14.11` | [GHSA-cx6f-vjhg-g3cc](https://osv.dev/vulnerability/GHSA-cx6f-vjhg-g3cc); CVE-2026-115586 | HIGH | `21.14.13` | Development dependency through pre-commit; Bash prompt expansion can execute an unsafe prompt. Dockerfiles export with `--no-dev`, so this edge is not included in their declared runtime install. This is not a live-image attestation. **oa-0:** upgrade/retest; developer-tool finding remains visible |

`docs/plans/evidence/oa-6/audit-lock-inventory.json` preserves versions, resolution markers and static runtime/development reachability. That reachability deliberately ignores marker evaluation and is not an exploitability or deployment claim. Mako `1.4.3` is included in the full scan and has no finding in this result. No dependency, Python target, workflow, scanner configuration or ignore entry changed in oa-6.

### OSV reproduction

The official [2.3.5 release](https://github.com/google/osv-scanner/releases/tag/v2.3.5) binary reports osv-scalibr 0.4.5, commit `30bcc134e23fbc35731021ee43ec433c483715d7`. Its SHA256 matched the release checksum: `bb30c580afe5e757d3e959f4afd08a4795ea505ef84c46962b9a738aa573b41b`.

```bash
osv-scanner scan source --lockfile uv.lock --format json --all-packages --all-vulns
```

Run from the repository root. Tool acquisition was outside the project dependency graph. `audit-osv.stderr.txt` retains extraction diagnostics and the one local-package warning. The source-walk diagnostic says root `/`, but explicitly records **0 directories visited / 1 inode / 1 extraction**: only the explicitly selected lockfile was scanned. The default plugin warning is retained; no transitive manifest build or recursive repository scan ran.

## Application scan scope

The successful run used **Semgrep 1.136.0, `engine_requested: OSS`**, with `SEMGREP_APP_TOKEN` removed for that process, metrics disabled, strict errors enabled and original rule identifiers preserved. The older CLI needed setuptools `80.10.2` for its `pkg_resources` import; that tooling-only environment is outside the project and does not affect `uv.lock`.

The five existing fallback packs were downloaded from their official `https://semgrep.dev/c/p/<pack>` endpoints. Their content hashes, 1,802 pack-rule entries and 1,144 unique rule identifiers are preserved in `audit-semgrep-rule-inventory.json`. No downloaded rule declares a minimum version newer than 1.136.0. Pack rules were neither edited nor filtered.

| Pack | Rule entries | SHA256 |
|---|---:|---|
| `p/default` | 1,074 | `f6a76d44cca586a6827b24f162c524560e0c230b38f65094872932d35ffc6804` |
| `p/owasp-top-ten` | 560 | `cd53fb9a7906c6ba1392b5e612230cb64bea1ba560be91b334bfa4483af6f195` |
| `p/python` | 151 | `de5bc79d074c3b4146338a0bf95830406f169bd91ac590a2f1ef111f162e23f5` |
| `p/fastapi` | 1 | `8f9b5bb920bc83e4f44fcbb1f38d8f77e08c3170b880c19a94854faf0ce34f28` |
| `p/jwt` | 16 | `a1aca26b241fb796bb01f56b804b57d52a9d32da18700e5c0ffef3b27c8644aa` |

The target was explicitly `ontokit/`: **192 Python files plus the two tracked PR-party asset files**. Source and ignore-file hashes were checked after the scan and unchanged. `audit-semgrep-summary.json` lists every scanned path and timing-rule identifier. The CLI reports 361 rules run; the timing payload contains 421 rule identifiers. Both counts are preserved as distinct tool-reported measures rather than forcing them to agree. The scan reports no skipped target, skipped rule, parsing error, timeout or finding. Its stdout JSON and stderr summary are preserved in `audit-semgrep-1.136.json.gz` and `audit-semgrep-1.136.stderr.txt`.

This local source scan omits deployment scripts, Docker/workflow configuration, generated migrations, tests, docs and all forbidden files. The existing `.semgrepignore` and 20 inline suppression sites remain unchanged; `audit-existing-suppressions.txt` inventories them. None of the three scoped routes or two scoped services has an inline suppression. The adjacent authentication HTTP-request suppression is justified by a URL built from operator settings plus a literal path; that was reviewed without accessing the settings' deployed values.

The latest locally acquired Semgrep **1.182.1** failed core rule-validation RPC for registry configs, its supported legacy flag, and downloaded config snapshots. Those failures produced `<ERROR: missing output>`, not a clean result. An initial unsupported `--oss` flag and the older CLI's missing `pkg_resources`/renamed `--oss-only` option were corrected; all diagnostics remain in `audit-semgrep-failed-attempts.txt.gz`. The source of the 1.182.1 subprocess failure was not established beyond its reported error; the sandbox's known socket restrictions are not presented as a proved root cause.

The controller must rerun its configured current scanner and authenticated CI policy/Pro engine. OSS results do not establish cross-file Pro coverage or hosted policy equivalence; the [Semgrep CLI reference](https://docs.semgrep.dev/cli-reference) distinguishes local `scan` from `ci` and their finding-exit behavior.

### Semgrep reproduction

Use the recorded pack snapshots, checking their hashes; downloading the same registry URL later may return different rules. The snapshot bodies remain in the build tool cache, while identifiers and hashes are durable. A controller unable to retrieve matching bodies should record new hashes and treat the result as a fresh audit.

```bash
semgrep scan --oss-only --jobs 1 --strict --error --metrics off \
  --disable-version-check --json --verbose --time --no-rewrite-rule-ids \
  --config "$XDG_CACHE_HOME/oa6/semgrep-rules/default.yaml" \
  --config "$XDG_CACHE_HOME/oa6/semgrep-rules/owasp-top-ten.yaml" \
  --config "$XDG_CACHE_HOME/oa6/semgrep-rules/python.yaml" \
  --config "$XDG_CACHE_HOME/oa6/semgrep-rules/fastapi.yaml" \
  --config "$XDG_CACHE_HOME/oa6/semgrep-rules/jwt.yaml" ontokit/
```

This invocation is for Semgrep 1.136.0. Keep authentication absent for an OSS reproduction; use the controller's existing CI policy for CI acceptance rather than changing that policy to match this fallback.

## Manual boundary trace and disposition

| Boundary | Observed enforcement | Evidence / disposition |
|---|---|---|
| Authenticated suggestion writes | Routes require `RequiredUser`; disabled-auth write protection remains in `get_current_user`; service ownership and reviewer-role checks are separate | Existing auth-disabled tests remain unchanged; U2 adds transport/validation proof |
| Own-session listing | SQL filters by both requested project and current user's ID, with hidden-demo exclusion | U4 must establish persisted HTTP isolation; this query trace alone is not persistence proof |
| Reviewer queue/decisions | Reviewer access permits owner/admin/editor or superadmin; project-scoped lookup precedes session mutation | Preserve suggestion editor authority separately from owner/admin PR approvals |
| Bulk review | Permission gate before loop; per-item exceptions roll back and refresh the project before continuing | U4's wrong-project-then-valid control checks persisted partial success |
| Anonymous save/submit/discard | Real token verification supplies a session ID; service checks project/session lookup, token binding, anonymous flag and active status before writes | HMAC token verification includes expiry and constant-time signature comparison; synthetic secrets only in U2 tests |
| Human PR review read | Existing `_can_view` permits public access or project membership; `_get_pr` filters project and number | No new policy or superadmin override introduced |
| Human PR comment-review write | **Reproduced omission corrected:** previously a private nonmember could write a `commented` review; `create_review` now applies the existing `_can_view` before PR lookup and effects | `u3-red.log` records the unauthorized 201; `u3-green-final.log` records passing regression/controls. Fix commit `fbb520e5` |
| PR decision and terminal state | Approval/request-changes remain owner/admin only; closed or merged PRs reject reviews | U3 retains role controls, terminal-state tests and no-effect assertions; U4 adds real persistence proof |

The reproduced access defect is owned and corrected by oa-6's narrowly authorized U3 change. Package findings remain owned by **oa-0**. The original Python 3.13/mypy target mismatch remains **oa-3**'s environment concern; the build owner's separate locked Python 3.11 check is reported in the final receipt. Current Semgrep/CI-policy verification, fresh `dev` reconciliation and missing campaign-source disposition remain **controller** responsibilities. No demonstrated blocker requires Damien's personal input.
