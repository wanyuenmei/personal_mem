"""Observability layer: access/audit logging for tool calls."""

from context_layer.observability.access_log import log_tool_call

__all__ = ["log_tool_call"]
