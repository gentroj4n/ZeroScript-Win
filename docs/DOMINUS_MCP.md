# Dominus 2 / romcp Roblox Studio integration

ZeroScript-Win now registers the official Dominus MCP package in `config.json` alongside the existing Roblox Studio adapter and native Blender `bpy` server. The original project is closed-source; this repository integrates its published CLI and plugin rather than copying or reimplementing its internals.

Dominus has since been renamed **romcp**. The npm package remains `dominus-cli`, and the package retains the `dominus-mcp` command alias. The original forum post described 46 typed tools; the current v3 product may have a changed catalog, so treat the live MCP tool list as authoritative rather than hard-coding a count.

## Requirements

- Windows 10/11 with Roblox Studio.
- Node.js 20 or newer, including npm/npx.
- HTTP requests enabled in Roblox Studio if prompted by the plugin setup.
- A place open in the Studio instance you intend the agent to control.

## Install and connect

From the repository root in PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File .\setup_dominus.ps1
```

The script installs the current `dominus-cli` package globally, runs the supported interactive `romcp setup` flow, then runs `romcp doctor --wait 30`. Follow all prompts. Setup provisions the local Studio credential and installs the plugin. If Studio was already open, reload or fully restart it and enable the installed plugin if Roblox presents that prompt.

The ZeroScript bridge config separately launches the MCP stdio process using:

```json
{
  "dominus": {
    "command": "npx",
    "args": ["-y", "-p", "dominus-cli@latest", "dominus-mcp"]
  }
}
```

This entry is already present in the checked-in `config.json`; do not replace the full file or remove the existing `roblox` and `bpy` entries. The first MCP startup may download the package through npm. Restart ZeroScript's bridge after setup, then inspect the live tool catalogue and call `dominus_status` to verify the connection. If the package exposes a newer status/select-studio tool name, use the names actually advertised by the server.

## What this integration provides

Dominus/romcp is intended to expose typed tools for live DataModel inspection, stable instance references, script reads and revision-locked writes, UI snapshots, structured building operations, API documentation lookup, asset uploads, Open Cloud commerce, playtesting, input simulation, device emulation, and generated models. The available tools depend on the installed package and Studio plugin version.

The existing `roblox` adapter is intentionally retained. It may expose overlapping capabilities; choose one integration per operation where possible and verify changes using read-back tools. Do not assume the two servers share instance IDs or transaction state.

## Safety

- Keep the bridge and Studio plugin local. Do not expose local bridge ports to the network.
- Luau execution is powerful and should remain disabled until deliberately enabled in the plugin. Where execution is enabled, follow the plugin's confirmation requirements.
- Treat script edits, deletion, uploads, and Open Cloud commerce as consequential operations. Inspect targets first, use revision tokens where required, and read back results after mutations.
- Open Cloud actions require a correctly scoped Roblox API key. Never commit API keys, bridge credentials, or local secret stores.
- The setup script installs software and runs an interactive installer; review prompts before approving changes to other MCP client configurations.
- Dominus/romcp is third-party software and is not affiliated with Roblox Corporation or ZeroScript-Win. Review its current license, documentation, and release notes before using it with a production experience.

## Official references

- Roblox Developer Forum announcement: <https://devforum.roblox.com/t/dominus-2-an-mcp-server-for-roblox-studio-46-tools/4833955>
- Current documentation (formerly Dominus): <https://romcp.dev/docs/>
- npm package: <https://www.npmjs.com/package/dominus-cli>
