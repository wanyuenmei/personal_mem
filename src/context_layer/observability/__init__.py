"""Observability layer: access/audit logging for tool calls and page views."""

from context_layer.observability.access_log import (
    log_dashboard_action,
    log_dashboard_view,
    log_tool_call,
)

__all__ = ["log_dashboard_action", "log_dashboard_view", "log_tool_call"]
