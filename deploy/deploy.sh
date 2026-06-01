#!/usr/bin/env bash
# Deploy the latest Winnow code AND refresh the candidate queue, in one step.
# This is the "I changed the code, make it live" command — distinct from
# clear_and_refresh.sh which also wipes verdicts (only use that for a true reset).
#
# Two-step gotcha that bit us before: `docker compose up -d --build` ships the
# new image but leaves the OLD candidates.jsonl in place. Anything driven by
# candidate shape (new buckets, new fields) won't appear until a refresh
# rebuilds the queue. This script chains both.
#
# RUN:  sudo ./deploy/deploy.sh
set -euo pipefail
unset TMOUT

DIR="${WINNOW_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
URL="${WINNOW_URL:-http://127.0.0.1:8077}"
ts="$(date +%F-%H%M.%S)"

log(){ printf '%s [%-5s] %s\n' "$(date +'%F %T')" "$1" "$2"; }
cd "$DIR"

log INFO "rebuilding image + restarting container"
docker compose up -d --build winnow

# Wait for the API to come back up before asking for a refresh.
log INFO "waiting for $URL to respond..."
for i in $(seq 1 30); do
  if curl -sf --max-time 3 "$URL/api/status" >/dev/null 2>&1; then break; fi
  sleep 1
done

log INFO "triggering refresh (rebuilds candidates.jsonl with the new code)"
curl -sf --max-time 30 -X POST "$URL/api/refresh" >/dev/null || log WARN "refresh trigger failed"

# Poll until the refresh completes (typical: <10s on a small instance).
for i in $(seq 1 60); do
  status="$(curl -s --max-time 5 "$URL/api/status" 2>/dev/null | python3 -c \
    "import sys,json;print(json.load(sys.stdin)['refresh']['status'])" 2>/dev/null || echo unknown)"
  if [[ "$status" == "idle" ]] && [[ $i -gt 1 ]]; then break; fi
  sleep 2
done

log INFO "summary:"
curl -s --max-time 5 "$URL/api/status" | python3 -c "
import sys, json, collections
s = json.load(sys.stdin)
b = collections.Counter(r.get('bucket', 'review') for r in s.get('identities', []))
print(f\"   total identities: {sum(b.values())} (review: {b['review']}, library: {b['library']})\")
print(f\"   pending in review pools: {sum(r['pending'] for r in s['identities'] if (r.get('bucket') or 'review')=='review')}\")
print(f\"   refresh: {s['refresh']['status']} ({s['refresh'].get('summary','')})\")
print(f\"   uncommitted: {s['uncommitted']}\")"
log INFO "done — reload the browser tab at $URL"
