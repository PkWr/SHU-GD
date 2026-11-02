#!/bin/bash
# Build index.html (+ assets/) from the vault content and publish to GitHub Pages.
# Commits index.html and assets/ only. Flags: --dry-run (build, show change, no commit), --no-fetch (skip og:image lookups).
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
CONTENT="${GD_SHOWCASE_CONTENT:-$HOME/Vaults/2026-PkWr/70 Sandpit/Sandpit - GD Digest/Claude - GD Showcase}"
export GD_SHOWCASE_CONTENT="$CONTENT"
DRY=0; BUILD_ARGS=()
for a in "$@"; do case "$a" in --dry-run) DRY=1;; --no-fetch) BUILD_ARGS+=("$a");; *) echo "Unknown flag: $a"; exit 1;; esac; done

[ -s "$CONTENT/showcase.md" ] || { echo "Stop: $CONTENT/showcase.md is missing or empty (vault moved or not synced?)"; exit 1; }
cd "$REPO"
git checkout -- index.html   # discard any earlier dry-run output
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echo "Stop: uncommitted changes in the repo:"; git status --short; exit 1
fi
git pull --ff-only
python3 build/build-embed.py ${BUILD_ARGS[@]+"${BUILD_ARGS[@]}"}

CHANGES="$(git status --porcelain -- index.html assets)"
if [ -z "$CHANGES" ]; then echo "Nothing changed: nothing to publish"; exit 0; fi
echo "$CHANGES"
if [ "$DRY" = 1 ]; then echo "Dry run: not committed. The next run discards index.html changes."; exit 0; fi
git add index.html assets
git commit -m "$(date +%d.%m.%y) Update"
git push
