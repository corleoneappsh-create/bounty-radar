#!/bin/sh
set -eu

ROOT="$HOME/MoneyLab/bounty-radar"
OUT="$ROOT/monitor"

if command -v gh >/dev/null 2>&1; then
  GH_TOKEN="$(gh auth token 2>/dev/null || true)"
  [ -z "$GH_TOKEN" ] || export GH_TOKEN
fi
mkdir -p "$OUT"

TMP="$OUT/latest.json.tmp"
LATEST="$OUT/latest.json"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)

cd "$ROOT"
node dist/cli.js --min 100 --max-age-days 180 --max-comments 10 --json > "$TMP"
python3 -m json.tool "$TMP" >/dev/null
mv "$TMP" "$LATEST"

COUNT=$(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1]))))' "$LATEST")
printf '%s claimable=%s\n' "$STAMP" "$COUNT" >> "$OUT/scan.log"

if [ "$COUNT" -gt 0 ]; then
  cp "$LATEST" "$OUT/candidates-$STAMP.json"
  printf '%s\n' "$STAMP" > "$OUT/NEW_CANDIDATES.flag"
else
  rm -f "$OUT/NEW_CANDIDATES.flag"
fi
