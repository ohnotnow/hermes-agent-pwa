#!/usr/bin/env bash
#
# hap installer — set up the hap gateway and the Hermes plugin on a machine
# that runs Hermes.
#
#   ./scripts/install.sh [--public-url URL] [--agent NAME] [--profile NAME] [--host HOST]
#
# --public-url  URL your phone reaches the hap gateway on (a Caddy domain, or a
#               tailscale-serve https://...ts.net address). Default: prints the
#               local bind. This only changes the printed text; it does NOT
#               configure anything or run tailscale serve.
# --agent       this agent's id on the gateway. Defaults to the profile name
#               when --profile is given, otherwise "hermes".
# --profile     install into a non-default Hermes profile
#               (~/.hermes/profiles/NAME/) and enable it there. Omit for the
#               default profile (~/.hermes/). One gateway can serve many agents;
#               run this once per profile, each with its own --agent id.
# --host        hap gateway bind address (default 127.0.0.1; use 0.0.0.0 for
#               trusted-LAN access — see the README for the HTTPS/PWA caveat).
#
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

PUBLIC_URL=""
AGENT_ID=""
PROFILE=""
HOST="127.0.0.1"
PORT="8088"

while [ $# -gt 0 ]; do
  case "$1" in
    --public-url) PUBLIC_URL="${2:?missing value}"; shift 2 ;;
    --agent)      AGENT_ID="${2:?missing value}"; shift 2 ;;
    --profile)    PROFILE="${2:?missing value}"; shift 2 ;;
    --host)       HOST="${2:?missing value}"; shift 2 ;;
    -h|--help)    grep '^#' "$0" | grep -v '^#!' | sed 's/^#\{1,\} \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 1 ;;
  esac
done

# Default the agent id to the profile name, or "hermes" for the default profile.
AGENT_ID="${AGENT_ID:-${PROFILE:-hermes}}"

command -v uv >/dev/null     || { echo "ERROR: 'uv' is required — https://docs.astral.sh/uv/" >&2; exit 1; }
command -v hermes >/dev/null || echo "WARNING: 'hermes' not on PATH — set up Hermes, then re-run (or enable the plugin by hand)." >&2

echo "==> Syncing gateway dependencies with uv…"
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

# The default profile lives in ~/.hermes; a named profile is a parallel home
# under ~/.hermes/profiles/<name>/ with its own plugins dir and -p selector.
if [ -n "$PROFILE" ]; then
  PLUGDIR="$HOME/.hermes/profiles/$PROFILE/plugins/hap"
  ENABLE=(hermes -p "$PROFILE" plugins enable hap)
  WHERE="profile '$PROFILE'"
else
  PLUGDIR="$HOME/.hermes/plugins/hap"
  ENABLE=(hermes plugins enable hap)
  WHERE="the default profile"
fi

echo "==> Placing the Hermes plugin into $WHERE…"
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
  "${ENABLE[@]}" >/dev/null 2>&1 || true
  echo "    plugin enabled in $WHERE (agent: $AGENT_ID)"
else
  echo "    plugin files written to $PLUGDIR (enable later: ${ENABLE[*]})"
fi

OPEN_URL="${PUBLIC_URL:-http://${HOST}:${PORT}}"
cat <<EOF

────────────────────────────────────────────────────────────
  hap is set up: agent '$AGENT_ID' in $WHERE.

  Open it:      ${OPEN_URL}
  Login token:  ${TOKEN}
  Or short PIN: ${PIN}

  Start the two pieces:
    1) hap gateway:  uv run uvicorn app.main:app --host ${HOST} --port ${PORT}
    2) Hermes:       hermes gateway run   (foreground; for a durable service
                     see the README, "Running Hermes as a service")

  If Hermes is ALREADY running, you must restart it to load the plugin —
  enabling alone "takes effect on next session".

  Reaching it from your phone (see README, "Reaching it from your phone"):
    • Tailscale — recommended; HTTPS so the PWA installs (Linux needs sudo)
    • Caddy     — public domain (caddy/Caddyfile.example)
    • LAN only  — re-run with --host 0.0.0.0 (plain http, no PWA install)
────────────────────────────────────────────────────────────
EOF
