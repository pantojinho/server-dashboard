"""
Plugin loader — discovers and manages dashboard plugins.

Plugins are Python files in the plugins/ directory that define one or more
DashboardPlugin subclasses. The loader imports them, instantiates, and
provides accessors for the core server to register tabs, routes, and collectors.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from typing import Any

from plugins.base import DashboardPlugin

logger = logging.getLogger("plugins")

# ── Plugin registry ──────────────────────────────────────────────────

_plugins: list[DashboardPlugin] = []
_loaded: bool = False

PLUGINS_DIR = Path(__file__).parent


def load_plugins(extra_dirs: list[str | Path] | None = None) -> list[DashboardPlugin]:
    """
    Discover and load all plugins from the plugins/ directory (and optional extra dirs).

    Each .py file in the directory is imported. Any DashboardPlugin subclass found
    in the module is instantiated and registered.

    Args:
        extra_dirs: Additional directories to scan for plugin modules.

    Returns:
        List of loaded DashboardPlugin instances.
    """
    global _plugins, _loaded

    if _loaded:
        return _plugins

    dirs_to_scan = [PLUGINS_DIR]
    if extra_dirs:
        dirs_to_scan.extend(Path(d) for d in extra_dirs)

    seen_names: set[str] = set()

    for plugins_dir in dirs_to_scan:
        if not plugins_dir.is_dir():
            continue

        for py_file in sorted(plugins_dir.glob("*.py")):
            if py_file.name.startswith("_") or py_file.name == "base.py":
                continue

            module_name = f"plugins.{py_file.stem}"

            try:
                spec = importlib.util.spec_from_file_location(module_name, py_file)
                if spec is None or spec.loader is None:
                    continue
                module = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = module
                spec.loader.exec_module(module)

                # Find DashboardPlugin subclasses
                for attr_name in dir(module):
                    attr = getattr(module, attr_name)
                    if (
                        isinstance(attr, type)
                        and issubclass(attr, DashboardPlugin)
                        and attr is not DashboardPlugin
                    ):
                        try:
                            instance = attr()
                            if instance.name in seen_names:
                                logger.warning(
                                    f"Duplicate plugin name '{instance.name}' — skipping {attr_name}"
                                )
                                continue
                            seen_names.add(instance.name)
                            instance.on_load()
                            _plugins.append(instance)
                            logger.info(f"Loaded plugin: {instance.name} ({instance.display_name})")
                        except Exception as e:
                            logger.error(f"Failed to instantiate {attr_name}: {e}")

            except Exception as e:
                logger.error(f"Failed to load plugin {py_file.name}: {e}")

    _loaded = True
    return _plugins


# ── Accessors ────────────────────────────────────────────────────────

def get_plugins() -> list[DashboardPlugin]:
    """Return all loaded plugin instances."""
    return _plugins


def get_plugin_tabs() -> list[dict[str, str]]:
    """
    Return tab metadata for all plugins that provide HTML content.

    Returns:
        List of dicts with keys: name, display_name, tab_id, nav_html, tab_html, scripts, styles
    """
    tabs = []
    for p in _plugins:
        html = p.tab_html()
        if html:
            tabs.append({
                "name": p.name,
                "display_name": p.display_name,
                "tab_id": p.tab_id,
                "nav_html": p.nav_button_html(),
                "tab_html": html,
                "scripts": p.scripts_html(),
                "styles": p.styles_html(),
            })
    return tabs


def get_plugin_routes() -> list[DashboardPlugin]:
    """
    Return plugins that want to register custom routes.
    The server calls register_routes(app) for each.
    """
    return _plugins


def get_plugin_collectors() -> list[DashboardPlugin]:
    """
    Return plugins that provide metric collectors.
    The TSDB calls collect_metrics() for each on every scrape.
    """
    return [p for p in _plugins if p.collect_metrics() is not None or True]
    # Note: we include all plugins; collect_metrics returns None by default (no-op)
