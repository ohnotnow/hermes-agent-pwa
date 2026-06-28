#!/usr/bin/env bash
#
# hap installer — set up the hap gateway and the Hermes plugin on a machine
# that runs Hermes.
#
#   ./scripts/install.sh [--public-url URL] [--agent NAME]
#                        [--profile NAME | --profiles NAME,NAME,…] [--host HOST]
#
# --public-url  URL your phone reaches the hap gateway on (a Caddy domain, or a
#               tailscale-serve https://...ts.net address). Default: prints the
#               local bind. This only changes the printed text; it does NOT
#               configure anything or run tailscale serve.
# --agent       this agent's id on the gateway (single-agent installs only).
#               Defaults to the profile name when --profile is given, otherwise
#               "hermes".
# --profile     install into a non-default Hermes profile
#               (~/.hermes/profiles/NAME/) and enable it there. Omit for the
#               default profile (~/.hermes/).
# --profiles    install several agents in one run: a comma-separated list where
#               "default" is the default home (~/.hermes) and any other name is a
#               profile, e.g. --profiles default,twinky. Prints the gateway
#               restart command for each agent at the end.
# --host        hap gateway bind address (default 127.0.0.1; use 0.0.0.0 for
#               trusted-LAN access — see the README for the HTTPS/PWA caveat).
#
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

PUBLIC_URL=""
AGENT_ID=""
PROFILE=""
PROFILES_CSV=""
HOST="127.0.0.1"
PORT="8088"

while [ $# -gt 0 ]; do
  case "$1" in
    --public-url) PUBLIC_URL="${2:?missing value}"; shift 2 ;;
    --agent)      AGENT_ID="${2:?missing value}"; shift 2 ;;
    --profile)    PROFILE="${2:?missing value}"; shift 2 ;;
    --profiles)   PROFILES_CSV="${2:?missing value}"; shift 2 ;;
    --host)       HOST="${2:?missing value}"; shift 2 ;;
    -h|--help)    grep '^#' "$0" | grep -v '^#!' | sed 's/^#\{1,\} \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 1 ;;
  esac
done

# Work out which agents to install. --profiles takes a comma-separated list;
# --profile (singular) and the bare invocation remain the single-agent path.
# "default" (or an empty entry) means the default Hermes home (~/.hermes); any
# other name is a profile under ~/.hermes/profiles/<name>/.
PROFILES=()
if [ -n "$PROFILES_CSV" ]; then
  IFS=',' read -ra PROFILES <<< "$PROFILES_CSV" || true
elif [ -n "$PROFILE" ]; then
  PROFILES=("$PROFILE")
else
  PROFILES=("default")
fi
# --agent only applies to a single-agent install; ignored for a --profiles list.
SINGLE=false
if [ "${#PROFILES[@]}" -eq 1 ]; then SINGLE=true; fi

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

# Install the plugin for one agent. $1 is a profile name, or "default" for the
# default Hermes home (~/.hermes). Records the agent id and the matching gateway
# restart command for the printed apply step.
install_one() {
  local prof="$1" plugdir agent where
  local -a enable
  if [ -z "$prof" ] || [ "$prof" = "default" ]; then
    plugdir="$HOME/.hermes/plugins/hap"
    enable=(hermes plugins enable hap)
    agent="hermes"
    where="the default profile"
    RESTARTS+=("hermes gateway restart")
    DID_DEFAULT=true
  else
    plugdir="$HOME/.hermes/profiles/$prof/plugins/hap"
    enable=(hermes -p "$prof" plugins enable hap)
    agent="$prof"
    where="profile '$prof'"
    RESTARTS+=("hermes -p $prof gateway restart")
  fi
  if [ "$SINGLE" = true ] && [ -n "$AGENT_ID" ]; then agent="$AGENT_ID"; fi

  echo "==> Placing the Hermes plugin into $where (agent: $agent)…"
  mkdir -p "$plugdir"
  cp hermes_plugin/hap/adapter.py hermes_plugin/hap/plugin.yaml hermes_plugin/hap/__init__.py "$plugdir/"
  python3 - "$plugdir/hap.json" "$TOKEN" "$agent" "$PORT" <<'PY'
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
    "${enable[@]}" >/dev/null 2>&1 || true
    echo "    plugin enabled in $where"
  else
    echo "    plugin files written to $plugdir (enable later: ${enable[*]})"
  fi
  AGENTS+=("$agent")
}

RESTARTS=()
AGENTS=()
DID_DEFAULT=false
for p in "${PROFILES[@]}"; do
  pc="$(echo "$p" | tr -d '[:space:]')"
  if [ -z "$pc" ]; then continue; fi
  install_one "$pc"
done

OPEN_URL="${PUBLIC_URL:-http://${HOST}:${PORT}}"
DEFAULT_NOTE=""
if [ "$DID_DEFAULT" = false ]; then
  DEFAULT_NOTE="
  Note: your default agent (~/.hermes) wasn't part of this run, so it still has
  its previous plugin — only the agents you named were touched, and \"default\"
  isn't added automatically. If that's deliberate (e.g. you only expose a
  restricted profile to hap), all good; otherwise add \"default\" and re-run,
  e.g. --profiles default,twinky."
fi
cat <<EOF

────────────────────────────────────────────────────────────
  hap is set up. Agents: ${AGENTS[*]}${DEFAULT_NOTE}

  Open it:      ${OPEN_URL}
  Login token:  ${TOKEN}
  Or short PIN: ${PIN}

  First time? Start the two pieces (for durable services see the README,
  "Running as a service"):
    1) hap gateway:  uv run uvicorn app.main:app --host ${HOST} --port ${PORT}
    2) Hermes:       hermes gateway run

  To apply this install/update — and to load the plugin if Hermes is already
  running (enabling alone only takes effect next session) — restart each piece:
    sudo systemctl restart hap-gateway
EOF
for r in "${RESTARTS[@]}"; do
  echo "    $r"
done
cat <<EOF

  (The 'hermes … restart' lines assume Hermes runs as an installed service —
  'hermes gateway install'. If you run it in the foreground, restart it the way
  you started it.)

  Reaching it from your phone (see README, "Reaching it from your phone"):
    • Tailscale — recommended; HTTPS so the PWA installs (Linux needs sudo)
    • Caddy     — public domain (caddy/Caddyfile.example)
    • LAN only  — re-run with --host 0.0.0.0 (plain http, no PWA install)
────────────────────────────────────────────────────────────
EOF
