#!/usr/bin/env bash
#
# hap installer (all-in-one box) — set up the gateway + the Hermes plugin on
# the machine that runs Hermes.
#
#   ./scripts/install.sh [--public-url URL] [--agent NAME] [--host HOST]
#
# --public-url  URL your phone reaches the gateway on (a Caddy domain, or a
#               tailscale-serve https://...ts.net address). Default: prints the
#               local bind. The gateway<->Hermes link always uses localhost, so
#               this is purely the human-facing URL.
# --agent       this box's agent id on the gateway (default: hermes)
# --host        gateway bind address (default: 127.0.0.1; use 0.0.0.0 for
#               trusted-LAN access — see README for the HTTPS/PWA caveat)
#
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

PUBLIC_URL=""
AGENT_ID="hermes"
HOST="127.0.0.1"
PORT="8088"

while [ $# -gt 0 ]; do
  case "$1" in
    --public-url) PUBLIC_URL="${2:?missing value}"; shift 2 ;;
    --agent)      AGENT_ID="${2:?missing value}"; shift 2 ;;
    --host)       HOST="${2:?missing value}"; shift 2 ;;
    -h|--help)    grep '^#' "$0" | grep -v '^#!' | sed 's/^#\{1,\} \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 1 ;;
  esac
done

command -v uv >/dev/null     || { echo "ERROR: 'uv' is required — https://docs.astral.sh/uv/" >&2; exit 1; }
command -v hermes >/dev/null || echo "WARNING: 'hermes' not on PATH — install Hermes, then re-run (or enable the plugin manually)." >&2

echo "==> Installing gateway dependencies (uv sync)…"
uv sync

echo "==> Bearer token…"
if [ ! -f hap_token.txt ]; then
  python3 -c "import secrets, pathlib; pathlib.Path('hap_token.txt').write_text(secrets.token_urlsafe(24))"
  echo "    generated a new token (hap_token.txt)"
else
  echo "    reusing existing hap_token.txt"
fi
TOKEN="$(tr -d '[:space:]' < hap_token.txt)"
PIN="$(python3 - "$TOKEN" <<'PY'
import hashlib, sys
print(f"{int.from_bytes(hashlib.sha256(sys.argv[1].encode()).digest(), 'big') % 1000000:06d}")
PY
)"

echo "==> Installing the Hermes plugin…"
PLUGDIR="$HOME/.hermes/plugins/hap"
mkdir -p "$PLUGDIR"
cp hermes_plugin/hap/adapter.py hermes_plugin/hap/plugin.yaml hermes_plugin/hap/__init__.py "$PLUGDIR/"
python3 - "$PLUGDIR/hap.json" "$TOKEN" "$AGENT_ID" "$PORT" <<'PY'
import json, pathlib, sys
path, token, agent, port = sys.argv[1:5]
pathlib.Path(path).write_text(json.dumps({
    "gateway_url": f"http://127.0.0.1:{port}",
    "token": token,
    "agent_id": agent,
    "poll_seconds": 3,
}, indent=2) + "\n")
PY
if command -v hermes >/dev/null; then
  hermes plugins enable hap >/dev/null 2>&1 || true
  echo "    plugin installed at $PLUGDIR and enabled (agent: $AGENT_ID)"
else
  echo "    plugin files written to $PLUGDIR (enable later: hermes plugins enable hap)"
fi

OPEN_URL="${PUBLIC_URL:-http://${HOST}:${PORT}}"
cat <<EOF

────────────────────────────────────────────────────────────
  hap is set up on this box.

  Open it:      ${OPEN_URL}
  Login token:  ${TOKEN}
  Or short PIN: ${PIN}

  Start the two pieces (in separate terminals, or via systemd/):
    1) gateway:  uv run uvicorn app.main:app --host ${HOST} --port ${PORT}
    2) Hermes:   hermes gateway run

  Reaching it from your phone (see README → "Remote access"):
    • Tailscale  — recommended; gives HTTPS so the PWA installs
    • Caddy      — for a public domain (caddy/Caddyfile.example)
    • LAN only   — re-run with --host 0.0.0.0 (plain http, no PWA install)
────────────────────────────────────────────────────────────
EOF
