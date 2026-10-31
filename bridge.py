# # SPDX-License-Identifier: GPL-3.0-or-later
# bridge.py
# ──────────────────────────────────────────────────────────────────────────
#  ArcScript-Win Bridge
#  Local WebSocket <-> workspace tools and optional MCP integrations.
#  The browser extension talks to this over ws://127.0.0.1:<PORT>.
#
#  What this bridge exposes to the extension (aggregated into one tool catalogue):
#    - Workspace tools plus every MCP server declared in config.json, each
#      spawned as a stdio child and routed by tool name.
#
#  Design goals (robustness first):
#   - Each MCP stdio process is read by ONE dedicated thread; responses are
#     matched by JSON-RPC id (no "read the next line and hope" races).
#   - stderr is drained so a child never blocks on a full pipe.
#   - A dead server is auto-restarted and the failing call retried once.
#   - Tool calls are locked PER SERVER, so a slow server never blocks another.
#   - Every call ALWAYS produces a reply: a result OR a structured error.
#     Nothing ever hangs the agentic loop silently.
# ──────────────────────────────────────────────────────────────────────────
import asyncio
import base64
import json
import os
import hashlib
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import time

try:
    from workspace_agent.tools import Workspace, WorkspaceError
except Exception:
    Workspace = None
    WorkspaceError = RuntimeError

try:
    from workspace_agent.agent_control import AgentControlStore
except Exception:
    AgentControlStore = None

try:
    from workspace_agent.roblox_export import export_tree as export_roblox_tree
except Exception:
    export_roblox_tree = None

try:
    from workspace_agent import unity_recovery
except Exception:
    unity_recovery = None

try:
    from workspace_agent import unity_asset_inspector
except Exception:
    unity_asset_inspector = None

try:
    from workspace_agent import reconstruction_tracker
except Exception:
    reconstruction_tracker = None

try:
    from workspace_agent import code_intelligence
except Exception:
    code_intelligence = None

try:
    from workspace_agent import http_client
except Exception:
    http_client = None

try:
    from workspace_agent import browser_qa
except Exception:
    browser_qa = None

try:
    from workspace_agent import browser_server
except Exception:
    browser_server = None

try:
    from workspace_agent import assetripper_jobs
except Exception:
    assetripper_jobs = None

try:
    from workspace_agent import job_manager
except Exception:
    job_manager = None

try:
    # Sibling script (same folder as bridge.py, which Python puts on sys.path
    # automatically) - reused here purely to detect a Studio version bump
    # (see _current_studio_exe below), not to launch anything.
    import launch_studio_mcp as _studio_scan
except Exception:
    _studio_scan = None

try:
    import websockets
except ImportError:
    print("[bridge] Missing dependency. Run:  pip install websockets")
    sys.exit(1)

# Windows consoles often default to a legacy codepage (cp1252): printing
# non-ASCII text then raises UnicodeEncodeError INSIDE the WS handler, which
# kills the connection. Force UTF-8 (best effort). We also keep all console
# output strictly ASCII (no arrows / dots) so nothing garbles on a console that
# stayed on a legacy codepage anyway.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def _enable_ansi_colors():
    """On Windows, turn on ANSI escape processing so color codes render instead
    of printing as literal gibberish like "<ESC>[92m". Returns True on success."""
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        k = ctypes.windll.kernel32
        h = k.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if not k.GetConsoleMode(h, ctypes.byref(mode)):
            return False
        # ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        return bool(k.SetConsoleMode(h, mode.value | 0x0004))
    except Exception:
        return False


HOST = "127.0.0.1"
# Keep in sync with zeroscript-extension/manifest.json "version" - printed at
# startup so a user's terminal output alone tells us which build they're on.
BRIDGE_VERSION = "1.11.0"
PORT = int(os.environ.get("ZS_BRIDGE_PORT", "17613"))
HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")

# Conventional ID for the optional Roblox Studio adapter. Workspace tools work
# independently, even when this server is absent or Studio is closed.
PRIMARY_SERVER_ID = "roblox"

if _enable_ansi_colors():
    C = {
        "reset": "\033[0m", "dim": "\033[2m", "gr": "\033[92m",
        "yl": "\033[93m", "rd": "\033[91m", "cy": "\033[96m",
        # Bold white-on-red: for a non-technical user, an "ACTION NEEDED" step
        # must look nothing like the routine cyan/yellow status noise around
        # it, or it gets scrolled past unread (seen live 2026-07-13 - the
        # toggle instruction and the boot banner's own yellow re-explanation
        # of the SAME step were visually indistinguishable). Bright-yellow-bg
        # with black text was tried first but reads as low-contrast/washed
        # out on several real terminal color schemes (also seen live) - white
        # on red is the universal high-contrast "act now" pairing.
        "act": "\033[1m\033[97m\033[41m",
    }
else:
    C = {k: "" for k in ("reset", "dim", "gr", "yl", "rd", "cy", "act")}

# Every run appends here (never truncated), so a whole test session - across
# multiple restarts - stays in one file the user can just send us. Each
# process start writes a banner (see main()) so restarts are easy to spot.
LOGS_DIR = os.path.join(HERE, "logs")
os.makedirs(LOGS_DIR, exist_ok=True)
LOG_PATH = os.path.join(LOGS_DIR, "bridge_debug.log")
try:
    _log_file = open(LOG_PATH, "a", encoding="utf-8", errors="replace")
except Exception:
    _log_file = None


class _Spinner:
    """Terminal-only progress indicator for waits that can run several seconds
    (server launch/handshake, Studio attach grace period) so the console never
    just sits there looking dead - the #1 thing that makes a user assume the
    bridge hung and close the window. Purely cosmetic: writes over its own line
    with \\r, never touches bridge_debug.log, and is skipped entirely when
    stdout isn't a real console (redirected to a file, no ANSI)."""
    FRAMES = "|/-\\"
    # Only ONE spinner may animate at a time: server launches now run in
    # PARALLEL (see MCPManager.start_all), and several spinners fighting over
    # the same console line with \r produced interleaved garbage. Whoever
    # acquires this lock animates; the others silently skip (the log lines
    # around them still tell the story).
    _active = threading.Lock()

    def __init__(self, label):
        self.label = label
        self._stop = threading.Event()
        self._thread = None
        self._owns_lock = False

    def __enter__(self):
        if sys.stdout.isatty() and _Spinner._active.acquire(blocking=False):
            self._owns_lock = True
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.0)
            # Wipe the spinner line so the next log() line doesn't get glued
            # onto trailing spinner characters.
            print("\r" + " " * (len(self.label) + 4) + "\r", end="", flush=True)
        if self._owns_lock:
            _Spinner._active.release()

    def _run(self):
        i = 0
        while not self._stop.is_set():
            frame = self.FRAMES[i % len(self.FRAMES)]
            print(f"\r{C['dim']}{self.label} {frame}{C['reset']}", end="", flush=True)
            i += 1
            self._stop.wait(0.15)


def _clear_spinner_line():
    """Wipe whatever a live _Spinner (running on its own thread, mid-frame) left
    on the current console line via bare \\r writes, so the next print() below
    doesn't get glued onto its trailing characters - seen live 2026-07-14: an
    action_banner() fired while '[roblox] starting... -' was still mid-line and
    the red box rendered smashed onto it instead of starting on a fresh line.
    \\033[K (clear to end of line) doesn't depend on knowing the spinner's label
    length the way Spinner.__exit__'s own wipe does."""
    if sys.stdout.isatty():
        print("\r\033[K", end="", flush=True)


def log(msg, color="dim", terminal=True):
    """terminal=False: written to bridge_debug.log only, not the console. Use
    for noisy/technical detail (raw stderr from child MCP servers, per-call
    traces) that would bury the handful of lines a non-technical user actually
    needs to read. Nothing is ever lost - it all still lands in the file."""
    if terminal:
        _clear_spinner_line()
        ts = time.strftime("%H:%M:%S")
        print(f"{C['dim']}{ts}{C['reset']} {C.get(color,'')}{msg}{C['reset']}", flush=True)
    if _log_file:
        try:
            _log_file.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
            _log_file.flush()
        except Exception:
            pass


def action_banner(lines):
    """Print a step the USER must physically go do, styled so it cannot be
    mistaken for routine status/warning noise (see the 'act' color above).
    Framed with blank lines so it visually stands alone in a scrolling
    terminal - a non-technical user should be able to glance at the window
    and immediately spot this without reading everything above it.

    Every line (header, content, footer) is padded to the SAME width so the
    yellow block renders as one clean rectangle - an earlier version padded
    each line to a fixed guess independently, which produced a ragged block
    with mismatched edges on a real console (seen live 2026-07-13)."""
    header = "ACTION NEEDED"
    width = max([len(header) + 8] + [len(ln) for ln in lines]) + 2
    top = f">>> {header} " + ">" * max(0, width - len(header) - 5)
    _clear_spinner_line()
    print()
    print(f"{C['act']}  {top.ljust(width)}{C['reset']}")
    for ln in lines:
        print(f"{C['act']}  {ln.ljust(width)}{C['reset']}")
    print(f"{C['act']}  {'>' * width}{C['reset']}")
    print()
    if _log_file:
        try:
            _log_file.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} ACTION NEEDED: "
                             f"{' | '.join(lines)}\n")
            _log_file.flush()
        except Exception:
            pass


# Roblox Studio exposes its built-in MCP server on this loopback port. StudioMCP
# (and our bridge, via it) reaches Studio through it.
STUDIO_MCP_PORT = 13469


def _port_owner(port):
    """(pid, name, path) of the process LISTENING on `port`, or None. Win32 only."""
    if sys.platform != "win32":
        return None
    # BOTH stacks: "-p TCP" alone is IPv4-only, and a squatter listening on
    # [::1]:<port> (IPv6 loopback) was then completely invisible to this probe
    # even while Get-NetTCPConnection showed it plainly (the likely reason the
    # boot-time squatter check stayed silent on a machine where ropilot
    # provably held the port - see the 2026-07-13 live report).
    out = ""
    for proto in ("TCP", "TCPv6"):
        try:
            out += subprocess.run(
                ["netstat", "-ano", "-p", proto],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=8,
            ).stdout
        except Exception:
            pass
    if not out:
        return None
    pid = None
    # v4 lines end the local address in ":<port>", v6 in "]:<port>" - matching
    # on the ":<port> " suffix (with the column gap) covers both shapes.
    needle = f":{port} "
    for line in out.splitlines():
        if "LISTENING" in line and needle in line:
            parts = line.split()
            if parts and parts[-1].isdigit():
                pid = parts[-1]
                break
    if not pid:
        return None
    name, path = "?", ""
    try:
        ps = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"$p=Get-Process -Id {pid} -ErrorAction SilentlyContinue; "
             f"if($p){{$p.Name; $p.Path}}"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=8,
        ).stdout.splitlines()
        ps = [l.strip() for l in ps if l.strip()]
        if ps:
            name = ps[0]
            path = ps[1] if len(ps) > 1 else ""
    except Exception:
        pass
    return (pid, name, path)


def _roblox_studio_app_running():
    """True/False whether a real Roblox Studio window process exists, or None
    if this can't be determined (non-Windows, or the check itself failed)."""
    if sys.platform != "win32":
        return None
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq RobloxStudioBeta.exe"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=8,
        ).stdout
    except Exception:
        return None
    return "RobloxStudioBeta.exe" in out


def _kill_orphan_studio_mcp():
    """Kill leftover StudioMCP.exe processes from a PREVIOUS session/crash.

    StudioMCP.exe is Roblox's own MCP proxy; launch_studio_mcp.py spawns one
    as a direct child every time the bridge starts. If an earlier restart's
    tree-kill missed the grandchild (a reparenting race), or Studio itself
    crashed and left its own StudioMCP.exe running (seen live 2026-07-11:
    RobloxStudioBeta.exe zombied after two RobloxCrashHandler.exe events),
    the orphan keeps LISTENING on Studio's MCP port. Every StudioMCP.exe we
    launch afterward - even a freshly restarted one - just connects to that
    zombie instead of a real Studio, so the bridge reports "Studio connected"
    forever even with Studio fully closed. studio_watch's auto-restart cannot
    fix this on its own: restarting our proxy still lands on the same zombie.

    Only acts when NO real Studio app is running at all - in that state any
    existing StudioMCP.exe is unambiguously orphaned (a legitimate one only
    exists to serve a live Studio), so it is safe to auto-kill without asking.
    If Studio IS running (or this can't be determined), this is a no-op: a
    live StudioMCP.exe might be legitimately serving it, so nothing is
    touched - this must never risk killing a working connection.
    """
    if sys.platform != "win32":
        return
    if _roblox_studio_app_running() is not False:
        return
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq StudioMCP.exe"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=8,
        ).stdout
    except Exception:
        return
    if "StudioMCP.exe" not in out:
        return
    log("Found leftover StudioMCP.exe process(es) with no Roblox Studio running - "
        "cleaning them up (known cause of a phantom 'Studio connected' state).", "yl")
    try:
        subprocess.run(["taskkill", "/F", "/IM", "StudioMCP.exe"],
                       capture_output=True, text=True, timeout=8)
    except Exception as e:
        log(f"could not clean up orphaned StudioMCP.exe: {e}", "rd")


def _descendant_pids(root_pid):
    """Set of PIDs = root_pid + every descendant, or None if the process tree
    could not be read (in which case callers must NOT make kill decisions)."""
    if sys.platform != "win32":
        return None
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process | ForEach-Object "
             "{ \"$($_.ProcessId) $($_.ParentProcessId)\" }"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=10,
        ).stdout
    except Exception:
        return None
    children = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            children.setdefault(int(parts[1]), []).append(int(parts[0]))
    if not children:
        return None
    pids = {int(root_pid)}
    stack = [int(root_pid)]
    while stack:
        for c in children.get(stack.pop(), []):
            if c not in pids:
                pids.add(c)
                stack.append(c)
    return pids


def _reclaim_studio_port(client):
    """Kill a StudioMCP.exe that owns Studio's MCP port but is NOT our own child.

    The deadlock this breaks (reported live, survives every restart combo):
    a zombie StudioMCP.exe from a crashed session keeps LISTENING on 13469.
    The user reopens Studio -> its MCP plugin does its ONE-SHOT registration
    against the ZOMBIE (wasted). The user restarts the bridge -> Studio is now
    running, so _kill_orphan_studio_mcp's safety guard skips the cleanup, and
    check_studio_port waves the zombie through too (its path IS under Roblox).
    Our fresh StudioMCP can't own the port, Studio never re-registers on its
    own -> 0 tools forever, no restart order can fix it by hand.

    Ownership is decided by PID, not heuristics: we know the PID of the
    launcher we spawned (client.proc), so a StudioMCP.exe holding the port
    outside that process tree is a leftover by definition - Studio open or
    not. If the process tree can't be read, we do nothing (never risk killing
    our own healthy child on bad data). Returns True if a zombie was killed;
    the caller must then restart the roblox proxy (safe here even with Studio
    open: the plugin's single registration already went to the zombie, so
    there is no attempt left for a restart to collide with) AND tell the user
    to open Assistant Settings > MCP Servers so the plugin re-registers.
    """
    owner = _port_owner(STUDIO_MCP_PORT)
    if not owner:
        return False
    pid, name, path = owner
    # Only ever kill a StudioMCP.exe. Studio itself holding the port is fine;
    # a non-Roblox squatter is check_studio_port's (interactive) job.
    if "studiomcp" not in (name or "").lower():
        return False
    try:
        pid_i = int(pid)
    except (TypeError, ValueError):
        return False
    if client is not None and client.proc is not None and client.is_alive():
        tree = _descendant_pids(client.proc.pid)
        if tree is None or pid_i in tree:
            return False  # ours, or unknowable - leave it alone
    log(f"port {STUDIO_MCP_PORT} is held by a StudioMCP.exe (pid {pid_i}) that this "
        "bridge did NOT launch - a leftover from a previous session. Studio "
        "registered to it, so our proxy sees 0 tools.", "yl")
    try:
        subprocess.run(["taskkill", "/F", "/PID", str(pid_i)],
                       capture_output=True, text=True, timeout=8)
    except Exception as e:
        log(f"could not kill the leftover StudioMCP.exe: {e}", "rd")
        return False
    log(f"killed the leftover StudioMCP.exe (pid {pid_i}) to free Studio's MCP port.", "cy")
    return True


def _process_cmdline(pid):
    """Full command line of `pid`, or "" if it can't be read. Win32 only.

    Used to tell OUR OWN kind of process (a python running bridge.py) apart
    from an unrelated app that merely happens to listen on the same port -
    the process NAME is just "python"/"py"/"pythonw", far too generic to kill
    on. The command line is what proves it is a leftover bridge."""
    if sys.platform != "win32":
        return ""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-CimInstance Win32_Process -Filter \"ProcessId={pid}\" "
             f"-ErrorAction SilentlyContinue).CommandLine"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=8,
        ).stdout
    except Exception:
        return ""
    return (out or "").strip()


def _reclaim_bridge_port():
    """Free OUR OWN listen port (17613) from a leftover bridge before we bind.

    The common failure (reported live, WinError 10048 on bind): the user
    relaunches start.bat while an earlier bridge.py is still running - window
    closed with the X instead of Ctrl+C, a previous crash that left a detached
    python, or a double double-click. The old process still holds the port, so
    websockets.serve() dies on bind with a cryptic (localised) OSError and the
    whole bridge exits code 1.

    We reuse _port_owner (already generic over the port) and only ever kill a
    process we can PROVE is another bridge.py - never a same-name innocent
    (some unrelated python listening on 17613): the guard is the command line
    containing "bridge.py", plus an explicit self-exclusion by PID. Anything
    else (a non-python app, or a python whose cmdline we can't read) is left
    alone and surfaced to the user by the caller's friendly bind-error path.
    Returns True if a leftover bridge was killed."""
    owner = _port_owner(PORT)
    if not owner:
        return False
    pid, name, path = owner
    try:
        pid_i = int(pid)
    except (TypeError, ValueError):
        return False
    if pid_i == os.getpid():
        return False  # never kill ourselves (defensive; we haven't bound yet)
    # Must look like a python interpreter AND be running bridge.py. Killing on
    # the port alone would murder whatever legitimately owns 17613.
    if "python" not in (name or "").lower() and "py" != (name or "").lower():
        return False
    cmdline = _process_cmdline(pid_i)
    if "bridge.py" not in cmdline.lower():
        log(f"port {PORT} is held by pid {pid_i} ('{name}') but it does not look "
            f"like an ArcScript bridge - leaving it alone.", "yl")
        return False
    log(f"port {PORT} is held by a leftover ArcScript bridge (pid {pid_i}) from a "
        "previous session - killing it so this one can start.", "yl")
    try:
        subprocess.run(["taskkill", "/F", "/PID", str(pid_i)],
                       capture_output=True, text=True, timeout=8)
    except Exception as e:
        log(f"could not kill the leftover bridge (pid {pid_i}): {e}", "rd")
        return False
    log(f"killed the leftover bridge (pid {pid_i}); the port is free now.", "cy")
    return True


def _kill_port_squatter():
    """Kill a NON-Roblox process holding Studio's MCP port, no questions asked.

    Called only when the child's stderr has PROVEN the port is hijacked (see
    MCPClient.saw_foreign_ws_host - StudioMCP connected to a foreign host and
    could not parse its protocol; the ropilot case). At that point there is no
    ambiguity left to justify check_studio_port's interactive prompt, and the
    prompt was itself a trap: many users never answer it, and the one-shot boot
    check often runs a beat before a background helper (ropilot) grabs the
    port. Here we have hard evidence, so kill the squatter outright. Returns
    (killed, name) so the caller can tell the user which app to uninstall /
    remove from startup, since it will otherwise reclaim the port on next boot.
    """
    owner = _port_owner(STUDIO_MCP_PORT)
    if owner:
        pid, name, path = owner
        if "roblox" in (path or "").lower() or "studiomcp" in (name or "").lower():
            return False, None  # legitimate Studio-side owner; not a squatter
        log(f"port {STUDIO_MCP_PORT} is hijacked by '{name}' (pid {pid}, {path}).", "yl")
        log("    StudioMCP connected to it instead of Roblox Studio - that is why "
            "there are 0 tools.", "yl")
        try:
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True, text=True, timeout=8)
        except Exception as e:
            log(f"could not kill '{name}': {e}", "rd")
            return False, name
        log(f"killed '{name}' so Studio can use the port.", "cy")
        return True, name
    # We could NOT resolve who owns the port, yet StudioMCP's stderr proved the
    # port is hijacked (this function is only called under that proof). This is
    # the state that used to fail SILENTLY: _port_owner returning None (e.g. a
    # squatter listening on IPv6 loopback that an IPv4-only netstat missed, or
    # any netstat quirk) left the user staring at 0 tools with no explanation.
    # Never be silent here. Try a name-based fallback for the known offender
    # (ropilot ships a background helper that squats this port), then always
    # tell the user what we know.
    log(f"port {STUDIO_MCP_PORT} is hijacked (StudioMCP could not talk to Roblox "
        "Studio on it) but the owning process could not be identified by port.", "yl")
    # ropilot is a multi-process app (validated live 2026-07-13): the port is
    # held by ropilot-infra-helper.exe, supervised by ropilot-infra.exe. Kill
    # both so the supervisor can't just respawn the helper and re-grab the port.
    killed_name = None
    for img in ("ropilot-infra-helper.exe", "ropilot-infra.exe", "ropilot.exe"):
        try:
            res = subprocess.run(["taskkill", "/F", "/IM", img],
                                 capture_output=True, text=True, timeout=8)
        except Exception:
            continue
        if res.returncode == 0:
            killed_name = img
            log(f"killed '{img}' (known port squatter) so Studio can use the port.", "cy")
    if killed_name:
        return True, killed_name
    log("    Could not auto-kill it. Find it manually: run  netstat -ano | "
        f"findstr {STUDIO_MCP_PORT}  then end that PID in Task Manager.", "yl")
    return False, None


def _print_squatter_hint(name):
    """After killing a port squatter (e.g. ropilot), tell the user how to stop
    it coming back - it is a background helper that respawns on the next boot
    and re-grabs the port before Studio, which is why a PC reboot never fixed
    this class of 0-tools report."""
    app = name or "the other app"
    action_banner([
        f"'{app}' fights Roblox Studio for its connection - it will keep",
        "coming back after every restart until you remove it.",
        f"1. Uninstall '{app}' (or remove it from Windows startup).",
        "2. In Roblox Studio: Assistant Settings > MCP Servers,",
        "   turn OFF then back ON 'Enable Studio as MCP server'.",
    ])


def _print_reregister_hint():
    """The one user action that completes a zombie-kill recovery: Studio's MCP
    plugin registers only once per boot and that attempt went to the zombie,
    so after the kill + proxy restart the user must make it register again."""
    # Opening the panel alone is technically enough to re-register, but we tell
    # the user to toggle OFF/ON to be sure - a toggle strictly implies opening
    # the panel, so it can never do less, and it removes any ambiguity about
    # whether "just looking at it" counted. Same wording as the squatter/no-place
    # banners so all three read as one identical instruction, not three variants.
    action_banner([
        "Go to Roblox Studio now.",
        "Turn OFF then back ON: Assistant Settings > MCP Servers",
        "         > 'Enable Studio as MCP server'",
        "Wait about 10 seconds - this window will turn green.",
    ])


def check_studio_port():
    """Warn (and optionally kill) a NON-Roblox process squatting Studio's MCP port.

    A third-party tool (e.g. "ropilot") that binds 13469 before Studio does
    hijacks the MCP channel: StudioMCP connects to IT instead of Studio, the
    handshake succeeds but tools/list never answers -> the bridge sees 0 tools.
    This is silent and brutal to diagnose, so we surface it up front.
    """
    owner = _port_owner(STUDIO_MCP_PORT)
    if not owner:
        return False
    pid, name, path = owner
    # The legitimate holder is Studio itself / a Roblox helper: its path lives
    # under a "...\Roblox\..." folder. Anything else is an intruder.
    if "roblox" in (path or "").lower():
        return False
    where = path or name
    log(f"port {STUDIO_MCP_PORT} (Studio's MCP port) is held by a non-Roblox process:", "yl")
    log(f"    {name} (pid {pid})  {where}", "yl")
    log("    This will block Studio's tools (the bridge will see 0 tools).", "yl")
    try:
        ans = input("    Kill this process so Studio can use the port? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        ans = ""
    if ans in ("y", "yes", "o", "oui"):
        try:
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True, text=True, timeout=8)
            log(f"killed {name} (pid {pid}). Studio can use the port now.", "cy")
            # Tell the user the finishing step IMMEDIATELY, here, instead of only
            # after the ~48s server-launch grace loop that follows: killing the
            # squatter frees the port, but Studio's MCP plugin registers only
            # once per boot and that attempt already went to the squatter, so it
            # will NOT re-attach on its own - a toggle is needed. Printing this
            # now (not 48s later, after start_all's grace loop) is what turns a
            # ~1-minute "why is nothing happening" wait into an act-right-away
            # instruction. Uses action_banner (not log) so a non-technical user
            # visually cannot miss it among the surrounding status lines - seen
            # live indistinguishable when both used the same plain color.
            action_banner([
                "Go to Roblox Studio now.",
                "Turn OFF then back ON: Assistant Settings > MCP Servers",
                "         > 'Enable Studio as MCP server'",
                "Wait about 10 seconds - this window will turn green.",
            ])
            return True  # a squatter WAS killed -> Studio must reclaim the port
        except Exception as e:
            log(f"could not kill it: {e}", "rd")
    else:
        log("left it running. Close it yourself, then restart the bridge.", "yl")
    return False


_TRANSIENT_STUDIO_MARKERS = (
    "no roblox studio instance", "no active studio", "studio instance is connect",
    "studio instance connected", "not connected to", "no studio instance",
)


def _looks_like_transient_studio_drop(text):
    low = (text or "").lower()
    return any(m in low for m in _TRANSIENT_STUDIO_MARKERS)


# ── config.json read / write (for extension-driven add/remove) ──────────────
def _read_config():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            if isinstance(cfg, dict):
                cfg.setdefault("mcpServers", {})
                return cfg
        except Exception as e:
            log(f"config.json unreadable ({e}) - starting from a fresh one", "yl")
    return {"mcpServers": {}}


def _write_config(cfg):
    """Atomic write so a crash mid-write never leaves a truncated config.json."""
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG_PATH)


def config_add_server(server_id, command, args=None, env=None):
    """Add or replace a configured MCP server. Returns (ok, error)."""
    sid = (server_id or "").strip()
    if not sid:
        return False, "server id is required"
    if not (command or "").strip():
        return False, "a command is required"
    cfg = _read_config()
    spec = {"command": command.strip(), "args": list(args or [])}
    if env:
        spec["env"] = dict(env)
    cfg["mcpServers"][sid] = spec
    try:
        _write_config(cfg)
    except Exception as e:
        return False, f"could not write config.json: {e}"
    return True, None


def config_remove_server(server_id):
    """Remove a configured MCP server from config.json."""
    sid = (server_id or "").strip()
    cfg = _read_config()
    if sid not in cfg.get("mcpServers", {}):
        return False, f"server '{sid}' is not in the config"
    del cfg["mcpServers"][sid]
    try:
        _write_config(cfg)
    except Exception as e:
        return False, f"could not write config.json: {e}"
    return True, None


def restart_self():
    """Replace this process with a fresh one so config.json is reloaded from
    scratch. Children are killed first to free their stdio pipes / ports before
    the new instance claims them. Never returns on success (os.execv)."""
    log("restarting bridge to load new server config...", "yl")
    try:
        for c in mgr.clients.values():
            c.stop()
    except Exception:
        pass
    if _log_file:
        try:
            _log_file.flush()
        except Exception:
            pass
    # sys.argv[0] may be relative ('bridge.py'); make it absolute so the restart
    # works regardless of the current working directory.
    argv = list(sys.argv)
    script = os.path.abspath(argv[0]) if argv else os.path.abspath(__file__)
    argv = [script] + argv[1:]
    try:
        os.execv(sys.executable, [sys.executable] + argv)
    except Exception as e:
        # execv failed (rare) - fall back to spawning a detached copy and exiting
        # so the user still ends up with a running, up-to-date bridge.
        log(f"in-place restart failed ({e}); spawning a fresh bridge...", "rd")
        try:
            subprocess.Popen([sys.executable] + argv, cwd=HERE)
        except Exception as e2:
            log(f"could not spawn a fresh bridge: {e2} - please restart it manually", "rd")
        os._exit(0)


# ══════════════════════════════════════════════════════════════════════════
#  HARDENED MCP CLIENT  (one per server in config.json)
# ══════════════════════════════════════════════════════════════════════════
class MCPClient:
    def __init__(self, server_id, command, args, env=None):
        self.id = server_id
        self.command = command
        self.args = list(args or [])
        self.env = env or {}
        self.proc = None
        self.req_id = 1
        self.write_lock = threading.Lock()
        self.call_lock = threading.Lock()   # serialize tool calls (single stdio pipe)
        self.pending = {}                    # id -> queue.Queue (one slot)
        self.pend_lock = threading.Lock()
        self.tools_cache = []
        self.start_lock = threading.Lock()
        self._reader_thread = None
        # Crash-loop forensics (read by server_watch). The auto-restart used to
        # hide a server that something else kills over and over: the terminal
        # showed an endless quiet restart cycle with no explanation at all. We
        # keep just enough state to NAME the problem in the terminal instead:
        #  - last_exit: exit code from the final _reader EOF (crash vs kill hint)
        #  - stderr_tail: the last few stderr lines (usually the actual reason -
        #    port bind failure, missing dependency, crash trace)
        #  - restart_times: recent auto-restart timestamps (loop detector input)
        #  - loop_warned_at: throttle so the big red banner prints once per
        #    cooldown, not every 5s poll
        self.last_exit = None
        self.stderr_tail = []
        self.restart_times = []
        self.loop_warned_at = 0.0
        # Set when the configured command itself couldn't be launched at all
        # (e.g. 'uvx' not installed / not on PATH). This is NOT a crash - the
        # process never existed, so last_exit/stderr_tail stay empty and the
        # generic crash-loop banner used to print "the server printed no error
        # output before dying", which is misleading for a config problem the
        # user can fix in seconds. Kept across restarts so the banner can name
        # the real cause instead.
        self.start_error = None
        # Set when StudioMCP's stderr shows it connected to a FOREIGN WS host on
        # Studio's MCP port (not Studio). The unmistakable signature is a parse
        # error on the host's messages ("missing field `type`") - Studio speaks
        # the expected protocol, a squatter like ropilot speaks its own. This is
        # a timing-independent proof that the port is hijacked, unlike the
        # one-shot check_studio_port() boot probe which can miss a squatter that
        # grabs the port a moment after boot (seen live 2026-07-13: ropilot took
        # the port ~1s after the boot check ran, so nothing was flagged).
        self.saw_foreign_ws_host = False

    # ── lifecycle ─────────────────────────────────────────────────────────
    def _resolve(self, s):
        return os.path.expandvars(os.path.expanduser(str(s)))

    def start(self):
        with self.start_lock:
            if self.is_alive():
                return
            cmd = [self._resolve(self.command)] + [self._resolve(a) for a in self.args]
            # A bare .py command (relative paths resolve against the bridge dir)
            # is run with the SAME interpreter the bridge itself uses, so it works
            # even on installs where only the `py` launcher exists (no `python`
            # on PATH). This is how the Studio MCP launcher is wired by default.
            if cmd[0].lower().endswith(".py"):
                script = cmd[0]
                if not os.path.isabs(script):
                    script = os.path.join(HERE, script)
                cmd = [sys.executable, script] + cmd[1:]
            # On Windows, npx/npm/yarn/pnpm/bunx are .cmd shims that Popen can't
            # launch directly (WinError 2). Run them through cmd.exe so any
            # node-based MCP server "just works" from config.json.
            if sys.platform == "win32":
                base = os.path.basename(cmd[0]).lower()
                if base in ("npx", "npm", "yarn", "pnpm", "bunx"):
                    cmd = ["cmd.exe", "/c"] + cmd
            env = dict(os.environ)
            for k, v in self.env.items():
                env[k] = self._resolve(v)
            log(f"[{self.id}] launching  ({' '.join(cmd)})", "cy")
            with _Spinner(f"    [{self.id}] starting..."):
                try:
                    self.proc = subprocess.Popen(
                        cmd,
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        bufsize=1,
                        encoding="utf-8",
                        errors="replace",
                        cwd=HERE,
                        env=env,
                    )
                except FileNotFoundError:
                    # The OS couldn't find cmd[0] at all - this is a config
                    # problem (missing dependency, typo, not on PATH), not a
                    # transient crash. Auto-restart will keep retrying (the
                    # user may install it later), but name the real cause so
                    # it doesn't just look like an endless silent restart loop.
                    self.start_error = (
                        f"command not found: '{cmd[0]}' - is it installed and on PATH? "
                        f"(configured for server '{self.id}' in config.json)"
                    )
                    log(f"[{self.id}] {self.start_error}", "rd")
                    raise
                except OSError as e:
                    self.start_error = f"could not launch '{cmd[0]}': {e}"
                    log(f"[{self.id}] {self.start_error}", "rd")
                    raise
                else:
                    self.start_error = None
                with self.pend_lock:
                    self.pending.clear()
                self.saw_foreign_ws_host = False  # fresh process, fresh verdict
                self._reader_thread = threading.Thread(target=self._reader, args=(self.proc,), daemon=True)
                self._reader_thread.start()
                threading.Thread(target=self._stderr_drain, args=(self.proc,), daemon=True).start()

                # MCP handshake.
                self._request("initialize", {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "arcscript-bridge", "version": "1.0"},
                }, timeout=30)
                self._notify("notifications/initialized")
                # Some MCP servers (notably Roblox's StudioMCP) advertise 0 tools at
                # the instant initialize returns, because they connect to their
                # backend (the running Studio) a moment AFTER the stdio handshake.
                # A single tools/list then caches an empty list forever. So if we
                # get nothing, retry for a few seconds to let the backend attach.
                # Short per-attempt timeout so the bridge never looks frozen if the
                # server stays silent (e.g. Studio not open yet); ~12s total budget.
                for _ in range(12):
                    if self.refresh_tools(timeout=3):
                        break
                    if not self.is_alive():
                        break
                    time.sleep(1.0)
            log(f"[{self.id}] MCP server up  ({len(self.tools_cache)} tools advertised)", "cy")

    def is_alive(self):
        return self.proc is not None and self.proc.poll() is None

    def restart(self):
        log(f"[{self.id}] restarting...", "yl")
        self.stop()
        time.sleep(0.4)
        self.start()

    def stop(self):
        with self.pend_lock:
            for q in self.pending.values():
                try:
                    q.put_nowait(None)
                except Exception:
                    pass
            self.pending.clear()
        if self.proc:
            # proc.terminate() (TerminateProcess on Windows) only kills THIS
            # pid. Our command is often a wrapper (e.g. launch_studio_mcp.py)
            # that Popen()s a real child (StudioMCP.exe) to own the stdio
            # pipes - terminate() would leave that child orphaned, still bound
            # to Studio's MCP port, fighting the next restart's fresh instance.
            # taskkill /T kills the whole tree.
            try:
                if sys.platform == "win32":
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(self.proc.pid)],
                        capture_output=True, timeout=8,
                    )
                else:
                    self.proc.terminate()
            except Exception:
                pass
        self.proc = None

    # ── io threads ────────────────────────────────────────────────────────
    def _reader(self, proc):
        stream = proc.stdout
        while True:
            try:
                line = stream.readline()
            except Exception:
                break
            if line == "":  # EOF -> process exited
                break
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except Exception:
                continue  # stray non-JSON log on stdout
            mid = msg.get("id")
            if mid is None:
                continue  # server notification, nothing waits on it
            with self.pend_lock:
                q = self.pending.get(mid)
            if q is not None:
                try:
                    q.put_nowait(msg)
                except Exception:
                    pass
        code = proc.poll()
        self.last_exit = code  # kept for the crash-loop banner in server_watch
        log(f"[{self.id}] stdout closed (process ended, exit code {code})", "rd")
        with self.pend_lock:
            for q in self.pending.values():
                try:
                    q.put_nowait(None)
                except Exception:
                    pass

    def _stderr_drain(self, proc):
        # Surface the child's stderr instead of silently discarding it - this
        # is often the ONLY clue why a server died (crash trace, port bind
        # failure, missing Studio, etc).
        try:
            for line in iter(proc.stderr.readline, ""):
                line = line.rstrip()
                if line:
                    # Ring buffer of the last stderr lines: when the server
                    # enters a crash loop, these are printed in the terminal
                    # banner - they are usually the only real explanation
                    # (port already in use, module not found, crash trace).
                    self.stderr_tail.append(line)
                    if len(self.stderr_tail) > 8:
                        self.stderr_tail.pop(0)
                    # Squatter signature: StudioMCP connected to a non-Studio host
                    # on the MCP port and can't parse its protocol. This is the
                    # ropilot hijack, timing-independent (see saw_foreign_ws_host).
                    low = line.lower()
                    if "failed to parse message from ws host" in low or "missing field `type`" in low:
                        self.saw_foreign_ws_host = True
                    log(f"[{self.id}] stderr: {line}", "yl", terminal=False)
        except Exception:
            pass

    # ── jsonrpc ───────────────────────────────────────────────────────────
    def _next_id(self):
        with self.write_lock:
            rid = self.req_id
            self.req_id += 1
            return rid

    def _notify(self, method, params=None):
        payload = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        with self.write_lock:
            self.proc.stdin.write(json.dumps(payload) + "\n")
            self.proc.stdin.flush()

    def _request(self, method, params, timeout):
        if not self.is_alive():
            raise RuntimeError(f"server '{self.id}' is not running")
        rid = self._next_id()
        q = queue.Queue(maxsize=1)
        with self.pend_lock:
            self.pending[rid] = q
        try:
            payload = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}
            with self.write_lock:
                self.proc.stdin.write(json.dumps(payload) + "\n")
                self.proc.stdin.flush()
            try:
                return q.get(timeout=timeout)
            except queue.Empty:
                return None
        finally:
            with self.pend_lock:
                self.pending.pop(rid, None)

    # ── high-level ────────────────────────────────────────────────────────
    def refresh_tools(self, timeout=20):
        msg = self._request("tools/list", {}, timeout=timeout)
        if msg and "result" in msg:
            self.tools_cache = msg["result"].get("tools", [])
        return self.tools_cache

    def call_tool(self, name, arguments, timeout):
        """Returns {"text":..., "images":[...]}. Raises on error/timeout."""
        with self.call_lock:
            for attempt in (1, 2):
                if not self.is_alive():
                    self.restart()
                msg = self._request("tools/call",
                                    {"name": name, "arguments": arguments}, timeout)
                if msg is None:
                    if not self.is_alive():
                        self.restart()
                        msg = self._request("tools/call",
                                            {"name": name, "arguments": arguments}, timeout)
                    if msg is None:
                        raise TimeoutError(
                            f"No response from server '{self.id}' after {timeout}s.")
                if msg.get("error"):
                    err = msg["error"]
                    err_text = err.get("message", json.dumps(err))
                    if attempt == 1 and _looks_like_transient_studio_drop(err_text):
                        log(f"[{self.id}] {name}: transient Studio drop, retrying once...", "yl")
                        time.sleep(1.5)
                        continue
                    raise RuntimeError(err_text)
                content = msg.get("result", {}).get("content", [])
                text = "\n".join(it.get("text", "") for it in content if it.get("type") == "text")
                images = [{"data": it["data"], "mimeType": it.get("mimeType", "image/jpeg")}
                          for it in content if it.get("type") == "image" and it.get("data")]
                if not text and not images and content:
                    text = json.dumps(content)[:4000]
                # Studio's own MCP proxy briefly loses its binding to the Studio
                # app every few seconds on some machines (seen live: repeated
                # "Bound studio ... disconnected for proxy ..." stderr, self-
                # healing within ~1-4s). A tool call landing in that window
                # fails with a "no Studio instance connected" style message
                # even though Studio is genuinely open - confirmed live via
                # start_stop_play. One short retry rides through it instead of
                # surfacing a spurious error to the user.
                if attempt == 1 and _looks_like_transient_studio_drop(text):
                    log(f"[{self.id}] {name}: transient Studio drop, retrying once...", "yl")
                    time.sleep(1.5)
                    continue
                return {"text": text, "images": images}


# ══════════════════════════════════════════════════════════════════════════
#  Hot reload  - reimport workspace_agent modules in the LIVE bridge process.
# ══════════════════════════════════════════════════════════════════════════
_WORKSPACE_RELOAD_TARGETS = (
    "workspace_agent",
    "workspace_agent.tools",
    "workspace_agent.http_client",
    "workspace_agent.code_intelligence",
    "workspace_agent.browser_qa",
    "workspace_agent.browser_server",
    "workspace_agent.assetripper_jobs",
    "workspace_agent.job_manager",
    "workspace_agent.unity_recovery",
    "workspace_agent.unity_asset_inspector",
    "workspace_agent.reconstruction_tracker",
    "workspace_agent.roblox_export",
    "workspace_agent.agent_control",
)


def _reload_workspace_modules():
    """Reload workspace_agent submodules in the running bridge process.

    Re-executes each module from disk (importlib.reload) and updates the
    module-level helper globals that _call_workspace dispatches against, so
    on-disk edits take effect without restarting the bridge or any MCP
    server process. The Workspace instance and workspace selection are
    preserved; only the code modules are replaced.

    Returns the list of module names that were reloaded.
    """
    import importlib
    reloaded = []
    for mod_name in _WORKSPACE_RELOAD_TARGETS:
        mod = sys.modules.get(mod_name)
        if mod is None:
            # Module was never imported (e.g. an optional helper whose import
            # failed at startup). Attempt a fresh import so a newly-installed
            # dependency can take effect without a restart.
            try:
                importlib.import_module(mod_name)
                reloaded.append(mod_name)
            except Exception:
                pass
            continue
        importlib.reload(mod)
        reloaded.append(mod_name)
    # Refresh the module-level globals used by _call_workspace dispatch.
    g = globals()
    try:
        from workspace_agent.tools import Workspace as _W, WorkspaceError as _WE
        g["Workspace"] = _W
        g["WorkspaceError"] = _WE
    except Exception:
        pass
    for helper_name in (
        "http_client", "browser_qa", "browser_server", "assetripper_jobs",
        "job_manager", "unity_recovery", "unity_asset_inspector",
        "reconstruction_tracker", "code_intelligence",
    ):
        mod = sys.modules.get(f"workspace_agent.{helper_name}")
        if mod is not None:
            g[helper_name] = mod
    try:
        from workspace_agent.agent_control import AgentControlStore as _ACS
        g["AgentControlStore"] = _ACS
    except Exception:
        pass
    try:
        from workspace_agent.roblox_export import export_tree as _export
        g["export_roblox_tree"] = _export
    except Exception:
        pass
    log(f"workspace modules reloaded: {len(reloaded)}", "cy")
    return reloaded


# ══════════════════════════════════════════════════════════════════════════
#  MANAGER  - aggregates every MCP server, routes by tool name.
# ══════════════════════════════════════════════════════════════════════════
class MCPManager:
    def __init__(self):
        self.clients = {}          # server_id -> MCPClient
        self.index = {}            # advertised_name -> (holder, real_name)
        self.index_lock = threading.Lock()
        # An explicit environment override wins; otherwise restore the last
        # user-selected workspace from a private local settings file. Falling
        # back to the bridge directory keeps old installs usable, but is NOT
        # treated as an intentional project selection.
        self.workspace_settings_path = os.path.join(HERE, ".zs-workspace.json")
        env_root = os.environ.get("ZS_WORKSPACE_ROOT", "").strip()
        settings = {}
        try:
            with open(self.workspace_settings_path, "r", encoding="utf-8") as fp:
                loaded_settings = json.load(fp)
            if isinstance(loaded_settings, dict):
                settings = loaded_settings
        except (OSError, ValueError, TypeError):
            settings = {}
        saved_root = str(settings.get("root", "")).strip()
        recent_roots = settings.get("recent_roots", [])
        self.workspace_history = []
        if isinstance(recent_roots, list):
            for entry in recent_roots:
                if isinstance(entry, str) and entry.strip() and entry.strip() not in self.workspace_history:
                    self.workspace_history.append(entry.strip())
        self.workspace_root = env_root or saved_root or HERE
        self.workspace_configured = bool(env_root or saved_root)
        self.workspace_source = "environment" if env_root else ("saved" if saved_root else "default")
        if self.workspace_configured and self.workspace_root not in self.workspace_history:
            self.workspace_history.insert(0, self.workspace_root)
        self.workspace = None
        self.workspace_error = None
        self._load_workspace()

    def _load_workspace(self):
        if Workspace is None:
            self.workspace_error = "Workspace tools could not be imported."
            self.workspace_configured = False
            self.workspace_source = "unavailable"
            return
        try:
            self.workspace = Workspace(self.workspace_root)
            log(f"workspace selected: {self.workspace.root}", "cy")
        except Exception as exc:
            self.workspace_error = str(exc)
            self.workspace_configured = False
            self.workspace_source = "invalid"
            log(f"workspace unavailable: {exc}", "yl")

    def _workspace_tools(self):
        tools = [
            {"name": "workspace_info", "description": f"Current workspace path: {self.workspace_root}; configured: {str(self.workspace_configured).lower()}. If configured is false, ask the user for a project folder before inspecting or editing anything.",
             "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}, "server": "workspace"},
            {"name": "workspace_recent_roots", "description": "List the most recently selected project folders saved locally, including whether each still exists and which one is active. Use this before offering a recent-project switch.",
             "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}, "server": "workspace"},
            {"name": "workspace_open_recent", "description": "Activate a folder only if it is present in workspace_recent_roots. This switches the active local project and persists the choice; ask the user to confirm which project they want before calling.",
             "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"], "additionalProperties": False}, "server": "workspace"},
            {"name": "workspace_set_root", "description": "Set the active workspace root. Use persist=false for temporary conversation-scoped activation so the general workspace setting is preserved.",
             "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}, "persist": {"type": "boolean", "default": True}}, "required": ["path"], "additionalProperties": False}, "server": "workspace"},
            {"name": "workspace_create_conversation_root", "description": "Create or return an isolated temporary workspace directory for a conversation without changing the general workspace setting.",
             "inputSchema": {"type": "object", "properties": {"conversation_id": {"type": "string"}}, "required": ["conversation_id"], "additionalProperties": False}, "server": "workspace"},
            {"name": "workspace_browse", "description": "Open a native folder picker and return the selected_path without changing the active workspace. After the user chooses the folder, call workspace_set_root with that exact selected_path to activate it.",
             "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}, "server": "workspace"},
            {"name": "workspace_project_info", "description": "Inspect common project manifests, framework/language hints, Git presence, source-file counts, and package scripts.",
             "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}, "server": "workspace"},
            {"name": "workspace_create_file", "description": "Create a new UTF-8 file inside the workspace without overwriting an existing file. Parent directory must exist.",
             "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string", "default": ""}}, "required": ["path"], "additionalProperties": False}, "server": "workspace"},
            {"name": "workspace_create_directory", "description": "Create a new directory within the workspace. Existing paths are never overwritten; use a workspace-relative path.",
             "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"], "additionalProperties": False}, "server": "workspace"},
            {"name": "workspace_move_path", "description": "Rename or move a file/directory within the selected workspace. Destination must not exist; symbolic links and moving the workspace root are refused.",
             "inputSchema": {"type": "object", "properties": {"source": {"type": "string"}, "destination": {"type": "string"}}, "required": ["source", "destination"], "additionalProperties": False}, "server": "workspace"},
            {"name": "workspace_delete_file", "description": "Delete one regular file inside the selected workspace. Directories and symbolic links are refused; only use when the user explicitly requests deletion.",
             "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"], "additionalProperties": False}, "server": "workspace"},
            {"name": "workspace_find_files", "description": "Find project files by glob pattern (for example **/*.luau, *.json, or src/*.ts). Generated directories are skipped.",
             "inputSchema": {"type": "object", "properties": {"pattern": {"type": "string"}, "path": {"type": "string", "default": "."}, "max_results": {"type": "integer", "minimum": 1, "maximum": 500}, "include_hidden": {"type": "boolean"}}, "required": ["pattern"], "additionalProperties": False}, "server": "workspace"},
            {"name": "workspace_symbols", "description": "Find likely function, class, type, and variable declarations across source files. Optional query filters symbol names or declaration text.",
             "inputSchema": {"type": "object", "properties": {"query": {"type": "string", "default": ""}, "path": {"type": "string", "default": "."}, "max_results": {"type": "integer", "minimum": 1, "maximum": 500}}, "additionalProperties": False}, "server": "workspace"},
            {"name": "workspace_tree", "description": "Return a bounded directory tree with file sizes, skipping generated folders and symbolic links.", 
             "inputSchema": {"type": "object", "properties": {"path": {"type": "string", "default": "."}, "max_depth": {"type": "integer", "minimum": 0, "maximum": 12, "default": 4}, "max_results": {"type": "integer", "minimum": 1, "maximum": 500, "default": 300}, "include_hidden": {"type": "boolean", "default": False}}, "additionalProperties": False}, "server": "workspace"},
            {"name": "workspace_file_outline", "description": "Show imports, headings, variables, and likely declarations with line numbers for one source file.", 
             "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}, "max_results": {"type": "integer", "minimum": 1, "maximum": 500, "default": 300}}, "required": ["path"], "additionalProperties": False}, "server": "workspace"},
            {"name": "workspace_list_files", "description": "List source files in the selected project. Generated directories are skipped.",
             "inputSchema": {"type": "object", "properties": {"path": {"type": "string", "default": "."}, "max_results": {"type": "integer", "minimum": 1, "maximum": 500}, "include_hidden": {"type": "boolean"}}, "additionalProperties": False}, "server": "workspace"},
            {"name": "workspace_read_file", "description": "Read a UTF-8 text file inside the selected project (maximum 1 MB). Optional 1-based start_line/end_line bounds the read.",
             "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}, "start_line": {"type": "integer", "minimum": 1}, "end_line": {"type": "integer", "minimum": 1}}, "required": ["path"], "additionalProperties": False}, "server": "workspace"},
            {"name": "workspace_preview_image", "description": "Preview an image inside the selected workspace using a workspace-relative path (forward or backslashes) or an absolute path that remains inside the workspace. Returns image content for the ArcScript preview panel and vision-capable providers. Supports PNG, JPEG, WebP, GIF, and safe SVG up to 8 MB. The active provider must support vision to analyze pixels; a preview-only result is not visual QA.",
             "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"], "additionalProperties": False}, "server": "workspace"},
            {"name": "workspace_search", "description": "Search source text inside the selected project. Supports optional case-sensitive matching and regular expressions; results include line and column.",
             "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}, "path": {"type": "string", "default": "."}, "max_results": {"type": "integer", "minimum": 1, "maximum": 500}, "case_sensitive": {"type": "boolean", "default": False}, "regex": {"type": "boolean", "default": False}}, "required": ["query"], "additionalProperties": False}, "server": "workspace"},
            {"name": "workspace_replace_in_files", "description": "Preview or apply literal/regex replacements across project files. First call with apply=false to inspect affected files/diffs; only apply after user confirmation by passing the returned preview_hash as expected_hash. Stale previews are rejected.",
             "inputSchema": {"type": "object", "properties": {"query": {"type": "string", "minLength": 1}, "replacement": {"type": "string"}, "path": {"type": "string", "default": "."}, "max_files": {"type": "integer", "minimum": 1, "maximum": 100, "default": 100}, "case_sensitive": {"type": "boolean", "default": False}, "regex": {"type": "boolean", "default": False}, "apply": {"type": "boolean", "default": False}, "expected_hash": {"type": "string", "default": ""}}, "required": ["query", "replacement"], "additionalProperties": False}, "server": "workspace"},
            {"name": "workspace_multi_edit", "description": "Apply a batch of exact-match replacements across existing files. All edits are validated before writing any file; ambiguous matches are rejected. Returns a per-file unified diff. Prefer this for coordinated multi-file code changes.",
             "inputSchema": {"type": "object", "properties": {"edits": {"type": "array", "minItems": 1, "maxItems": 200, "items": {"type": "object", "properties": {"path": {"type": "string"}, "old_string": {"type": "string"}, "new_string": {"type": "string"}, "replace_all": {"type": "boolean", "default": False}, "whole_file": {"type": "boolean", "default": False}}, "required": ["path", "old_string", "new_string"], "additionalProperties": False}}}, "required": ["edits"], "additionalProperties": False}, "server": "workspace"},
            {"name": "workspace_apply_patch", "description": "Apply an exact old_string to new_string edit to an existing UTF-8 file. Ambiguous matches are rejected unless replace_all is true. Prefer this to replacing whole files for targeted edits.",
             "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}, "old_string": {"type": "string"}, "new_string": {"type": "string"}, "replace_all": {"type": "boolean", "default": False}}, "required": ["path", "old_string", "new_string"], "additionalProperties": False}, "server": "workspace"},
            {"name": "workspace_write_files", "description": "Import many UTF-8 text files into the selected workspace in one call. Use this to transfer source captured from Roblox Studio MCP into local project files. Paths must be workspace-relative. Existing files are protected unless overwrite=true. Validates the entire batch before writing; returns per-file byte counts. Maximum 200 files and 10 MB total.",
             "inputSchema": {"type": "object", "properties": {"files": {"type": "array", "minItems": 1, "maxItems": 200, "items": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"], "additionalProperties": False}}, "overwrite": {"type": "boolean", "default": False}}, "required": ["files"], "additionalProperties": False}, "server": "workspace"},
            {"name": "workspace_write_file", "description": "Create or replace a UTF-8 text file inside the selected project. Returns a unified diff. This modifies files.",
             "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"], "additionalProperties": False}, "server": "workspace"},
            {"name": "workspace_git_changed_files", "description": "List changed files with staged, unstaged, untracked, added, deleted, or renamed state. Use for source-control browsing; it does not mutate Git.",
             "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}, "server": "workspace"},
            {"name": "workspace_git_stage_file", "description": "Stage one file path inside the selected workspace using Git. Does not stage an entire directory. Only call after the user explicitly asks to stage that file.",
             "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"], "additionalProperties": False}, "server": "workspace"},
            {"name": "workspace_git_unstage_file", "description": "Remove one file path from the Git index without changing its working-tree contents. Only call after the user explicitly asks to unstage that file.",
             "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"], "additionalProperties": False}, "server": "workspace"},
            {"name": "workspace_git_commit", "description": "Create a local commit from already-staged changes; this does not push. Only call when the user explicitly requests a commit and provides or confirms the commit message.",
             "inputSchema": {"type": "object", "properties": {"message": {"type": "string", "minLength": 1, "maxLength": 500}}, "required": ["message"], "additionalProperties": False}, "server": "workspace"},
            {"name": "workspace_git_status", "description": "Get Git working-tree status for the selected project.",
             "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}, "server": "workspace"},
            {"name": "workspace_git_diff", "description": "Read staged or unstaged Git changes, optionally limited to a workspace-relative path.",
             "inputSchema": {"type": "object", "properties": {"staged": {"type": "boolean", "default": False}, "path": {"type": "string", "default": "."}}, "additionalProperties": False}, "server": "workspace"},
            {"name": "workspace_git_branches", "description": "List local and remote Git branches without changing repository state.", 
             "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}, "server": "workspace"},
            {"name": "workspace_git_log", "description": "Read recent Git commit summaries without modifying the repository.",
             "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 30, "default": 10}}, "additionalProperties": False}, "server": "workspace"},
            {"name": "workspace_run_task", "description": "Run a whitelisted built-in task for detected Python/Rust/Go/.NET/Maven/Gradle/CMake projects, or a root package.json script addressed as package:<script-name>. Package scripts are executable project code and may modify files or access networks; only run relevant tasks requested by the user.",
             "inputSchema": {"type": "object", "properties": {"task": {"type": "string", "minLength": 1}, "timeout": {"type": "integer", "minimum": 1, "maximum": 300, "default": 300}}, "required": ["task"], "additionalProperties": False}, "server": "workspace"},
            {"name": "workspace_run_command", "description": "Run a command as an argument array from the selected project directory. Shell expansion is disabled. Use only commands required for the requested development task.",
             "inputSchema": {"type": "object", "properties": {"command": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 100}, "timeout": {"type": "integer", "minimum": 1, "maximum": 300}}, "required": ["command"], "additionalProperties": False}, "server": "workspace"},
            {"name": "workspace_http_request", "description": "Perform one bounded HTTP request (GET/HEAD/POST/PUT/PATCH/DELETE/OPTIONS) to http or https URLs. Body is capped at max_bytes; secrets can be supplied via header values of the form 'env:VARIABLE_NAME' so the token never appears in this conversation. Loopback/private/link-local destinations are refused unless allow_local=true. Redirects are re-validated per hop.",
             "inputSchema": {"type": "object", "properties": {"url": {"type": "string", "minLength": 1}, "method": {"type": "string", "enum": ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"], "default": "GET"}, "headers": {"type": "object", "additionalProperties": {"type": "string"}}, "body": {"type": ["string", "null"]}, "timeout": {"type": "number", "minimum": 0.1, "maximum": 60, "default": 20}, "max_bytes": {"type": "integer", "minimum": 1024, "maximum": 2000000, "default": 2000000}, "allow_local": {"type": "boolean", "default": False}}, "required": ["url"], "additionalProperties": False}, "server": "workspace"},
            {"name": "scan_unity_installation", "description": "Inspect an installed Unity player without modifying it. Reports Unity version when detectable, Mono vs IL2CPP evidence, managed assemblies, large asset files, and whether a full editable project exists. Defaults to the selected workspace; pass install_path only when the installation is elsewhere.",
             "inputSchema": {"type": "object", "properties": {"install_path": {"type": "string", "default": ""}}, "additionalProperties": False}, "server": "workspace"},
            {"name": "catalog_unity_assets", "description": "Create or resume a deterministic, file-level Unity asset catalog. Writes only under the selected workspace; never extracts or modifies original assets. Re-call until status=complete. Serialized Unity object dependencies are not parsed.",
             "inputSchema": {"type": "object", "properties": {"install_path": {"type": "string", "default": ""}, "output_path": {"type": "string", "default": "recovery/asset-catalog.json"}, "batch_size": {"type": "integer", "minimum": 1, "maximum": 2000, "default": 500}}, "additionalProperties": False}, "server": "workspace"},
            {"name": "update_reconstruction_feature", "description": "Create/update an evidence-linked browser restoration feature record. Records decompiled source locators, asset files, implementation files, tests, status, and notes in recovery/reconstruction/features.json. Paths must exist inside the workspace; this records evidence but does not claim tests passed.",
             "inputSchema": {"type": "object", "properties": {"feature_id": {"type": "string", "pattern": "^[a-z0-9][a-z0-9._-]{0,79}$"}, "name": {"type": "string", "minLength": 1, "maxLength": 200}, "status": {"type": "string", "enum": ["planned", "investigating", "implemented", "blocked"], "default": "planned"}, "notes": {"type": "string", "maxLength": 4000, "default": ""}, "source_evidence": {"type": "array", "items": {"type": "object", "properties": {"path": {"type": "string"}, "locator": {"type": "string"}}, "required": ["path", "locator"], "additionalProperties": False}}, "asset_evidence": {"type": "array", "items": {"type": "string"}}, "implementation_files": {"type": "array", "items": {"type": "string"}}, "test_files": {"type": "array", "items": {"type": "string"}}}, "required": ["feature_id", "name"], "additionalProperties": False}, "server": "workspace"},
            {"name": "report_reconstruction_readiness", "description": "Report missing source, asset, implementation, and regression-test evidence for browser restoration features. Can inspect one feature_id or the entire inventory. Readiness is not a test pass or proof of gameplay parity.",
             "inputSchema": {"type": "object", "properties": {"feature_id": {"type": "string", "default": ""}}, "additionalProperties": False}, "server": "workspace"},
            {"name": "inspect_unity_serialized_file", "description": "Inspect UnityPy object types, path IDs, and object names in one selected serialized asset/bundle file. Refuses files above a bounded size (default 256 MiB) to protect memory; do not point it at a 1.64 GB resources.assets file. Requires UnityPy installed in the bridge Python environment and never modifies source assets.",
             "inputSchema": {"type": "object", "properties": {"install_path": {"type": "string"}, "asset_path": {"type": "string", "description": "Path relative to the Unity installation, such as YandereSimulator_Data/sharedassets0.assets."}, "max_file_mb": {"type": "integer", "minimum": 1, "maximum": 512, "default": 256}, "max_objects": {"type": "integer", "minimum": 1, "maximum": 5000, "default": 1000}}, "required": ["install_path", "asset_path"], "additionalProperties": False}, "server": "workspace"},
            {"name": "inspect_recovery_tooling", "description": "Check PATH availability of ILSpy CLI, AssetRipper, Unity Editor, .NET, Node/npm, Git, and 7-Zip, plus browser-port markers. Does not install software.",
             "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}, "server": "workspace"},
            {"name": "decompile_unity_type", "description": "Decompile a named managed type from the installed Mono assembly using ilspycmd. Bounded timeout, no shell, original DLL is read-only. Requires ILSpy CLI to be installed.",
             "inputSchema": {"type": "object", "properties": {"install_path": {"type": "string"}, "type_name": {"type": "string", "minLength": 1}, "assembly_path": {"type": "string", "default": ""}, "timeout": {"type": "integer", "minimum": 1, "maximum": 180, "default": 90}}, "required": ["install_path", "type_name"], "additionalProperties": False}, "server": "workspace"},
            {"name": "export_unity_assembly", "description": "Export one managed assembly to C# source using ilspycmd. Refuses to overwrite a non-empty output directory; output must remain within the selected workspace. Requires ILSpy CLI.",
             "inputSchema": {"type": "object", "properties": {"install_path": {"type": "string"}, "assembly_path": {"type": "string", "default": ""}, "output_path": {"type": "string", "default": "recovery/decompiled"}, "timeout": {"type": "integer", "minimum": 1, "maximum": 180, "default": 180}}, "required": ["install_path"], "additionalProperties": False}, "server": "workspace"},
            {"name": "list_unity_symbols", "description": "List Unity managed assembly types, methods, fields, or namespaces using ILSpy metadata listing. Supports a substring filter and bounded results; does not modify the assembly.",
             "inputSchema": {"type": "object", "properties": {"install_path": {"type": "string", "default": ""}, "symbol_kind": {"type": "string", "enum": ["types", "classes", "methods", "fields", "namespaces"], "default": "types"}, "assembly_path": {"type": "string", "default": ""}, "query": {"type": "string", "default": ""}, "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 200}, "timeout": {"type": "integer", "minimum": 1, "maximum": 180, "default": 90}}, "additionalProperties": False}, "server": "workspace"},
            {"name": "search_unity_source", "description": "Search exported C# source for a symbol, method, field, or string. Returns file/line/context matches. Run export_unity_assembly first; this is source search, not an IL-level call graph.",
             "inputSchema": {"type": "object", "properties": {"query": {"type": "string", "minLength": 1}, "source_path": {"type": "string", "default": "recovery/decompiled"}, "max_results": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100}, "case_sensitive": {"type": "boolean", "default": False}, "regex": {"type": "boolean", "default": False}, "context_lines": {"type": "integer", "minimum": 0, "maximum": 5, "default": 2}}, "required": ["query"], "additionalProperties": False}, "server": "workspace"},
            {"name": "inspect_browser_port", "description": "Audit browser-port/index.html and local script/style references without launching a browser. Reports missing local files and package scripts. Output is diagnostic only.",
             "inputSchema": {"type": "object", "properties": {"port_path": {"type": "string", "default": "browser-port"}}, "additionalProperties": False}, "server": "workspace"},
            {"name": "run_browser_qa", "description": "Smoke-test a running browser restoration over HTTP; optionally run up to 40 selector-based Playwright steps (click, fill, press, visibility/text assertions, waits, screenshots). Interactive scenarios are restricted to localhost/127.0.0.1. Requires Python Playwright plus Chromium for browser scenarios; does not install dependencies.",
             "inputSchema": {"type": "object", "properties": {"url": {"type": "string"}, "port_path": {"type": "string", "default": "browser-port"}, "screenshot_path": {"type": "string", "default": "recovery/browser-qa.png"}, "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 60, "default": 20}, "scenarios": {"type": "array", "maxItems": 40, "items": {"type": "object", "properties": {"action": {"type": "string", "enum": ["click", "fill", "press", "expect_visible", "expect_text", "wait_ms", "screenshot"]}, "selector": {"type": "string"}, "value": {"type": ["string", "integer"]}, "key": {"type": "string"}, "timeout_ms": {"type": "integer", "minimum": 100, "maximum": 10000}, "label": {"type": "string"}}, "required": ["action"], "additionalProperties": False}}}, "required": ["url"], "additionalProperties": False}, "server": "workspace"},
            {"name": "start_browser_server", "description": "Start a static HTTP server for browser-port/index.html bound only to 127.0.0.1. Returns a job ID, PID, URL, and log path. Does not install packages or expose the server to the LAN.",
             "inputSchema": {"type": "object", "properties": {"port_path": {"type": "string", "default": "browser-port"}, "port": {"type": "integer", "minimum": 1024, "maximum": 65535, "default": 4173}}, "additionalProperties": False}, "server": "workspace"},
            {"name": "browser_server_status", "description": "Read the recorded browser-server job state and report whether the process is running.",
             "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}, "server": "workspace"},
            {"name": "stop_browser_server", "description": "Stop only a browser server process spawned and tracked by this bridge instance. It will not kill an unverified PID left over from another bridge session.",
             "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}, "server": "workspace"},
            {"name": "start_assetripper_export", "description": "Start an asynchronous AssetRipper CLI-compatible export. Requires an explicit executable path and input directory. Writes only to a workspace-relative output directory, refuses non-empty output, supports Unity/Primary modes, and returns a job ID plus log path. It does not install AssetRipper.",
             "inputSchema": {"type": "object", "properties": {"executable_path": {"type": "string"}, "input_path": {"type": "string"}, "output_path": {"type": "string", "default": "recovery/assetripper-export"}, "mode": {"type": "string", "enum": ["Unity", "Primary"], "default": "Primary"}, "script_content_level": {"type": "string", "enum": ["Level0", "Level1", "Level2"], "default": "Level1"}, "timeout_minutes": {"type": "integer", "minimum": 1, "maximum": 1440, "default": 240}}, "required": ["executable_path", "input_path"], "additionalProperties": False}, "server": "workspace"},
            {"name": "assetripper_job_status", "description": "Poll an AssetRipper export job by its returned job ID. Persists state and reports success, failure, interruption, or whether the process is still running.",
             "inputSchema": {"type": "object", "properties": {"job_id": {"type": "string"}}, "required": ["job_id"], "additionalProperties": False}, "server": "workspace"},
            {"name": "cancel_assetripper_job", "description": "Cancel an AssetRipper job only if this bridge instance spawned and still tracks the process. It will not kill an unverified PID after a bridge restart.",
             "inputSchema": {"type": "object", "properties": {"job_id": {"type": "string"}}, "required": ["job_id"], "additionalProperties": False}, "server": "workspace"},
            {"name": "workspace_job_start", "description": "Start an argv command as a background job scoped to this workspace. Combined stdout+stderr go to a log file; use workspace_job_status to poll, workspace_job_logs to tail, workspace_job_stop to terminate. cwd must resolve inside the workspace. Use this for dev servers, watchers, test runners, and build jobs that must outlive a single tool call.",
             "inputSchema": {"type": "object", "properties": {"command": {"type": "array", "items": {"type": "string", "minLength": 1}, "minItems": 1, "maxItems": 100}, "cwd": {"type": "string", "default": "."}, "label": {"type": "string", "maxLength": 120}, "env": {"type": "object", "additionalProperties": {"type": "string"}}, "timeout_minutes": {"type": "integer", "minimum": 1, "maximum": 1440, "default": 60}}, "required": ["command"], "additionalProperties": False}, "server": "workspace"},
            {"name": "workspace_job_status", "description": "Poll one background job by its job_id and report the settled status (running, succeeded, failed, cancelled, interrupted, untracked_running). Enforces the deadline on a running job and honours the never-terminate-an-unverified-PID rule across bridge restarts.",
             "inputSchema": {"type": "object", "properties": {"job_id": {"type": "string"}}, "required": ["job_id"], "additionalProperties": False}, "server": "workspace"},
            {"name": "workspace_job_logs", "description": "Return a bounded tail of one job's combined stdout+stderr log. The leading partial line is dropped when the tail starts mid-file; max_bytes caps the returned content.",
             "inputSchema": {"type": "object", "properties": {"job_id": {"type": "string"}, "max_bytes": {"type": "integer", "minimum": 1024, "maximum": 1048576, "default": 65536}}, "required": ["job_id"], "additionalProperties": False}, "server": "workspace"},
            {"name": "workspace_job_list", "description": "List every background job recorded for this workspace, newest first, with the current status and active count against the concurrency limit.",
             "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}, "server": "workspace"},
            {"name": "workspace_job_stop", "description": "Terminate one background job started by THIS bridge instance. Refuses to kill an untracked PID after a bridge restart because the process cannot be verified as the one recorded here.",
             "inputSchema": {"type": "object", "properties": {"job_id": {"type": "string"}}, "required": ["job_id"], "additionalProperties": False}, "server": "workspace"},
            {"name": "workspace_wait_port", "description": "Block until a TCP port accepts a connection, or the timeout elapses. TCP connect only; no data is sent. Defaults to 127.0.0.1. Pair with workspace_job_start to wait for a dev server before opening a browser.",
             "inputSchema": {"type": "object", "properties": {"port": {"type": "integer", "minimum": 1, "maximum": 65535}, "host": {"type": "string", "default": "127.0.0.1"}, "timeout_seconds": {"type": "number", "minimum": 0.1, "maximum": 300, "default": 30}, "interval_ms": {"type": "integer", "minimum": 50, "maximum": 5000, "default": 200}}, "required": ["port"], "additionalProperties": False}, "server": "workspace"},
        ]
        tools.append({
            "name": "roblox_export_tree",
            "description": "Export all Script, LocalScript, and ModuleScript source from the connected Roblox Studio place into the selected local workspace. Traverses the full DataModel, preserves hierarchy as directories, keeps server/client/module suffixes, and writes locally in bounded batches without sending script contents through the agent conversation. Requires a configured workspace and Roblox Studio MCP connection. Existing files are protected unless overwrite=true.",
            "inputSchema": {"type": "object", "properties": {
                "root_path": {"type": "string", "default": "RobloxExport", "description": "Workspace-relative directory to create the export under."},
                "overwrite": {"type": "boolean", "default": False, "description": "Explicitly replace matching existing files."},
                "exclude": {"type": "array", "items": {"type": "string", "minLength": 1}, "maxItems": 100, "default": [], "description": "Case-insensitive substrings matched against slash-separated instance hierarchy paths; matching scripts are skipped before source is read."},
                "max_script_bytes": {"type": "integer", "minimum": 1, "maximum": 1048576, "default": 1048576, "description": "Skip and report scripts larger than this UTF-8 byte limit; oversized scripts do not abort the export."}
            }, "additionalProperties": False},
            "server": "roblox-export"
        })
        tools.extend([
            {
                "name": "zeroscript_handoff_to_cli",
                "description": "Transfer this task to the independent ZeroScript CLI agent. Include the original goal, relevant context, completed work, outstanding steps, a plan, and acceptance criteria where known. After a successful handoff, stop further implementation/tool calls in the browser agent. The CLI must claim the handoff before ownership changes.",
                "inputSchema": {"type": "object", "properties": {
                    "goal": {"type": "string", "minLength": 1, "maxLength": 12000},
                    "context": {"type": "string", "maxLength": 40000, "default": ""},
                    "plan": {"type": "array", "maxItems": 80, "items": {"type": "string", "maxLength": 2000}, "default": []},
                    "acceptance_criteria": {"type": "array", "maxItems": 80, "items": {"type": "string", "maxLength": 2000}, "default": []}
                }, "required": ["goal"], "additionalProperties": False},
                "server": "zeroscript-control"
            },
            {
                "name": "zeroscript_agent_control_status",
                "description": "Read shared browser/CLI agent-control state. This is the only tool available to the browser agent while the CLI owns an active run.",
                "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
                "server": "zeroscript-control"
            }
        ])
        return tools

    def _set_workspace(self, path, source="selected", persist=True):
        if Workspace is None:
            raise RuntimeError("Workspace tools could not be imported.")
        if not isinstance(path, str) or not path.strip():
            raise RuntimeError("A local folder path is required.")
        if not Path(path).expanduser().is_absolute():
            raise RuntimeError("Use an absolute folder path, or open workspace_browse to choose a folder.")
        candidate = Workspace(path)
        selected_root = str(candidate.root)
        history = [selected_root]
        for prior in getattr(self, "workspace_history", []):
            if not isinstance(prior, str) or not prior.strip():
                continue
            try:
                normalized_prior = str(Path(prior).expanduser().resolve())
            except OSError:
                continue
            if normalized_prior != selected_root and normalized_prior not in history:
                history.append(normalized_prior)
            if len(history) >= 10:
                break
        self.workspace_history = history
        if persist:
            try:
                temp_path = self.workspace_settings_path + ".tmp"
                with open(temp_path, "w", encoding="utf-8") as fp:
                    json.dump({"root": selected_root, "recent_roots": history}, fp, ensure_ascii=False, indent=2)
                os.replace(temp_path, self.workspace_settings_path)
            except OSError as exc:
                raise RuntimeError(f"The folder is valid, but workspace settings could not be saved: {exc}") from exc
        self.workspace = candidate
        self.workspace_root = selected_root
        self.workspace_configured = True
        self.workspace_source = source
        self.workspace_error = None
        log(f"workspace selected: {self.workspace_root}", "cy")
        return self._workspace_description()

    def _workspace_description(self):
        if self.workspace is None:
            return {"root": self.workspace_root, "name": os.path.basename(self.workspace_root), "exists": False,
                    "configured": self.workspace_configured, "source": self.workspace_source, "error": self.workspace_error}
        data = self.workspace.describe()
        data.update({"configured": self.workspace_configured, "source": self.workspace_source})
        return data

    def _browse_workspace(self):
        try:
            import tkinter as tk
            from tkinter import filedialog
        except Exception as exc:
            raise RuntimeError("The native folder picker is unavailable in this Python install. Use workspace_set_root with the full folder path instead.") from exc
        root = None
        try:
            root = tk.Tk()
            root.withdraw()
            try:
                root.attributes("-topmost", True)
            except Exception:
                pass
            chosen = filedialog.askdirectory(parent=root, title="Choose an ArcScript coding workspace",
                                            initialdir=self.workspace_root if os.path.isdir(self.workspace_root) else HERE,
                                            mustexist=True)
        except Exception as exc:
            raise RuntimeError(f"Could not open the native folder picker: {exc}. Use workspace_set_root with the full folder path.") from exc
        finally:
            if root is not None:
                try:
                    root.destroy()
                except Exception:
                    pass
        if not chosen:
            return {"cancelled": True, "message": "Folder selection cancelled; the active workspace was not changed.", **self._workspace_description()}
        selected = Path(chosen).expanduser().resolve()
        if not selected.is_dir():
            raise RuntimeError("The selected workspace path is no longer an existing directory.")
        return {"chosen": True, "selected_path": str(selected),
                "message": "Folder selected but not yet activated. Call workspace_set_root with this exact selected_path to make it active."}

    def _call_workspace(self, name, arguments):
        args = arguments or {}
        if name == "workspace_info":
            return self._workspace_description()
        if name == "workspace_recent_roots":
            history = list(getattr(self, "workspace_history", []))
            if self.workspace_configured and self.workspace_root not in history:
                history.insert(0, self.workspace_root)
            roots = []
            seen = set()
            for item in history:
                try:
                    resolved = str(Path(item).expanduser().resolve())
                except (OSError, RuntimeError):
                    resolved = item
                if resolved in seen:
                    continue
                seen.add(resolved)
                candidate = Path(resolved)
                roots.append({
                    "path": resolved,
                    "name": candidate.name or resolved,
                    "exists": candidate.is_dir(),
                    "current": bool(self.workspace_configured and resolved == str(Path(self.workspace_root).expanduser().resolve())),
                })
            return {"recent_roots": roots[:10]}
        if name == "workspace_set_root":
            return self._set_workspace(args.get("path", ""), persist=args.get("persist", True))
        if name == "workspace_create_conversation_root":
            conversation_id = str(args.get("conversation_id", "")).strip()
            if not conversation_id:
                raise RuntimeError("A conversation ID is required for an isolated workspace.")
            digest = hashlib.sha256(conversation_id.encode("utf-8")).hexdigest()[:24]
            local_app_data = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
            root = Path(local_app_data) / "ZeroScript-Win" / "ConversationWorkspaces" / digest
            root.mkdir(parents=True, exist_ok=True)
            candidate = Workspace(str(root))
            return {"root": str(candidate.root), "name": root.name, "configured": True,
                    "temporary": True, "conversation_id_hash": digest,
                    "message": "Isolated conversation workspace ready; the general workspace setting was not changed."}
        if name == "workspace_browse":
            return self._browse_workspace()
        if name == "workspace_open_recent":
            requested = args.get("path", "")
            if not isinstance(requested, str) or not requested.strip():
                raise RuntimeError("A recent workspace path is required.")
            try:
                normalized = str(Path(requested).expanduser().resolve())
                history_paths = {
                    str(Path(item).expanduser().resolve())
                    for item in getattr(self, "workspace_history", [])
                    if isinstance(item, str) and item.strip()
                }
                if self.workspace_configured and self.workspace_root:
                    history_paths.add(str(Path(self.workspace_root).expanduser().resolve()))
            except (OSError, RuntimeError) as exc:
                raise RuntimeError("The requested recent workspace path is invalid.") from exc
            if normalized not in history_paths:
                raise RuntimeError("That path is not in the saved recent-workspace list. Ask the user to choose a folder or use workspace_set_root with their explicit path.")
            return self._set_workspace(normalized, source="recent")
        if not self.workspace_configured:
            raise RuntimeError("No project workspace has been selected. Ask the user to choose an absolute folder path or call workspace_browse; do not inspect the default bridge directory.")
        if self.workspace is None:
            raise RuntimeError(self.workspace_error or "Workspace is unavailable. Select a folder with workspace_set_root or workspace_browse.")
        if name == "workspace_create_file":
            return self.workspace.create_file(args["path"], args.get("content", ""))
        if name == "workspace_create_directory":
            return self.workspace.create_directory(args["path"])
        if name == "workspace_move_path":
            return self.workspace.move_path(args["source"], args["destination"])
        if name == "workspace_delete_file":
            return self.workspace.delete_file(args["path"])
        if name == "workspace_project_info":
            return self.workspace.project_info()
        if name == "workspace_find_files":
            return self.workspace.find_files(args.get("pattern", "*"), args.get("path", "."), args.get("max_results", 200), args.get("include_hidden", False))
        if name == "workspace_symbols":
            return self.workspace.symbols(args.get("query", ""), args.get("path", "."), args.get("max_results", 200))
        if name == "workspace_tree":
            return self.workspace.tree(args.get("path", "."), args.get("max_depth", 4), args.get("max_results", 300), args.get("include_hidden", False))
        if name == "workspace_file_outline":
            return self.workspace.file_outline(args["path"], args.get("max_results", 300))
        if name == "workspace_list_files":
            return self.workspace.list_files(args.get("path", "."), args.get("max_results", 200), args.get("include_hidden", False))
        if name == "workspace_read_file":
            result = self.workspace.read_file(args["path"])
            if "start_line" in args or "end_line" in args:
                lines = result["content"].splitlines(keepends=True)
                start_line = max(1, int(args.get("start_line", 1)))
                end_line = max(start_line, int(args.get("end_line", len(lines) or 1)))
                result["content"] = "".join(lines[start_line - 1:end_line])
                result["start_line"] = start_line
                result["end_line"] = min(end_line, len(lines))
                result["total_lines"] = len(lines)
            return result
        if name == "workspace_search":
            return self.workspace.search(args["query"], args.get("path", "."), args.get("max_results", 100),
                                         args.get("case_sensitive", False), args.get("regex", False))
        if name == "workspace_replace_in_files":
            return self.workspace.replace_in_files(
                args["query"], args["replacement"], args.get("path", "."),
                args.get("max_files", 100), args.get("case_sensitive", False),
                args.get("regex", False), args.get("apply", False), args.get("expected_hash", ""),
            )
        if name == "workspace_multi_edit":
            return self.workspace.multi_edit(args["edits"])
        if name == "workspace_apply_patch":
            return self.workspace.apply_patch(args["path"], args["old_string"], args["new_string"], args.get("replace_all", False))
        if name == "workspace_write_files":
            return self.workspace.write_files(args["files"], args.get("overwrite", False))
        if name == "workspace_write_file":
            return self.workspace.write_file(args["path"], args["content"])
        if name == "workspace_git_changed_files":
            return self.workspace.git_changed_files()
        if name == "workspace_git_stage_file":
            return self.workspace.git_stage_file(args["path"])
        if name == "workspace_git_unstage_file":
            return self.workspace.git_unstage_file(args["path"])
        if name == "workspace_git_commit":
            return self.workspace.git_commit(args["message"])
        if name == "workspace_git_status":
            return self.workspace.git_status()
        if name == "workspace_git_diff":
            return self.workspace.git_diff(args.get("staged", False), args.get("path", "."))
        if name == "workspace_git_branches":
            return self.workspace.git_branches()
        if name == "workspace_git_log":
            return self.workspace.git_log(args.get("limit", 10))
        if name == "workspace_run_task":
            return self.workspace.run_task(args["task"], args.get("timeout", 300))
        if name == "workspace_run_command":
            return self.workspace.run_command(args["command"], args.get("timeout", 60))
        if name == "workspace_http_request":
            if http_client is None:
                raise RuntimeError("workspace_http_request helper could not be imported.")
            return http_client.http_request(
                args["url"], args.get("method", "GET"), args.get("headers"),
                args.get("body"), args.get("timeout"), args.get("max_bytes"),
                bool(args.get("allow_local", False)),
            )
        if name in {"workspace_job_start", "workspace_job_status", "workspace_job_logs", "workspace_job_list", "workspace_job_stop", "workspace_wait_port"}:
            if job_manager is None:
                raise RuntimeError("Job manager helper could not be imported.")
            if name == "workspace_job_start":
                return job_manager.start_job(
                    self.workspace_root, args["command"], args.get("cwd", "."),
                    args.get("label"), args.get("env"), args.get("timeout_minutes"),
                )
            if name == "workspace_job_status":
                return job_manager.job_status(self.workspace_root, args["job_id"])
            if name == "workspace_job_logs":
                return job_manager.job_logs(self.workspace_root, args["job_id"], args.get("max_bytes", 65536))
            if name == "workspace_job_list":
                return job_manager.job_list(self.workspace_root)
            if name == "workspace_job_stop":
                return job_manager.stop_job(self.workspace_root, args["job_id"])
            return job_manager.wait_port(
                self.workspace_root, args["port"], args.get("host", "127.0.0.1"),
                args.get("timeout_seconds", 30), args.get("interval_ms", 200),
            )
        if name in {"start_assetripper_export", "assetripper_job_status", "cancel_assetripper_job"}:
            if assetripper_jobs is None:
                raise RuntimeError("AssetRipper job helpers could not be imported.")
            if name == "start_assetripper_export":
                return assetripper_jobs.start_assetripper_export(
                    self.workspace_root, args["executable_path"], args["input_path"],
                    args.get("output_path", "recovery/assetripper-export"),
                    args.get("mode", "Primary"), args.get("script_content_level", "Level1"),
                    args.get("timeout_minutes", 240),
                )
            if name == "assetripper_job_status":
                return assetripper_jobs.assetripper_job_status(self.workspace_root, args["job_id"])
            return assetripper_jobs.cancel_assetripper_job(self.workspace_root, args["job_id"])
        if name in {"start_browser_server", "browser_server_status", "stop_browser_server"}:
            if browser_server is None:
                raise RuntimeError("Browser server helpers could not be imported.")
            if name == "start_browser_server":
                return browser_server.start_browser_server(self.workspace_root, args.get("port_path", "browser-port"), args.get("port", 4173))
            if name == "browser_server_status":
                return browser_server.browser_server_status(self.workspace_root)
            return browser_server.stop_browser_server(self.workspace_root)
        if name == "inspect_browser_port":
            if browser_qa is None:
                raise RuntimeError("Browser QA helpers could not be imported.")
            return browser_qa.inspect_browser_port(self.workspace_root, args.get("port_path", "browser-port"))
        if name == "run_browser_qa":
            if browser_qa is None:
                raise RuntimeError("Browser QA helpers could not be imported.")
            return browser_qa.run_browser_qa(self.workspace_root, args["url"], args.get("port_path", "browser-port"), args.get("screenshot_path", "recovery/browser-qa.png"), args.get("timeout_seconds", 20), args.get("scenarios"))
        if name in {"list_unity_symbols", "search_unity_source"}:
            if code_intelligence is None:
                raise RuntimeError("Unity code intelligence helpers could not be imported.")
            if name == "list_unity_symbols":
                return code_intelligence.list_unity_symbols(
                    (args.get("install_path") or self.workspace_root), args.get("symbol_kind", "types"),
                    args.get("assembly_path", ""), args.get("query", ""), args.get("limit", 200),
                    args.get("timeout", 90),
                )
            return code_intelligence.search_unity_source(
                self.workspace_root, args["query"], args.get("source_path", "recovery/decompiled"),
                args.get("max_results", 100), args.get("case_sensitive", False),
                args.get("regex", False), args.get("context_lines", 2),
            )
        if name in {"update_reconstruction_feature", "report_reconstruction_readiness"}:
            if reconstruction_tracker is None:
                raise RuntimeError("Reconstruction tracker could not be imported.")
            if name == "update_reconstruction_feature":
                return reconstruction_tracker.update_reconstruction_feature(
                    self.workspace_root, args["feature_id"], args["name"], args.get("status", "planned"),
                    args.get("notes", ""), args.get("source_evidence"), args.get("asset_evidence"),
                    args.get("implementation_files"), args.get("test_files"),
                )
            return reconstruction_tracker.report_reconstruction_readiness(self.workspace_root, args.get("feature_id", ""))
        if name == "inspect_unity_serialized_file":
            if unity_asset_inspector is None:
                raise RuntimeError("Unity asset inspector could not be imported.")
            return unity_asset_inspector.inspect_unity_serialized_file(
                args["install_path"], args["asset_path"], args.get("max_file_mb", 256), args.get("max_objects", 1000)
            )
        if name in {"scan_unity_installation", "catalog_unity_assets", "inspect_recovery_tooling", "decompile_unity_type", "export_unity_assembly"}:
            if unity_recovery is None:
                raise RuntimeError("Unity recovery helpers could not be imported.")
            if name == "scan_unity_installation":
                return unity_recovery.scan_unity_installation(args.get("install_path", ""), self.workspace_root)
            if name == "catalog_unity_assets":
                return unity_recovery.catalog_unity_assets(args.get("install_path", ""), self.workspace_root, args.get("output_path", "recovery/asset-catalog.json"), args.get("batch_size", 500))
            if name == "inspect_recovery_tooling":
                return unity_recovery.inspect_recovery_tooling(self.workspace_root)
            if name == "decompile_unity_type":
                return unity_recovery.decompile_unity_type(args["install_path"], self.workspace_root, args["type_name"], args.get("assembly_path", ""), args.get("timeout", 90))
            if name == "export_unity_assembly":
                return unity_recovery.export_unity_assembly(args["install_path"], self.workspace_root, args.get("assembly_path", ""), args.get("output_path", "recovery/decompiled"), args.get("timeout", 180))
        raise RuntimeError(f"unknown workspace tool '{name}'")
    def load_config(self):
        servers = _read_config().get("mcpServers", {}) or {}
        for sid, spec in servers.items():
            self.clients[sid] = MCPClient(
                sid, spec.get("command"), spec.get("args"), spec.get("env"))
        log(f"configured {len(self.clients)} MCP server(s): {', '.join(self.clients) or '(none)'}", "cy")

    def start_all(self):
        # Launch every configured server IN PARALLEL, not one after another.
        # client.start() can block for up to ~12s (its own "wait for Studio's
        # tools to appear" grace loop) - with a sequential for-loop, Roblox
        # being first in config.json meant every OTHER server (Blender, any
        # addon) didn't even begin launching until Roblox's grace loop gave
        # up, even though that addon has nothing to do with Roblox and could
        # have been ready in 1-2s. A thread per client removes that
        # dependency entirely: a slow/absent Roblox Studio no longer holds up
        # an addon server the user actually wants right now.
        threads = []
        for sid, client in self.clients.items():
            def _run(sid=sid, client=client):
                try:
                    client.start()
                except Exception as e:
                    log(f"[{sid}] failed to start: {e}  (other servers continue)", "rd")
            t = threading.Thread(target=_run, daemon=True)
            t.start()
            threads.append(t)
        for t in threads:
            t.join()
        self.rebuild_index()

    def rebuild_index(self):
        """Aggregate server tools. Collisions get a 'server/' prefix."""
        with self.index_lock:
            self.index = {}
            for sid, client in self.clients.items():
                for t in (client.tools_cache or []):
                    name = t.get("name")
                    if not name:
                        continue
                    # Keep Roblox as a distinct tool namespace. Workspace and
                    # other MCP tools remain independently addressable, even when
                    # a third-party server advertises the same bare name.
                    advertised = f"{sid}/{name}" if sid == PRIMARY_SERVER_ID else name
                    if advertised in self.index:
                        advertised = f"{sid}/{name}"
                    base = advertised
                    suffix = 2
                    while advertised in self.index:
                        advertised = f"{base}#{suffix}"
                        suffix += 1
                    self.index[advertised] = (client, name)

    def list_tools(self, refresh=False):
        if refresh:
            for sid, client in self.clients.items():
                try:
                    if not client.is_alive():
                        client.start()
                    else:
                        client.refresh_tools()
                except Exception as e:
                    log(f"[{sid}] refresh failed: {e}", "yl")
            self.rebuild_index()
        out = []
        for sid, client in self.clients.items():
            for t in (client.tools_cache or []):
                name = t.get("name")
                advertised = name
                with self.index_lock:
                    # find the advertised key that maps to this (client, name)
                    for k, (holder, real) in self.index.items():
                        if holder is client and real == name:
                            advertised = k
                            break
                tt = dict(t)
                tt["name"] = advertised
                tt["server"] = sid
                out.append(tt)
        out.extend(self._workspace_tools())
        return out

    def call(self, name, arguments, timeout):
        if arguments is not None and not isinstance(arguments, dict):
            raise RuntimeError("MCP arguments must be a JSON object.")
        args = arguments or {}
        if name == "zeroscript_handoff_to_cli":
            if agent_control is None:
                raise RuntimeError("ZeroScript agent-control module is unavailable.")
            result = agent_control.handoff(arguments or {}, self._workspace_description())
            return {"text": json.dumps(result, ensure_ascii=False, indent=2), "images": []}
        if name == "zeroscript_agent_control_status":
            if agent_control is None:
                raise RuntimeError("ZeroScript agent-control module is unavailable.")
            return {"text": json.dumps(agent_control.status(), ensure_ascii=False, indent=2), "images": []}
        if name == "roblox_export_tree":
            if export_roblox_tree is None:
                raise RuntimeError("Roblox tree exporter could not be imported. Check workspace_agent/roblox_export.py.")
            result = export_roblox_tree(self, arguments or {})
            return {"text": json.dumps(result, ensure_ascii=False, indent=2), "images": []}
        if name == "workspace_preview_image":
            args = arguments or {}
            if not self.workspace_configured or self.workspace is None:
                raise RuntimeError("No project workspace has been selected. Select a workspace before previewing images.")
            relative_path = args.get("path")
            if not isinstance(relative_path, str) or not relative_path.strip():
                raise RuntimeError("A workspace-relative image path is required.")
            # Accept workspace-relative paths with either separator, plus absolute
            # paths that still resolve INSIDE the selected workspace. Normalize
            # separators before resolution; otherwise a Windows-style relative
            # path can be interpreted as a literal backslash-containing filename
            # by path implementations running in compatibility/test environments.
            normalized_path = relative_path.strip()
            if not Path(normalized_path).is_absolute():
                normalized_path = normalized_path.replace("\\", os.sep).replace("/", os.sep)
            try:
                raw_target = self.workspace.root / normalized_path
                if raw_target.is_symlink():
                    raise RuntimeError("Symbolic-link image paths are not supported.")
                target = self.workspace._resolve(normalized_path)
            except (OSError, ValueError, WorkspaceError) as exc:
                raise RuntimeError(f"Could not resolve image path '{relative_path}': {exc}") from exc
            if not target.exists():
                raise RuntimeError(f"Image file not found: '{relative_path}' (resolved path: {target}).")
            if not target.is_file():
                raise RuntimeError(f"Image path is not a regular file: '{relative_path}' (resolved path: {target}).")
            mime_types = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                          ".webp": "image/webp", ".gif": "image/gif", ".svg": "image/svg+xml"}
            mime_type = mime_types.get(target.suffix.lower())
            if not mime_type:
                raise RuntimeError("Unsupported image type. Supported formats: PNG, JPEG, WebP, GIF, SVG.")
            size = target.stat().st_size
            if size < 1 or size > 8 * 1024 * 1024:
                raise RuntimeError(f"Image size must be between 1 byte and 8 MB; file is {size} bytes.")
            data = target.read_bytes()
            signatures = {
                "image/png": data.startswith(b"\x89PNG\r\n\x1a\n"),
                "image/jpeg": data.startswith(b"\xff\xd8\xff"),
                "image/gif": data.startswith((b"GIF87a", b"GIF89a")),
                "image/webp": len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP",
            }
            if mime_type == "image/svg+xml":
                try:
                    svg = data.decode("utf-8-sig")
                except UnicodeDecodeError as exc:
                    raise RuntimeError("SVG must be valid UTF-8 text.") from exc
                if "<svg" not in svg[:4096].lower():
                    raise RuntimeError("The .svg file does not contain a valid SVG root element.")
                lower_svg = svg.lower()
                if (any(token in lower_svg for token in ("<script", "<foreignobject", "javascript:", " onload=", " onclick=", " onerror="))
                        or re.search(r"""(?:href|src)\s*=\s*["']\s*(?:https?:|file:|javascript:|//)""", svg, re.I)):
                    raise RuntimeError("SVG contains active script, event handlers, embedded HTML, or external resource references; remove them before previewing.")
            elif not signatures[mime_type]:
                expected = {
                    "image/png": "PNG signature 89 50 4E 47 0D 0A 1A 0A",
                    "image/jpeg": "JPEG signature FF D8 FF",
                    "image/gif": "GIF87a or GIF89a signature",
                    "image/webp": "RIFF....WEBP signature",
                }[mime_type]
                actual = data[:12].hex(" ").upper() or "(empty)"
                raise RuntimeError(
                    f"Image validation failed for '{target}' (extension {target.suffix.lower()}, "
                    f"{size} bytes): expected {expected}; first bytes were {actual}. "
                    "The path resolved and the file was read, so verify that this is the intended image file."
                )
            metadata = {"path": target.relative_to(self.workspace.root).as_posix(),
                        "mime_type": mime_type, "bytes": size,
                        "message": "Image attached for visual inspection."}
            return {"text": json.dumps(metadata, ensure_ascii=False, indent=2),
                    "images": [{"data": base64.b64encode(data).decode("ascii"), "mimeType": mime_type}]}
        if name.startswith("workspace_"):
            result = self._call_workspace(name, arguments)
            return {"text": json.dumps(result, ensure_ascii=False, indent=2), "images": []}
        with self.index_lock:
            entry = self.index.get(name)
            # Backward compatibility for old conversations that call a Roblox
            # command without its required roblox/ namespace.
            if entry is None and "/" not in name:
                candidates = [(key, value) for key, value in self.index.items()
                              if value[0].id == PRIMARY_SERVER_ID and value[1] == name]
                if len(candidates) == 1:
                    entry = candidates[0][1]
        if entry is None:
            # Maybe a freshly added tool - rebuild once and retry.
            self.rebuild_index()
            with self.index_lock:
                entry = self.index.get(name)
                if entry is None and "/" not in name:
                    candidates = [(key, value) for key, value in self.index.items()
                                  if value[0].id == PRIMARY_SERVER_ID and value[1] == name]
                    if len(candidates) == 1:
                        entry = candidates[0][1]
        if entry is None:
            raise RuntimeError(f"unknown tool '{name}'")
        holder, real_name = entry
        return holder.call_tool(real_name, arguments, timeout)

    def restart(self, server_id=None):
        targets = [self.clients[server_id]] if server_id and server_id in self.clients else list(self.clients.values())
        for client in targets:
            try:
                client.restart()
            except Exception as e:
                log(f"[{client.id}] restart failed: {e}", "rd")
        self.rebuild_index()

    def health(self):
        return [{"id": sid, "alive": c.is_alive(), "tools": len(c.tools_cache)}
                for sid, c in self.clients.items()]

    def any_alive(self):
        return any(c.is_alive() for c in self.clients.values())


# ══════════════════════════════════════════════════════════════════════════
#  WEBSOCKET SERVER
# ══════════════════════════════════════════════════════════════════════════
mgr = MCPManager()
clients = set()
client_roles = {}
client_inflight = {}
# A browser-to-CLI handoff is allowed only while a configured CLI is waiting.
# Keys are WebSocket objects; handler cleanup removes stale arm records.
client_takeover_armed = {}
agent_control = AgentControlStore() if AgentControlStore is not None else None
# Serializes tool admission with run ownership transitions. The tool itself runs
# outside this lock; only the ownership check and in-flight counter are atomic.
agent_control_lock = asyncio.Lock()

# ── Studio connectivity probe ──────────────────────────────────────────────
# The MCP server process stays alive even when Roblox Studio is closed or its
# MCP option is disabled - tool calls then return instantly with an "Unable to
# find an active Studio instance" text. So "mcp_alive" alone is misleading.
#
# TWO LEVELS (validated live 2026-06):
#  - list_roblox_studios: instant, side-effect-free. studios == [] means NO Studio
#    is connected to the MCP (app closed, OR its "Studio as MCP Server" option is
#    disabled - the two are indistinguishable at this layer). A non-empty list
#    means a Studio app IS connected, BUT note its entry stays present (active:true)
#    even when no place is open - only its "name" goes null. So presence != usable.
#  - get_studio_state: tells whether a PLACE is actually loaded. With a place open
#    it returns "Available DataModels: ..."; with the Studio on the home screen (or
#    the active place closed) it returns "...doesn't have a place opened / previously
#    active Studio has disconnected". That is the authoritative "place loaded" signal
#    (same phrase the call path already recognises in core/main.js).
STUDIO_PROBE_TOOL = "roblox/list_roblox_studios"
STUDIO_STATE_TOOL = "roblox/get_studio_state"
# Substrings get_studio_state emits when a Studio is connected but no place is open.
NO_PLACE_MARKERS = ("doesn't have a place", "no place opened", "place opened",
                    "has disconnected", "no active studio")


def _probe_tool_text(tool):
    """Call a side-effect-free probe tool with no args; return its text, or None if
    the tool is unavailable / the server is busy / it errored (best-effort)."""
    with mgr.index_lock:
        entry = mgr.index.get(tool)
    if entry is None:
        return None
    holder, real_name = entry
    # Never queue behind a long-running tool call (the probe is best-effort).
    if not holder.call_lock.acquire(blocking=False):
        return None
    try:
        if not holder.is_alive():
            return None
        msg = holder._request("tools/call", {"name": real_name, "arguments": {}}, timeout=8)
        if not msg or msg.get("error"):
            return None
        content = msg.get("result", {}).get("content", [])
        return "\n".join(it.get("text", "") for it in content if it.get("type") == "text")
    except Exception:
        return None
    finally:
        holder.call_lock.release()


def probe_studio():
    """Two-level Studio connectivity. Returns {"app": x, "place": y} where each is
    True / False / None (None = unknown: probe tool missing or server busy).
      app   - a Roblox Studio instance is connected to the MCP server. False = Studio
              closed OR its MCP-server option disabled (indistinguishable here).
      place - a place/datamodel is actually loaded and usable. False = Studio open on
              the home screen, or the active place was closed. Only meaningful when
              app is True (when app is False/None, place mirrors it)."""
    roblox = mgr.clients.get(PRIMARY_SERVER_ID)
    if roblox is not None and roblox.is_alive() and not roblox.tools_cache:
        # StudioMCP advertises ZERO tools - including list_roblox_studios itself -
        # until Studio actually attaches. That makes _probe_tool_text() below
        # return None (tool missing) the same way it would for a genuinely
        # transient "probe busy" blip, even though "Studio is simply closed" is
        # the common, SUSTAINED case here, not a blip. Left unhandled, the
        # extension's "unknown = don't degrade" rule (by design, for real
        # transient blips) then leaves the status dot stuck GREEN forever with
        # Studio fully closed (seen live 2026-07-11: dot stayed "on", tooltip
        # showing only an addon server's tool count). An alive client with an
        # empty catalogue is an unambiguous "not connected", so short-circuit
        # straight to that verdict instead of falling through to "unknown".
        return {"app": False, "place": False}
    text = _probe_tool_text(STUDIO_PROBE_TOOL)
    if text is None:
        return {"app": None, "place": None}
    try:
        studios = json.loads(text).get("studios") or []
    except Exception:
        return {"app": None, "place": None}
    if not studios:
        return {"app": False, "place": False}
    # A Studio app is connected - now check whether a place is actually open.
    state = _probe_tool_text(STUDIO_STATE_TOOL)
    if state is None:
        return {"app": True, "place": None}
    low = state.lower()
    place = not any(m in low for m in NO_PLACE_MARKERS)
    return {"app": True, "place": place}


def safe_call(name, arguments, timeout):
    """Never raises. Always returns a dict the extension can feed back to DeepSeek."""
    try:
        # Workspace introspection is local and must never wait behind an MCP
        # server's call lock/restart. The extension uses this for connection and
        # per-conversation workspace setup, so keep it on a direct fast path.
        if name == "workspace_info":
            result = {"text": json.dumps(mgr._workspace_description(), ensure_ascii=False), "images": []}
        elif name == "workspace_recent_roots":
            result = {"text": json.dumps(mgr._call_workspace(name, arguments or {}), ensure_ascii=False), "images": []}
        else:
            result = mgr.call(name, arguments, timeout)
        return {"ok": True, "text": result["text"], "images": result["images"]}
    except TimeoutError as e:
        return {"ok": False, "error": str(e), "kind": "timeout"}
    except Exception as e:
        return {"ok": False, "error": str(e), "kind": type(e).__name__}


async def run_tool_task(ws, name, args, timeout, rid, role):
    """Execute one tool off the socket read loop; always settle in-flight accounting."""
    t0 = time.monotonic()
    try:
        if name == "zeroscript_handoff_to_cli" and role == "browser":
            # Recheck the CLI readiness while holding the same lock used by
            # disarm/claim. This closes the race where the CLI exits between
            # request admission and the handoff tool actually queueing state.
            async with agent_control_lock:
                cli_ready = any(
                    client_roles.get(client) == "cli" and bool(client_takeover_armed.get(client))
                    for client in clients
                )
                if cli_ready:
                    res = await asyncio.to_thread(safe_call, name, args, timeout)
                else:
                    res = {
                        "ok": False,
                        "kind": "control",
                        "error": "No configured CLI is waiting for takeover. The handoff was not queued; the browser agent retains control.",
                    }
        else:
            res = await asyncio.to_thread(safe_call, name, args, timeout)
        elapsed = time.monotonic() - t0
        tag = "gr" if res.get("ok") else "rd"
        summary = (res.get("text") or res.get("error") or "")[:80].replace("\\n", " ")
        slow = "  [SLOW]" if elapsed > 5 else ""
        log(f"<- {name} ({elapsed:.1f}s){slow}: {summary}", tag, terminal=not res.get("ok") or elapsed > 5)
        try:
            await ws.send(json.dumps({"type": "tool_result", "id": rid, **res}))
        except websockets.ConnectionClosed:
            pass
        if res.get("ok") and name.startswith("workspace_"):
            await broadcast_status()
        if res.get("ok") and name == "zeroscript_handoff_to_cli":
            await broadcast_agent_control()
    finally:
        # A disconnected handler may already have removed this socket's
        # counters. Do not resurrect an orphaned entry after the task finishes.
        if ws in client_inflight:
            client_inflight[ws] = max(0, int(client_inflight.get(ws, 1)) - 1)

def _connected_status_payload(studio, studio_proc):
    """Build a complete bridge status payload without blocking the asyncio loop."""
    return json.dumps({
        "type": "connected",
        "mcp_alive": mgr.any_alive(),
        "studio": studio["place"], "studio_app": studio["app"],
        "studio_proc": studio_proc,
        "servers": mgr.health(),
        "tools": mgr.list_tools(),
        "workspace": mgr._workspace_description(),
        "agent_control": agent_control.status() if agent_control is not None else {},
        "port": PORT,
    })


async def broadcast_status():
    """Broadcast a fresh status snapshot while keeping the socket event loop responsive.

    MCP manager introspection can block when a child process is slow or unhealthy.
    Running the entire snapshot in a worker thread prevents routine workspace tools
    (including workspace_info) from starving WebSocket ping/pong and agent-control
    messages while they finish.
    """
    if not clients:
        return
    try:
        _st = await asyncio.to_thread(probe_studio)
        _proc = await asyncio.to_thread(_roblox_studio_app_running)
        # Keep manager health/list_tools calls off the asyncio loop too. In
        # particular, workspace_info triggers this broadcast on every success.
        payload = await asyncio.to_thread(_connected_status_payload, _st, _proc)
    except Exception as exc:
        log(f"status broadcast failed: {exc}", "yl", terminal=False)
        return
    for ws in list(clients):
        try:
            await ws.send(payload)
        except Exception:
            pass


async def broadcast_agent_control():
    """Push ownership changes so the browser agent can stop when CLI takes over."""
    if agent_control is None:
        return
    try:
        state = await asyncio.to_thread(agent_control.status)
        payload = json.dumps({"type": "agent_control", "control": state}, ensure_ascii=False)
    except Exception as exc:
        log(f"agent-control broadcast failed: {exc}", "rd")
        return
    for client in list(clients):
        if client_roles.get(client) != "browser":
            continue
        try:
            await client.send(payload)
        except Exception:
            pass


async def handler(ws):
    peer = getattr(ws, "remote_address", ("?",))[0]
    clients.add(ws)
    client_roles[ws] = "browser"
    client_inflight[ws] = 0
    log(f"client connected  ({peer})  [{len(clients)} client(s)]", "gr")
    try:
        _st = await asyncio.to_thread(probe_studio)
        await ws.send(json.dumps({
            "type": "connected",
            "mcp_alive": mgr.any_alive(),
            "studio": _st["place"], "studio_app": _st["app"],
            "studio_proc": await asyncio.to_thread(_roblox_studio_app_running),
            "servers": mgr.health(),
            "tools": mgr.list_tools(),
            "workspace": mgr._workspace_description(),
            "agent_control": agent_control.status() if agent_control is not None else {},
            "port": PORT,
        }))
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            mtype = msg.get("type")
            rid = msg.get("id")

            if mtype == "register_client":
                requested = msg.get("client")
                if requested in ("browser", "cli"):
                    client_roles[ws] = requested
                await ws.send(json.dumps({"type": "client_registered", "client": client_roles.get(ws, "browser")}))

            elif mtype == "agent_control_status":
                try:
                    state = await asyncio.to_thread(agent_control.status) if agent_control is not None else {}
                    await ws.send(json.dumps({"type": "agent_control_result", "id": rid, "ok": True, "control": state}))
                except Exception as exc:
                    await ws.send(json.dumps({"type": "agent_control_result", "id": rid, "ok": False, "error": str(exc)}))

            elif mtype in ("agent_arm", "agent_disarm", "agent_claim", "agent_start", "agent_heartbeat", "agent_complete", "agent_stop", "agent_yield"):
                if client_roles.get(ws, "browser") != "cli":
                    await ws.send(json.dumps({"type": "agent_control_result", "id": rid, "ok": False, "error": "This control action is available only to the registered CLI client."}))
                    continue
                try:
                    if agent_control is None:
                        raise RuntimeError("ZeroScript agent-control module is unavailable.")
                    async with agent_control_lock:
                        if mtype == "agent_arm":
                            arm_session = str(msg.get("session_id") or "").strip()
                            if not arm_session:
                                raise RuntimeError("A session ID is required to arm CLI takeover.")
                            current_control = await asyncio.to_thread(agent_control.status)
                            if current_control.get("owner") == "cli" and current_control.get("state") in {"active", "stop_requested"}:
                                raise RuntimeError("The CLI already owns an active run; finish or stop it before arming another takeover.")
                            another_waiter = any(
                                client is not ws
                                and client_roles.get(client) == "cli"
                                and bool(client_takeover_armed.get(client))
                                for client in clients
                            )
                            if another_waiter:
                                raise RuntimeError("Another CLI session is already armed and waiting for takeover.")
                            client_takeover_armed[ws] = arm_session
                            payload = {"ok": True, "control": current_control, "armed": True}
                        elif mtype == "agent_disarm":
                            requested_session = str(msg.get("session_id") or "").strip()
                            active_arm = client_takeover_armed.get(ws)
                            if active_arm and requested_session and requested_session != active_arm:
                                raise RuntimeError("The requested session does not match the armed CLI session.")
                            current_control = await asyncio.to_thread(agent_control.status)
                            another_waiter = any(
                                client is not ws
                                and client_roles.get(client) == "cli"
                                and bool(client_takeover_armed.get(client))
                                for client in clients
                            )
                            if active_arm and current_control.get("state") == "queued" and not another_waiter:
                                # If the only waiter exits before claiming, cancel
                                # the queued handoff so the browser is not left paused.
                                current_control = await asyncio.to_thread(
                                    agent_control.request_stop, current_control.get("run_id")
                                )
                            client_takeover_armed.pop(ws, None)
                            payload = {"ok": True, "control": current_control, "armed": False}
                        elif mtype == "agent_claim":
                            current_control = await asyncio.to_thread(agent_control.status)
                            selected_workspace = mgr._workspace_description()
                            expected_root = str(current_control.get("workspace") or "")
                            selected_root = str(selected_workspace.get("root") or "") if selected_workspace.get("configured") is True else ""
                            if expected_root and os.path.normcase(os.path.abspath(expected_root)) != os.path.normcase(os.path.abspath(selected_root)):
                                raise RuntimeError("The selected workspace no longer matches the queued run. Restore the original project folder before claiming or resuming this task.")
                            browser_busy = any(
                                client_roles.get(client) == "browser" and count > 0
                                for client, count in client_inflight.items()
                            )
                            result = await asyncio.to_thread(agent_control.claim, msg.get("session_id", ""), msg.get("run_id"), not browser_busy)
                            payload = {"ok": bool(result.get("ok")), "control": result.get("control") or {}, "error": result.get("error"), "busy": bool(result.get("busy"))}
                        elif mtype == "agent_start":
                            browser_busy = any(
                                client_roles.get(client) == "browser" and count > 0
                                for client, count in client_inflight.items()
                            )
                            if browser_busy:
                                raise RuntimeError("A browser tool call is still in flight. Wait for it to finish before starting a direct CLI task.")
                            state = await asyncio.to_thread(
                                agent_control.start_run,
                                {"goal": msg.get("goal", ""), "context": msg.get("context", ""),
                                 "plan": msg.get("plan") or [], "acceptance_criteria": msg.get("acceptance_criteria") or []},
                                mgr._workspace_description(), msg.get("session_id", ""))
                            payload = {"ok": True, "control": state}
                        elif mtype == "agent_heartbeat":
                            state = await asyncio.to_thread(agent_control.heartbeat, msg.get("run_id", ""), msg.get("session_id", ""), msg.get("progress"))
                            payload = {"ok": True, "control": state}
                        elif mtype == "agent_complete":
                            state = await asyncio.to_thread(agent_control.finish, msg.get("run_id", ""), msg.get("session_id", ""), msg.get("status", "completed"), msg.get("summary", ""))
                            payload = {"ok": True, "control": state}
                        elif mtype == "agent_stop":
                            state = await asyncio.to_thread(agent_control.request_stop, msg.get("run_id"))
                            payload = {"ok": True, "control": state}
                        else:
                            cli_busy = any(
                                client_roles.get(client) == "cli" and count > 0
                                for client, count in client_inflight.items()
                            )
                            if cli_busy:
                                raise RuntimeError("A CLI tool call is still in flight. Wait for it to finish before returning control to the browser.")
                            state = await asyncio.to_thread(agent_control.yield_to_browser, msg.get("run_id", ""), msg.get("session_id", ""), msg.get("summary", ""))
                            payload = {"ok": True, "control": state}
                except Exception as exc:
                    payload = {"ok": False, "error": str(exc)}
                if payload.get("ok") and mtype == "agent_claim":
                    # Ownership is now persisted; this session no longer needs
                    # to advertise itself as waiting for a future handoff.
                    client_takeover_armed.pop(ws, None)
                await ws.send(json.dumps({"type": "agent_control_result", "id": rid, **payload}))
                if payload.get("ok") and mtype not in {"agent_heartbeat", "agent_arm"}:
                    await broadcast_agent_control()

            elif mtype == "ping":
                await ws.send(json.dumps({"type": "pong", "id": rid}))

            elif mtype == "studio_status":
                studio = await asyncio.to_thread(probe_studio)
                await ws.send(json.dumps({
                    "type": "studio_status", "id": rid,
                    "studio": studio["place"], "studio_app": studio["app"],
                    "studio_proc": await asyncio.to_thread(_roblox_studio_app_running),
                    "workspace": mgr._workspace_description(),
            "agent_control": agent_control.status() if agent_control is not None else {},
                    "mcp_alive": mgr.any_alive(),
                }))

            elif mtype == "list_tools":
                try:
                    tools = await asyncio.to_thread(mgr.list_tools, True)
                except Exception as e:
                    tools = mgr.list_tools()
                    log(f"list_tools error: {e}", "yl")
                _st = await asyncio.to_thread(probe_studio)
                await ws.send(json.dumps({
                    "type": "tools", "id": rid,
                    "tools": tools, "mcp_alive": mgr.any_alive(),
                    "workspace": mgr._workspace_description(),
            "agent_control": agent_control.status() if agent_control is not None else {},
                    "studio": _st["place"], "studio_app": _st["app"],
                    "studio_proc": await asyncio.to_thread(_roblox_studio_app_running),
                    "servers": mgr.health(),
                }))

            elif mtype == "call_tool":
                name = msg.get("name", "")
                raw_args = msg.get("arguments")
                # Tool arguments MUST be a JSON object. A model occasionally
                # emits an array (e.g. [{"url": ...}]) or a bare string when
                # its function-call decoder misfires; treating that as a dict
                # crashed the WebSocket handler at `args.keys()` below and
                # dropped the client (reported live 2026-10-27 with a list
                # argument). Reject malformed shapes with a clean tool_result
                # the model can correct, instead of tearing down the socket.
                if raw_args is None:
                    args = {}
                elif isinstance(raw_args, dict):
                    args = raw_args
                else:
                    args = None
                if args is None:
                    await ws.send(json.dumps({
                        "type": "tool_result", "id": rid, "ok": False,
                        "kind": "invalid_arguments",
                        "error": (
                            f"Tool '{name}' arguments must be a JSON object, "
                            f"not {type(raw_args).__name__}. Send an object "
                            f"like {{\"arg\": \"value\"}} and retry."
                        ),
                    }))
                    continue
                role = client_roles.get(ws, "browser")
                blocked_reason = None
                async with agent_control_lock:
                    if role == "browser" and name == "zeroscript_handoff_to_cli":
                        cli_ready = any(
                            client_roles.get(client) == "cli" and bool(client_takeover_armed.get(client))
                            for client in clients
                        )
                        if not cli_ready:
                            blocked_reason = (
                                "No configured ZeroScript CLI is waiting for takeover. "
                                "Start the CLI, configure its model endpoint, and run /takeover first. "
                                "This handoff was not queued; the browser agent retains control."
                            )
                    if (blocked_reason is None and role == "browser" and agent_control is not None
                            and not agent_control.browser_tool_allowed(name)):
                        blocked_reason = (
                            "The independent CLI agent currently owns this task, or a handoff is already queued. "
                            "Browser-originated tool calls are paused until control is released."
                        )
                    if blocked_reason is None:
                        client_inflight[ws] = int(client_inflight.get(ws, 0)) + 1
                if blocked_reason is not None:
                    await ws.send(json.dumps({
                        "type": "tool_result", "id": rid, "ok": False, "kind": "control",
                        "error": blocked_reason,
                    }))
                    continue
                timeout = float(msg.get("timeout", 120000)) / 1000.0
                log(f"-> tool  {name}({', '.join(args.keys())}) [{role}]", "cy", terminal=False)
                # Run the tool as a BACKGROUND task instead of awaiting it here.
                # Awaiting inline parks this read loop for the WHOLE tool call, so
                # a long tool (e.g. wait_job_finished > 25s) means the client's
                # app-level pings are never read/answered - its half-open-socket
                # watchdog then force-closes the connection and the in-flight call
                # is dropped as "bridge unreachable" (reported live). As a task,
                # the loop stays free to answer pings/status while the tool runs.
                # The extension only ever has ONE call_tool in flight (its agent
                # loop awaits each result before sending the next), so this never
                # overlaps tool executions.
                asyncio.create_task(run_tool_task(ws, name, args, timeout, rid, role))

            elif mtype in ("add_server", "remove_server"):
                # Adding/removing an addon MCP server rewrites config.json, which
                # the bridge only reads at launch - so we ack, then restart the
                # whole process to pick it up cleanly. The primary Roblox server
                # is protected inside config_add/remove_server.
                if mtype == "add_server":
                    ok, err = await asyncio.to_thread(
                        config_add_server,
                        msg.get("server_id"), msg.get("command"),
                        msg.get("args"), msg.get("env"))
                else:
                    ok, err = await asyncio.to_thread(
                        config_remove_server, msg.get("server_id"))
                await ws.send(json.dumps({
                    "type": "server_changed", "id": rid,
                    "ok": ok, "error": err, "restarting": ok,
                }))
                if ok:
                    # Give the ack a beat to flush over the socket, then restart.
                    async def _do_restart():
                        await asyncio.sleep(0.4)
                        restart_self()
                    asyncio.create_task(_do_restart())

            elif mtype == "bridge_reload":
                # Hot-reload the workspace tool layer without dropping the
                # WebSocket or restarting any MCP server process. Reloads the
                # workspace_agent package modules and re-imports the optional
                # helpers (http_client, browser_qa, ...) so edits on disk become
                # live. Workspace selection and the running MCP child processes
                # are untouched.
                try:
                    reloaded = await asyncio.to_thread(_reload_workspace_modules)
                    payload = {"ok": True, "reloaded": reloaded}
                except Exception as exc:
                    payload = {"ok": False, "error": str(exc)}
                if payload.get("ok"):
                    # Push an updated catalogue so connected clients refresh
                    # their cached tool list without a manual reconnect.
                    try:
                        tools_now = await asyncio.to_thread(mgr.list_tools, True)
                    except Exception:
                        tools_now = mgr.list_tools()
                    try:
                        await ws.send(json.dumps({
                            "type": "tools", "id": rid,
                            "tools": tools_now, "mcp_alive": mgr.any_alive(),
                            "workspace": mgr._workspace_description(),
                            "agent_control": agent_control.status() if agent_control is not None else {},
                            "reloaded": payload.get("reloaded", []),
                        }))
                    except Exception:
                        await ws.send(json.dumps({"type": "bridge_reload_result", "id": rid, **payload}))
                else:
                    await ws.send(json.dumps({"type": "bridge_reload_result", "id": rid, **payload}))

            elif mtype == "restart_mcp":
                sid = msg.get("server")
                try:
                    await asyncio.to_thread(mgr.restart, sid)
                    ok, err = True, None
                except Exception as e:
                    ok, err = False, str(e)
                await ws.send(json.dumps({
                    "type": "mcp_status", "id": rid,
                    "alive": mgr.any_alive(), "ok": ok, "error": err,
                    "servers": mgr.health(), "tools": mgr.list_tools(),
                }))

            else:
                await ws.send(json.dumps({
                    "type": "error", "id": rid,
                    "error": f"unknown message type: {mtype}",
                }))
    except websockets.ConnectionClosed:
        pass
    except Exception as e:
        log(f"handler error: {e}", "rd")
    finally:
        should_broadcast_control = False
        # Serialize disconnect cleanup with the handoff task's readiness check
        # and queue write. Otherwise a CLI could disconnect after the readiness
        # check but just before the handoff was recorded, leaving an orphan queue.
        async with agent_control_lock:
            armed_session = client_takeover_armed.pop(ws, None)
            clients.discard(ws)
            client_roles.pop(ws, None)
            client_inflight.pop(ws, None)
            if armed_session and agent_control is not None:
                try:
                    current_control = await asyncio.to_thread(agent_control.status)
                    another_waiter = any(
                        client_roles.get(client) == "cli" and bool(client_takeover_armed.get(client))
                        for client in clients
                    )
                    if current_control.get("state") == "queued" and not another_waiter:
                        await asyncio.to_thread(agent_control.request_stop, current_control.get("run_id"))
                        should_broadcast_control = True
                except Exception as exc:
                    log(f"could not clean up abandoned handoff: {exc}", "yl")
        if should_broadcast_control:
            await broadcast_agent_control()
        log(f"client disconnected  [{len(clients)} client(s)]", "yl")


async def server_watch():
    """Poll every MCP server and restart any that died unexpectedly (e.g. the
    StudioMCP proxy crashing on its own - see stop()'s taskkill /T fix and the
    stderr logging above for why this used to happen silently). Without this,
    a dead server only got noticed on the NEXT real tool call, which is what
    made "Studio looks connected but nothing responds" possible."""
    # Crash-LOOP detection thresholds: LOOP_N deaths within LOOP_WINDOW seconds
    # means something is killing (or instantly crashing) the server every time
    # we bring it back - the silent restart cycle the auto-restart otherwise
    # hides completely. We still keep restarting (the cause may be transient,
    # e.g. the user is about to start Blender), but the terminal now NAMES the
    # problem: exit code, the child's last stderr lines, and - for a port-bound
    # server - who is squatting the port. Banner re-prints at most every
    # LOOP_WARN_COOLDOWN so the terminal stays readable.
    LOOP_N = 3
    LOOP_WINDOW = 60
    LOOP_WARN_COOLDOWN = 120
    while True:
        await asyncio.sleep(5)
        for sid, client in list(mgr.clients.items()):
            try:
                if not client.is_alive():
                    now = time.time()
                    # restart_times holds RESTART ATTEMPTS (appended just before
                    # each start below), never per-poll sightings - appending on
                    # every 5s poll would keep the window full forever and the
                    # "slow down" branch would then block restarts permanently.
                    client.restart_times = [t for t in client.restart_times if now - t < LOOP_WINDOW]
                    looping = len(client.restart_times) >= LOOP_N
                    if looping and now - client.loop_warned_at > LOOP_WARN_COOLDOWN:
                        client.loop_warned_at = now
                        log(f"[{sid}] CRASH LOOP: died {len(client.restart_times)} times in the last "
                            f"{LOOP_WINDOW}s (last exit code: {client.last_exit}). Something is killing it "
                            f"or it cannot start.", "rd")
                        if client.start_error:
                            log(f"[{sid}] {client.start_error}", "rd")
                        elif client.stderr_tail:
                            log(f"[{sid}] last error output (usually the real reason):", "rd")
                            for ln in client.stderr_tail:
                                log(f"[{sid}]   {ln}", "yl")
                        else:
                            log(f"[{sid}] the server printed no error output before dying.", "yl")
                        # Port forensics: name the process squatting a port this
                        # server needs. For the primary Roblox proxy that is
                        # Studio's MCP port; a squatter there (seen live: a
                        # 'ropilot' app) makes the proxy die/misbehave forever.
                        if sid == "roblox":
                            owner = _port_owner(STUDIO_MCP_PORT)
                            if owner:
                                pid, name, path = owner
                                if "roblox" not in (name or "").lower() and "studio" not in (path or "").lower():
                                    log(f"[{sid}] port {STUDIO_MCP_PORT} is held by '{name}' (pid {pid}, {path}) - "
                                        f"close that program, it is squatting Studio's MCP port.", "rd")
                        log(f"[{sid}] common causes: its app is not running (e.g. Blender + addon), a port "
                            f"conflict, an antivirus killing it, or a bad command in config.json. "
                            f"Auto-restart continues in the background.", "yl")
                    if looping and client.restart_times and now - client.restart_times[-1] < 15:
                        # Clearly hopeless right now: drop to a ~15s cadence so a
                        # broken command isn't hammer-spawned every 5 seconds,
                        # while still retrying forever (the cause may clear, e.g.
                        # the user finally opens Blender).
                        continue
                    client.restart_times.append(now)
                    log(f"[{sid}] found dead - auto-restarting...", "yl")
                    await asyncio.to_thread(client.start)
                    mgr.rebuild_index()
                    await broadcast_status()  # tell any connected extension right away
            except Exception as e:
                log(f"[{sid}] auto-restart failed: {e}", "rd")


def _current_studio_exe():
    """The StudioMCP.exe our launcher would currently pick (newest version
    folder paired with a real RobloxStudioBeta.exe), or None. Reused here only
    to detect a Studio update happening mid-session: Roblox's own bug report
    ("Studio MCP turning off after update") says the toggle resets to OFF
    whenever Studio auto-updates - restarting our proxy can't fix that (Studio
    itself refuses the connection while its toggle is off), so the terminal
    should say "re-enable the toggle" instead of "wait for auto-recovery"
    when a version bump coincides with the disconnect."""
    if _studio_scan is None:
        return None
    try:
        return _studio_scan.find_studio_mcp()
    except Exception:
        return None


async def studio_watch(initial_app, initial_place=None):
    """Poll Studio attachment and log transitions, so the terminal confirms in
    GREEN the moment Studio attaches (e.g. after the user toggles its MCP server)
    and warns again if it later drops. Best-effort; never raises.

    Also auto-recovers from a real disconnect: two bugs reported on the Roblox
    devforum leave StudioMCP.exe alive (our client stays "alive" - the process
    never dies, so server_watch's dead-process restart never fires) but stuck
    talking to nothing - (1) StudioMCP keeps a stale named-pipe handle keyed by
    Studio's old PID after Studio is closed and reopened, and never rediscovers
    the new one; (2) MCP silently disconnects every 5-15 minutes on some
    machines. The documented user workaround for both is "toggle Studio's MCP
    server off/on" / "reopen the MCP panel" - which just forces StudioMCP to
    redo its handshake. Restarting OUR proxy process is the equivalent from
    this side (taskkill + fresh launch_studio_mcp.py), so do it automatically
    once a drop looks real (sustained, not a momentary blip) instead of leaving
    the user to notice and toggle it themselves."""
    prev_app = initial_app
    prev_place = initial_place
    disconnected_since = None
    last_auto_restart = 0.0
    empty_since = None       # when the roblox catalogue was first seen empty
    last_reclaim = 0.0       # cooldown for the zombie-StudioMCP port reclaim
    place_transitions = []
    known_studio_exe = await asyncio.to_thread(_current_studio_exe)
    update_suspected = False
    # Only auto-restart a disconnect that follows a real connection (matches
    # the two known bugs above) - never spam-restart while Studio simply isn't
    # open yet at all (prev_app starting False/None is the common cold-start
    # case and restarting there would just be noise every cooldown).
    ever_connected = initial_app is True
    while True:
        await asyncio.sleep(4)
        # If StudioMCP launched while Studio was closed, its catalogue is EMPTY
        # and stays that way: start()'s 12s retry loop has long given up, and
        # nothing else ever re-asks for tools/list (probe_studio can't - the
        # probe tools themselves are part of the missing catalogue, which is
        # why it short-circuits to "not connected" on an empty cache). So a
        # Studio opened AFTER that window was never detected until the user
        # restarted the whole bridge (seen live 2026-07-11). Re-ask here on
        # every poll while the catalogue is empty; the moment Studio attaches,
        # tools appear, the index rebuilds, and the normal probe below flips
        # the state to connected on this same iteration.
        rc0 = mgr.clients.get("roblox")
        if rc0 is not None and rc0.is_alive() and not rc0.tools_cache:
            got = False
            try:
                got = bool(await asyncio.to_thread(rc0.refresh_tools, 3))
            except Exception:
                got = False
            if got:
                mgr.rebuild_index()
                log(f"Roblox Studio's tools appeared ({len(rc0.tools_cache)}) - Studio attached.", "gr")
                empty_since = None
            else:
                now0 = time.time()
                if empty_since is None:
                    empty_since = now0
                # First: a PROVEN port hijack (stderr showed StudioMCP talking to
                # a foreign host, e.g. ropilot). Hard evidence, so recover fast
                # and unconditionally - no need to wait out the sustained-empty
                # window the ambiguous zombie case below uses.
                if (rc0.saw_foreign_ws_host and now0 - last_reclaim > 180):
                    last_reclaim = now0
                    killed, sname = await asyncio.to_thread(_kill_port_squatter)
                    if killed:
                        try:
                            await asyncio.to_thread(mgr.restart, "roblox")
                        except Exception as e:
                            log(f"roblox proxy restart after squatter kill failed: {e}", "rd")
                        _print_squatter_hint(sname)
                        await broadcast_status()
                # Otherwise: catalogue stuck empty WITH a Studio window open often
                # means a zombie StudioMCP.exe (not ours) still owns port 13469 and
                # swallowed Studio's one-shot registration - a state no manual
                # restart combination can escape (see _reclaim_studio_port).
                # Sustained-empty threshold + cooldown so a Studio that is
                # merely slow to boot never triggers a spurious kill.
                elif (now0 - empty_since > 20 and now0 - last_reclaim > 180
                        and await asyncio.to_thread(_roblox_studio_app_running) is True):
                    last_reclaim = now0
                    if await asyncio.to_thread(_reclaim_studio_port, rc0):
                        try:
                            await asyncio.to_thread(mgr.restart, "roblox")
                        except Exception as e:
                            log(f"roblox proxy restart after zombie kill failed: {e}", "rd")
                        _print_reregister_hint()
                        await broadcast_status()
        else:
            empty_since = None
        try:
            st = await asyncio.to_thread(probe_studio)
        except Exception:
            continue
        app, place = st["app"], st["place"]
        if app is not None and app != prev_app:
            if app is True:
                # Roblox-only count, not mgr.list_tools() (sums every server,
                # e.g. + Blender) - this message is specifically about Roblox
                # attaching, so it must not borrow addon tool counts (same
                # class of bug as the startup banner, see roblox_total above).
                rc = mgr.clients.get("roblox")
                roblox_now = len(rc.tools_cache) if rc else 0
                log(f"Roblox Studio connected - {roblox_now} tools ready.", "gr")
                ever_connected = True
                disconnected_since = None
                update_suspected = False
            else:
                cur_exe = await asyncio.to_thread(_current_studio_exe)
                if cur_exe and known_studio_exe and cur_exe != known_studio_exe:
                    # A newer Studio version folder appeared since we last saw
                    # one - restarting the proxy will NOT fix this (Studio
                    # itself refuses the MCP connection while its own toggle
                    # is off), so tell the user the actual fix instead of
                    # letting the generic auto-recovery below spin uselessly.
                    ver = os.path.basename(os.path.dirname(cur_exe))
                    log(f"Roblox Studio appears to have UPDATED (new version: {ver}). "
                        "Studio often turns its MCP toggle back OFF after an update - open "
                        "Roblox Studio > Assistant Settings > MCP Servers and re-enable "
                        "'Enable Studio as MCP server'.", "yl")
                    update_suspected = True
                else:
                    log("Roblox Studio disconnected - re-enable its MCP server (toggle off/on).", "yl")
                    update_suspected = False
                known_studio_exe = cur_exe or known_studio_exe
                if ever_connected and disconnected_since is None:
                    disconnected_since = time.time()
            prev_app = app
            # studio_watch only used to LOG transitions - an extension sitting
            # on the pre-start standby screen (no tool calls happening, so
            # nothing else round-trips to the bridge) never saw Studio connect
            # or disconnect mid-session until it happened to poll for an
            # unrelated reason. Push it immediately instead of leaving that
            # extension staring at a stale snapshot indefinitely.
            await broadcast_status()
        # Set once per iteration: BOTH the app-drop branch and the place-churn
        # block below use it. It used to be assigned only inside the app-drop
        # branch, so any iteration that skipped that branch crashed the whole
        # watcher with UnboundLocalError on the churn line (seen live: the task
        # died right after a successful reconnect, silently ending ALL Studio
        # monitoring and status broadcasts until the bridge was restarted).
        now = time.time()
        if app is False and ever_connected and disconnected_since is not None and not update_suspected:
            # ~20s sustained (5 polls) before treating it as a real drop, not a
            # momentary blip; 90s cooldown between recovery attempts so a
            # Studio that is genuinely closed for a while doesn't get hammered.
            # Skipped entirely when a version bump was the likely cause (see
            # above) - restarting our proxy cannot flip Studio's own toggle
            # back on, so retrying would just be noise every 90s.
            if now - disconnected_since > 20 and now - last_auto_restart > 90:
                last_auto_restart = now
                # Which recovery applies depends on whether a Studio WINDOW is
                # actually running (validated live 2026-07-11, both directions):
                #  - Studio RUNNING but not attached: Studio's MCP plugin only
                #    registers ONCE, at Studio boot or on a toggle flip. It
                #    never retries by itself, and restarting OUR proxy cannot
                #    reach into Studio to re-register it - worse, a restart
                #    that lands while Studio is booting kills the listener at
                #    the exact moment the plugin makes its single attempt,
                #    which is precisely how this state got created. So: do NOT
                #    touch the proxy; tell the user the one action that works.
                #  - No Studio running: a restart is safe (nothing to collide
                #    with) and clears genuinely stuck/stale proxy state.
                if await asyncio.to_thread(_roblox_studio_app_running) is True:
                    log("Roblox Studio is RUNNING but its MCP plugin has not registered with the "
                        "bridge yet.", "yl")
                    log("If Studio is still STARTING UP, give it a minute (its plugin registers "
                        "late in boot).", "yl")
                    log("If Studio is fully loaded and this stays yellow: in Roblox Studio, simply "
                        "OPEN Assistant Settings > MCP Servers - opening that panel makes the "
                        "plugin re-register (validated twice live). If that's not enough, toggle "
                        "'Enable Studio as MCP server' OFF then ON there.", "yl")
                else:
                    log("Roblox Studio proxy looks stuck (known StudioMCP disconnect bug) - "
                        "restarting it to recover.", "yl")
                    try:
                        await asyncio.to_thread(mgr.restart, "roblox")
                        await broadcast_status()
                    except Exception as e:
                        log(f"auto-restart of roblox proxy failed: {e}", "rd")
        # PLACE-level churn: `app` can stay stuck reporting True the whole time
        # (seen live 2026-07-11 - Studio fully closed, list_roblox_studios kept
        # answering with a leftover studio entry for 4+ minutes, so the app-drop
        # trigger above never fires) while `place` flip-flops "loaded"/"closed"
        # every ~10-20s forever. That is not a user opening/closing places that
        # fast - it is the same class of stuck-proxy bug, just visible at the
        # place layer instead of the app layer. A fresh StudioMCP.exe process
        # cannot carry over stale cached state, so the same restart applies.
        place_transitions[:] = [t for t in place_transitions if now - t < 90]
        if len(place_transitions) >= 4 and now - last_auto_restart > 90:
            # Same running-Studio guard as the app-drop recovery above: with a
            # real Studio window up, a proxy restart can only collide with the
            # plugin's one-shot registration; the churn is Studio-side state.
            if await asyncio.to_thread(_roblox_studio_app_running) is not True:
                last_auto_restart = now
                log(f"Roblox Studio's place status flipped {len(place_transitions)} times in the last "
                    "90s (known StudioMCP stuck-proxy bug) - restarting the proxy to recover.", "yl")
                try:
                    await asyncio.to_thread(mgr.restart, "roblox")
                    await broadcast_status()
                except Exception as e:
                    log(f"auto-restart of roblox proxy failed: {e}", "rd")
                place_transitions.clear()
        if place is not None and place != prev_place:
            # Debounce: StudioMCP's binding to Studio blips every few seconds
            # on some machines (self-healing in ~1-4s - seen live as "Bound
            # studio ... disconnected" stderr). A probe landing in that window
            # can misread EITHER direction - most confusingly, reporting
            # "place loaded" from a stale cached response while the place is
            # actually still closed (seen live). Recheck once before trusting
            # a transition, in either direction.
            await asyncio.sleep(1.2)
            try:
                confirm = (await asyncio.to_thread(probe_studio))["place"]
            except Exception:
                confirm = None
            if confirm is None or confirm != place:
                continue  # didn't hold up on recheck - treat as noise, not a real change
            if place is True:
                log("Place loaded in Studio.", "gr")
            else:
                log("Place closed (Studio app still connected).", "yl")
            prev_place = place
            place_transitions.append(time.time())
            await broadcast_status()


async def _supervised(name, coro_factory):
    """Run a watcher coroutine forever, restarting it if it ever raises.

    Both watchers are designed to never raise, but one line proved that wrong
    in practice (an UnboundLocalError killed studio_watch SILENTLY - asyncio
    only prints 'Task exception was never retrieved' at shutdown, so all
    Studio monitoring and status broadcasts just stopped until the user
    restarted the bridge). A crash in a watcher must never be silent or
    permanent: log it loudly, wait a beat, start a fresh instance.
    """
    while True:
        try:
            await coro_factory()
            return  # normal completion (doesn't happen today, but respect it)
        except Exception as e:
            log(f"{name} crashed: {type(e).__name__}: {e} - restarting it in 5s "
                f"(please report this).", "rd")
            await asyncio.sleep(5)


async def main():
    print(f"\n{C['cy']}  ArcScript Bridge v{BRIDGE_VERSION}{C['reset']}  {C['dim']}- Roblox Studio - ws://{HOST}:{PORT}{C['reset']}\n")
    log(f"===== BRIDGE START  v{BRIDGE_VERSION}  pid={os.getpid()}  log={LOG_PATH} =====", "cy")
    await asyncio.to_thread(_kill_orphan_studio_mcp)
    killed_squatter = await asyncio.to_thread(check_studio_port)
    mgr.load_config()

    # Shared "we already told the user the corrective step" flag. Two producers
    # can print the 'toggle Studio's MCP server' action banner: the early
    # _early_studio_guidance task (fast, doesn't wait for start_all's ~48s grace
    # loop) and the post-start_all diagnostic block in _boot_and_diagnose. This
    # flag lets whichever fires first suppress the other, so the user never sees
    # the same instruction twice. Mutable dict so both nested coroutines share it.
    _guidance_shown = {"v": False}

    async def _early_studio_guidance():
        """Print the corrective action banner WITHOUT waiting for start_all()'s
        full ~48s grace loop.

        RobloxClient.start() retries tools/list for up to ~48s to catch a Studio
        that attaches a little late - correct for a Studio that IS coming, but it
        also means a user whose Studio is simply closed or whose MCP toggle is off
        waits ~48s before the terminal tells them what to do (the extension, which
        reads status over the socket, already says it immediately). So after a
        short grace we check independently and, if still not connected, show the
        step now. If Studio then attaches, studio_watch prints the green
        'connected' line - so an early banner is at worst redundant, never wrong
        (the user confirmed a premature toggle hint during boot is harmless).

        Deliberately does NOT try to distinguish 'Studio closed' from 'MCP toggle
        off': probe_studio can't tell them apart (both read app=False, see its
        docstring), and the banner wording already covers both, so there is no
        finer detection to preserve here. A proven port-squatter case is left to
        _boot_and_diagnose / studio_watch, which print their own, more specific
        hint."""
        if PRIMARY_SERVER_ID not in mgr.clients:
            return
        await asyncio.sleep(12)  # give a fast, normal attach the chance to win
        if _guidance_shown["v"]:
            return
        rc = mgr.clients.get(PRIMARY_SERVER_ID)
        if rc is None or getattr(rc, "saw_foreign_ws_host", False):
            return
        if rc.tools_cache:
            # Tools present - either connected, or an attach is mid-flight; defer
            # to the authoritative probe / studio_watch rather than second-guess.
            st = await asyncio.to_thread(probe_studio)
            if st["app"] is not False:
                return
        _guidance_shown["v"] = True
        action_banner([
            "Open your place in Roblox Studio.",
            "Go to: Assistant Settings > MCP Servers",
            "       > 'Enable Studio as MCP server'",
            "It can take up to ~10s; this window will turn green.",
        ])

    async def _boot_and_diagnose():
        """Launch every configured MCP server and print the boot diagnostic
        banner. Runs as a background task AFTER the socket below is already
        listening, so a slow or absent Roblox Studio never delays the
        extension's ability to connect and use OTHER MCP servers (e.g.
        Blender) right away - only the terminal banner and Roblox's own
        auto-recovery loop wait on this. (mgr.start_all() itself also
        launches every server in parallel now, for the same reason.)"""
        try:
            await asyncio.to_thread(mgr.start_all)
        except Exception as e:
            log(f"server startup error: {e}", "rd")
            log("The bridge will keep running; it retries on the first tool call.", "yl")
        total = len(mgr.list_tools())
        # Roblox-only count for the corrective message below: list_tools() sums
        # every configured server (Roblox + addons like Blender), so printing
        # `total` there falsely blamed addon tools on "NO Roblox Studio connected"
        # (seen live: 49 = 27 Roblox + 22 Blender, message only about Roblox).
        roblox_client = mgr.clients.get("roblox")
        roblox_total = len(roblox_client.tools_cache) if roblox_client else 0

        # Port-hijack check (ropilot etc.), done at boot: the child's stderr has
        # by now had its ~12s grace loop to reveal it connected to a foreign host
        # on the MCP port. This is proof the port is squatted even when the
        # one-shot check_studio_port() at startup missed it (a background helper
        # grabbing the port a beat after that check ran - seen live 2026-07-13).
        if (roblox_client is not None and roblox_total == 0
                and roblox_client.saw_foreign_ws_host):
            killed, sname = await asyncio.to_thread(_kill_port_squatter)
            if killed:
                try:
                    await asyncio.to_thread(mgr.restart, "roblox")
                    roblox_total = len(roblox_client.tools_cache)
                    total = len(mgr.list_tools())
                except Exception as e:
                    log(f"roblox proxy restart after squatter kill failed: {e}", "rd")
                _print_squatter_hint(sname)

        # Set True if we kill a leftover StudioMCP zombie below and print the
        # re-register hint - so the diagnostic block further down doesn't ALSO
        # print its own near-identical action banner (the same de-duplication
        # killed_squatter already does for the ropilot-squatter path).
        reclaimed_zombie = False
        # Zombie-port deadlock check, done at boot too (not just studio_watch):
        # with Studio ALREADY open, _kill_orphan_studio_mcp was skipped by its
        # safety guard, so a leftover StudioMCP.exe may still own the port and
        # our fresh proxy just spent its 12s grace loop talking to nothing.
        if (roblox_client is not None and roblox_total == 0
                and await asyncio.to_thread(_roblox_studio_app_running) is True
                and await asyncio.to_thread(_reclaim_studio_port, roblox_client)):
            try:
                await asyncio.to_thread(mgr.restart, "roblox")
                roblox_total = len(roblox_client.tools_cache)
                total = len(mgr.list_tools())
            except Exception as e:
                log(f"roblox proxy restart after zombie kill failed: {e}", "rd")
            reclaimed_zombie = True
            _print_reregister_hint()

        # A tool count alone only proves StudioMCP (the proxy) is up - it advertises
        # its catalogue even with NO Studio attached. The authoritative "a Studio is
        # actually connected" signal is the list_roblox_studios probe. So we probe
        # FIRST and only show the green "ready" line when Studio is really attached;
        # otherwise we show just the corrective step (no misleading green success).
        # Probe even when total == 0: StudioMCP advertises an EMPTY catalogue when
        # Studio's MCP server toggle is off (or no place is open), so 0 tools is the
        # most common "needs a corrective step" state, not a success.
        _st = await asyncio.to_thread(probe_studio)
        # Even when Studio (and its place) were ALREADY open before the bridge
        # started, the freshly-launched StudioMCP proxy needs a moment to (re)bind
        # to Studio's own MCP port - so an instant probe right after launch often
        # reads app=False for a beat before flipping True a few seconds later
        # (studio_watch would catch it, but only after printing a scary yellow
        # "not connected" block first). Give it the same grace period the tools
        # probe already gets before deciding it is a real problem.
        if _st["app"] is False:
            with _Spinner("    waiting for Roblox Studio to attach..."):
                for _ in range(8):
                    await asyncio.sleep(1)
                    _st = await asyncio.to_thread(probe_studio)
                    if _st["app"] is not False:
                        break
        # A single app=True reading can be a STALE positive: StudioMCP.exe can
        # answer list_roblox_studios with a leftover studio entry from a PREVIOUS
        # session even though no Studio window is actually open right now (seen
        # live 2026-07-11: bridge booted with Studio fully closed, still printed
        # "Roblox Studio connected" from the very first probe). studio_watch
        # already distrusts a single reading for PLACE transitions the same way -
        # apply the identical confirm-before-trusting step here for APP, so the
        # boot banner can't announce a connection that isn't really there.
        if _st["app"] is True:
            await asyncio.sleep(1.5)
            confirm = await asyncio.to_thread(probe_studio)
            if confirm["app"] is not True:
                _st = confirm
        if roblox_client is not None and (roblox_total == 0 or _st["app"] is False):
            if killed_squatter or reclaimed_zombie or _guidance_shown["v"]:
                # The action banner was ALREADY shown - either right after a kill
                # (check_studio_port / _print_reregister_hint for a zombie) or by
                # the early _early_studio_guidance task. Repeating the full
                # explanation here in a different color, seconds later, reads as a
                # second unrelated problem to a non-technical user (seen live
                # 2026-07-13: the toggle instruction and this block blurred
                # together). Just confirm we're still waiting, no new instructions.
                log("    still waiting for you to toggle Studio's MCP server "
                    "(see the action box above)...", "yl")
            else:
                _guidance_shown["v"] = True
                # No squatter: Studio is simply closed, or its MCP option is off.
                # Match the exact steps the extension itself tells the user
                # (Assistant Settings > MCP Servers), not a paraphrase - a
                # differently-worded instruction here reads as a second,
                # unrelated problem instead of the same one step.
                if roblox_total > 0:
                    log(f"    {roblox_total} Roblox tools loaded, but NO Roblox Studio is connected yet.", "yl")
                    log("    (This can be a slow attach that clears itself within ~10-15s -", "yl")
                    log("    watch for a green 'Roblox Studio connected' line right after.)", "yl")
                action_banner([
                    "Open your place in Roblox Studio.",
                    "Go to: Assistant Settings > MCP Servers",
                    "       > 'Enable Studio as MCP server'",
                    "It can take up to ~10s; this window will turn green.",
                ])
        elif _st["app"] is True:
            log(f"ready {total} tools available - Roblox Studio connected", "gr")
        else:
            log(f"ready {total} tools available ({len(mgr.clients)} MCP server(s))", "gr")
        asyncio.create_task(_supervised(
            "studio_watch", lambda: studio_watch(_st["app"], _st["place"])))

    async def _early_status_pushes():
        """A few follow-up status broadcasts shortly after boot.

        mgr.start_all() still doesn't RETURN until every server's thread has
        joined - including Roblox's, which can take up to ~48s (StudioMCP's
        own internal "waiting for tools" retry loop, seen live). So a single
        broadcast placed after start_all() would be just as slow as the old
        blocking behavior for the exact case this is meant to fix: an addon
        server (e.g. Blender) that's ready in 1-13s while Roblox is still
        slowly timing out. Poll-and-broadcast a few times instead, cheaply,
        so any extension that connected during that window self-corrects
        quickly instead of staying stuck on its first, incomplete snapshot.
        """
        for interval in (2, 2, 4, 6, 6):  # cumulative: 2s, 4s, 8s, 14s, 20s after boot
            await asyncio.sleep(interval)
            await broadcast_status()

    # Free our own port from a leftover bridge (double-launch / X-closed window /
    # prior crash) BEFORE binding, so relaunching start.bat "just works" instead
    # of dying on WinError 10048. Only ever kills a proven bridge.py; anything
    # else falls through to the friendly bind-error below.
    if await asyncio.to_thread(_reclaim_bridge_port):
        await asyncio.sleep(0.6)  # let Windows release the socket before we bind

    try:
        server_ctx = await websockets.serve(
            handler, HOST, PORT, ping_interval=20, ping_timeout=60,
            max_size=16 * 1024 * 1024)
    except OSError as e:
        # errno 10048 (Win) / EADDRINUSE: something we could NOT auto-kill still
        # owns the port - another app, or a python whose cmdline we couldn't read.
        if getattr(e, "errno", None) in (98, 10048) or "10048" in str(e):
            owner = await asyncio.to_thread(_port_owner, PORT)
            who = f" by '{owner[1]}' (pid {owner[0]})" if owner else ""
            log(f"could not start: port {PORT} is already in use{who}.", "rd")
            log(f"    A previous bridge may still be running, or another app took "
                f"the port. Close it, then relaunch. To find it:", "yl")
            log(f"      netstat -ano | findstr {PORT}", "yl")
            log(f"      taskkill /F /PID <the pid from the last column>", "yl")
            log(f"    Or set a different port before start.bat:  set ZS_BRIDGE_PORT=17614", "yl")
            return
        raise

    async with server_ctx:
        log(f"listening on ws://{HOST}:{PORT}  - load the extension and open a supported AI chat", "cy")
        asyncio.create_task(_supervised("server_watch", server_watch))
        asyncio.create_task(_boot_and_diagnose())
        asyncio.create_task(_early_studio_guidance())
        asyncio.create_task(_early_status_pushes())
        await asyncio.Future()  # run forever


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("shutting down...", "yl")
        for c in mgr.clients.values():
            c.stop()
    finally:
        log("===== BRIDGE STOP =====", "cy")
