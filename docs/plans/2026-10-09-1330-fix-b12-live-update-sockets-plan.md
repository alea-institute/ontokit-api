---
title: Live-Update Sockets Proof and Hardening (B12) - Plan
type: fix
date: 2026-10-09
topic: b12-live-update-sockets
artifact_contract: ce-unified-plan/v1
product_contract_source: ce-brainstorm
execution: code
---

# Live-Update Sockets Proof and Hardening (B12) - Plan

## Goal Capsule

- **Objective:** an editor who keeps OntoKit open sees index, lint and quality progress keep up, through server restarts and token refreshes, without a stuck "Reindexing..." state. Their access token never appears in server logs, and the UI claims no real-time collaboration that doesn't exist.
- **Product authority:**
  - **Scope:** roadmap B12, scoped by the cockpit ask `ontokit-web-2026-10-09-1324-ontokit-b12-scope` (Chief: "Harden what exists").
  - **Out of scope:** building presence or co-editing is a separate feature direction.
- **Open blockers:** none.

## Product Contract

### Summary

Prove the three real notification sockets (`index-ws`, `lint/ws`, `quality/ws`) end to end on the isolated full-stack harness, and fix what the survey found broken. The fixes cover tokens in URLs, give-up-forever reconnects, retried auth failures, missed-update recovery, handshake-only authorization, and the fake collaboration badge.

### Problem Frame

- **Missing collaboration socket.** The API has no collaboration socket. `ontokit/collab` holds only unused models, yet the editor shows a "Real-time collaboration (coming soon)" badge for `/api/v1/collab/ws`, an endpoint that doesn't exist.
- **Bugs in the sockets that do exist (survey 2026-10-09):**
  - **Token in the URL.** Every socket carries the bearer token in `?token=`, which the uvicorn handshake log records.
  - **Finite reconnects.** Clients give up for good after about 31s of retries.
  - **Auth failures retried.** 4001/4003/4004 close codes are retried as if they were transient.
  - **No missed-update recovery.** A notification lost during a disconnect is never recovered, so "Reindexing..." sticks until reload.
  - **Handshake-only authorization.** Access is checked only at the handshake, so a removed member keeps receiving project events.
  - **Quality socket.** It doesn't close on unexpected errors.
- **No end-to-end test.** No test exercises any socket against a real server.

### Requirements

**Credentials**

- R1. Web clients send the access token in the `Sec-WebSocket-Protocol` handshake header, not the URL. The API accepts that form and, for one compatibility window, still accepts `?token=`.
- R2. No API log line contains a token value. Any `token` query parameter in access and error logs is redacted.

**Connection lifecycle**

- R3. A socket closed with 4001, 4003 or 4004 is not retried. The UI shows why (signed out, no access, project not found).
- R4. Other closes retry indefinitely with capped exponential backoff (max 30s) plus jitter. A retry also starts immediately when the browser comes back online or the tab becomes visible.
- R5. After any reconnect, the settings page refetches index status, and the health panel refetches lint and quality status, so an update missed during the outage is recovered.
- R6. An open socket re-checks the user's project access and token expiry at least every 60s, and closes with 4001 or 4003 when either fails.
- R7. Every socket closes with 1011 on an unexpected server error and logs the traceback.

**Honest UI**

- R8. The editor no longer shows a collaboration badge for a non-existent endpoint. The lint-status socket keeps its own correctly named indicator, which has a stable test id.

**Proof**

- R9. A new `realtime` e2e profile proves on a real stack:
  - **Handshake close codes:** valid, missing, garbage, unrelated-user and unknown-project.
  - **Live updates:** the editor indicator connects; lint frames reach the panel; index frames drive the settings page.
  - **Fan-out and isolation:** two-client fan-out, with isolation from another project.
  - **Restart recovery:** reconnect after an API restart, and recovery of an update missed during the restart.
  - **Revocation:** a removed member's socket closes.
  - **No leaks:** no token appears in the API container logs.

### Acceptance Examples

- AE1. **Covers R3.** An unrelated user opens a private project's settings page. The index socket closes 4003 once, the page shows "No access to live updates", and no further handshakes are attempted.
- AE2. **Covers R4, R5.** A reindex is running when the API restarts mid-way. The settings page leaves "Reindexing..." and shows the final status within 30s of the API being healthy again, without a reload.
- AE3. **Covers R6.** An owner removes the editor from a project while the editor has the settings page open. The editor's socket closes with 4003 within 60s, and no later project event reaches it.

### Scope Boundaries

- Presence, acknowledgments, sync, and anything that builds on `ontokit/collab`.
- Per-project Redis channels, an optimization tracked as TODO #78.
- Removing `?token=` support entirely, which follows once deployed web clients use the header.

### Sources / Research

- **API sockets:** `ontokit/api/utils/ws_auth.py`, plus the handlers in `ontokit/api/routes/projects.py:1774`, `ontokit/api/routes/lint.py:720` and `ontokit/api/routes/quality.py:347`.
- **Web clients:** `lib/api/websocketUrl.ts`, `lib/hooks/useCollaborationStatus.ts`, `lib/api/indexStatus.ts`, `lib/api/lint.ts`, `lib/api/quality.ts`, `components/ui/ConnectionStatus.tsx`, `components/editor/HealthCheckPanel.tsx`, `app/projects/[id]/settings/page.tsx`, and the editor badge at `app/projects/[id]/editor/page.tsx:1458`.
- **Harness:** `scripts/e2e/auth-modes.mjs` (`PROFILE_REGISTRY`), `scripts/e2e/full-stack.mjs`, `scripts/e2e/runtime.mjs`, `scripts/e2e/evidence.mjs` and `e2e/fixtures/run.ts`.

## Planning Contract

### Key Technical Decisions

- KTD1. **Token subprotocol shape.**
  - **Client:** offers `["ontokit.bearer.v1", "ontokit.token.<base64url(token)>"]`.
  - **Server:** accepts by selecting `ontokit.bearer.v1` and never echoes the token protocol. Base64url keeps JWT characters valid as a subprotocol token.
  - Covers R1.
- KTD2. **One shared server forwarder.** `forward_project_events(websocket, channel, project_id, reauthorize)` replaces the three copy-pasted loops. It owns Redis subscription, project filtering, the R6 re-authorization timer, 1011 on error and cleanup. Covers R6 and R7.
- KTD3. **One shared client manager.** `ReconnectingProjectSocket` in `lib/api/projectSocket.ts` replaces the index, lint and quality managers and the hook's private loop. It owns backoff and jitter, terminal close codes, online and visibility triggers, the `onReconnect` callback and subprotocol auth. Covers R3, R4 and R5.
- KTD4. **Log redaction in two places.** A `logging.Filter` on the `uvicorn.access` and `uvicorn.error` loggers rewrites `token=<value>` to `token=REDACTED`. Covers R2.
- KTD5. **The harness restarts the API through a control file.** It is private and run-bound, following the auth-clock pattern. The launcher serves `restart-api` with `compose(ctx, 'restart', 'api')` and waits on `/health`, so ownership stays in `ownedCommand`. Covers R9.

## Implementation Units

### API (ontokit-api, branch `feat/drain-b12-sockets`)

#### U1. Subprotocol auth and log redaction

- **Requirements:** R1, R2.
- **Files:**
  - `ontokit/api/utils/ws_auth.py`
  - `ontokit/core/logging_filters.py` (new)
  - logging setup in `ontokit/main.py`
  - tests in `tests/unit/test_ws_auth.py` and `tests/unit/test_logging_filters.py`
- **Tests:**
  - A subprotocol token authenticates, and the selected protocol is `ontokit.bearer.v1`.
  - `?token=` still works.
  - A malformed subprotocol is treated as no token.
  - The filter redacts a `token` query value in an access-log record, and leaves other params intact.

#### U2. Shared forwarder with re-authorization

- **Requirements:** R6, R7.
- **Files:**
  - `ontokit/api/utils/ws_forward.py` (new)
  - the three route handlers (`projects.py`, `lint.py`, `quality.py`, socket functions only)
  - `tests/unit/test_ws_forward.py`, and updates to `test_index_ws.py`, `test_lint_ws.py` and `test_quality_ws.py`
- **Approach:** a re-authorization callback built from `authenticate_ws` inputs, with a 60s interval configurable for tests.
- **Tests:**
  - Revoked access closes 4003.
  - An expired token closes 4001.
  - An unexpected error closes 1011 and logs the traceback.
  - Project filtering still works.

#### U3. Local compose healthcheck and presence datetime bug

- **Files:** `compose.yaml` (api `start_period: 180s`), `ontokit/collab/presence.py` (aware `datetime.min`), and their tests.

### Web (ontokit-web, branch `feat/drain-b12-sockets`)

#### U4. ReconnectingProjectSocket

- **Requirements:** R1, R3, R4.
- **Files:**
  - `lib/api/projectSocket.ts` (new)
  - `lib/api/indexStatus.ts`, `lib/api/lint.ts` and `lib/api/quality.ts` (migrated onto it)
  - `lib/hooks/useCollaborationStatus.ts`, renamed to `lib/hooks/useLintStatusSocket.ts`
  - `lib/api/websocketUrl.ts`
  - their tests
- **Tests:**
  - Terminal codes stop retrying and expose a reason.
  - Backoff is capped with jitter, with no give-up.
  - The online and visibility triggers fire.
  - Subprotocol values are correct and no token appears in the URL.

#### U5. Recovery and honest UI

- **Requirements:** R5, R8.
- **Files:**
  - `app/projects/[id]/settings/page.tsx` (index section)
  - `components/editor/HealthCheckPanel.tsx`
  - the editor badge region of `app/projects/[id]/editor/page.tsx`
  - `components/ui/ConnectionStatus.tsx` (test id and reason text)
  - `lib/hooks/useProjectViewer.ts` (rename only)
  - their tests
- **Depends on:** U4's API.

#### U6. `realtime` e2e profile

- **Requirements:** R9.
- **Files:**
  - `scripts/e2e/auth-modes.mjs`, `scripts/e2e/evidence.mjs`, `scripts/e2e/full-stack.mjs` (control channel) and `scripts/e2e/bootstrap-identity.mjs` if needed
  - `e2e/fixtures/run.ts` and `e2e/browser/realtime.spec.ts`
  - `package.json` (`test:e2e:realtime`)
  - the matching `*.test.mjs` files
- **Depends on:** U1-U5.
- **Run:** with `--api-source` pointing at the API branch.

## Verification Contract

- **API:** `uv run pytest tests/` (at least the merged baseline), `ruff`, and mypy on Python 3.11.
- **Web:** `npx vitest run`, `npx tsc --noEmit` and `npx eslint .`, plus the e2e unit suites under umask 077.
- **Full stack:** `npm run test:e2e:realtime -- --api-source <api worktree>` passes, and cleanup succeeds.

## Definition of Done

- Every R is covered by a test that fails without its fix.
- The e2e `realtime` profile is green on a real stack, with screenshots captured.
- No `?token=` is sent by any web client.
- Abandoned code is removed.
