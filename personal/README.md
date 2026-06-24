# Personal Hermes Configuration

Portable copy of my `~/.hermes/` setup. Everything here is **API-key safe** — all secrets are redacted.

## Structure

| Directory | Contents |
|-----------|----------|
| `config.yaml` | Sanitized Hermes config (replace `YOUR_KEY_HERE` values) |
| `.env` | Sanitized environment variables |
| `scripts/` | Custom shell/Python scripts (cron monitors, fleet supervisor, gateway watchdog) |
| `cron/` | Cron job definitions |
| `skills/` | Snapshot of custom + cached skills from `~/.hermes/skills/` |
| `plugins/` | Plugin sources (hermes-mnemosyne, memory) |
| `memories/` | Persistent memory state (MEMORY.md, USER.md) |
| `agent-persona/` | Agent personality config (SOUL.md) |
| `agent-hooks/` | Custom agent lifecycle hooks |
| `models/` | Local model references |

## Restore on new machine

```bash
# 1. Clone fork
git clone git@github.com:alexanderchang1/hermes-agent.git ~/.hermes/hermes-agent

# 2. Copy personal config
cp ~/.hermes/hermes-agent/personal/config.yaml ~/.hermes/
cp ~/.hermes/hermes-agent/personal/.env ~/.hermes/
# Fill in real API keys in ~/.hermes/.env

# 3. Copy scripts, cron, hooks
cp -r ~/.hermes/hermes-agent/personal/scripts/ ~/.hermes/
cp -r ~/.hermes/hermes-agent/personal/cron/ ~/.hermes/
cp -r ~/.hermes/hermes-agent/personal/agent-hooks/ ~/.hermes/
cp -r ~/.hermes/hermes-agent/personal/memories/ ~/.hermes/
cp -r ~/.hermes/hermes-agent/personal/plugins/ ~/.hermes/
cp ~/.hermes/hermes-agent/personal/agent-persona/SOUL.md ~/.hermes/agent-persona/

# 4. Build & install
cd ~/.hermes/hermes-agent && pip install -e .
```

## ⚠️ Security

This repo is public. Never commit real API keys. All keys in `config.yaml` and `.env` here are placeholders.
