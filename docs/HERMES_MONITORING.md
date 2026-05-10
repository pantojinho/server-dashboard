# Hermes Monitoring Guide

This dashboard can monitor both the server and Hermes services, but the result depends on `config.yaml` matching how Hermes is installed.

## Recommended Configuration

```yaml
services:
  - name: "hermes-gateway"
    scope: "user"
    display_name: "Hermes Gateway"
    allow_restart: true
  - name: "server-dashboard"
    scope: "system"
    display_name: "Dashboard"
    allow_restart: true

hermes:
  home: "~/.hermes"
  gateway_service: "hermes-gateway.service"
  gateway_scope: "user"
```

Use `scope: "system"` and `gateway_scope: "system"` if Hermes runs as a system service.

## What Is Monitored

- Server resources: CPU, memory, swap, disk, temperature, network, uptime, and load.
- Configured systemd services: active state, PID, memory, CPU, uptime, restart permission, and logs.
- Hermes Agent: gateway status, model/provider, configured platforms, cron jobs, sessions, and kanban tasks.
- Connectivity: internet, DNS, Cloudflare tunnel process, Hermes gateway service, optional Binance bot API, and the dashboard itself.
- Time-series metrics: system metrics plus configured integrations in the local SQLite TSDB.

## API Checks

```bash
curl http://localhost:18791/api/health
curl http://localhost:18791/api/hermes
curl http://localhost:18791/api/services
curl http://localhost:18791/api/logs/hermes?lines=80
```

If Basic Auth is enabled:

```bash
curl -u admin:$DASHBOARD_PASSWORD http://localhost:18791/api/health
```

## Health Rules

The health endpoint returns:

- `green`: required checks are healthy and resource usage is below warning levels.
- `yellow`: CPU, memory, or disk is above warning thresholds.
- `red`: internet is down, Hermes gateway is down, an enabled bot API is unreachable, or disk usage is critical.

Optional integrations do not make health red when disabled. For example, an empty `integrations.bot_api_url` disables the Binance bot connectivity check.

## Hermes-Specific Troubleshooting

1. Confirm the service scope:

```bash
systemctl --user is-active hermes-gateway.service
systemctl is-active hermes-gateway.service
```

2. Match `hermes.gateway_scope` to the command that works.

3. Confirm `hermes.home` points to the directory containing Hermes `config.yaml`, `.env`, `state.db`, `kanban.db`, and `cron/jobs.json`.

4. Check logs:

```bash
journalctl --user -u hermes-gateway.service -n 80 --no-pager
```

or, for a system service:

```bash
journalctl -u hermes-gateway.service -n 80 --no-pager
```

5. Restart only if `allow_restart: true` is set for the service and `sudo_mode` is configured when needed.

## Verdict

With the configuration above, the current system is suitable for monitoring a Hermes host: it covers host health, gateway service health, Hermes runtime state, logs, and historical metrics. The main operational requirement is keeping the Hermes service name/scope and Hermes home path accurate in `config.yaml`.
