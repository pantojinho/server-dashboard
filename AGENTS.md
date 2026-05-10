# AI Agent Guide

This repository is a Python/FastAPI server dashboard with a static single-page UI. Use this guide when an AI agent edits, reviews, or operates the project.

## Project Shape

- `server.py` is the main FastAPI application, API router, service monitor, and Hermes integration layer.
- `metrics_tsdb.py` owns the SQLite time-series collector and retention logic.
- `static/index.html` contains the dashboard UI, CSS, and browser JavaScript. There is no Node.js build step.
- `plugins/` contains optional dashboard extensions. Prefer plugins for new isolated tabs, routes, or metric collectors.
- `config.example.yaml` is the documented configuration template. `config.yaml` is local-only and must not be committed.

## Local Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config.example.yaml config.yaml
.venv/bin/python3 server.py
```

Default URL: `http://localhost:18791`.

## Safety Rules

- Do not commit secrets, `config.yaml`, `metrics.db`, backups, or local Hermes data.
- Document every behavioral change in `CHANGELOG.md` and update README/docs when configuration or API behavior changes.
- Keep service names normalized: accept both `name` and `name.service`, but avoid generating `name.service.service`.
- Do not run destructive system commands from tests or docs.
- For systemd operations, use argument-list `subprocess.run(...)`; avoid shell strings for service names or sudo passwords.

## Hermes Monitoring Notes

- Hermes home defaults to `~/.hermes` and can be changed with `HERMES_HOME` or `hermes.home`.
- Gateway service defaults to `hermes-gateway.service`.
- Gateway scope defaults to the matching configured service scope, then `user`. Override with `HERMES_GATEWAY_SCOPE` or `hermes.gateway_scope`.
- A valid Hermes monitor should check:
  - `/api/hermes` for gateway, model, provider, sessions, cron jobs, and platform status.
  - `/api/services` for configured systemd unit status, PID, memory, CPU, and uptime.
  - `/api/health` for aggregate state.
  - `/api/logs/hermes` or `/api/services/logs/hermes-gateway` for journal logs.

## Validation Checklist

Before handing off:

- Run `python -m py_compile server.py metrics_tsdb.py plugins/*.py`.
- Start the server locally when possible and verify `/api/health`, `/api/hermes`, `/api/services`, and `/api/features`.
- If `DASHBOARD_PASSWORD` is set, verify unauthenticated requests return `401` and authenticated requests work.
- If changing frontend behavior, open the dashboard in a browser and check the Console.

## Preferred Change Style

- Keep changes small and operationally useful.
- Add helper functions when they remove repeated service/systemd logic.
- Use configuration-driven behavior instead of hardcoded hostnames, ports, or service scopes.
- Update examples when adding config keys.
