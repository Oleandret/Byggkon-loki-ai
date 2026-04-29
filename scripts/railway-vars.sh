#!/usr/bin/env bash
# Loki AI for Byggkon — populate every Railway variable with a sensible
# placeholder, but DO NOT overwrite values that are already set.
#
# Prereq:  railway CLI installed and `railway link` already run for the
# right service. Test with:
#     railway variables
#
# Usage:
#   bash scripts/railway-vars.sh           # interactive — confirms each new add
#   bash scripts/railway-vars.sh --yes     # non-interactive — adds missing only
#
# What it does:
#   • Reads every "KEY=value" line from .env.example
#   • For each, checks if Railway already has a value
#   • If not, sets it to the example default (or leaves it empty if the
#     example has no default — common for secrets)
#   • Existing values are NEVER touched

set -euo pipefail

YES=false
if [[ "${1:-}" == "--yes" || "${1:-}" == "-y" ]]; then
  YES=true
fi

if ! command -v railway >/dev/null 2>&1; then
  echo "✘ Railway CLI not installed. Install with: brew install railway" >&2
  exit 1
fi

if ! railway whoami >/dev/null 2>&1; then
  echo "✘ Not logged in. Run: railway login" >&2
  exit 1
fi

ENV_FILE=".env.example"
if [[ ! -f "$ENV_FILE" ]]; then
  echo "✘ $ENV_FILE not found — run this from the project root." >&2
  exit 1
fi

echo "→ Fetching current Railway variables…"
EXISTING=$(railway variables --json 2>/dev/null || railway variables 2>/dev/null || true)

# Parse existing variables — works with both JSON and `KEY=value` outputs.
have_var() {
  local key="$1"
  if echo "$EXISTING" | grep -qE "(^|\")${key}(\"|\\s*=)"; then
    return 0
  fi
  return 1
}

ADDED=()
SKIPPED=()
EMPTY_SECRETS=()

while IFS= read -r line; do
  # Strip comments and whitespace.
  [[ "$line" =~ ^[[:space:]]*# ]] && continue
  [[ -z "${line// }" ]] && continue

  # Match KEY=value, optionally with trailing inline comment.
  if [[ "$line" =~ ^([A-Z_][A-Z0-9_]*)=([^#]*)(\#.*)?$ ]]; then
    KEY="${BASH_REMATCH[1]}"
    RAW_VALUE="${BASH_REMATCH[2]}"
    # Trim trailing whitespace
    VALUE="$(echo -n "$RAW_VALUE" | sed -E 's/[[:space:]]+$//')"

    if have_var "$KEY"; then
      SKIPPED+=("$KEY")
      continue
    fi

    if [[ -z "$VALUE" ]]; then
      # Railway CLI doesn't accept empty values via --set, so we skip these
      # and report them at the end. User fills them in manually in the
      # dashboard or via `railway variables --set KEY=value`.
      EMPTY_SECRETS+=("$KEY")
      continue
    fi

    if $YES; then
      railway variables --set "${KEY}=${VALUE}" >/dev/null
      ADDED+=("$KEY")
    else
      read -r -p "   Add ${KEY}=${VALUE}? [Y/n] " ans
      if [[ "${ans,,}" != "n" ]]; then
        railway variables --set "${KEY}=${VALUE}" >/dev/null
        ADDED+=("$KEY")
      fi
    fi
  fi
done < "$ENV_FILE"

echo
echo "─────────────────────────────────────────────"
echo "✓ Lagt til med default-verdi (${#ADDED[@]}):"
for v in "${ADDED[@]}"; do echo "    • $v"; done
echo
echo "↻ Allerede satt — ikke rørt (${#SKIPPED[@]}):"
for v in "${SKIPPED[@]}"; do echo "    • $v"; done
echo
echo "✎ Trenger verdi (${#EMPTY_SECRETS[@]}) — fyll inn i Railway-dashboardet:"
for v in "${EMPTY_SECRETS[@]}"; do echo "    • $v"; done
echo "─────────────────────────────────────────────"
echo
echo "Sett en hemmelig verdi raskt fra terminalen:"
echo "    railway variables --set 'OPENAI_API_KEY=sk-...'"
echo
echo "Eller åpne dashboardet:"
echo "    railway open"
