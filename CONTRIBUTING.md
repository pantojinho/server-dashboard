# Contributing to Hermes Server Dashboard

Thanks for your interest! This project welcomes contributions.

## Quick Start

1. Fork the repository
2. Create a feature branch: `git checkout -b feature/my-feature`
3. Make your changes
4. Test: `.venv/bin/python3 server.py` and verify in browser
5. Commit: `git commit -m "Add: my feature description"`
6. Push and open a Pull Request

## Code Style

- **Python**: PEP 8, type hints preferred, max 120 char lines
- **JavaScript**: 2-space indent, semicolons, descriptive variable names
- **CSS**: Use CSS custom properties from `:root` for theming
- **Commits**: Conventional format (`Add:`, `Fix:`, `Refactor:`, `Docs:`)

## Adding a Plugin

Plugins are the recommended way to extend the dashboard. See `plugins/example_plugin.py.example` for a complete reference.

1. Create `plugins/my_plugin.py` (copy from `example_plugin.py.example`)
2. Subclass `DashboardPlugin` and override the hooks you need
3. Plugin is auto-discovered on next server restart

## Adding a Core Tab

If your feature belongs in the core dashboard (not a plugin):

1. Add backend routes in `server.py`
2. Add the tab HTML in `static/index.html`
3. Use existing CSS classes for consistent retro-terminal styling
4. Update `README.md` API docs

## Theme Guidelines

The dashboard uses a retro terminal aesthetic:

- **Background**: `var(--bg)` (#060610), panels: `var(--bg-panel)` (#0a0a16)
- **Text**: `var(--text)` for primary, `var(--text-dim)` for secondary
- **Status colors**: `var(--green)` for healthy, `var(--red)` for errors, `var(--yellow)` for warnings, `var(--cyan)` for info
- **Font**: `'Fira Code', 'Courier New', monospace` at 12px base
- **Borders**: `var(--border)` (#1a1a2a), 1px solid, border-radius 2px
- **Effects**: Subtle glow via `text-shadow` and `box-shadow` with theme colors

## Reporting Issues

- Include server OS, Python version, and browser
- Check browser console for errors
- Include relevant log output from `/api/logs/dashboard`

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
