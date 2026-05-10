"""
Base class for dashboard plugins.

Subclass this to create a custom plugin. Override only the hooks you need.
All methods have safe no-op defaults so plugins can be minimal.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from fastapi import FastAPI

__all__ = ["DashboardPlugin"]


class DashboardPlugin:
    """
    Base class for Hermes Server Dashboard plugins.

    Lifecycle:
      1. Plugin .py file is discovered in the plugins/ directory
      2. Module is imported; any DashboardPlugin subclass is instantiated
      3. register_routes(app) is called at server startup
      4. tab_html() is called to build the frontend tab navigation
      5. collect_metrics() is called on each TSDB scrape cycle (if defined)
    """

    # ── Identity (required) ──────────────────────────────────────────

    @property
    def name(self) -> str:
        """Short machine name for this plugin (used in URLs, IDs). Must be unique."""
        return self.__class__.__name__.lower().replace("plugin", "")

    @property
    def display_name(self) -> str:
        """Human-readable tab label shown in the dashboard navigation."""
        return self.__class__.__name__.replace("Plugin", "").strip()

    @property
    def tab_id(self) -> str:
        """DOM id for this plugin's tab. Default: 'plugin-{name}'."""
        return f"plugin-{self.name}"

    # ── Frontend hooks ───────────────────────────────────────────────

    def tab_html(self) -> str:
        """
        Return the full HTML content for this plugin's tab panel.

        The dashboard wraps this in a <div id="tab-{tab_id}" class="tab-content">.
        Use retro-terminal CSS classes from the dashboard theme:
          - .panel, .panel-header, .panel-body
          - .retro-text, .retro-green, .retro-red, .retro-cyan, .retro-yellow
          - .mono, .status-badge, .status-on, .status-off

        Returns:
            HTML string, or empty string to skip this tab entirely.
        """
        return ""

    def nav_button_html(self) -> str:
        """
        Return custom nav button HTML. Override for full control.

        Default generates:
            <button class="tab-btn" data-tab="{tab_id}">{display_name}</button>
        """
        return (
            f'<button class="tab-btn" data-tab="{self.tab_id}">'
            f"{self.display_name}</button>"
        )

    def scripts_html(self) -> str:
        """
        Return JavaScript for this plugin (included once at page load).

        Use the 'dashboard' global object for helpers:
          - dashboard.api(url) — fetch JSON from API
          - dashboard.formatBytes(n) — human-readable byte sizes
          - dashboard.escapeHtml(s) — escape HTML entities
          - dashboard.sparkline(data) — render unicode sparkline

        Returns:
            HTML <script> tag string, or empty string.
        """
        return ""

    def styles_html(self) -> str:
        """
        Return additional CSS for this plugin (included once at page load).

        Returns:
            HTML <style> tag string, or empty string.
        """
        return ""

    # ── Backend hooks ────────────────────────────────────────────────

    def register_routes(self, app: "FastAPI") -> None:
        """
        Register custom API routes on the FastAPI app.

        Example:
            @app.get("/api/my-plugin/data")
            async def my_data():
                return {"items": [...]}

        Args:
            app: The FastAPI application instance.
        """
        pass

    def collect_metrics(self) -> dict[str, Any] | None:
        """
        Called on each TSDB scrape cycle (default: every 30 seconds).

        Return a dict of {metric_name: value} pairs to be stored in the TSDB.
        Metric names should be prefixed: "plugin_{name}_{metric}".

        Return None or empty dict to skip.

        Example:
            return {"plugin_mqtt_messages": 42, "plugin_mqtt_errors": 0}
        """
        return None

    # ── Lifecycle hooks ──────────────────────────────────────────────

    def on_load(self) -> None:
        """Called once when the plugin is loaded. Use for initialization."""
        pass

    def on_unload(self) -> None:
        """Called when the server is shutting down. Use for cleanup."""
        pass

    # ── Utilities ────────────────────────────────────────────────────

    def __repr__(self) -> str:
        return f"<Plugin: {self.name}>"
