"""
Hermes Server Dashboard — Plugin System.

Plugins allow extending the dashboard with custom tabs, API endpoints,
and metric collectors without modifying core framework files.

To create a plugin:
  1. Create a .py file in the plugins/ directory
  2. Subclass DashboardPlugin
  3. Implement the hooks you need (name, tab_html, register_routes, collect_metrics)
  4. The plugin loader discovers and registers everything automatically

See plugins/example_plugin.py for a working reference.
"""

from plugins.base import DashboardPlugin
from plugins.loader import (
    load_plugins,
    get_plugins,
    get_plugin_tabs,
    get_plugin_routes,
    get_plugin_collectors
)

__all__ = [
    "DashboardPlugin",
    "load_plugins",
    "get_plugins",
    "get_plugin_tabs",
    "get_plugin_routes",
    "get_plugin_collectors",
]
