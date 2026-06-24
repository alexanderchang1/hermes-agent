#!/bin/bash
# Usage: mark-wrapup-sent.sh W1 [W2 W3 ...]
# Marks one or more windows as having had /wrap-up sent, preventing repeated flags.
set -euo pipefail

VAULT="/ix1/alee/LO_LAB/Personal/Alexander_Chang/alc376/vault"
WRAPUP_FILE="$VAULT/lab/agents/supervisor/_wrapup_state.json"

if [ ! -f "$WRAPUP_FILE" ]; then
  echo "No wrapup state file found. Creating empty state."
  echo '{}' > "$WRAPUP_FILE"
fi

TIMESTAMP=$(date +%s)

for WINDOW in "$@"; do
  # Remove W prefix: W1 → 1
  WID="${WINDOW#W}"
  python3 -c "
import json, sys
with open('$WRAPUP_FILE', 'r') as f:
    state = json.load(f)
wid = '${WID}'
if wid in state:
    state[wid]['wrapup_sent_at'] = $TIMESTAMP
    print(f'Marked {WINDOW} as wrap-up sent (since: {state[wid].get(\"since\", \"?\")})')
else:
    print(f'WARNING: {WINDOW} not in wrapup state, skipping')
    sys.exit(1)
with open('$WRAPUP_FILE', 'w') as f:
    json.dump(state, f, indent=2)
"
done
