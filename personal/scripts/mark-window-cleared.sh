#!/bin/bash
# Usage: mark-window-cleared.sh W1 [W2 W3 ...]
# Marks windows as recently cleared (via /clear), preventing /wrap-up for 10 minutes.
set -euo pipefail

VAULT="/ix1/alee/LO_LAB/Personal/Alexander_Chang/alc376/vault"
WRAPUP_FILE="$VAULT/lab/agents/supervisor/_wrapup_state.json"

if [ ! -f "$WRAPUP_FILE" ]; then
  echo '{}' > "$WRAPUP_FILE"
fi

TIMESTAMP=$(date +%s)

for WINDOW in "$@"; do
  WID="${WINDOW#W}"
  python3 -c "
import json, time
with open('$WRAPUP_FILE', 'r') as f:
    state = json.load(f)
wid = '${WID}'
if wid not in state or wid not in state:
    state[wid] = {}
state[wid]['cleared_at'] = $TIMESTAMP
# Also set wrapup_sent_at to ensure the NON_WORKING guard applies
state[wid]['wrapup_sent_at'] = $TIMESTAMP
with open('$WRAPUP_FILE', 'w') as f:
    json.dump(state, f, indent=2)
print(f'Marked {WINDOW} as cleared at {time.strftime(\"%Y-%m-%d %H:%M UTC\", time.gmtime($TIMESTAMP))}')
"
done
