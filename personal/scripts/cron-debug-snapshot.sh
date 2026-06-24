#!/bin/bash
# Hermes cron error tracker — captures all evidence around cron failures
# Run this to populate ~/.hermes/cron_debug/ with forensic data

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
OUTDIR="$HOME/.hermes/cron_debug/${TIMESTAMP}"
mkdir -p "$OUTDIR"

echo "=== Hermes Cron Debug Snapshot: $(date) ===" > "$OUTDIR/summary.txt"
echo "" >> "$OUTDIR/summary.txt"

# 1. Jobs state
echo "--- jobs.json last_status ---" >> "$OUTDIR/summary.txt"
python3 -c "
import json
with open('$HOME/.hermes/cron/jobs.json') as f:
    j = json.load(f)['jobs'][0]
for k in ['last_status','last_error','last_delivery_error','last_run_at','next_run_at','base_url']:
    print(f'  {k}: {j.get(k)}')
" >> "$OUTDIR/summary.txt"

# 2. Recent cron log entries (last 5 min)
echo "" >> "$OUTDIR/summary.txt"
echo "--- agent.log cron entries (last 10) ---" >> "$OUTDIR/summary.txt"
grep 'cron_c16568fa305c' "$HOME/.hermes/logs/agent.log" | tail -10 >> "$OUTDIR/summary.txt"

# 3. Recent errors
echo "" >> "$OUTDIR/summary.txt"
echo "--- errors.log cron-related (last 10) ---" >> "$OUTDIR/summary.txt"
grep -i 'cron\|c16568\|delivery error\|Telegram send failed' "$HOME/.hermes/logs/errors.log" | tail -10 >> "$OUTDIR/summary.txt"

# 4. Process state
echo "" >> "$OUTDIR/summary.txt"
echo "--- Hermes processes ---" >> "$OUTDIR/summary.txt"
ps aux | grep -E 'hermes' | grep -v grep | grep -v pyright >> "$OUTDIR/summary.txt"

# 5. Gateway health
echo "" >> "$OUTDIR/summary.txt"
echo "--- Gateway recent (last 20 lines) ---" >> "$OUTDIR/summary.txt"
tail -20 "$HOME/.hermes/logs/gateway.log" >> "$OUTDIR/summary.txt"

# 6. vLLM health
echo "" >> "$OUTDIR/summary.txt"
echo "--- vLLM health ---" >> "$OUTDIR/summary.txt"
curl -s http://localhost:8010/health 2>/dev/null >> "$OUTDIR/summary.txt" || echo "  proxy unreachable" >> "$OUTDIR/summary.txt"
curl -s http://localhost:8000/v1/models 2>/dev/null | python3 -c "import sys,json; ms=json.load(sys.stdin); print(f'  vLLM models: {[m[\"id\"] for m in ms.get(\"data\",[])]}')" 2>/dev/null || echo "  vLLM unreachable" >> "$OUTDIR/summary.txt"

echo "" >> "$OUTDIR/summary.txt"
echo "=== Done ===" >> "$OUTDIR/summary.txt"
echo "Saved to $OUTDIR/summary.txt"
cat "$OUTDIR/summary.txt"
