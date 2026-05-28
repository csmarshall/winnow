#!/usr/bin/env bash
# Clear Winnow's accumulated verdicts + stale build and refresh against the
# CURRENT Frigate topology (rebuilds candidates/targets from /api/config). Use
# after the Frigate model set changes (e.g. deleting old classifiers) so Winnow
# stops referencing models that no longer exist. Stays in whatever commit mode
# the compose defines (WINNOW_NO_COMMIT is untouched).
#
# review/ files are written by the container as root -> this needs sudo for the
# file surgery and the docker compose calls. Archives verdicts before clearing.
#
# RUN: sudo ./deploy/clear_and_refresh.sh
set -euo pipefail
unset TMOUT

DIR="${WINNOW_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"   # repo root (script lives in deploy/)
REVIEW="$DIR/review"
URL="${WINNOW_URL:-http://127.0.0.1:8077}"
ts="$(date +%F-%H%M.%S)"

log(){ printf '%s [%-5s] %s\n' "$(date +'%F %T')" "$1" "$2"; }
[[ -d "$REVIEW" ]] || { log ERROR "no review dir: $REVIEW"; exit 2; }
cd "$DIR"

log INFO "stopping winnow"
docker compose stop winnow

mkdir -p "$REVIEW/_archive"
if [[ -s "$REVIEW/verdicts.jsonl" ]]; then
  cp -a "$REVIEW/verdicts.jsonl" "$REVIEW/_archive/verdicts_$ts.jsonl"
  log INFO "archived verdicts -> _archive/verdicts_$ts.jsonl ($(wc -l <"$REVIEW/verdicts.jsonl") lines)"
fi

: > "$REVIEW/verdicts.jsonl"                 # empty the verdict log (source of truth)
rm -f "$REVIEW/candidates.jsonl" \
      "$REVIEW/targets.json" \
      "$REVIEW/.refresh_state.json"          # stale build + resume pointer -> full rebuild
log INFO "cleared verdicts; removed stale candidates/targets/refresh-state"

log INFO "starting winnow (--build so the latest code ships; rebuilds queue on boot)"
docker compose up -d --build winnow

# wait for the rebuild and report what it now discovers
log INFO "waiting for refresh..."
for i in $(seq 1 30); do
  sleep 2
  out="$(curl -s --max-time 5 "$URL/api/status" 2>/dev/null || true)"
  [[ -n "$out" ]] && echo "$out" | grep -q '"targets"' && break
done
echo "$out" | python3 -c "
import sys,json
try: s=json.load(sys.stdin)
except Exception: print('  (status not ready yet — check $URL manually)'); raise SystemExit
t=s.get('targets',{})
print('  source:',s.get('source_name'),'| uncommitted:',s.get('uncommitted'))
print('  dog targets :', list((t.get('dog') or {}).keys()))
print('  car targets :', list((t.get('car') or {}).keys()))
print('  person count:', len(t.get('person') or []))
print('  identities  :', len(s.get('identities',[])))
" 2>/dev/null || log WARN "could not parse status; open $URL"
log INFO "done — verify the pools reflect your current Frigate models (deleted models gone)"
