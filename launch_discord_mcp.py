# SPDX-License-Identifier: GPL-3.0-or-later
"""Launcher for the Discord MCP server.

The bridge runs a bare `.py` command with the SAME interpreter it is running
under, so this works even on installs where only the `py` launcher exists and
no `python` is on PATH. Keeping the entry point here also means config.json
never has to hardcode an interpreter path.
"""
from zeroscript_discord.mcp_server import main

if __name__ == "__main__":
    main()
