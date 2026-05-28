# ADR-0005: Target auth-ON Frigate; client is auth-optional

- **Status:** Accepted
- **Date:** 2026-05-26

## Context
Frigate 0.14+ enables authentication by default, so most instances (and any
future/other users) run with auth on. We could simplify by coding against an
auth-disabled instance, but that would ship the auth code path **untested** —
and auth (token capture, cookie-vs-Bearer, 401 retry) is exactly where subtle
bugs hide — and would force every user to weaken their security posture.

Probe of the reference instance (internal port 5000 → host 5001): API served at
**root** (`/version` 200, `/api/version` 404), auth **enforced** (`/stats` 401,
`/faces` 403 role-gated), `/login` at root.

## Decision
Develop and test against an **auth-protected** instance (the strict superset),
with the client **auth-optional**: if `FRIGATE_USER`/`FRIGATE_PASSWORD` are set
it logs in and carries the token (sent as both a cookie and a Bearer header,
re-login on 401); if absent it calls directly (works against an auth-disabled
instance or unauthenticated port). The API prefix is auto-detected via `probe`.

## Consequences
- One client supports both deployments; auth-off is the trivial sub-case.
- The bug-prone auth path is exercised on a real instance, not shipped blind.
- Credentials live in a gitignored `.env`; never written to tracked files/logs.
- Don't disable Frigate auth just to make tooling easier — keep it on.
