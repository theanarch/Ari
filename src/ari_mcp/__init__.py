"""Ari: an MCP server that gives an AI agent read-only access to a folder of public documents."""

from .server import create_server, main

__all__ = ["create_server", "main"]
