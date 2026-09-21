# Schema mint authorization readiness

The [D07 plan](../plans/2026-09-20-1820-fix-schema-mint-authorization-plan.md) closes the named schema declaration omission for `owl:DeprecatedClass`, `owl:DeprecatedProperty`, and `rdfs:ContainerMembershipProperty`. U2 characterizes U1 implementation commit `d49a40243b821a5f95f9ceb204f467371ee96396`; U2 changes tests and this record only.

## Executed U2 evidence

On 2026-09-20, the locked Python 3.11 environment ran:

```text
python3 /tmp/d07-run.py pytest tests/unit/test_suggestion_trust_integration.py tests/integration/test_llm_review_regressions.py -q --no-cov
298 passed, 9 warnings in 9.75s; no skips
```

The launcher supplied disposable migrated PostgreSQL and Redis. An earlier sandboxed attempt was interrupted (exit 130) after slow local-service retries and is not a pass. The final run used authorized loopback access. Warnings concern existing Pydantic/Starlette deprecations and the unregistered integration marker.

The existing resumed-session unit matrix now covers all three schema types across all four writers: label edits, typing replacements and punning in both directions, new identities, first typing of label-only/reference-only/structural-only subjects, and mixed permitted-edit/forbidden-addition refusal. The existing trusted-contributor/reviewer authenticated creation matrix includes all three types. Existing missing-baseline and malformed-input controls execute unchanged in intent.

The persisted denial matrix contains 66 cases: six writer/hint forms for malformed input and ten declaration/proposal variants, including pure creation and mixed permitted edits plus new forbidden identities for each schema type. It verifies unchanged Git source/head, session fields before and after database refresh, and no refresh enqueue. Twelve persisted positive cases cover every schema type and every writer, including schema-to-ordinary replacement. Identities exist only on the suggestion branch. Authenticated cases commit trust revocation and reload project membership before saving. Fresh Git reads and database refresh verify source, change count, activity, revision, status, anonymous byte accounting and writer-specific entity metadata. Only normal authenticated save enqueues refresh.

Both affected fixtures enter committed-project cleanup before Git setup and assert row removal after successful execution. Git repositories use pytest temporary paths. Shared disposable service shutdown belongs to the delivery orchestrator.

Ruff check, Ruff format check, and `git diff --check` pass for the U2 changes. No production behavior was changed by U2, and no artificial pre-fix failure is claimed; recognition red/green evidence belongs to [U1 evidence](d07-u1-evidence.json).

## Remaining delivery and B14 scope

The authoritative full API suite passed 3,478 tests with zero skips and 90% coverage. Ruff check and formatting pass; mypy passes across 193 source files and pyright reports zero errors. Both D07-owned test containers and their anonymous volumes were removed, with every preexisting container, volume and network preserved. [Local verification](d07-local-verification.json) records these results. Independent review is complete: [raw report](d07-code-review.json), [corrected documentation rationale](d07-review-resolution.json). Required CI and merge remain pending. The full suite ran before the final test-only fixture simplification; afterward all 94 affected integration tests and static checks passed, as [simplification evidence](d07-simplification.json) records. This API change has no rendered UI; [browser applicability](d07-browser-applicability.json) records a skip. No browser pass, deployment or authenticated hosted persona acceptance is claimed. D02 retains matched deployment and persona acceptance.

This repair addresses B14's three schema declaration omissions only. Historical branch reconciliation and wider namespace/untyped-entity policy remain open; the web master roadmap remains authoritative and must retain those distinctions. This receipt does not close B14 as a whole.
