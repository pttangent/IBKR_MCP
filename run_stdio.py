#!/usr/bin/env python3
"""Stdio entry point for WorkBuddy MCP connector"""
import os, sys
os.environ.setdefault("IB_HOST", "127.0.0.1")
os.environ.setdefault("IB_PORT", "7497")
os.environ.setdefault("TWS_CLIENT_ID", "1")
os.environ.setdefault("TWS_PAPER_ACCOUNT", "DUO919088")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.server import mcp

if __name__ == "__main__":
    mcp.run(transport="stdio")
