#!/usr/bin/env bash
# Cron monitor — shell-only wrapper.
# Runs the healthcheck and reports failures only.
# Used as a cronjob script (no_agent=True) so it delivers stdout verbatim.
# Silent when everything is OK (empty stdout = no delivery).

set -uo pipefail

HEALTHCHECK="/ihome/alee/alc376/.hermes/scripts/cron-healthcheck.sh"

if [[ ! -x "$HEALTHCHECK" ]]; then
    chmod +x "$HEALTHCHECK" 2>/dev/null || true
fi

output=$("$HEALTHCHECK" 2>&1)
status=$?

if [[ "$status" -ne 0 ]]; then
    echo "$output"
fi

exit 0
