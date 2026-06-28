#!/usr/bin/env bash
#
# hap installer — set up the hap gateway and the Hermes plugin on a machine
# that runs Hermes.
#
#   ./scripts/install.sh [--public-url URL] [--agent NAME]
#                        [--profile NAME | --profiles NAME,… | --all-profiles]
#                        [--host HOST]
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
#               "default" is the default home (~/.hermes) and any other name is an
#               EXISTING Hermes profile, e.g. --profiles default,twinky. Names are
#               validated first — malformed or unknown profiles bail before
#               anything is installed. Prints the gateway restart command per agent.
# --all-profiles  do every agent in one go (no list to typo). Fresh install: every
#               Hermes profile. Update: every agent that already has hap (Hermes
#               profiles without hap are reported, not enrolled). Can't be combined
#               with --profile/--profiles.
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
ALL_PROFILES=false
HOST="127.0.0.1"
PORT="8088"

while [ $# -gt 0 ]; do
  case "$1" in
    --public-url)   PUBLIC_URL="${2:?missing value}"; shift 2 ;;
    --agent)        AGENT_ID="${2:?missing value}"; shift 2 ;;
    --profile)      PROFILE="${2:?missing value}"; shift 2 ;;
    --profiles)     PROFILES_CSV="${2:?missing value}"; shift 2 ;;
    --all-profiles) ALL_PROFILES=true; shift ;;
    --host)         HOST="${2:?missing value}"; shift 2 ;;
    -h|--help)      grep '^#' "$0" | grep -v '^#!' | sed 's/^#\{1,\} \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 1 ;;
  esac
done

# Both tools are required — there's no sensible install without them.
command -v uv >/dev/null     || { echo "ERROR: 'uv' is required — https://docs.astral.sh/uv/" >&2; exit 1; }
command -v hermes >/dev/null || { echo "ERROR: 'hermes' is required but not on PATH — set up Hermes first, then re-run." >&2; exit 1; }

# Fresh (first-ever) vs update is decided by whether a token already exists here.
FRESH=false
if [ ! -f hap_token.txt ]; then FRESH=true; fi

if [ "$ALL_PROFILES" = true ] && { [ -n "$PROFILES_CSV" ] || [ -n "$PROFILE" ]; }; then
  echo "ERROR: --all-profiles can't be combined with --profile or --profiles." >&2
  exit 1
fi

# Work out which agents to install. "default" means the default Hermes home
# (~/.hermes); any other name is a profile under ~/.hermes/profiles/<name>/.
#   --profiles a,b   explicit list     --profile a   single     (bare) default
#   --all-profiles   fresh install: every Hermes profile; update: every agent
#                    that already has hap (un-enrolled profiles are surfaced, not
#                    enrolled — so adding a profile never silently exposes it).
PROFILES=()
if [ "$ALL_PROFILES" = true ]; then
  echo "==> --all-profiles: finding your agents…"
  HAP_KEYS=()   # agents that already have hap (pure filesystem)
  if [ -f "$HOME/.hermes/plugins/hap/hap.json" ]; then HAP_KEYS+=("default"); fi
  for d in "$HOME"/.hermes/profiles/*/plugins/hap/hap.json; do
    [ -e "$d" ] || continue
    d2="${d%/plugins/hap/hap.json}"; HAP_KEYS+=("${d2##*/}")
  done
  HERM_KEYS=("default")   # every real Hermes profile (confirmed via hermes)
  for d in "$HOME"/.hermes/profiles/*/; do
    [ -e "$d" ] || continue
    nm="${d%/}"; nm="${nm##*/}"
    if hermes profile show "$nm" >/dev/null 2>&1; then HERM_KEYS+=("$nm"); fi
  done
  if [ "$FRESH" = true ]; then
    PROFILES=("${HERM_KEYS[@]}")
  elif [ "${#HAP_KEYS[@]}" -gt 0 ]; then
    PROFILES=("${HAP_KEYS[@]}")
  else
    PROFILES=("default")
  fi
elif [ -n "$PROFILES_CSV" ]; then
  IFS=',' read -ra PROFILES <<< "$PROFILES_CSV" || true
elif [ -n "$PROFILE" ]; then
  PROFILES=("$PROFILE")
else
  PROFILES=("default")
fi
# --agent only applies to a single-agent install; ignored for a multi-agent list.
SINGLE=false
if [ "${#PROFILES[@]}" -eq 1 ]; then SINGLE=true; fi

# Validate names up front — before touching anything — so a malformed name, or a
# typo'd profile that isn't a real Hermes profile, bails here instead of
# scattering half-built (possibly secret-bearing) profile dirs around.
echo "==> Checking agent names…"
NAME_RE='^[a-z0-9_-]{1,32}$'
# Only trust `hermes profile show` as an existence check if it works on this box
# (degrade gracefully on a Hermes that behaves differently — regex still applies).
PROFILE_CHECK=false
if command -v hermes >/dev/null 2>&1 && hermes profile show default >/dev/null 2>&1; then
  PROFILE_CHECK=true
fi
BAD_FORMAT=()
BAD_MISSING=()
if [ -n "$AGENT_ID" ] && ! printf '%s' "$AGENT_ID" | grep -qE "$NAME_RE"; then
  BAD_FORMAT+=("$AGENT_ID")
fi
for p in "${PROFILES[@]}"; do
  name="$(printf '%s' "$p" | tr -d '[:space:]')"
  if [ -z "$name" ]; then continue; fi
  if ! printf '%s' "$name" | grep -qE "$NAME_RE"; then
    BAD_FORMAT+=("$name"); continue
  fi
  if [ "$name" != "default" ] && [ "$PROFILE_CHECK" = true ] && ! hermes profile show "$name" >/dev/null 2>&1; then
    BAD_MISSING+=("$name")
  fi
done
if [ "${#BAD_FORMAT[@]}" -gt 0 ] || [ "${#BAD_MISSING[@]}" -gt 0 ]; then
  echo "ERROR: not installing — please fix these names:" >&2
  if [ "${#BAD_FORMAT[@]}" -gt 0 ]; then
    for n in "${BAD_FORMAT[@]}"; do echo "  • '$n' — invalid name (must match ${NAME_RE})" >&2; done
  fi
  if [ "${#BAD_MISSING[@]}" -gt 0 ]; then
    for n in "${BAD_MISSING[@]}"; do
      echo "  • '$n' — no such Hermes profile; create it first (hermes profile create $n) or fix the typo" >&2
    done
  fi
  exit 1
fi

echo "==> Syncing gateway dependencies with uv…"
uv sync

echo "==> Bearer token…"
if [ "$FRESH" = true ]; then
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
  local prof="$1" plugdir agent where key status
  local -a enable
  if [ -z "$prof" ] || [ "$prof" = "default" ]; then
    plugdir="$HOME/.hermes/plugins/hap"
    enable=(hermes plugins enable hap)
    agent="hermes"; key="default"; where="the default profile"
    RESTARTS+=("hermes gateway restart")
    DID_DEFAULT=true
  else
    plugdir="$HOME/.hermes/profiles/$prof/plugins/hap"
    enable=(hermes -p "$prof" plugins enable hap)
    agent="$prof"; key="$prof"; where="profile '$prof'"
    RESTARTS+=("hermes -p $prof gateway restart")
  fi
  if [ "$SINGLE" = true ] && [ -n "$AGENT_ID" ]; then agent="$AGENT_ID"; fi

  # Was this agent already here? (check before we write the new hap.json)
  status="new"
  if [ -f "$plugdir/hap.json" ]; then status="updated"; fi
  DONE_KEYS+=("$key")

  echo "==> Placing the Hermes plugin into $where (agent: $agent, $status)…"
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
  "${enable[@]}" >/dev/null 2>&1 || true
  echo "    plugin enabled in $where"
  AGENTS+=("$agent ($status)")
}

RESTARTS=()
AGENTS=()
DONE_KEYS=()
DID_DEFAULT=false
for p in "${PROFILES[@]}"; do
  pc="$(echo "$p" | tr -d '[:space:]')"
  if [ -z "$pc" ]; then continue; fi
  install_one "$pc"
done

# Find hap installs already on disk, to flag any agent that exists but wasn't
# part of this run (the classic "forgot a profile" trap).
FOUND_KEYS=()
if [ -f "$HOME/.hermes/plugins/hap/hap.json" ]; then FOUND_KEYS+=("default"); fi
for d in "$HOME"/.hermes/profiles/*/plugins/hap/hap.json; do
  [ -e "$d" ] || continue
  d2="${d%/plugins/hap/hap.json}"
  FOUND_KEYS+=("${d2##*/}")
done
OMITTED=()
for k in "${FOUND_KEYS[@]}"; do
  seen=false
  for j in "${DONE_KEYS[@]}"; do
    if [ "$k" = "$j" ]; then seen=true; break; fi
  done
  if [ "$seen" = false ]; then OMITTED+=("$k"); fi
done

OPEN_URL="${PUBLIC_URL:-http://${HOST}:${PORT}}"

# Educational note: default not touched *and* not even installed — shown only
# when the omitted-agent warning below won't already cover it.
default_on_disk=false
for k in "${FOUND_KEYS[@]}"; do if [ "$k" = "default" ]; then default_on_disk=true; fi; done
DEFAULT_NOTE=""
if [ "$DID_DEFAULT" = false ] && [ "$default_on_disk" = false ]; then
  DEFAULT_NOTE="
  Note: your default agent (~/.hermes) wasn't part of this run, and \"default\"
  isn't added automatically. If you only ever expose a restricted profile to hap
  that's fine; otherwise add \"default\", e.g. --profiles default,twinky."
fi

# Omitted-agent warning: hap is set up for these, but they weren't touched.
OMITTED_NOTE=""
if [ "${#OMITTED[@]}" -gt 0 ]; then
  OMITTED_NOTE="
  ⚠ hap is also installed for: ${OMITTED[*]} — NOT updated in this run; they
    still have the previous plugin. To update every agent at once, re-run:
      ./scripts/install.sh --all-profiles"
fi

# --all-profiles update: surface Hermes profiles that have no hap (not enrolled).
UNENROLLED_NOTE=""
if [ "$ALL_PROFILES" = true ] && [ "$FRESH" = false ]; then
  unenrolled=()
  for k in "${HERM_KEYS[@]}"; do
    in_hap=false
    if [ "${#HAP_KEYS[@]}" -gt 0 ]; then
      for j in "${HAP_KEYS[@]}"; do if [ "$k" = "$j" ]; then in_hap=true; break; fi; done
    fi
    if [ "$in_hap" = false ]; then unenrolled+=("$k"); fi
  done
  if [ "${#unenrolled[@]}" -gt 0 ]; then
    UNENROLLED_NOTE="
  Note: these Hermes profiles have no hap and were left untouched: ${unenrolled[*]}.
  --all-profiles only updates agents that already have hap; to add one, name it:
      ./scripts/install.sh --profiles default,<name>"
  fi
fi

cat <<EOF

────────────────────────────────────────────────────────────
  hap is set up. Agents: ${AGENTS[*]}${DEFAULT_NOTE}${OMITTED_NOTE}${UNENROLLED_NOTE}

  Open it:      ${OPEN_URL}
  Login token:  ${TOKEN}
  Or short PIN: ${PIN}
EOF

if [ "$FRESH" = true ]; then
  cat <<EOF

  First run — start the two pieces (for durable services see the README,
  "Running as a service"):
    1) hap gateway:  uv run uvicorn app.main:app --host ${HOST} --port ${PORT}
    2) Hermes:       hermes gateway run

  If Hermes was already running, restart it to load the plugin (enabling alone
  only takes effect next session):
EOF
  for r in "${RESTARTS[@]}"; do echo "    $r"; done
  cat <<EOF

  Reaching it from your phone (see README, "Reaching it from your phone"):
    • Tailscale — recommended; HTTPS so the PWA installs (Linux needs sudo)
    • Caddy     — public domain (caddy/Caddyfile.example)
    • LAN only  — re-run with --host 0.0.0.0 (plain http, no PWA install)
EOF
else
  cat <<EOF

  ↻ Updated. Restart each piece to apply (services assumed — if you run Hermes
  in the foreground, restart it the way you started it):
    sudo systemctl restart hap-gateway
EOF
  for r in "${RESTARTS[@]}"; do echo "    $r"; done
fi
echo "────────────────────────────────────────────────────────────"
