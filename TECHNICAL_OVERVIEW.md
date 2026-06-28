# Technical Overview

Last updated: 2026-06-28

> Orientation for programmers and agents working on hap. For *what it is* and
> how to install/run it, see the [README](README.md). This file is the
> "how it's built and why" companion — the data flow, the invariants, and the
> non-obvious bits.

## What This Is

hap (Hermes Agent PWA) is a small, self-hosted gateway that lets one trusted
person message their own local [Hermes](https://hermes-agent.nousresearch.com/)
agents from a phone/browser, like a lightweight chat app. No third-party
platform, no message broker — SQLite is the durable source of truth.

## Stack

- Python ≥ 3.11, managed with [uv](https://docs.astral.sh/uv/).
- **Gateway**: FastAPI ≥ 0.138, uvicorn (single worker). Persistence is stdlib
  `sqlite3` — no ORM, no extra DB dependency.
- **Hermes plugin**: stdlib + `httpx` (an async client). Runs *inside* Hermes,
  against Hermes's own `gateway.platforms.base.BasePlatformAdapter` API.
- **Web app**: vanilla JS / HTML / CSS, no build step, no framework. Installable
  PWA (manifest + service worker).

## The Two Pieces (read this first)

The whole system is two long-running processes that share **nothing** except an
HTTP contract and a bearer token:

```
  Phone/browser PWA                hap gateway (this repo, app/)         Hermes + plugin (hermes_plugin/hap/)
  ─────────────────                ─────────────────────────────         ────────────────────────────────────
  GET  /                  ──────▶  FastAPI serves static shell
  POST /api/login         ──────▶  sets signed session cookie
  POST /api/conversations ──────▶  store.add_message(sender='user')  ┐
  GET  /api/events  ◀── SSE ─────  Broadcaster (in-process fan-out)  │
                                                                     │  POST /api/agent/poll  ◀── every ~3s ── HapAdapter._poll_loop
                                   store.poll_undelivered ───────────┘  returns undelivered user msgs ──▶ self.handle_message() → agent
                                                                        POST /api/agent/reply  ◀────────── adapter.send()
                                   store.add_message(sender='agent')
                                   Broadcaster.publish ──── SSE ──▶ browser
```

- **hap gateway** (`app/`): the FastAPI app + SQLite store. It serves the PWA
  and exposes both the browser API and the agent API. It never talks to Hermes;
  it just waits to be polled.
- **Hermes plugin** (`hermes_plugin/hap/`): a Hermes *platform adapter*. It
  polls the gateway for user messages, hands them to the agent, and POSTs
  replies back. To the agent, hap looks like any other chat platform. This code
  is **copied into** `~/.hermes/plugins/hap/` (or a profile) by the installer —
  the running copy is not this repo's copy.

Delivery is **async by default**: a reply can land seconds or hours later, even
after the browser has closed. The browser holds no state; SQLite does.

## Directory Structure

```
app/                     the hap gateway (FastAPI)
  main.py                all HTTP routes + request models (the entry point)
  store.py               data-access helpers (pure functions over a sqlite3 conn)
  db.py                  schema, connection, tiny in-place migration
  auth.py                token / PIN / signed-cookie auth + login rate-limiter
  config.py              env-driven Settings (no settings library)
  events.py              Broadcaster — in-process SSE fan-out
  static/                the PWA: index.html, app.js, styles.css, sw.js, manifest, icons
hermes_plugin/hap/       the Hermes platform adapter (copied into ~/.hermes by installer)
  adapter.py             HapAdapter: poll loop (inbound) + send/typing (outbound)
  plugin.yaml            Hermes plugin manifest + optional_env prompts
  hap.json.example       config shape (gateway_url, token, agent_id, poll_seconds)
tests/                   pytest suite over the gateway (conftest + auth/store/events/endpoints)
serve.py                 ⚠ throwaway stdlib bootstrap — NOT the production gateway (see below)
scripts/install.sh       the installer (token gen, plugin copy, multi-profile logic)
systemd/                 hap-gateway.service unit (Linux targets)
caddy/                   Caddyfile example for public-domain HTTPS
.ait/ .ant/              local issue tracker + design-decision notes (see end)
```

### `serve.py` — don't be fooled by it

`serve.py` is a zero-dependency `http.server` that mirrors a *subset* of the
gateway's endpoints, written before fastapi/uvicorn were approved so the
send/reply loop could be proven against a live Hermes. It reuses `app.db` +
`app.store`. **The production gateway is `app.main:app`.** `serve.py` has no
browser/session auth, no SSE, no static serving — treat it as historical.

## Data Model (SQLite — `app/db.py`)

```
agents          conversations              messages
──────          ─────────────              ────────
id (PK)    ◀──  agent_id (FK)              message_id (PK)   ← dedupe key
display_name    id (PK)            ◀────── conversation_id (FK)
created_at      title (unused in v1)       agent_id
last_seen_at    created_at                 sender  CHECK IN ('user','agent')
                deleted_at (nullable)       body
                                            created_at
                                            delivered_to_agent_at (NULL until polled)
```

Key points:
- **`messages.message_id` is the primary key and the dedupe key.** Inserts are
  idempotent (`add_message` swallows `IntegrityError`), so a replayed poll/reply
  never double-stores.
- **`delivered_to_agent_at`** is the durable inbox cursor. NULL = the agent
  hasn't polled it yet. `poll_undelivered` selects NULLs for the agent and marks
  them in the same call → **at-most-once** delivery (v1). A future hardening
  (deferred-ack for at-least-once) is noted in `store.py` / QUESTIONS #3.
- **`conversations.deleted_at`** is a *soft* delete (hide). A late agent reply
  calls `set_conversation_deleted(..., False)` to **un-hide** the thread.
- `db.init_db` runs the schema `IF NOT EXISTS` and does one in-place migration
  (adds `deleted_at` to pre-existing DBs). WAL mode; `foreign_keys=ON`.

## Auth Model (`app/auth.py`)

One shared bearer token is the master credential — the design assumes a single
trusted human, so agents are *not* told apart by token, only by `agent_id`.

| Caller            | Credential                                  | Dependency                  |
|-------------------|---------------------------------------------|-----------------------------|
| Agent (adapter)   | raw `Bearer <token>`                        | `require_bearer`            |
| Browser           | signed `hap_session` cookie (or bearer)     | `require_session_or_bearer` |
| SSE stream        | session cookie only                         | `require_session`           |
| State-changing    | + same-origin check                         | `require_same_origin`       |

The derivation chain (all from the one token, so no separate secrets and stable
across restarts):

```
bearer token ──sha256 % 1e6──▶ 6-digit PIN        (login convenience; a leaked PIN must NOT leak the token, hence hash not slice)
bearer token ──sha256(prefix)─▶ HMAC session key  (signs the cookie payload {iat})
```

### How the PIN is derived from the token (`pin_for_token`)

The PIN is a one-way fingerprint of the bearer token, squeezed down to six
digits. Step by step, given the token string:

1. **SHA-256 the token** → 32 raw bytes. This is a one-way hash: you can't run
   it backwards to recover the token.
2. **Read those 32 bytes as one big integer** (`int.from_bytes(..., "big")`,
   big-endian) — a ~77-digit number.
3. **Take it modulo 1,000,000** → an integer in `0 … 999999`.
4. **Zero-pad to six digits** (`f"{n:06d}"`) → the PIN string, e.g. `002493`.

```python
n = int.from_bytes(hashlib.sha256(token.encode()).digest(), "big") % 1_000_000
return f"{n:06d}"
```

Two things this buys us:
- **A leaked PIN can't be turned back into the token.** It's a hash, not a slice
  of the token — and the modulo throws away almost all the information, so the
  PIN→token direction is closed. (The flip side: six digits is only a million
  possibilities, hence the login rate-limit/lockout below.)
- **It's deterministic and needs no storage.** The same token always yields the
  same PIN, so the gateway recomputes it at startup (`PIN = pin_for_token(...)`)
  and the installer computes the *identical* value in `install.sh` to print at
  setup time — nothing about the PIN is persisted. Login accepts either the PIN
  or the full token; both are compared with `hmac.compare_digest` (constant-time,
  so a wrong guess can't be timed character-by-character).

- **Rotating the token changes the PIN *and* invalidates every existing
  session cookie** (both are derived from it; the HMAC key moves too). Worth
  knowing before you regenerate.
- The browser **never stores the token** — it logs in with token-or-PIN and
  gets an HttpOnly cookie (14-day max-age).
- 6 digits is brute-forceable, so login is rate-limited: 5 fails / 300s window →
  300s lockout (`LoginLimiter`, in-memory, per-process).
- `require_same_origin` only fires when an `Origin` header is present (browsers
  send it; curl doesn't, and the `SameSite=Lax` cookie covers curl).

## HTTP API (`app/main.py`)

Browser side:

| Method | Path                                        | Auth                  | Purpose                          |
|--------|---------------------------------------------|-----------------------|----------------------------------|
| POST   | `/api/login` `/api/logout`                  | same-origin / —       | set/clear session cookie         |
| GET    | `/api/me`                                    | —                     | `{authenticated}`                |
| GET    | `/api/agents`                                | session/bearer        | agents + computed `online`       |
| GET    | `/api/conversations`                         | session/bearer        | non-deleted, newest-activity     |
| POST   | `/api/conversations`                         | session/bearer + SO   | start a thread (first user msg)  |
| GET    | `/api/conversations/{id}`                    | session/bearer        | conversation + full thread       |
| POST   | `/api/conversations/{id}/messages`           | session/bearer + SO   | user reply                       |
| DELETE | `/api/conversations/{id}`                    | session/bearer + SO   | soft-delete (hide)               |
| GET    | `/api/events`                                | session               | SSE stream                       |

Agent side (bearer only):

| Method | Path                | Purpose                                                          |
|--------|---------------------|------------------------------------------------------------------|
| POST   | `/api/agent/poll`   | auto-registers agent, returns + marks undelivered user msgs      |
| POST   | `/api/agent/reply`  | stores agent reply, un-hides thread, broadcasts                  |
| POST   | `/api/agent/typing` | ephemeral "working" ping — broadcast only, never stored          |

Plus `GET /healthz`, and `/`, `/sw.js`, `/manifest.json`, `/offline.html`.

- **Agents auto-register on first contact** (`store.ensure_agent` in poll). There
  is no separate registration step.
- `agent_id` must match `^[a-z0-9_-]{1,32}$` (`valid_agent`) — enforced in routes
  *and* in the installer's name validation.
- An agent is **`online` if it polled within `ONLINE_WINDOW_SECONDS` (15s)** —
  derived from `last_seen_at`, not stored. Poll cadence is ~3s.
- `/api/agent/reply` 409s if the conversation's `agent_id` doesn't match the
  caller — agents can't reply into each other's threads.

## SSE Events (`app/events.py` → browser `app.js`)

`Broadcaster` is an **in-process** fan-out to each connected client's asyncio
queue. Event types pushed: `message`, `delivered` (ticks user bubbles),
`typing` (animated dots, auto-hidden), `deleted` (sync other open tabs). The
stream sends a `: keepalive` comment every 15s.

> ⚠️ **Single-worker only.** Because the broadcaster lives in process memory, a
> multi-worker uvicorn deployment would silently drop events to clients on other
> workers. v1 runs one worker; a multi-worker setup would need a shared bus.

## Web App Notes (`app/static/`)

- No framework, no build. `app.js` is the whole client: login → conversation
  list → conversation view, driven by `/api/me` on load and the SSE stream.
- **XSS posture: nothing is ever assigned to `innerHTML`.** All text is rendered
  via `textContent` / `createElement`. The Markdown renderer for agent messages
  (`renderMarkdown`) builds DOM nodes for a safe subset (code, lists, headings,
  quotes, bold/italic, links with scheme-checked hrefs, GFM tables). Underscore
  emphasis is *deliberately unsupported* so `snake_case` isn't mangled.
- **Service worker** caches the app shell for offline open; `/api/*`, SSE and
  non-GET always go to the network. **Bump `CACHE` (currently `hap-v7`) in
  `sw.js` whenever a shell asset changes**, or clients keep the stale version.

## Config (env, `app/config.py`)

All optional. `HAP_AUTH_TOKEN` (else read from `hap_token.txt` /
`HAP_TOKEN_FILE`), `HAP_HOST`/`HAP_PORT` (`127.0.0.1:8088`), `HAP_DB_PATH`
(`hap.db`), `HAP_COOKIE_SECURE` (`false`).

> ⚠️ `HAP_COOKIE_SECURE` trap, both directions: `true` over `http://localhost`
> makes login *silently* fail (browser won't send a Secure cookie over http);
> `false` over HTTPS works but the cookie isn't Secure-flagged. Set it `true`
> only when actually behind Caddy/Tailscale HTTPS.

The plugin reads `hap.json` beside `adapter.py` (env vars override): `gateway_url`,
`token`, `agent_id`, `poll_seconds`.

## Testing

A pytest suite (`tests/`) covers the gateway in process — run it with `uv run
pytest`. It uses FastAPI's `TestClient`; the only test-time deps are `pytest`
and `httpx` (the `dev` dependency-group — no new *runtime* deps). Layers:

- `test_auth.py` — PIN derivation, signed-cookie sign/verify/expiry, the secret
  checks, the dependency guards, the same-origin check, the login limiter.
- `test_store.py` — the SQLite helpers driven against an in-memory connection:
  upsert, CRUD, the deliver-once poll cursor, `message_id` dedupe, soft-delete /
  un-hide, deleted-filtering.
- `test_events.py` — the `Broadcaster` fan-out directly (no HTTP).
- `test_endpoints.py` — the HTTP surface: login/me/logout + lockout + cross-origin,
  auth-required 401s, the agent online flag, start/reply/detail, agent poll
  (deliver-once) and reply (incl. the 409 mismatch), delete + un-hide.

Two things shape the suite and are worth knowing before you touch it:

- **Import-time config.** `auth.py` derives the PIN and the cookie-signing key,
  and `main.py` captures `db_path`, *at import*. So `tests/conftest.py` pins
  `HAP_AUTH_TOKEN`/`HAP_DB_PATH` **before** importing any `app.*` module.
- **No live SSE stream in the tests.** `broadcaster.publish` is monkeypatched to
  capture events into `client.published`, so endpoint tests assert *which* event
  fired without consuming the infinite `text/event-stream`. The stream mechanism
  itself is covered directly in `test_events.py`.

It's a unit/integration net around the gateway, not the full loop — still
exercise the real Hermes→gateway→browser path (recorded in `ant` note
`hap-Ed6UZ`) for anything touching the adapter or the browser.

## Local Development

```
uv sync
uv run uvicorn app.main:app --reload     # gateway at http://127.0.0.1:8088
```

Login needs a token: `./scripts/install.sh` generates `hap_token.txt` (and prints
the PIN), or set `HAP_AUTH_TOKEN`. The full two-process setup, profiles, and
service install are covered in the README.

## Design Notes (the "why")

Rationale lives in `ant` (run `ant show <id>`), not just code comments:

- `hap-VYQvH` (ADR) — custom Hermes platform adapter, **no NATS / no broker**.
- `hap-sYVTv` — the *verified* Hermes adapter contract (what `BasePlatformAdapter`
  actually requires: `supports_async_delivery`, the `HAP_HOME_CHANNEL` shim,
  `send`/`send_typing` signatures). Check this before changing `adapter.py`.
- `hap-AkRXV` (foundation) and `hap-XKtxA` — scope: v1 ships plain-text
  confirmations; rich action buttons deferred.

Issues are tracked in `ait` (epic `hap-UkLWZ`).
