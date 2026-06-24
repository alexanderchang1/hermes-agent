#!/usr/bin/env python3
"""Cron monitor — runs the healthcheck and reports failures only.

Used as a cronjob script (no_agent=True) so it delivers stdout verbatim.
Designed to be SILENT when everything is OK (empty stdout = no delivery).
"""

import subprocess
import sys

result = subprocess.run(
    [sys.executable, "/ihome/alee/alc376/.hermes/scripts/cron-healthcheck.py"],
    capture_output=True, text=True, timeout=30,
)

# Only deliver if healthcheck found problems (exit code 1)
if result.returncode != 0:
    print(result.stdout)
    if result.stderr:
        print("STDERR:", result.stderr)
# exit 0 = silent, nothing delivered
