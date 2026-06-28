# CLAUDE.md

Project-specific notes for working on **hap** (Hermes Agent PWA). Your global
guidelines still apply on top of this.

## Read these first

- **[README.md](README.md)** — what it is, install, run, deploy. The user-facing
  story.
- **[TECHNICAL_OVERVIEW.md](TECHNICAL_OVERVIEW.md)** — how it's built and *why*:
  the data-flow diagram, the SQLite model, the auth/PIN derivation, the
  invariants. Start here for any code change.

## ⚠️ Keep the docs in sync with the code

These two files drift from reality if you don't tend them, and that drift has
caused real confusion in past sessions. **When you change behaviour, update the
README and/or TECHNICAL_OVERVIEW in the *same* change — not "later".** Concretely:

- Changed an endpoint, the data model, auth, config, or an invariant? → update
  **TECHNICAL_OVERVIEW.md** and bump its "Last updated" date.
- Changed install, run, deploy, or anything a user does? → update **README.md**.
- If you're unsure whether a doc covers what you touched, grep it. A stale doc is
  worse than no doc — treat the edit as part of the task, not an optional extra.

## The one mental model that matters

There are **two independent processes** that share only an HTTP contract and a
bearer token:

1. **hap gateway** (`app/`, FastAPI + SQLite) — serves the PWA and waits to be
   polled. Never talks to Hermes.
2. **Hermes plugin** (`hermes_plugin/hap/`) — runs *inside* Hermes, polls the
   gateway, hands messages to the agent, POSTs replies back.

Get clear on which one you're touching before you start. (Full picture in the
technical overview.)

## Commands

```
uv sync                                          # deps
uv run uvicorn app.main:app --reload             # gateway, dev (http://127.0.0.1:8088)
./scripts/install.sh [--help]                    # token + plugin install (see README)
```

Login needs a token — `install.sh` writes `hap_token.txt` and prints the PIN, or
set `HAP_AUTH_TOKEN`.

## House style

- **Simple and stdlib-first.** Persistence is plain `sqlite3`, config is plain
  `os.getenv`, auth is `hashlib`/`hmac` — no ORM, no settings library. YAGNI is
  the default; don't reach for a dependency without a real need.
- **No build step on the front end.** `app/static/` is vanilla JS/HTML/CSS by
  choice. Keep it that way unless there's a strong reason not to.

## Gotchas that will bite (verify, don't assume)

- **The running plugin is a *copy*.** `install.sh` copies `hermes_plugin/hap/`
  into `~/.hermes/plugins/hap/` (or a profile). Editing the repo copy does
  nothing until you re-run the installer and restart Hermes.
- **`serve.py` is a throwaway**, not the production gateway. Production is
  `app.main:app`.
- **Single uvicorn worker only.** The SSE broadcaster is in-process; multiple
  workers would silently drop live events.
- **Bump `CACHE` in `app/static/sw.js`** whenever a shell asset changes, or
  clients keep serving the stale cached version.
- **Never assign `innerHTML`** in the front end. All text (incl. the Markdown
  renderer) is built with `textContent`/`createElement` — that's the XSS
  guarantee. Don't break it.
- **`HAP_COOKIE_SECURE`** must be `true` only behind real HTTPS; `true` over
  `http://localhost` makes login silently fail.
- **Rotating the bearer token** also changes the PIN and logs everyone out (both
  are derived from it).

## Testing

A pytest suite covers the gateway in process — `uv run pytest` (deps are in the
`dev` group; no new runtime deps). It does **not** cover the adapter or the
browser, so still exercise the real gateway→Hermes→browser loop after anything
touching those. See the Testing section of TECHNICAL_OVERVIEW.md for what it
covers and the two gotchas (import-time config in `conftest.py`; SSE asserted via
a monkeypatched broadcaster, not the live stream).

## Where the "why" lives

- Design decisions / rationale: **`ant`** (e.g. `ant show hap-VYQvH` for the
  no-broker ADR; `hap-sYVTv` for the verified Hermes adapter contract).
- Work / issues: **`ait`** (epic `hap-UkLWZ`).

Prefer these over guessing — and if you make a load-bearing decision, record it
the same way for the next Claude.
