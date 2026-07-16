# TODO — known issues to dig & fix

From the 2026-07 code review. The four critical ones (dead-GM-socket eviction,
roster-slot cap, create rate-limit, single timing-safe token verify) are fixed;
these remain, roughly ordered by value.

## Correctness

- [ ] **`share` against a deleted campaign raises unhandled `IntegrityError`.**
  GM deletes the campaign over REST while a player is connected; the player's
  next `share` hits the FK and the exception kills the socket with a stack trace
  instead of an error frame. Catch in `_handle_share` → send `no_campaign` and
  close. Same for `unshare` (delete is a no-op there, but verify).

## Security / abuse

- [ ] **Rate-limit `hello` and `DELETE /campaigns/{code}`.** Both are
  unauthenticated probes; `no_campaign` (404) vs `forbidden` (403) is an
  existence oracle for join codes. Reuse `SlidingWindowLimiter` keyed on IP.
- [ ] **Forwarded headers.** Rate limiting keys on the socket peer IP — behind a
  reverse proxy that's the proxy. Honour `X-Forwarded-For` **only** when a
  trusted-proxy flag is set (uvicorn `--proxy-headers` + `PCS_TRUSTED_PROXY`).
- [ ] **Limiter is single-process.** Fine for one uvicorn worker + SQLite; a
  multi-worker/multi-instance deploy needs a shared store (redis) or a
  proxy-level limiter instead.
- [ ] **Idle-campaign retention.** Hosted instance accumulates dead campaigns
  forever. Auto-purge campaigns with no connection for N days (config), and
  document it in the privacy story.

## Ops

- [ ] **Dockerfile `HEALTHCHECK`** hitting `/healthz`.
- [ ] **README: hosted instance must sit behind a TLS proxy** (iOS ATS blocks
  cleartext `ws://`); compose publishes plain `:8000` which is fine for LAN
  self-host only.

## Code quality (minor)

- [ ] `_handle_share`/`_handle_unshare` duplicate the validate→ownership
  preamble — factor a small helper once a third message type appears.
- [ ] `tests/test_ws.py` repeats the GM+player connection dance — a fixture or
  helper would halve the file.
- [ ] `_receive` does `len(raw.encode())` (full copy) just to measure — cheap
  pre-check `len(raw) > max` first, encode only near the boundary.
- [ ] `Projection.updated_at` has `default`/`onupdate` that no write path uses
  (all writes supply it explicitly) — drop or keep deliberately.
