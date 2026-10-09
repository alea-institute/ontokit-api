# B12 API socket proof completion

Completes API units U1–U3 of `2026-10-09-1330-fix-b12-live-update-sockets-plan.md`.
The lane's existing dirty changes are explicitly assigned for completion.

1. Verify and finish bearer subprotocol authentication, legacy query redaction,
   shared forwarding, periodic access checks, error closes, and bounded cleanup.
   Preserve the identity admitted at handshake through token expiry.
2. Exercise all three production routes through a real Uvicorn server, PostgreSQL
   project access, Redis pub/sub, and generated test JWTs. Prove handshake codes,
   two-client fan-out/project isolation, revocation, token expiry, server restart,
   and REST recovery of updates published during the outage. Services run offline
   over Unix sockets; substitute only the identity provider's JWKS response.
3. Run memory-capped, timeout-bounded focused tests, Ruff, mypy on Python 3.11,
   and advisory Pyright. Review the diff and commit complete units independently.
4. Incorporate the local dev branch's memory-cap prerequisite by a normal merge,
   preserving its protections, and leave `.codex-out/lane-result.md` for publication.

Presence/sync are **NOT built**. The inherited timezone fix in unused presence
code is maintenance only. Web reconnect policy/UI behavior and full browser-stack
acceptance belong to the separate web lane; no claim of browser acceptance here.
No network publication, GitHub calls, or credential access is authorized for this lane.
