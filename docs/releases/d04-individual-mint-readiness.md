# Individual mint authorization verification

The reviewed [D04 plan](../plans/2026-09-20-0839-fix-individual-mint-enforcement-plan.md) repairs the omission of named individuals from suggestion-save mint authorization. It preserves the existing submission classifier and its duplicate-validation, billing and entity-cap behavior.

## Baseline and scope

API base: `39a1fa140af2a6bd2ef547bc69e4664590a5036e` (`dev`). Plan and resolved independent reviews: `28e2f5c5`; [review evidence](d04-plan-review.json). Recognition implementation: `a5b3c163`.

The implementation recognizes explicit declarations, ordinary local/external class membership, and named instances of anonymous class expressions. It compares recognized subject IRIs in the existing branch and proposed content. Existing recognized identities remain editable, including punning. Blank-node subjects, untyped references and structural metadata remain outside this bounded individual detector. Literal type objects are not class-membership assertions.

The submission classifier remains unchanged. Historical branch reconciliation and wider namespace/untyped-entity policy remain open under B14 in the web master roadmap.

## Local verification

The first nine named-individual regressions failed against the baseline before production changes. The expanded pre-fix matrix produced 32 failures and 25 passes. The implementation then passed all 57 focused recognition/submission tests, independently rerun by the delivery orchestrator.

Production Ruff check and format check pass. Pyright reports zero errors or warnings. Mypy reports no issues in 193 source files under Python 3.11 using the exact repository lockfile. An initial run in the shared Python 3.13 environment hit newer NumPy stub syntax; a separate locked environment resolved that mismatch without source or dependency changes. Required CI remains the publication gate.

All 3,243 tests pass (34 warnings, no skips), with 87% combined coverage. The focused save/submission suite passes 227 tests; 30 real database/Git denial cases and 36 unit cases cover all four public writers, resumed identities, trusted creation and missing baselines. Verification commit: `beef32dd`. Independent final review found no primary or actionable findings and all six requirements met; see [code review](d04-code-review.json). Publication evidence is pending. Disposable PostgreSQL 17 and Redis instances isolate test state from the running application. This receipt does not claim deployment or persona acceptance.

## Browser verification

`ce-test-browser mode:pipeline` scoped this API branch against `dev`. The only production change is the suggestion service; no rendered routes or browser assets changed. The consuming web route `/projects/[id]/editor` is **Skip**: end-to-end persona verification requires the matched deployed API/web pair and authenticated OIDC sessions, reserved for D02. No browser server was started, no driver session was initialized, and no browser pass is claimed. Save authorization was exercised through all four public service entry points with real Git/database denial assertions.

## Remaining taxonomy policy

The review retains one pre-existing P2 for B14: subjects typed only as `owl:DeprecatedClass`, `owl:DeprecatedProperty` or `rdfs:ContainerMembershipProperty` are not recognized by mint authorization. The baseline already omitted these types. A separate schema-taxonomy follow-up must decide recognition and add denial/compatibility tests while preserving submission billing and caps. This repair does not close B14. No eligible review fixes remain for D04.
