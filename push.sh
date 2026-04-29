#!/usr/bin/env bash
# Loki AI for Byggkon — first-time push to GitHub.
#
# Usage:
#   1. Create an empty repo on GitHub: https://github.com/new
#      Suggested name: loki-ai (under your personal account or a
#      "Byggkon" organization). Keep it private if it'll hold real
#      tenant IDs in commits — but normally secrets live in Railway,
#      not in the repo, so public is fine.
#   2. Run this script from inside this folder:
#        bash push.sh git@github.com:Byggkon/loki-ai.git
#      or with HTTPS:
#        bash push.sh https://github.com/Byggkon/loki-ai.git
#
# What it does:
#   * git init (if needed)
#   * git add -A and an initial commit
#   * sets the remote and pushes main
#
# If you have the GitHub CLI (`gh`) installed and authenticated, you
# can instead run:
#   gh repo create Byggkon/loki-ai --public --source=. --push
# which creates the repo and pushes in one go.

set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: bash push.sh <git-remote-url>"
  echo "Example: bash push.sh git@github.com:Byggkon/loki-ai.git"
  exit 1
fi

REMOTE="$1"

if [[ ! -d .git ]]; then
  echo "→ git init"
  git init -b main
fi

# Make sure we don't accidentally push secrets.
if [[ -f .env ]]; then
  echo "⚠  Found .env — this is gitignored, but double-check before pushing."
fi

echo "→ Staging files"
git add -A

# If this is our scaffolded state (one commit and unstaged README/etc edits),
# amend rather than creating a second "patch" commit on top.
SINGLE_INITIAL_COMMIT=false
if [[ "$(git rev-list --count HEAD 2>/dev/null || echo 0)" == "1" ]] \
   && git log -1 --pretty=%s | grep -q "Initial commit: Loki AI for Byggkon"; then
  SINGLE_INITIAL_COMMIT=true
fi

if git diff --cached --quiet; then
  echo "Nothing to commit (already up to date)."
elif $SINGLE_INITIAL_COMMIT; then
  echo "→ Amending initial commit with latest edits"
  git commit --amend --no-edit
else
  git commit -m "Initial commit: Loki AI for Byggkon

OneDrive → Unstructured → Pinecone tenant-wide sync, with
admin UI in Byggkon's design language."
fi

if git remote get-url origin >/dev/null 2>&1; then
  echo "→ Updating remote 'origin' to $REMOTE"
  git remote set-url origin "$REMOTE"
else
  echo "→ Adding remote 'origin' = $REMOTE"
  git remote add origin "$REMOTE"
fi

echo "→ Pushing to origin/main"
# --force-with-lease covers the case where we amended (rewriting the only
# commit) on a brand-new remote. It's a no-op for fresh repos.
git push -u --force-with-lease origin main

echo
echo "✓ Done. Repository pushed to:"
echo "    $REMOTE"
echo
echo "Next steps:"
echo "  • Connect the repo in Railway: New Project → Deploy from GitHub repo."
echo "  • Add a Volume mounted at /data (for SQLite state)."
echo "  • Set env vars from .env.example (or use the admin UI after first boot)."
