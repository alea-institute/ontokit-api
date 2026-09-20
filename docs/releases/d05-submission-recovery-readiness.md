# D05 submission embedding-error recovery

D05 applies the established duplicate-check error contract to submission validation: budget refusal returns 402 with the existing application message; unavailable pricing returns 503 with the fixed public message. Unknown failures retain their existing path. The patch adds no automatic retry, refund policy, resubmit validation or transaction boundary.

## Verification

- Proof first: both new mapping tests failed against the baseline with unhandled domain exceptions before the production change.
- Focused service and embedding regressions: 157 passed.
- Eight real HTTP/PostgreSQL/Redis/Git cases cover budget/pricing refusal, before/after a completed paid call, for untrusted contributors and editors. Each failed request closes before fresh-session assertions and an explicit new request succeeds.
- Refusal preserves saved content, branch head, draft/revision/feedback/PR linkage, consumes no submission allowance and produces no PR, notifications or index jobs. Retry creates exactly one linked PR and the expected successful-path effects.
- Real reservation/finalization records from completed provider work remain after a later refusal. The test does not mock accounting persistence or promise free retries.
- Resubmit retains its existing no-paid-validation path; background failures restore ACTIVE and do not count as successful submissions.
- Full locked Python3.11 suite: **3,260 passed, zero skipped, 35 warnings, 90% coverage**, 46.48s.
- Ruff check/format: passed, 194 production files formatted. Mypy: clean, 193 source files. Pyright: zero errors and warnings.
- Simplification: independent reuse, quality and efficiency checks found no changes worth making. Production code remained unchanged after verification.

## Traceability

- [Reviewed plan](../plans/2026-09-20-1224-fix-submission-embedding-errors-plan.md)
- [Plan review and resolved claims](d05-plan-review.json)
- [Unit proof-first evidence](d05-u1-verification.json)
- [Complete execution receipt](d05-execution-receipt.json)

This is API verification only. Matched deployment and real authenticated persona acceptance belong to D02. Frontend parsed-error messaging and B14 schema-taxonomy reconciliation remain separately tracked.

## Final review and browser coverage

CE code review completed against `ab7b0c5f5456c7fc2ca4ffa1448c3c717b695b83`: eight local lenses, all eight requirements and both units met, no actionable findings. [Review receipt](d05-code-review.json). The Claude attempt exhausted its turn limit without a usable review; the native adversarial fallback completed. Independent cross-model code-review corroboration is unavailable.

Browser result: **PARTIAL**. `/projects/[id]/editor` submission flow: **Skip**. No frontend assets changed; the matched API/web pair is not deployed, and authenticated persona acceptance requires the DEV recovery in D02. No browser session or OAuth success is claimed. API behavior was exercised by the eight real HTTP integration cases above.

No additional compound learning was created: the mapper, regression tests, plan and receipts already retain the reasoning. The final publication commit adds evidence documents only; production and test content remain identical to the reviewed and tested head.
