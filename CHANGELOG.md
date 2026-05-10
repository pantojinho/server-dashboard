# Changelog

All notable changes to the Hermes Server Dashboard project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Added `AGENTS.md` with repository guidance for AI coding agents.
- Added `docs/HERMES_MONITORING.md` with Hermes service monitoring setup, API checks, and troubleshooting steps.
- Added optional Basic Auth enforcement when `DASHBOARD_PASSWORD` or `auth.password` is configured.
- Added configurable Hermes gateway scope via `hermes.gateway_scope` or `HERMES_GATEWAY_SCOPE`.

### Changed
- Normalized systemd service names so config values work with or without the `.service` suffix.
- Hermes gateway checks now respect the configured service scope instead of assuming `systemctl --user`.
- Optional bot connectivity no longer marks `/api/health` red when `integrations.bot_api_url` is empty.
- Replaced shell-string systemd/journalctl calls with argument-list subprocess calls where service names or sudo passwords are involved.
- Added safe fallbacks for `os.getloadavg()` and `os.uname()` so local development on non-Linux hosts does not crash core endpoints.

### Fixed
- Fixed the missing `Response` import used by the favicon fallback route.
- Fixed a configuration mismatch by documenting `hermes.home`, `hermes.gateway_service`, and `hermes.gateway_scope` in `config.example.yaml`.

## [2.0.0] - 2026-05-09

### Added
- Full **System Overview** tab with live sparklines for CPU, RAM, disk, swap, temperatures, and network metrics
- **Service Management** panel to monitor and restart systemd services from the browser
- **Hermes Agent** integration showing live model, provider, platforms, cron jobs, sessions, and kanban board status
- **Trading Bot** tab with Binance bot integration: live P&L, regime indicators, trading decisions, and order book visualization
- **Time-Series Charts** tab with embedded SQLite TSDB and Chart.js visualizations (no Prometheus needed)
- **Log Viewer** with real-time journalctl output and filtering options
- **Operations Panel** for switching AI models, creating config backups, and quick actions
- **Plugin System** for extending the dashboard with custom tabs, API endpoints, and metric collectors
- Responsive layout with hamburger menu for mobile devices (breakpoints: 1200px/768px/480px)
- Dark retro-terminal aesthetic with phosphor-green on dark-blue-black background
- ASCII art neofetch display and glowing status indicators
- **9 interactive charts** in the Charts tab: CPU, RAM, Disk, Temp CPU, Network RX/TX, BitsyMiner Hashrate, BitsyMiner Temp, Bot P&L, BTC Price

### Changed
- Migrated from single-page mockup to fully functional dashboard with real-time data
- Implemented secure REST API with FastAPI for backend endpoints
- Added background TSDB collector with SQLite backend
- Replaced static mockups with live dynamic UI components

### Fixed
- Mobile responsiveness issues
- Chart rendering performance

### Documentation
- Added comprehensive **CONTRIBUTING.md** with code style guidelines
- Added **LICENSE** file (MIT)
- Added **config.example.yaml** for easy setup
- Added **server-dashboard.service.template** for systemd integration

## [1.0.0] - 2026-05-08

### Added
- Initial dashboard mockup with retro-terminal aesthetic
- Basic UI structure and placeholder content

[Unreleased]: https://github.com/pantojinho/server-dashboard/compare/v2.0.0...HEAD
[2.0.0]: https://github.com/pantojinho/server-dashboard/compare/v1.0.0...v2.0.0
[1.0.0]: https://github.com/pantojinho/server-dashboard/releases/tag/v1.0.0
