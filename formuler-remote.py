#!/usr/bin/env python3
"""
Formuler IPTV Remote Control via ADB

Usage:
  ./formuler-remote.py [flags] [device_ip_or_hostname] [command] [args...]

Flags:
  --json     Output structured JSON ({"ok": bool, "data"|"error": ...})
  --yes      Skip confirmation prompts (for automation)
  --first    Auto-select first match instead of prompting

Device IP resolution (first match wins):
  1. CLI argument:        ./formuler-remote.py 192.168.0.100 tune TF1
  2. Environment variable: FORMULER_IP=192.168.0.100
  3. .env file:           FORMULER_IP=192.168.0.100  (in ./ or ~/.config/formuler-remote/)
  4. Config file:         [device] ip = "192.168.0.100"

Examples:
  ./formuler-remote.py                                  # interactive mode
  ./formuler-remote.py tune TF1                          # tune live channel
  ./formuler-remote.py play-movie batman                 # search & play a movie
  ./formuler-remote.py play-series "breaking bad" 1 4    # play S1E4
  ./formuler-remote.py search-vod batman                 # search movies on device
  ./formuler-remote.py search-series "walking dead"      # search series on device
  ./formuler-remote.py star                              # toggle star on current page
  ./formuler-remote.py star-vod batman                   # find movie & toggle star
  ./formuler-remote.py section vod                       # switch to VOD section
  ./formuler-remote.py stop-vod                          # stop with confirmation
  ./formuler-remote.py --json categories                 # JSON output for scripting
  ./formuler-remote.py --json --first tune TF1           # agent-friendly: JSON + auto-select
  ./formuler-remote.py --json commands                   # list all commands as JSON schema
  ./formuler-remote.py --yes reboot                      # skip confirmation prompts
  ./formuler-remote.py macro morning                     # run a saved macro

Prerequisites:
  1. Install ADB: sudo pacman -S android-tools
  2. Enable ADB on Formuler: Settings > Developer Options > ADB Debugging > ON
  3. Find device IP: Settings > Network (on the Formuler)
"""

__version__ = "1.2.0"

import datetime
import json
import os
import re
import subprocess
import sys
import shutil
import tempfile
import threading
import time
import unicodedata
from pathlib import Path

try:
    import readline  # arrow-key history in interactive mode
except ImportError:
    readline = None  # Windows without pyreadline3: no completion/history

if os.name == "nt":
    os.system("")  # enable ANSI escape sequences in the Windows console
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except AttributeError:
            pass

TMP_DIR = Path(tempfile.gettempdir())

# ──────────────────────── Color Support ────────────────────────


class _C:
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    CYAN = "\033[36m"
    MAGENTA = "\033[35m"
    RESET = "\033[0m"


if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
    for _attr in ("BOLD", "DIM", "RED", "GREEN", "YELLOW", "BLUE", "CYAN", "MAGENTA", "RESET"):
        setattr(_C, _attr, "")


# ──────────────────────── Config ────────────────────────

CONFIG_DIR = Path.home() / ".config" / "formuler-remote"
CONFIG_FILE = CONFIG_DIR / "config.toml"
CONFIG_JSON = CONFIG_DIR / "config.json"


def _load_env(path: Path) -> dict[str, str]:
    """Load KEY=VALUE pairs from a .env file."""
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip().strip("'\"")
    return env


def _load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            import tomllib
            with open(CONFIG_FILE, "rb") as f:
                return tomllib.load(f)
        except ImportError:
            pass
    if CONFIG_JSON.exists():
        try:
            with open(CONFIG_JSON, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


# Load .env from project dir or config dir
_dotenv = _load_env(Path(".env"))
_dotenv.update(_load_env(CONFIG_DIR / ".env"))

CONFIG = _load_config()

# ──────────────────────── Constants ────────────────────────

ADB_PORT = CONFIG.get("device", {}).get("port", 5555)
# IP resolution order: FORMULER_IP env var > .env file > config file
DEFAULT_IP = (
    os.environ.get("FORMULER_IP")
    or _dotenv.get("FORMULER_IP")
    or CONFIG.get("device", {}).get("ip", "")
)
MOL3_PKG = "tv.formuler.mol3.real"
CACHE_DIR = Path.home() / ".cache" / "formuler-remote"
CHANNELS_CACHE = CACHE_DIR / "channels.json"
FULL_CHANNELS_CACHE = CACHE_DIR / "full_channels.json"
TUNE_HISTORY_FILE = CACHE_DIR / "tune_history.json"

JSON_MODE = False
AUTO_YES = False
AUTO_FIRST = False
VERBOSE = False
DRY_RUN = False
ADB_TIMEOUT = 10

# Configurable timing (overridable via [timing] in config)
_timing = CONFIG.get("timing", {})
NAV_DELAY = float(_timing.get("nav_delay", 0.25))
LOAD_DELAY = float(_timing.get("load_delay", 2.0))
SEARCH_DELAY = float(_timing.get("search_delay", 1.5))
PLAYBACK_TIMEOUT = float(_timing.get("playback_timeout", 60.0))  # max wait for VOD to start; slow providers take ~50s

KEYS = {
    "power": 26, "home": 3, "back": 4, "menu": 82,
    "up": 19, "down": 20, "left": 21, "right": 22,
    "ok": 66, "select": 23,
    "play": 126, "pause": 127, "play-pause": 85, "stop": 86,
    "rewind": 89, "fast-forward": 90, "next": 87, "previous": 88,
    "mute": 164, "volume-up": 24, "volume-down": 25,
    "channel-up": 166, "channel-down": 167,
    "info": 165, "guide": 172,
    "record": 130, "delete": 112, "tab": 61,
    "wakeup": 224, "sleep": 223,
    "0": 7, "1": 8, "2": 9, "3": 10, "4": 11,
    "5": 12, "6": 13, "7": 14, "8": 15, "9": 16,
}

CATEGORIES = {
    1: "Live History", 3: "VOD History", 4: "Favorites",
    5: "Series History", 6: "Series Favorites", 7: "VOD Favorites",
}

MOL3_SECTIONS = {
    "live": 0, "tv": 0, "vod": 1, "movies": 1, "series": 2,
    "radio": 3, "radios": 3, "matchday": 4, "recordings": 5,
    "schedule": 6, "content": 7, "notifications": 8, "settings": 9,
}

KNOWN_APPS = {
    "mytvonline": MOL3_PKG, "mytv": MOL3_PKG, "mol3": MOL3_PKG,
    "youtube": "com.google.android.youtube.tv",
    "netflix": "com.netflix.ninja",
    "plex": "com.plexapp.android",
    "kodi": "org.xbmc.kodi",
    "vlc": "org.videolan.vlc",
    "prime": "com.amazon.amazonvideo.livingroom",
    "disney": "com.disney.disneyplus",
    "settings": "com.android.tv.settings",
}

SEARCH_PREFIXES = (
    list("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    + list("0123456789")
    + ["|BE|", "|FR|", "|NL|", "|UK|", "|US|", "|DE|", "|ES|", "|IT|", "|PT|"]
)

# Country code -> (display name, title prefixes the provider uses, e.g. "NL| NPO1 4K").
# Extend via [countries] in config: be = ["België", ["BE"]]
COUNTRIES = {
    "nl": ("Nederland", ["NL"]),
    "be": ("België", ["BE"]),
    "us": ("USA", ["U.S", "US"]),
    "ca": ("Canada", ["CA"]),
    "au": ("Australië", ["AU"]),
    "nz": ("Nieuw-Zeeland", ["NZ"]),
}
COUNTRIES.update({k.lower(): (v[0], list(v[1])) for k, v in CONFIG.get("countries", {}).items()})


# ──────────────────────── Fuzzy Matching ────────────────────────


def _normalize(s: str) -> str:
    """Strip accents, lowercase, collapse whitespace."""
    nfkd = unicodedata.normalize("NFKD", s)
    stripped = "".join(c for c in nfkd if unicodedata.category(c) != "Mn")
    return " ".join(stripped.lower().split())


def _fuzzy_match(query: str, candidates: list[dict], key: str = "title") -> list[dict]:
    """Match query against candidates with tiered matching.

    Priority: exact > starts-with > all-tokens-contained > spaces-removed containment.
    Within each tier, shorter titles are preferred (e.g., "TF1" before "TF1 SERIES HD").
    """
    nq = _normalize(query)
    nq_nospace = nq.replace(" ", "")
    tokens = nq.split()

    exact, starts, contains, spaceless = [], [], [], []
    for item in candidates:
        val = item.get(key, "")
        nv = _normalize(val)
        nv_nospace = nv.replace(" ", "")
        if nv == nq:
            exact.append(item)
        elif nv.startswith(nq):
            starts.append(item)
        elif all(t in nv for t in tokens):
            contains.append(item)
        elif nq_nospace in nv_nospace:
            spaceless.append(item)

    # Within each tier, prefer shorter titles (more specific matches first)
    _by_len = lambda items: sorted(items, key=lambda x: len(x.get(key, "")))
    return _by_len(exact) + _by_len(starts) + _by_len(contains) + _by_len(spaceless)


# ──────────────────────── Command Registry ────────────────────────
# Machine-readable command schema for AI agents and scripting

COMMANDS = {
    # Navigation
    "up": {"args": "", "desc": "Navigate up"},
    "down": {"args": "", "desc": "Navigate down"},
    "left": {"args": "", "desc": "Navigate left"},
    "right": {"args": "", "desc": "Navigate right"},
    "ok": {"args": "", "desc": "Confirm / Enter"},
    "back": {"args": "", "desc": "Go back"},
    "home": {"args": "", "desc": "Go to home screen"},
    "menu": {"args": "", "desc": "Open menu"},
    # Playback
    "play": {"args": "", "desc": "Start playback"},
    "pause": {"args": "", "desc": "Pause playback"},
    "play-pause": {"args": "", "desc": "Toggle play/pause"},
    "stop": {"args": "", "desc": "Stop playback"},
    "rewind": {"args": "", "desc": "Rewind"},
    "fast-forward": {"args": "", "desc": "Fast forward"},
    # Volume
    "volume-up": {"args": "", "desc": "Volume up"},
    "volume-down": {"args": "", "desc": "Volume down"},
    "mute": {"args": "", "desc": "Toggle mute"},
    # Channels
    "channel-up": {"args": "", "desc": "Next channel"},
    "channel-down": {"args": "", "desc": "Previous channel"},
    "channel": {"args": "<number>", "desc": "Go to channel number using digit keys"},
    # Sections
    "section": {"args": "<name>", "desc": "Switch section (live/vod/series/radio/recordings/settings)"},
    "browse": {"args": "<section>", "desc": "Open section for manual browsing"},
    # Live TV
    "tune": {"args": "<name|number>", "desc": "Tune channel by name or number. Auto-selects first match with --first"},
    "search": {"args": "<query>", "desc": "Search channels in local DB and device provider"},
    "list": {"args": "[filter]", "desc": "List channels from favorites/history cache"},
    "list-all": {"args": "[filter]", "desc": "List all enumerated channels (requires refresh-all first)"},
    "channel-info": {"args": "<number>", "desc": "Show details for a channel number"},
    "country": {"args": "[code] [filter]", "desc": "List countries, or all live channels of one (e.g. 'country be')"},
    "tune-id": {"args": "<unique_id>", "desc": "Tune a channel by its provider id (from 'country --json')"},
    "last": {"args": "", "desc": "Retune the last tuned channel"},
    "prev": {"args": "", "desc": "Swap to previous channel (recall)"},
    "tune-history": {"args": "[count]", "desc": "Show recent tune history with timestamps"},
    "categories": {"args": "", "desc": "Show content category counts"},
    "refresh": {"args": "", "desc": "Reload favorites/history cache from device"},
    "refresh-all": {"args": "", "desc": "Rebuild full channel database (slow, scans A-Z)"},
    # VOD
    "search-vod": {"args": "<query>", "desc": "Search movies on device UI"},
    "play-movie": {"args": "<query>", "desc": "Search and play first matching movie"},
    "stop-vod": {"args": "", "desc": "Stop VOD playback with confirmation dialog"},
    # Series
    "search-series": {"args": "<query>", "desc": "Search series on device UI"},
    "play-series": {"args": "<query> [season] [episode]", "desc": "Play series episode (default S1E1)"},
    "episodes": {"args": "<query>", "desc": "Open episode browser and take screenshot"},
    # Browsing
    "resume": {"args": "[vod|series|live]", "desc": "Resume last watched content from history"},
    "info": {"args": "<query>", "desc": "Show content details from local database"},
    "vod-history": {"args": "[count] [filter]", "desc": "Show VOD/series watch history"},
    # EPG
    "epg": {"args": "[channel]", "desc": "Show EPG info overlay and take screenshot"},
    "guide": {"args": "", "desc": "Open full EPG guide and take screenshot"},
    "search-epg": {"args": "<query>", "desc": "Search EPG program listings"},
    "now-playing": {"args": "", "desc": "Capture player log entries from logcat"},
    # Favorites
    "star": {"args": "", "desc": "Toggle star/favorite on current detail page"},
    "star-vod": {"args": "<query>", "desc": "Find movie and toggle star"},
    "star-series": {"args": "<query>", "desc": "Find series and toggle star"},
    # Macros & Timers
    "macro": {"args": "<name>", "desc": "Run a saved macro from config"},
    "macros": {"args": "", "desc": "List available macros"},
    "at": {"args": "<HH:MM> <command>", "desc": "Schedule a command at a specific time"},
    "timers": {"args": "", "desc": "List pending scheduled timers"},
    "cancel-timer": {"args": "<index>", "desc": "Cancel a pending timer by index"},
    "sleep-timer": {"args": "<minutes>", "desc": "Auto-off: stop playback + CEC standby after N minutes"},
    "sleep-at": {"args": "<HH:MM>", "desc": "Auto-off at specific time"},
    "sleep-cancel": {"args": "", "desc": "Cancel pending sleep timer"},
    # Advanced
    "repeat": {"args": "<key> <count>", "desc": "Press a key N times"},
    "cec": {"args": "<on|off|standby>", "desc": "TV power control via CEC pass-through"},
    "record": {"args": "[duration] [path]", "desc": "Screen record (default 30s)"},
    "mirror": {"args": "[--no-audio] [--fullscreen]", "desc": "Mirror screen to this computer via scrcpy (audio here; --no-audio keeps sound on the TV)"},
    "serve": {"args": "[port] [--local]", "desc": "Start the web remote (default port 8765, LAN; --local binds to this computer only)"},
    # General
    "type": {"args": "<text>", "desc": "Type text on device keyboard"},
    "key": {"args": "<keyname>", "desc": "Send a single key event"},
    "open": {"args": "<app>", "desc": "Launch app (youtube/netflix/prime/mytv/plex/kodi/vlc/disney/settings)"},
    "apps": {"args": "", "desc": "List installed third-party apps"},
    "screenshot": {"args": "[path]", "desc": "Capture screen to file (default: temp dir/formuler-screenshot.png)"},
    "status": {"args": "", "desc": "Show device info (model, Android version, uptime, foreground app)"},
    "reboot": {"args": "", "desc": "Reboot device (requires --yes to skip confirmation)"},
    "export-m3u": {"args": "[path]", "desc": "Export channel lineup as M3U playlist (metadata only, no stream URLs)"},
    "wake": {"args": "", "desc": "CEC wakeup + launch MyTVOnline"},
    "power-off": {"args": "", "desc": "CEC sleep + disconnect"},
    "history": {"args": "[count]", "desc": "Show recent command history"},
    "watch": {"args": "[interval] [dir]", "desc": "Take screenshot every N seconds (default 5)"},
    "ping": {"args": "", "desc": "Check device connectivity (adb shell echo pong)"},
    "commands": {"args": "", "desc": "List all commands with args and descriptions (JSON schema)"},
    "keys": {"args": "", "desc": "List all remote key names"},
    "help": {"args": "", "desc": "Show help text"},
}


# ──────────────────────── Output Helpers ────────────────────────

_exit_code = 0


def _json_out(data: dict):
    print(json.dumps(data, ensure_ascii=False, indent=2))


def output(data, human_text: str = ""):
    if JSON_MODE:
        _json_out({"ok": True, "data": data})
    else:
        print(human_text or str(data))


def output_ok(msg: str = "", data=None):
    if JSON_MODE:
        _json_out({"ok": True, "message": msg, **({"data": data} if data is not None else {})})
    elif msg:
        print(f"{_C.GREEN}{msg}{_C.RESET}")


def output_err(msg: str, data=None):
    global _exit_code
    _exit_code = 1
    if JSON_MODE:
        _json_out({"ok": False, "error": msg, **({"data": data} if data is not None else {})})
    else:
        print(f"{_C.RED}{msg}{_C.RESET}")


def info(msg: str):
    if not JSON_MODE:
        print(f"{_C.GREEN}{msg}{_C.RESET}")


def warn(msg: str):
    if not JSON_MODE:
        print(f"{_C.YELLOW}{msg}{_C.RESET}")


def error(msg: str):
    global _exit_code
    _exit_code = 1
    if not JSON_MODE:
        print(f"{_C.RED}{msg}{_C.RESET}")
    else:
        _json_out({"ok": False, "error": msg})


def _notify(title: str, body: str = ""):
    if shutil.which("notify-send"):
        subprocess.Popen(
            ["notify-send", "-a", "Formuler Remote", title, body],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )


# ══════════════════════════════════════════════════════════════
#  ADB TRANSPORT
# ══════════════════════════════════════════════════════════════

_current_ip: str = ""


def run_adb(*args: str, timeout: int = 10) -> tuple[int, str]:
    if DRY_RUN:
        print(f"[dry-run] adb {' '.join(args)}", file=sys.stderr)
        return 0, ""
    try:
        result = subprocess.run(
            ["adb", *args], capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",  # device output is UTF-8, not the Windows locale
        )
        return result.returncode, (result.stdout + result.stderr).strip()
    except subprocess.TimeoutExpired:
        return 1, "timeout"
    except FileNotFoundError:
        return 1, "adb not found"


def tgt(ip: str) -> str:
    return f"{ip}:{ADB_PORT}"


def adb(ip: str, *args: str, timeout: int = 0) -> tuple[int, str]:
    """Run ADB command with auto-reconnect on connection loss."""
    if timeout == 0:
        timeout = ADB_TIMEOUT
    if VERBOSE:
        print(f"[debug] adb -s {tgt(ip)} {' '.join(args)}", file=sys.stderr)
    code, out = run_adb("-s", tgt(ip), *args, timeout=timeout)
    if code != 0 and ("error: device" in out.lower() or "not found" in out.lower()
                       or "offline" in out.lower()):
        warn("Connection lost. Reconnecting...")
        if connect(ip, quiet=True):
            code, out = run_adb("-s", tgt(ip), *args, timeout=timeout)
    if VERBOSE:
        print(f"[debug] exit={code}", file=sys.stderr)
    return code, out


_TRANSIENT_ERRORS = ("timeout", "device busy", "closed", "connection reset", "broken pipe")


def _adb_retry(ip: str, *args: str, timeout: int = 10, max_attempts: int = 3) -> tuple[int, str]:
    """Run ADB command with exponential backoff retry on transient errors."""
    delays = [0.5, 1.0, 2.0]
    for attempt in range(max_attempts):
        code, out = adb(ip, *args, timeout=timeout)
        if code == 0:
            return code, out
        out_lower = out.lower()
        if not any(err in out_lower for err in _TRANSIENT_ERRORS):
            return code, out  # non-transient error, don't retry
        if attempt < max_attempts - 1:
            delay = delays[min(attempt, len(delays) - 1)]
            warn(f"Transient error (attempt {attempt + 1}/{max_attempts}), retrying in {delay}s...")
            time.sleep(delay)
    return code, out


def connect(ip: str, quiet: bool = False) -> bool:
    global _current_ip
    code, out = run_adb("connect", tgt(ip))
    if "connected" in out.lower():
        _current_ip = ip
        if not quiet:
            info(f"Connected to {tgt(ip)}")
        return True
    error(f"Failed to connect: {out}")
    return False


def key(ip: str, name: str) -> bool:
    if name not in KEYS:
        error(f"Unknown key: {name}")
        return False
    code, out = adb(ip, "shell", "input", "keyevent", str(KEYS[name]))
    if code != 0:
        output_err(f"Key '{name}' failed: {out}")
        return False
    return True


def keys(ip: str, *names: str, delay: float = 0):
    if delay == 0:
        delay = NAV_DELAY
    for n in names:
        key(ip, n)
        time.sleep(delay)


def text_input(ip: str, t: str) -> bool:
    """Type text on the device via ADB input.

    ADB input text only supports ASCII. Non-ASCII characters (accents, etc.)
    cause a NullPointerException on Android. We strip accents and drop
    unsupported characters before sending.

    Spaces are encoded as %s (ADB convention). Single quotes are escaped.
    """
    # Strip accents: é→e, à→a, ü→u, etc.
    ascii_text = _normalize(t)
    # Remove any remaining non-ASCII characters
    ascii_text = "".join(c for c in ascii_text if ord(c) < 128)
    if not ascii_text.strip():
        return False
    escaped = ascii_text.replace(" ", "%s").replace("'", "'\\''")
    code, _ = adb(ip, "shell", f"input text '{escaped}'")
    return code == 0


def ui_dump(ip: str) -> str:
    """Dump UI hierarchy and return XML string."""
    adb(ip, "shell", "uiautomator", "dump", "/sdcard/ui.xml", timeout=10)
    code, out = run_adb("-s", tgt(ip), "pull", "/sdcard/ui.xml", str(TMP_DIR / "formuler-ui.xml"))
    if code != 0:
        return ""
    try:
        return (TMP_DIR / "formuler-ui.xml").read_text(encoding="utf-8")
    except OSError:
        return ""


def ui_focused_text(ip: str) -> list[str]:
    """Return text content of the currently focused UI element."""
    import xml.etree.ElementTree as ET
    xml_str = ui_dump(ip)
    if not xml_str:
        return []
    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError:
        return []
    texts = []
    for node in root.iter("node"):
        if node.get("focused") == "true":
            for n in node.iter("node"):
                t = n.get("text", "")
                if t:
                    texts.append(t)
            break
    return texts


def ui_find_text(ip: str, target: str) -> bool:
    """Check if target text exists anywhere in the current UI."""
    import xml.etree.ElementTree as ET
    xml_str = ui_dump(ip)
    if not xml_str:
        return False
    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError:
        return False
    target_lower = target.lower()
    for node in root.iter("node"):
        text = node.get("text", "")
        if target_lower in text.lower():
            return True
    return False


def ui_get_texts(ip: str) -> list[dict]:
    """Return all text elements with their resource IDs and positions."""
    import xml.etree.ElementTree as ET
    xml_str = ui_dump(ip)
    if not xml_str:
        return []
    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError:
        return []
    results = []
    for node in root.iter("node"):
        text = node.get("text", "")
        if text:
            results.append({
                "text": text,
                "rid": node.get("resource-id", "").split("/")[-1],
                "focused": node.get("focused") == "true",
                "selected": node.get("selected") == "true",
                "bounds": node.get("bounds", ""),
            })
    return results


def wait(seconds: float = 1.0):
    time.sleep(seconds)


# ══════════════════════════════════════════════════════════════
#  APP MANAGEMENT
# ══════════════════════════════════════════════════════════════

def is_mytv_foreground(ip: str) -> bool:
    code, out = adb(ip, "shell", "dumpsys", "activity", "recents", timeout=5)
    for line in out.splitlines():
        if "Recent #0" in line or "topActivity" in line:
            if MOL3_PKG in line:
                return True
            break
    return False


def ensure_mytv(ip: str):
    if not is_mytv_foreground(ip):
        warn("Launching MyTVOnline...")
        adb(ip, "shell", "monkey", "-p", MOL3_PKG,
            "-c", "android.intent.category.LAUNCHER", "1")
        wait(LOAD_DELAY * 1.5)


def open_app(ip: str, package: str):
    code, out = adb(ip, "shell", "monkey", "-p", package,
                    "-c", "android.intent.category.LAUNCHER", "1")
    if code != 0 or "No activities found" in out:
        output_err(f"Error launching {package}: {out}")
    else:
        output_ok(f"Launched {package}")


def list_apps(ip: str):
    code, out = adb(ip, "shell", "pm", "list", "packages", "-3")
    if code == 0:
        pkgs = sorted(l.replace("package:", "") for l in out.splitlines() if l.startswith("package:"))
        if JSON_MODE:
            output(pkgs)
        else:
            for pkg in pkgs:
                print(f"  {pkg}")


# ══════════════════════════════════════════════════════════════
#  SECTION NAVIGATION
# ══════════════════════════════════════════════════════════════

def go_to_section(ip: str, section: str) -> bool:
    section = section.lower()
    if section not in MOL3_SECTIONS:
        error(f"Unknown section: {section}")
        print(f"Available: {', '.join(sorted(set(MOL3_SECTIONS.keys())))}")
        return False

    ensure_mytv(ip)
    key(ip, "menu")
    wait(NAV_DELAY * 2)
    for _ in range(MOL3_SECTIONS[section]):
        key(ip, "down")
        wait(NAV_DELAY)
    key(ip, "ok")
    wait(LOAD_DELAY)
    output_ok(f"Switched to: {section}")
    return True


# ══════════════════════════════════════════════════════════════
#  IN-APP SEARCH
# ══════════════════════════════════════════════════════════════

def open_search(ip: str, section: str = "vod"):
    """Open the universal search screen from any section.

    The search icon is at the top-right of the content area.
    Navigation: go to section → right (into content) → up until search icon focused → ok.
    Verified via UI dump: search icon has resource-id 'dashboard_overview_option'.
    """
    go_to_section(ip, section)

    # Wait for the section page: pressing on before it loads lands OK on a tile
    icon = "dashboard_overview_option"
    deadline = time.time() + 8
    while time.time() < deadline:
        root = _ui_root(ip)
        if root is not None and any(_rid(n) == icon for n in root.iter("node")):
            break
        wait(0.5)

    key(ip, "right")
    wait(NAV_DELAY)
    for _ in range(2):
        for _ in range(10):
            key(ip, "up")
            wait(NAV_DELAY * 0.6)
        root = _ui_root(ip)
        if root is not None and any(n.get("focused") == "true" and _rid(n) == icon
                                    for n in root.iter("node")):
            break
    else:
        return False
    key(ip, "ok")
    wait(SEARCH_DELAY)

    # Verify we landed on the search screen (not e.g. a detail page)
    deadline = time.time() + 5
    while time.time() < deadline:
        if _get_current_activity(ip) == "UsActivity":
            return True
        wait(0.5)
    return False


def _wait_for_activity(ip: str, name: str, timeout: float = 5) -> bool:
    """Wait for a specific activity to be in the foreground."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        code, out = adb(ip, "shell", "dumpsys", "activity", "activities", timeout=5)
        if code == 0 and name in out:
            return True
        time.sleep(0.5)
    return False


def _get_current_activity(ip: str) -> str:
    """Return the simple class name of the current foreground activity."""
    code, out = adb(ip, "shell", "dumpsys", "activity", "activities", timeout=5)
    if code != 0:
        return ""
    for line in out.splitlines():
        if "mResumedActivity" in line:
            # Extract: ...mol3.vod.ui.search.StreamSearchActivity t454
            m = re.search(r"/[\w.]+\.(\w+Activity)\b", line)
            return m.group(1) if m else ""
    return ""


def _reset_to_live(ip: str):
    """Make sure MyTVOnline starts from its live TV screen.

    Section navigation (menu → down×N → ok) only works from LiveActivity. From a
    VOD player, detail page or the search screen those keys land elsewhere (e.g.
    "Continue" on a detail page), so restart the app in that case.
    """
    if _get_current_activity(ip) == "LiveActivity":
        return
    adb(ip, "shell", "am", "force-stop", MOL3_PKG)
    wait(NAV_DELAY * 2)
    adb(ip, "shell", "monkey", "-p", MOL3_PKG, "-c", "android.intent.category.LAUNCHER", "1")
    if not _wait_for_activity(ip, "LiveActivity", timeout=15):
        warn("MyTVOnline did not return to live TV; search navigation may be off")
    wait(LOAD_DELAY)


def do_search(ip: str, query: str, section: str = "vod"):
    """Search for content using the universal search UI.

    Flow verified via ADB/uiautomator:
    1. Open search → lands on UsActivity with EditText focused
    2. Type query (spaces encoded as %s for ADB)
    3. Press Enter → results appear with category tabs
    """
    _reset_to_live(ip)
    if not open_search(ip, section):
        # Retry once from a fresh app start
        adb(ip, "shell", "am", "force-stop", MOL3_PKG)
        _reset_to_live(ip)
        if not open_search(ip, section):
            output_err(f"Could not open the search screen in {section}")
            return False
    text_input(ip, query)
    wait(NAV_DELAY * 2)
    key(ip, "ok")  # submit search (Enter; DPAD_CENTER would type on the on-screen keyboard)
    wait(LOAD_DELAY)
    output_ok(f"Searched for '{query}' in {section}")
    return True


def select_first_result(ip: str):
    """Select the first search result.

    After searching, the UI shows category tabs (Chaînes TV, VOD, Séries TV, etc.)
    with one tab focused. Results are BELOW the tabs.
    Press down once to move from tabs to results, then ok to select.
    """
    # After search, tab bar is focused. One "down" moves to first result.
    # (Verified via uiautomator: tabs are Chaînes TV / VOD / Séries TV / etc.)
    key(ip, "down")
    wait(NAV_DELAY)
    key(ip, "ok")
    wait(LOAD_DELAY)


def _ui_root(ip: str):
    """Dump the UI and return the parsed XML root, or None."""
    import xml.etree.ElementTree as ET
    xml_str = ui_dump(ip)
    if not xml_str:
        return None
    try:
        return ET.fromstring(xml_str)
    except ET.ParseError:
        return None


def _rid(node) -> str:
    return node.get("resource-id", "").split("/")[-1]


def _focused_texts(root, rid: str) -> list[str]:
    """Texts with the given resource-id inside the focused node."""
    for node in root.iter("node"):
        if node.get("focused") == "true":
            return [n.get("text", "") for n in node.iter("node") if _rid(n) == rid]
    return []


def _move_focus_to(ip: str, rid: str, target_index, axis: str = "horizontal",
                   max_rounds: int = 4) -> bool:
    """Move D-pad focus until the focused item is the target among items with `rid`.

    `target_index(texts)` returns the wanted index in the list of all texts with
    `rid` (document order). Each round reads the UI, presses the arrow key the
    needed number of times, and re-checks, so slow UIs self-correct.
    """
    prev_key, next_key = ("left", "right") if axis == "horizontal" else ("up", "down")
    for _ in range(max_rounds):
        root = _ui_root(ip)
        if root is None:
            return False
        texts = [n.get("text", "") for n in root.iter("node") if _rid(n) == rid]
        focused = _focused_texts(root, rid)
        want = target_index(texts)
        if want is None:
            return False
        if not focused or focused[0] not in texts:
            return False
        delta = want - texts.index(focused[0])
        if delta == 0:
            return True
        for _ in range(abs(delta)):
            key(ip, next_key if delta > 0 else prev_key)
            wait(NAV_DELAY)
        wait(NAV_DELAY)
    return False


def _clean_result_title(title: str) -> str:
    """'EN - Below Deck Adventure (2022) (US)' → 'Below Deck Adventure'."""
    title = re.sub(r"^[A-Z]{2,3}\s*[-|]\s*", "", title)
    return re.sub(r"\s*\([^)]*\)", "", title).strip()


def select_matching_result(ip: str, query: str) -> str | None:
    """Select the search result whose title best matches `query` and open it.

    Uses _fuzzy_match on the result titles (exact > starts-with > contains,
    shorter preferred), so 'below deck' picks 'Below Deck' and not the first
    tile 'Below Deck Mediterranean'. Returns the chosen title, or None.
    """
    rid = "stream_search_result_title"

    def target(texts):
        candidates = [{"title": _clean_result_title(t), "index": i} for i, t in enumerate(texts)]
        matches = _fuzzy_match(query, candidates)
        return matches[0]["index"] if matches else None

    # Focus starts on the category tabs; move down into the results row
    root = _ui_root(ip)
    for _ in range(2):
        if root is not None and _focused_texts(root, rid):
            break
        key(ip, "down")
        wait(NAV_DELAY * 2)
        root = _ui_root(ip)
    if root is None or not _focused_texts(root, rid):
        return None

    if not _move_focus_to(ip, rid, target):
        return None
    chosen = _focused_texts(_ui_root(ip), rid)
    key(ip, "ok")
    wait(LOAD_DELAY)
    return chosen[0] if chosen else None


_ALL_EPISODES_LABELS = ("alle afleveringen", "all episodes", "tous les episodes",
                        "alle folgen", "todos los episodios", "tutti gli episodi")


def open_all_episodes(ip: str) -> bool:
    """On a series detail page, focus the 'All episodes' action and press OK.

    The focused action differs per firmware and watch state (e.g. 'Doorgaan',
    or the trailer 'Voorbeeld' row), so the label is looked up instead of
    pressing OK on whatever happens to be focused.
    """
    rid = "detail_overview_action_label"

    def target(texts):
        for i, t in enumerate(texts):
            if _normalize(t) in _ALL_EPISODES_LABELS:
                return i
        return None

    # Focus may sit below the action row (e.g. on the trailer); move up into it
    for _ in range(4):
        root = _ui_root(ip)
        if root is None:
            return False
        if _focused_texts(root, rid):
            break
        key(ip, "up")
        wait(NAV_DELAY * 2)
    else:
        return False

    if not _move_focus_to(ip, rid, target):
        return False
    key(ip, "ok")
    wait(LOAD_DELAY)
    return True


def _episode_number(breadcrumb: str) -> tuple[int, int] | None:
    """'S1:A3' / 'S1:E3' → (1, 3)."""
    m = re.match(r"S(\d+)\s*[:.]\s*\D*(\d+)", breadcrumb)
    return (int(m.group(1)), int(m.group(2))) if m else None


def select_episode(ip: str, season: int, episode: int) -> bool:
    """In the episode browser, pick season (by position) and episode (by breadcrumb)."""
    if not _move_focus_to(ip, "season_name", lambda texts: season - 1 if season <= len(texts) else None,
                          axis="vertical"):
        return False
    wait(LOAD_DELAY * 0.5)
    key(ip, "right")  # into the episode list, E1 focused
    wait(NAV_DELAY * 2)

    for _ in range(episode + 2):
        root = _ui_root(ip)
        focused = _focused_texts(root, "series_episode_breadcrumb") if root is not None else []
        parsed = _episode_number(focused[0]) if focused else None
        if parsed is None:
            return False
        if parsed[1] == episode:
            return True
        # Episode list is virtualized, so step and re-check instead of jumping
        steps = episode - parsed[1]
        for _ in range(abs(steps)):
            key(ip, "down" if steps > 0 else "up")
            wait(NAV_DELAY)
        wait(NAV_DELAY * 2)
    return False


# ══════════════════════════════════════════════════════════════
#  PLAYBACK CONTROL
# ══════════════════════════════════════════════════════════════

def stop_playback(ip: str):
    key(ip, "stop")
    wait(LOAD_DELAY * 0.5)
    key(ip, "up")
    wait(NAV_DELAY)
    key(ip, "ok")
    output_ok("Playback stopped.")


# ══════════════════════════════════════════════════════════════
#  PLAY MOVIE
# ══════════════════════════════════════════════════════════════

def play_movie(ip: str, query: str):
    """Search for a movie and play the first match.

    Strategy (fast path — 1-2 ADB commands, <5s):
    1. Query content provider for matching VOD in favorites/history
    2. Extract intent_uri or build deeplink from stalker URL
    3. Fire deeplink with channel_type=1 (history) → auto-plays

    Fallback (slow path — UI navigation):
    1. Search in VOD → select first result → detail overlay opens
    2. Down → "Regarder" (Watch) button → OK → playback starts
    """
    # Fast path: query content provider and launch deeplink
    matches = _query_vod_content(ip, query, stream_type="movie")
    if matches:
        match = matches[0]
        intent = match.get("intent_uri", "")
        # Use existing intent or build one from content data
        if not intent and match["stream_id"]:
            intent = _build_stalker_deeplink("movie", match["stream_id"], match["category_id"])
        if intent:
            ensure_mytv(ip)  # deeplink needs MOL3 running
            before = _mol3_media_players(ip)
            if launch_intent(ip, intent):
                # Deeplink with history channel_type auto-plays without detail page.
                verified = _verify_playback(ip, timeout=3.5)
                method = "deeplink" if verified else "deeplink+ok"
                if not verified:
                    # Safety: press down+OK in case detail page appeared
                    key(ip, "down")
                    wait(NAV_DELAY)
                    key(ip, "ok")
                _report_playback(ip, before, f"movie: '{match['title']}'", {
                    "query": query, "title": match["title"],
                    "stream_id": match["stream_id"], "method": method
                })
                return

    # Slow path: UI-based search
    if not do_search(ip, query, section="vod"):
        return
    select_first_result(ip)
    # Detail overlay: focus is on description. Down to "Regarder" button.
    wait(LOAD_DELAY * 0.5)
    key(ip, "down")   # description → "Regarder" button
    wait(NAV_DELAY)
    before = _mol3_media_players(ip)
    key(ip, "ok")     # start playback
    _report_playback(ip, before, f"movie: '{query}'", {"query": query, "method": "search"})


# ══════════════════════════════════════════════════════════════
#  PLAY SERIES EPISODE
# ══════════════════════════════════════════════════════════════

def play_series(ip: str, query: str, season: int = 1, episode: int = 1):
    """Search for a series and play a specific episode.

    Strategy (fast path — 1-2 ADB commands, <5s):
    1. Query content provider for matching series in favorites/history
    2. Build deeplink with target season/episode using stalker:// URL
    3. Fire deeplink with channel_type=2 (series history) → auto-plays

    Fallback (slow path — UI navigation, verified with UI dumps at each step):
    1. Search → focus the result whose title best matches the query → OK
    2. Detail page: focus the "All episodes" action by label → OK
    3. Episode browser: seasons left, episodes right; navigate until the
       focused breadcrumb reads S{season}:A{episode}, then OK to play
    """
    label = f"'{query}' S{season:02d}E{episode:02d}"

    # Fast path: query content provider and build targeted deeplink
    matches = _query_vod_content(ip, query, stream_type="tv")
    if matches:
        match = matches[0]
        stream_id = match["stream_id"]
        category_id = match["category_id"]
        if stream_id and category_id:
            ensure_mytv(ip)  # deeplink needs MOL3 running
            intent = _build_stalker_deeplink("tv", stream_id, category_id, season, episode)
            before = _mol3_media_players(ip)
            if launch_intent(ip, intent):
                # Deeplink with history channel_type auto-plays without detail page.
                verified = _verify_playback(ip, timeout=3.5)
                method = "deeplink" if verified else "deeplink+ok"
                if not verified:
                    # Safety: press OK in case detail page appeared
                    key(ip, "ok")
                _report_playback(ip, before, label, {
                    "query": query, "title": match["title"],
                    "stream_id": stream_id, "season": season,
                    "episode": episode, "method": method
                })
                return

    # Slow path: UI-based search
    if not do_search(ip, query, section="series"):
        return
    title = select_matching_result(ip, query)
    if not title:
        output_err(f"No series result matching '{query}'")
        return

    wait(LOAD_DELAY * 0.5)
    if not open_all_episodes(ip):
        output_err(f"Could not open the episode list for '{title}'")
        return

    if not select_episode(ip, season, episode):
        output_err(f"Episode S{season:02d}E{episode:02d} not found for '{title}'")
        return

    # Play
    before = _mol3_media_players(ip)
    key(ip, "ok")
    label = f"'{_clean_result_title(title)}' S{season:02d}E{episode:02d}"
    _report_playback(ip, before, label, {
        "query": query, "title": title, "season": season, "episode": episode, "method": "search"
    })


# ══════════════════════════════════════════════════════════════
#  STAR / FAVORITE TOGGLE
# ══════════════════════════════════════════════════════════════

def toggle_star(ip: str):
    keys(ip, "right", "right", "right", "right", delay=NAV_DELAY * 0.6)
    wait(NAV_DELAY)
    key(ip, "ok")
    wait(NAV_DELAY * 2)
    keys(ip, "left", "left", "left", "left", delay=NAV_DELAY * 0.6)
    output_ok("Star toggled.")


def star_search(ip: str, query: str, content_type: str = "vod"):
    do_search(ip, query, section=content_type)
    select_first_result(ip)
    toggle_star(ip)
    key(ip, "back")
    wait(NAV_DELAY * 2)
    output_ok(f"Toggled star for '{query}'")


# ══════════════════════════════════════════════════════════════
#  CONTENT PROVIDERS
# ══════════════════════════════════════════════════════════════

def _parse_content_rows(raw: str) -> list[dict]:
    rows = []
    for line in raw.splitlines():
        if not line.startswith("Row:"):
            continue
        row = {}
        data = re.sub(r"^Row:\s+\d+\s+", "", line)
        for match in re.finditer(r"(\w+)=(.*?)(?=, \w+=|$)", data):
            k, v = match.group(1), match.group(2).strip()
            row[k] = None if v == "NULL" else v
        rows.append(row)
    return rows


def fetch_channels(ip: str) -> list[dict]:
    if not JSON_MODE:
        print("Fetching channels from device...", end="", flush=True)
    code, out = _adb_retry(
        ip, "shell", "content", "query",
        "--uri", "content://formuler.media.tv/preview_program",
        "--projection", "title:content_id:channel_id:poster_art_uri:intent_uri:short_description",
        timeout=30
    )
    if code != 0:
        if not JSON_MODE:
            print()
        return []
    channels = []
    for row in _parse_content_rows(out):
        title = row.get("title", "")
        content_id = row.get("content_id", "")
        if not title or title == content_id:
            continue
        cat_id = int(row.get("channel_id", 0))
        channels.append({
            "title": title, "content_id": content_id,
            "category_id": cat_id, "category": CATEGORIES.get(cat_id, "Unknown"),
            "logo": row.get("poster_art_uri"), "intent": row.get("intent_uri"),
            "description": row.get("short_description"),
        })
    if not JSON_MODE:
        print(f" {len(channels)} entries")
    return channels


def search_provider(ip: str, query: str, limit: int | None = None) -> list[dict]:
    """Query MyTVOnline's search provider. It returns 10 rows unless `limit` is given."""
    uri = f"content://tv.formuler.mol3.real.searchProvider/search_suggest_query/{query}"
    if limit:
        uri += f"?limit={limit}"
    # The device shell re-parses the command line: quote so '|' and spaces stay in the URI
    code, out = _adb_retry(
        ip, "shell", "content", "query",
        "--uri", "'" + uri.replace("'", "'\\''") + "'",
        timeout=30 if limit else 15
    )
    if code != 0:
        return []
    results = []
    for row in _parse_content_rows(out):
        t = row.get("suggest_text_1", "")
        intent_data = row.get("suggest_intent_data", "")
        num_m = re.match(r"^(\d+)\.\s+(.+)", t)
        uid_m = re.search(r"uniqueId=([^&]+)", intent_data) if intent_data else None
        results.append({
            "number": num_m.group(1) if num_m else None,
            "title": num_m.group(2) if num_m else t,
            "unique_id": uid_m.group(1) if uid_m else None,
            "logo": row.get("suggest_result_card_image"),
        })
    return results


def get_channels(ip: str, force_refresh: bool = False) -> list[dict]:
    if not force_refresh and CHANNELS_CACHE.exists():
        with open(CHANNELS_CACHE, encoding="utf-8") as f:
            return json.load(f)
    channels = fetch_channels(ip)
    if channels:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with open(CHANNELS_CACHE, "w", encoding="utf-8") as f:
            json.dump(channels, f, indent=2, ensure_ascii=False)
        info(f"Cached {len(channels)} channels/entries")
    return channels


# ══════════════════════════════════════════════════════════════
#  FULL CHANNEL ENUMERATION (Phase 7)
# ══════════════════════════════════════════════════════════════

def enumerate_channels(ip: str) -> list[dict]:
    seen_ids: set[str] = set()
    all_channels: list[dict] = []
    total = len(SEARCH_PREFIXES)
    for i, prefix in enumerate(SEARCH_PREFIXES):
        if not JSON_MODE:
            print(f"\r  Enumerating channels... {i + 1}/{total} (found {len(all_channels)})", end="", flush=True)
        results = search_provider(ip, prefix)
        for r in results:
            uid = r.get("unique_id")
            if uid and uid not in seen_ids:
                seen_ids.add(uid)
                all_channels.append(r)
    if not JSON_MODE:
        print(f"\r  Enumerated {len(all_channels)} unique channels from {total} queries.     ")
    all_channels.sort(key=lambda c: int(c["number"]) if c.get("number") and c["number"].isdigit() else 99999)
    return all_channels


def get_full_channels(ip: str, force_refresh: bool = False) -> list[dict]:
    max_age = CONFIG.get("cache", {}).get("channels_max_age_hours", 24)
    if not force_refresh and FULL_CHANNELS_CACHE.exists():
        age_hours = (time.time() - FULL_CHANNELS_CACHE.stat().st_mtime) / 3600
        if age_hours < max_age:
            with open(FULL_CHANNELS_CACHE, encoding="utf-8") as f:
                return json.load(f)
        warn(f"Channel cache stale ({age_hours:.0f}h old). Refreshing...")

    channels = enumerate_channels(ip)
    if channels:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with open(FULL_CHANNELS_CACHE, "w", encoding="utf-8") as f:
            json.dump(channels, f, indent=2, ensure_ascii=False)
        info(f"Cached {len(channels)} channels")
    return channels


def _channel_kind(title: str) -> str:
    t = title.upper()
    if "RADIO" in t:
        return "radio"
    if "24/7" in t:
        return "24/7"
    if "PPV" in t or "EVENT" in t:
        return "events"
    return "tv"


# Quality tags in channel titles, best first; untagged ranks between HD and HEVC
_QUALITY_RANK = {"4K": 7, "UHD": 7, "ULTRA": 6, "FHD": 5, "RAW": 4, "HD": 3, "HEVC": 1, "H265": 1, "SD": 1, "LQ": 0}
_QUALITY_TAG = re.compile(r"\b(4K|UHD|ULTRA|FHD|RAW|HD|HEVC|H265|SD|LQ|HQ|50FPS)\b")


def _channel_key(title: str) -> tuple[str, int, str]:
    """(group key, quality rank, quality label) for a title: 'NL| NPO 1 FHD' -> ('NPO1', 5, 'FHD')."""
    t = unicodedata.normalize("NFKC", title).upper()  # NFKC turns 'ᴿᴬᵂ' into 'RAW'
    t = re.sub(r"^[A-Z.]{2,4}\s*\|\s*", "", t)
    tags = _QUALITY_TAG.findall(t)
    key = re.sub(r"[^A-Z0-9+&!]", "", _QUALITY_TAG.sub("", t))
    rank = max((_QUALITY_RANK.get(q, 2) for q in tags), default=2)
    label = next((q for q in tags if _QUALITY_RANK.get(q, 2) == rank), "")
    return key or t, rank, label


def country_channels(ip: str, code: str, force_refresh: bool = False) -> list[dict]:
    """All live channels of one country, read from the search provider in one query per prefix."""
    code = code.lower()
    if code not in COUNTRIES:
        return []
    cache = CACHE_DIR / f"country_{code}.json"
    max_age = CONFIG.get("cache", {}).get("channels_max_age_hours", 24)
    if not force_refresh and cache.exists() and (time.time() - cache.stat().st_mtime) / 3600 < max_age:
        with open(cache, encoding="utf-8") as f:
            return json.load(f)

    seen: set[str] = set()
    channels: list[dict] = []
    for prefix in COUNTRIES[code][1]:
        own = re.compile(rf"^{re.escape(prefix)}\s*\|\s*", re.I)  # search matches anywhere in the title
        for r in search_provider(ip, f"{prefix}|", limit=20000):
            uid = r.get("unique_id")
            if uid and uid not in seen and own.match(r.get("title", "")):
                seen.add(uid)
                channels.append({**r, "kind": _channel_kind(r["title"])})
    channels.sort(key=lambda c: int(c["number"]) if (c.get("number") or "").isdigit() else 1 << 30)
    if channels:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with open(cache, "w", encoding="utf-8") as f:
            json.dump(channels, f, indent=2, ensure_ascii=False)
    return channels


def cmd_country(ip: str, code: str = "", filter_text: str = ""):
    if not code:
        rows = [{"code": k, "name": v[0], "prefixes": v[1]} for k, v in COUNTRIES.items()]
        if JSON_MODE:
            output(rows)
            return
        for r in rows:
            print(f"  {r['code']:<4} {r['name']}  ({', '.join(p + '|' for p in r['prefixes'])})")
        return
    if code.lower() not in COUNTRIES:
        error(f"Unknown country '{code}'. Known: {', '.join(COUNTRIES)}")
        return
    channels = country_channels(ip, code)
    if filter_text:
        channels = _fuzzy_match(filter_text, channels)
    if JSON_MODE:
        output(channels)
        return
    for ch in channels:
        print(f"  {ch.get('number') or '':>6}. {ch['title']}")
    print(f"\n  {_C.DIM}{COUNTRIES[code.lower()][0]}: {len(channels)} channels{_C.RESET}")


def cmd_tune_id(ip: str, unique_id: str):
    """Tune a channel by its provider unique id (as listed by 'country')."""
    if not re.fullmatch(r"[\w.-]+", unique_id):
        error(f"Invalid channel id '{unique_id}'")
        return
    ch = {"title": unique_id, "unique_id": unique_id}
    for path in [FULL_CHANNELS_CACHE, *CACHE_DIR.glob("country_*.json")]:
        try:
            with open(path, encoding="utf-8") as f:
                hit = next((c for c in json.load(f) if c.get("unique_id") == unique_id), None)
        except (OSError, json.JSONDecodeError):
            continue
        if hit:
            ch = hit
            break
    output_ok(f"Tuning to: {ch['title']}", ch)
    _record_tune(ch)
    _tune_by_uid(ip, unique_id)


def channel_by_number(ip: str, number: str) -> dict | None:
    for ch in get_full_channels(ip):
        if ch.get("number") == number:
            return ch
    return None


# ══════════════════════════════════════════════════════════════
#  TUNE HISTORY
# ══════════════════════════════════════════════════════════════

def _load_tune_history() -> list[dict]:
    if TUNE_HISTORY_FILE.exists():
        try:
            with open(TUNE_HISTORY_FILE, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return []


def _save_tune_history(history: list[dict]):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(TUNE_HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history[-50:], f, indent=2, ensure_ascii=False)


def _record_tune(channel: dict):
    """Record a channel tune event to history."""
    history = _load_tune_history()
    entry = {
        "title": channel.get("title", "Unknown"),
        "timestamp": datetime.datetime.now().isoformat(),
    }
    # Copy relevant fields
    for k in ("number", "unique_id", "intent", "content_id"):
        if channel.get(k):
            entry[k] = channel[k]
    history.append(entry)
    _save_tune_history(history)


def cmd_last(ip: str):
    """Retune the last tuned channel."""
    history = _load_tune_history()
    if not history:
        output_err("No tune history. Tune a channel first.")
        return
    last = history[-1]
    output_ok(f"Retuning: {last['title']}", last)
    _notify("Formuler Remote", f"Retuning: {last['title']}")
    if last.get("unique_id"):
        _tune_by_uid(ip, last["unique_id"])
    elif last.get("intent"):
        tune_by_intent(ip, last["intent"])
    else:
        cmd_tune(ip, last["title"])


def cmd_prev(ip: str):
    """Swap to previous channel (like TV recall button)."""
    history = _load_tune_history()
    if len(history) < 2:
        output_err("Need at least 2 channels in history.")
        return
    prev = history[-2]
    output_ok(f"Switching to previous: {prev['title']}", prev)
    _notify("Formuler Remote", f"Previous: {prev['title']}")
    if prev.get("unique_id"):
        _tune_by_uid(ip, prev["unique_id"])
    elif prev.get("intent"):
        tune_by_intent(ip, prev["intent"])
    else:
        cmd_tune(ip, prev["title"])


def cmd_tune_history(count: int = 10):
    """Show recent tune history."""
    history = _load_tune_history()
    if not history:
        output_err("No tune history.")
        return
    recent = history[-count:]
    recent.reverse()
    if JSON_MODE:
        output(recent)
    else:
        for i, entry in enumerate(recent, 1):
            ts = entry.get("timestamp", "")[:19].replace("T", " ")
            num = f" #{entry['number']}" if entry.get("number") else ""
            print(f"  {i:3d}  {ts}  {entry['title']}{num}")


# ══════════════════════════════════════════════════════════════
#  LIVE TV COMMANDS
# ══════════════════════════════════════════════════════════════

def launch_intent(ip: str, intent_uri: str) -> bool:
    """Launch any MOL3 deeplink intent by parsing its URI.

    Handles live channels (channel_type, group_id, unique_channel_id),
    VOD (vod_unique_id), and series (vod_id, vod_unique_id).
    """
    from urllib.parse import unquote
    comp = re.search(r"component=([^;]+)", intent_uri)
    if not comp:
        return False
    cmd = ["shell", "am", "start", "-n", comp.group(1)]
    # Integer extras (i.xxx=val)
    for m in re.finditer(r"i\.(\w+)=([^;]+)", intent_uri):
        cmd += ["--ei", m.group(1), m.group(2)]
    # String extras (S.xxx=val)
    for m in re.finditer(r"S\.(\w+)=([^;]+)", intent_uri):
        cmd += ["--es", m.group(1), unquote(m.group(2))]
    code, _ = adb(ip, *cmd)
    return code == 0


# Backward compat alias
tune_by_intent = launch_intent


def _build_stalker_deeplink(stream_type: str, stream_id: int | str, category_id: int | str,
                            season: int | None = None, episode: int | None = None) -> str:
    """Build a deeplink intent URI from content identifiers.

    Uses channel_type=1 (VOD history) for movies and channel_type=2 (series history)
    for series. History-type intents auto-play without showing the detail page.
    """
    stalker_url = (f"stalker://tv.formuler.stream?server_id=0"
                   f"&category_id={category_id}&stream_type={stream_type}&stream_id={stream_id}")
    if stream_type == "tv" and season is not None and episode is not None:
        stalker_url += f"&season_id={stream_id}:{season}&episode_id={episode}"
        channel_type = 2  # series history → auto-plays
    else:
        channel_type = 1  # VOD history → auto-plays

    return (f"intent:#Intent;component={MOL3_PKG}/tv.formuler.mol3.deeplink.SchemeLinkActivity;"
            f"S.vod_unique_id={stalker_url};i.channel_type={channel_type};end")


def _verify_playback(ip: str, timeout: float = 3.5) -> bool:
    """Check that StreamActivity is the focused activity (playback started).

    Uses grep on device-side to minimize data transfer over slow connections.
    """
    time.sleep(1.5)
    for _ in range(max(1, int(timeout))):
        # Use single-string shell command so pipe works on device
        code, out = adb(ip, "shell",
                        "dumpsys activity activities | grep -m1 mResumedActivity",
                        timeout=4)
        if code == 0 and "StreamActivity" in out:
            return True
        time.sleep(0.5)
    return False


def _mol3_media_players(ip: str) -> set[str]:
    """IDs (piid) of MyTVOnline media players that are currently playing.

    Read from `dumpsys audio`: a started USAGE_MEDIA AudioTrack owned by the
    MOL3 process means the player is actually rendering content. This holds
    while the TV is muted or scrcpy captures the audio.
    """
    code, pid = adb(ip, "shell", "pidof", MOL3_PKG, timeout=5)
    pid = pid.strip().split()[0] if code == 0 and pid.strip() else ""
    if not pid:
        return set()
    code, out = adb(ip, "shell", "dumpsys audio | grep AudioPlaybackConfiguration", timeout=8)
    players = set()
    for line in out.splitlines():
        if f"/{pid} " in line and "state:started" in line and "USAGE_MEDIA" in line:
            m = re.search(r"piid:(\d+)", line)
            if m:
                players.add(m.group(1))
    return players


def _player_error(ip: str) -> str | None:
    """Return the message of a player error dialog on screen, if any."""
    root = _ui_root(ip)
    if root is None:
        return None
    texts = {_rid(n): n.get("text", "") for n in root.iter("node") if _rid(n).startswith("dialog_")}
    message = texts.get("dialog_message", "")
    # The stop confirmation ("Stop afspelen?") uses the same dialog layout
    if not message or "stop" in _normalize(message):
        return None
    title = texts.get("dialog_title", "")
    return f"{title}: {message}" if title else message


def _wait_for_playback(ip: str, before: set[str], timeout: float = 30) -> tuple[bool, str]:
    """Wait until a new MOL3 media player is playing, or an error dialog shows.

    `before` holds the players that were already playing (e.g. live TV) so they
    don't count. Requires two consecutive hits so a player that starts and
    immediately fails isn't reported as playing. Returns (ok, reason).
    """
    deadline = time.time() + timeout
    hits = 0
    while time.time() < deadline:
        if _mol3_media_players(ip) - before:
            hits += 1
            if hits >= 2:
                return True, "playing"
        else:
            hits = 0
            err = _player_error(ip)
            if err:
                return False, err
        time.sleep(1.5)
    return False, f"no playback after {int(timeout)}s (still loading?)"


def _report_playback(ip: str, before: set[str], label: str, data: dict):
    """Report success only once playback is confirmed, otherwise the reason."""
    if DRY_RUN:
        output_ok(f"Playing {label}", data)
        return
    ok, reason = _wait_for_playback(ip, before, timeout=PLAYBACK_TIMEOUT)
    if ok:
        output_ok(f"Playing {label}", {**data, "verified": True})
        _notify("Formuler Remote", f"Playing: {label}")
    else:
        output_err(f"Could not play {label}: {reason}", {**data, "verified": False})


def _query_vod_content(ip: str, query: str, stream_type: str = "") -> list[dict]:
    """Search content provider for VOD/series matching query.

    Returns list of dicts with title, stream_type, stream_id, category_id,
    season, episode, intent_uri, channel_id, description.
    """
    channels = get_channels(ip)
    if not channels:
        return []

    # Determine which categories to search based on stream_type
    if stream_type == "movie":
        cats = ("VOD Favorites", "VOD History")
    elif stream_type == "tv":
        cats = ("Series Favorites", "Series History")
    else:
        cats = ("VOD Favorites", "VOD History", "Series Favorites", "Series History")

    filtered = [c for c in channels if c.get("category") in cats]
    matches = _fuzzy_match(query, filtered)

    results = []
    for m in matches:
        intent = m.get("intent", "")
        content_id = m.get("content_id", "")
        stalker_url = content_id or ""

        # Parse stalker URL for structured data
        sid_m = re.search(r"stream_id=(\d+)", stalker_url)
        cat_m = re.search(r"category_id=(\d+)", stalker_url)
        type_m = re.search(r"stream_type=(\w+)", stalker_url)
        season_m = re.search(r"season_id=\d+[%3A:]+(\d+)", stalker_url)
        ep_m = re.search(r"episode_id=(\d+)", stalker_url)

        results.append({
            "title": m.get("title", ""),
            "stream_type": type_m.group(1) if type_m else "",
            "stream_id": int(sid_m.group(1)) if sid_m else 0,
            "category_id": int(cat_m.group(1)) if cat_m else 0,
            "season": int(season_m.group(1)) if season_m else None,
            "episode": int(ep_m.group(1)) if ep_m else None,
            "intent_uri": intent,
            "channel_id": m.get("category_id", 0),
            "category": m.get("category", ""),
            "description": m.get("description", ""),
            "logo": m.get("logo", ""),
        })
    return results


def _find_content(query: str, categories: tuple[str, ...]) -> list[dict]:
    """Search favorites/history cache for content matching query.

    Returns list of matches with title, intent_uri, category, etc.
    Prefers favorites over history.
    """
    if not CHANNELS_CACHE.exists():
        return []
    try:
        with open(CHANNELS_CACHE, encoding="utf-8") as f:
            channels = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []
    # Filter to requested categories
    filtered = [c for c in channels if c.get("category") in categories]
    if not filtered:
        return []
    return _fuzzy_match(query, filtered)


def _play_by_intent(ip: str, query: str, categories: tuple[str, ...]) -> bool:
    """Try to play content by launching its cached intent directly.

    Returns True if a match was found and intent launched, False otherwise.
    """
    matches = _find_content(query, categories)
    if not matches:
        return False
    match = matches[0]
    intent = match.get("intent")
    if not intent:
        return False
    return launch_intent(ip, intent)


def _build_series_intent(base_intent: str, season: int, episode: int) -> str:
    """Modify a series intent to target a specific season and episode.

    Series intents contain a stalker:// URL with season_id=SID:SEASON&episode_id=EP.
    We rewrite these parameters to the desired season/episode.
    """
    from urllib.parse import unquote, quote
    # Extract and decode the vod_unique_id
    m = re.search(r"S\.vod_unique_id=([^;]+)", base_intent)
    if not m:
        return base_intent
    encoded_url = m.group(1)
    url = unquote(unquote(encoded_url))  # double decode (may be double-encoded)

    # Rewrite season_id and episode_id
    url = re.sub(r"season_id=[^&]+", lambda _: f"season_id={_extract_stream_id(url)}:{season}", url)
    url = re.sub(r"episode_id=\d+", f"episode_id={episode}", url)

    # Re-encode and substitute back
    new_encoded = quote(url, safe="")
    return base_intent[:m.start(1)] + new_encoded + base_intent[m.end(1):]


def _extract_stream_id(stalker_url: str) -> str:
    """Extract stream_id from a stalker:// URL."""
    m = re.search(r"stream_id=(\d+)", stalker_url)
    return m.group(1) if m else "0"


def _tune_by_uid(ip: str, unique_id: str) -> bool:
    return tune_by_intent(
        ip,
        f"intent:#Intent;component={MOL3_PKG}/tv.formuler.mol3.deeplink.SchemeLinkActivity;"
        f"i.channel_type=5;S.group_id=32768_0_0;S.unique_channel_id={unique_id};end"
    )


def cmd_tune(ip: str, query: str):
    # Resolve aliases
    aliases = CONFIG.get("aliases", {})
    query = aliases.get(query.lower(), query)

    # Try full channel cache first
    if FULL_CHANNELS_CACHE.exists():
        with open(FULL_CHANNELS_CACHE, encoding="utf-8") as f:
            full = json.load(f)
        # Try exact channel number
        if query.isdigit():
            for ch in full:
                if ch.get("number") == query:
                    output_ok(f"Tuning to: {ch['title']} (#{query})", ch)
                    _record_tune(ch)
                    _notify("Formuler Remote", f"Tuning: {ch['title']}")
                    if ch.get("unique_id"):
                        _tune_by_uid(ip, ch["unique_id"])
                    return
        # Try fuzzy match in full cache
        full_matches = _fuzzy_match(query, full)
        if len(full_matches) == 1 or (full_matches and all(m["title"] == full_matches[0]["title"] for m in full_matches)):
            ch = full_matches[0]
            output_ok(f"Tuning to: {ch['title']}", ch)
            _record_tune(ch)
            _notify("Formuler Remote", f"Tuning: {ch['title']}")
            if ch.get("unique_id"):
                _tune_by_uid(ip, ch["unique_id"])
            return
        if full_matches:
            _show_tune_choices(ip, full_matches, is_full=True)
            return

    # Fallback to favorites/history cache
    channels = get_channels(ip)
    matches = _fuzzy_match(query, channels)
    live = [m for m in matches if m["category"] in ("Favorites", "Live History")]
    if live:
        matches = live

    if not matches:
        results = search_provider(ip, query)
        if results:
            r = results[0]
            output_ok(f"Tuning to: {r['title']}", r)
            _record_tune(r)
            _notify("Formuler Remote", f"Tuning: {r['title']}")
            if r.get("unique_id"):
                _tune_by_uid(ip, r["unique_id"])
                return
            output_err("No unique ID found")
        else:
            error(f"No channel matching '{query}'")
        return

    if len(matches) == 1 or all(m["title"] == matches[0]["title"] for m in matches):
        ch = matches[0]
        output_ok(f"Tuning to: {ch['title']}", ch)
        _record_tune(ch)
        _notify("Formuler Remote", f"Tuning: {ch['title']}")
        if ch.get("intent"):
            tune_by_intent(ip, ch["intent"])
        return

    _show_tune_choices(ip, matches, is_full=False)


def _show_tune_choices(ip: str, matches: list[dict], is_full: bool):
    if AUTO_FIRST or JSON_MODE:
        ch = matches[0]
        output_ok(f"Tuning to: {ch['title']}", ch)
        _record_tune(ch)
        _notify("Formuler Remote", f"Tuning: {ch['title']}")
        if is_full and ch.get("unique_id"):
            _tune_by_uid(ip, ch["unique_id"])
        elif ch.get("intent"):
            tune_by_intent(ip, ch["intent"])
        return

    print(f"Found {len(matches)} matches:")
    for i, ch in enumerate(matches[:15]):
        marker = f"{_C.BOLD}>> " if i == 0 else "   "
        cat = f" [{ch.get('category', '')}]" if ch.get("category") else ""
        num = f" #{ch['number']}" if ch.get("number") else ""
        print(f"  {marker}{i + 1}. {ch['title']}{num}{_C.DIM}{cat}{_C.RESET}")
    try:
        choice = input(f"Select [1-{min(len(matches), 15)}] or Enter for #1: ").strip()
    except (EOFError, KeyboardInterrupt):
        return
    idx = int(choice) - 1 if choice.isdigit() else 0
    if 0 <= idx < len(matches):
        ch = matches[idx]
        output_ok(f"Tuning to: {ch['title']}", ch)
        _record_tune(ch)
        _notify("Formuler Remote", f"Tuning: {ch['title']}")
        if is_full and ch.get("unique_id"):
            _tune_by_uid(ip, ch["unique_id"])
        elif ch.get("intent"):
            tune_by_intent(ip, ch["intent"])


def cmd_search(ip: str, query: str):
    channels = get_channels(ip)
    cached = _fuzzy_match(query, channels)
    device = search_provider(ip, query)

    if JSON_MODE:
        output({"favorites_history": cached[:20], "live_channels": device})
        return

    if cached:
        print(f"\n  {_C.CYAN}Favorites/history{_C.RESET} ({len(cached)} matches):")
        for ch in cached[:20]:
            print(f"    {_C.DIM}[{ch['category']:<18}]{_C.RESET} {ch['title']}")
    if device:
        print(f"\n  {_C.CYAN}Full channel list{_C.RESET} ({len(device)} matches):")
        for r in device:
            num = f"{r['number']:>4}. " if r.get("number") else "      "
            print(f"    {num}{r['title']}")
    if not cached and not device:
        warn("  No results found.")


def cmd_list(ip: str, filter_text: str = ""):
    channels = get_channels(ip)
    filtered = _fuzzy_match(filter_text, channels) if filter_text else channels

    if JSON_MODE:
        output(filtered or [])
        return

    if not filtered:
        warn("No matches. Searching device...")
        for r in search_provider(ip, filter_text):
            num = f"{r['number']:>4}. " if r.get("number") else "      "
            print(f"  {num}{r['title']}")
        return
    for ch in filtered:
        print(f"  {_C.DIM}[{ch['category']:<18}]{_C.RESET} {ch['title']}")


def cmd_list_all(ip: str, filter_text: str = ""):
    channels = get_full_channels(ip)
    if filter_text:
        channels = _fuzzy_match(filter_text, channels)

    if JSON_MODE:
        output(channels)
        return

    for ch in channels:
        num = f"{ch['number']:>4}. " if ch.get("number") else "      "
        print(f"  {num}{ch['title']}")
    print(f"\n  {_C.DIM}Total: {len(channels)} channels{_C.RESET}")


def cmd_channel_info(ip: str, number: str):
    ch = channel_by_number(ip, number)
    if not ch:
        error(f"Channel #{number} not found. Run 'refresh-all' to build the full database.")
        return
    if JSON_MODE:
        output(ch)
        return
    print(f"  {_C.BOLD}#{ch.get('number', '?')} {ch['title']}{_C.RESET}")
    if ch.get("unique_id"):
        print(f"  Unique ID: {ch['unique_id']}")
    if ch.get("logo"):
        print(f"  Logo: {ch['logo']}")


def cmd_categories(ip: str):
    counts: dict[str, int] = {}
    for ch in get_channels(ip):
        counts[ch["category"]] = counts.get(ch["category"], 0) + 1

    if JSON_MODE:
        output(counts)
        return

    for cat, n in sorted(counts.items()):
        print(f"  {_C.CYAN}{cat}{_C.RESET}: {n}")


# ══════════════════════════════════════════════════════════════
#  VOD / SERIES BROWSING (Phase 9)
# ══════════════════════════════════════════════════════════════

def cmd_browse(ip: str, section: str):
    go_to_section(ip, section)
    output_ok(f"Browsing {section}. Use arrow keys to navigate, 'back' to return.")


def cmd_episodes(ip: str, query: str):
    """Open the episode browser for a series."""
    do_search(ip, query, section="series")
    select_first_result(ip)
    # Detail page: "Tous les épisodes" should be focused
    wait(LOAD_DELAY * 0.5)
    key(ip, "ok")  # open episodes browser
    wait(LOAD_DELAY)
    screenshot(ip, str(TMP_DIR / "formuler-episodes.png"))
    output_ok(f"Episode browser open for '{query}'. Screenshot saved to {TMP_DIR / 'formuler-episodes.png'}")
    if not JSON_MODE:
        print("Navigate with arrow keys. 'back' to return.")


def cmd_resume(ip: str, content_type: str = "vod"):
    """Resume the last played content of the given type.

    Uses cached intents for instant launch (no UI navigation).
    """
    channels = get_channels(ip, force_refresh=True)
    history_cat = {"vod": "VOD History", "series": "Series History", "live": "Live History"}
    cat_name = history_cat.get(content_type, "")
    hist = [c for c in channels if c["category"] == cat_name]
    if not hist:
        error(f"No {content_type} history found.")
        return

    last = hist[0]
    output_ok(f"Resuming: {last['title']}", last)
    _notify("Formuler Remote", f"Resuming: {last['title']}")

    # Fast path: launch via cached intent (works for all content types)
    if last.get("intent"):
        launch_intent(ip, last["intent"])
        return

    # Slow path: UI search
    do_search(ip, last["title"], section=content_type)
    select_first_result(ip)
    if content_type == "vod":
        key(ip, "down")  # description → "Regarder"
    wait(NAV_DELAY)
    key(ip, "ok")


def cmd_info(ip: str, query: str, content_type: str = ""):
    channels = get_channels(ip)
    matches = _fuzzy_match(query, channels)
    if content_type:
        cat_filter = {"vod": ("VOD History", "VOD Favorites"),
                      "series": ("Series History", "Series Favorites"),
                      "live": ("Favorites", "Live History")}
        allowed = cat_filter.get(content_type, ())
        if allowed:
            filtered = [m for m in matches if m["category"] in allowed]
            if filtered:
                matches = filtered

    if not matches:
        error(f"No match for '{query}' in local database.")
        return

    if JSON_MODE:
        output(matches[:10])
        return

    for m in matches[:5]:
        print(f"\n  {_C.BOLD}{m['title']}{_C.RESET}")
        print(f"  Category: {_C.CYAN}{m['category']}{_C.RESET}")
        if m.get("description"):
            print(f"  {m['description']}")
        if m.get("logo"):
            print(f"  {_C.DIM}Logo: {m['logo']}{_C.RESET}")


def cmd_vod_history(ip: str, count: int = 20, filter_text: str = ""):
    """Show VOD and series watch history from the content provider.

    Displays title, type (movie/series), season/episode for series,
    and category. History entries are from the device's preview_program
    content provider (channels 3=VOD History, 5=Series History).
    """
    channels = get_channels(ip, force_refresh=True)
    history = [c for c in channels if c.get("category") in ("VOD History", "Series History")]

    if filter_text:
        history = _fuzzy_match(filter_text, history)

    if not history:
        error("No VOD/series history found.")
        return

    entries = []
    for h in history[:count]:
        # Parse stalker URL from content_id for episode info
        content_id = h.get("content_id", "")
        stalker_url = content_id or ""

        # Extract structured data
        type_m = re.search(r"stream_type=(\w+)", stalker_url)
        sid_m = re.search(r"stream_id=(\d+)", stalker_url)
        cat_m = re.search(r"category_id=(\d+)", stalker_url)
        season_m = re.search(r"season_id=\d+[%3A:]+(\d+)", stalker_url)
        ep_m = re.search(r"episode_id=(\d+)", stalker_url)

        stream_type = type_m.group(1) if type_m else "unknown"
        entry = {
            "title": h.get("title", ""),
            "type": "series" if stream_type == "tv" else "movie",
            "stream_id": int(sid_m.group(1)) if sid_m else None,
            "category_id": int(cat_m.group(1)) if cat_m else None,
            "category": h.get("category", ""),
            "description": h.get("description", ""),
        }
        if stream_type == "tv":
            entry["season"] = int(season_m.group(1)) if season_m else None
            entry["episode"] = int(ep_m.group(1)) if ep_m else None
        entries.append(entry)

    if JSON_MODE:
        output(entries)
        return

    print(f"\n  {_C.BOLD}Watch History{_C.RESET} ({len(entries)} entries)\n")
    for e in entries:
        type_badge = f"{_C.MAGENTA}SERIES{_C.RESET}" if e["type"] == "series" else f"{_C.BLUE}MOVIE {_C.RESET}"
        ep_info = ""
        if e["type"] == "series" and e.get("season") is not None:
            ep_info = f" {_C.CYAN}S{e['season']:02d}E{e.get('episode', 0):02d}{_C.RESET}"
        print(f"  [{type_badge}]{ep_info} {e['title']}")


# ══════════════════════════════════════════════════════════════
#  EPG (Phase 8)
# ══════════════════════════════════════════════════════════════

def cmd_epg(ip: str, channel: str = ""):
    if channel:
        cmd_tune(ip, channel)
        wait(3)
    key(ip, "info")
    wait(1)
    path = str(TMP_DIR / "formuler-epg.png")
    screenshot(ip, path)
    output_ok(f"EPG info screenshot saved to {path}", {"screenshot": path})
    key(ip, "info")  # dismiss


def cmd_guide(ip: str):
    ensure_mytv(ip)
    key(ip, "guide")
    wait(2)
    path = str(TMP_DIR / "formuler-guide.png")
    screenshot(ip, path)
    output_ok(f"EPG guide screenshot saved to {path}", {"screenshot": path})


def cmd_search_epg(ip: str, query: str):
    open_search(ip, "live")
    text_input(ip, query)
    wait(0.5)
    key(ip, "ok")
    wait(2)
    # Navigate to "Programme TV" tab (second tab)
    key(ip, "right")
    wait(0.5)
    key(ip, "ok")
    wait(1)
    output_ok(f"Showing EPG results for '{query}'. Navigate with arrows.")


def cmd_now_playing(ip: str):
    """Show what's currently or recently playing.

    Checks the current activity to determine content type, then queries
    the content provider for the most recent entry in the matching history.
    Falls back to logcat if activity detection fails.
    """
    # Detect current activity
    _, actout = adb(ip, "shell", "dumpsys", "activity", "activities")
    activity = ""
    m = re.search(r"mResumedActivity:.*?/([^\s]+)", actout)
    if m:
        activity = m.group(1)

    result = {"activity": activity}

    # Map activity to content type
    if "LiveActivity" in activity:
        result["type"] = "live"
        # Get most recent from Live History (channel_id=1)
        code, out = adb(ip, "shell", "content", "query",
                        "--uri", "content://formuler.media.tv/preview_program",
                        "--projection", "title:poster_art_uri:intent_uri",
                        "--where", "channel_id=1")
        entries = _parse_content_rows(out)
        if entries:
            result["channel"] = entries[0].get("title", "Unknown")
            result["logo"] = entries[0].get("poster_art_uri", "")
    elif "StreamActivity" in activity or "StreamSearchActivity" in activity:
        # Could be VOD or series — check both histories
        for cid, ctype in [("3", "vod"), ("5", "series")]:
            code, out = adb(ip, "shell", "content", "query",
                            "--uri", "content://formuler.media.tv/preview_program",
                            "--projection", "title:short_description:genre:poster_art_uri:intent_uri",
                            "--where", f"channel_id={cid}")
            entries = _parse_content_rows(out)
            if entries:
                result["type"] = ctype
                result["title"] = entries[0].get("title", "Unknown")
                result["description"] = entries[0].get("short_description", "")
                result["genre"] = entries[0].get("genre", "")
                result["poster"] = entries[0].get("poster_art_uri", "")
                break
    else:
        result["type"] = "idle"
        result["note"] = "No media activity detected"

    if JSON_MODE:
        output(result)
    else:
        if result.get("type") == "live":
            info(f"Now playing: {result.get('channel', 'Unknown')} (Live TV)")
        elif result.get("type") in ("vod", "series"):
            info(f"Now playing: {result.get('title', 'Unknown')} ({result.get('type').upper()})")
            if result.get("genre"):
                print(f"  Genre: {result['genre']}")
            if result.get("description"):
                desc = result["description"][:200]
                print(f"  {_C.DIM}{desc}{'...' if len(result.get('description', '')) > 200 else ''}{_C.RESET}")
        else:
            warn("No media currently playing")


    # _parse_content_rows is defined earlier (near content provider helpers)


# ══════════════════════════════════════════════════════════════
#  MACROS (Phase 10)
# ══════════════════════════════════════════════════════════════

def get_macros() -> dict[str, str]:
    return CONFIG.get("macros", {})


def run_macro(ip: str, name: str):
    macros = get_macros()
    if name not in macros:
        error(f"Unknown macro: {name}")
        if macros:
            print(f"Available: {', '.join(sorted(macros.keys()))}")
        else:
            warn("No macros defined. Add [macros] section to config file.")
        return
    info(f"Running macro: {name}")
    for step in macros[name].split(";"):
        step = step.strip()
        if not step:
            continue
        if step.startswith("wait "):
            try:
                wait(float(step.split()[1]))
            except (ValueError, IndexError):
                wait(1)
        else:
            dispatch(ip, step)


def cmd_macros(ip: str):
    macros = get_macros()
    if JSON_MODE:
        output(macros)
        return
    if not macros:
        warn("No macros defined. Add [macros] section to your config file:")
        print(f"  {_C.DIM}{CONFIG_FILE}{_C.RESET}")
        print(f'  [macros]')
        print(f'  morning = "open mytv; wait 3; tune BFM TV"')
        return
    for name, cmds in sorted(macros.items()):
        print(f"  {_C.BOLD}{name}{_C.RESET}: {_C.DIM}{cmds}{_C.RESET}")


# ══════════════════════════════════════════════════════════════
#  TIMED ACTIONS (Phase 10)
# ══════════════════════════════════════════════════════════════

_timers: list[dict] = []  # {"timer": Timer, "time": str, "command": str}


def cmd_at(ip: str, time_str: str, command: str):
    try:
        h, m = map(int, time_str.split(":"))
    except ValueError:
        error("Time format: HH:MM (e.g., 20:30)")
        return

    now = datetime.datetime.now()
    target = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if target <= now:
        target += datetime.timedelta(days=1)
    delay = (target - now).total_seconds()

    def fire():
        print(f"\n{_C.YELLOW}[Timer]{_C.RESET} Executing: {command}")
        dispatch(ip, command)
        if not JSON_MODE:
            print(f"{_C.BLUE}remote>{_C.RESET} ", end="", flush=True)

    t = threading.Timer(delay, fire)
    t.daemon = True
    t.start()
    _timers.append({"timer": t, "time": target.strftime("%H:%M"), "command": command})
    output_ok(f"Scheduled '{command}' at {target.strftime('%H:%M')} ({delay / 60:.0f}min from now)")


def cmd_timers():
    active = [t for t in _timers if t["timer"].is_alive()]
    if JSON_MODE:
        output([{"time": t["time"], "command": t["command"]} for t in active])
        return
    if not active:
        warn("No pending timers.")
        return
    for i, t in enumerate(active):
        print(f"  {i + 1}. {_C.BOLD}{t['time']}{_C.RESET} → {t['command']}")


def cmd_cancel_timer(index: int):
    active = [t for t in _timers if t["timer"].is_alive()]
    if 0 <= index < len(active):
        active[index]["timer"].cancel()
        output_ok(f"Cancelled: {active[index]['command']}")
    else:
        output_err(f"Invalid timer index. Use 'timers' to see list.")


# ══════════════════════════════════════════════════════════════
#  SLEEP TIMER
# ══════════════════════════════════════════════════════════════

_sleep_timer: dict | None = None  # {"timer": Timer, "time": str}


def _sleep_fire(ip: str):
    global _sleep_timer
    print(f"\n{_C.YELLOW}[Sleep Timer]{_C.RESET} Time's up — stopping playback and powering off")
    key(ip, "stop")
    wait(LOAD_DELAY * 0.5)
    key(ip, "sleep")
    _sleep_timer = None
    _notify("Formuler Remote", "Sleep timer fired — device powering off")


def cmd_sleep_timer(ip: str, minutes: float):
    global _sleep_timer
    if _sleep_timer and _sleep_timer["timer"].is_alive():
        _sleep_timer["timer"].cancel()
    t = threading.Timer(minutes * 60, _sleep_fire, args=[ip])
    t.daemon = True
    t.start()
    target = (datetime.datetime.now() + datetime.timedelta(minutes=minutes)).strftime("%H:%M")
    _sleep_timer = {"timer": t, "time": target}
    output_ok(f"Sleep timer set for {minutes:.0f}min (at {target})")


def cmd_sleep_at(ip: str, time_str: str):
    try:
        h, m = map(int, time_str.split(":"))
    except ValueError:
        output_err("Time format: HH:MM (e.g., 23:30)")
        return
    now = datetime.datetime.now()
    target = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if target <= now:
        target += datetime.timedelta(days=1)
    minutes = (target - now).total_seconds() / 60
    cmd_sleep_timer(ip, minutes)


def cmd_sleep_cancel():
    global _sleep_timer
    if _sleep_timer and _sleep_timer["timer"].is_alive():
        _sleep_timer["timer"].cancel()
        output_ok(f"Sleep timer cancelled (was set for {_sleep_timer['time']})")
        _sleep_timer = None
    else:
        output_err("No sleep timer active.")


# ══════════════════════════════════════════════════════════════
#  ADVANCED REMOTE (Phase 10)
# ══════════════════════════════════════════════════════════════

def cmd_repeat(ip: str, key_name: str, count: int, delay_ms: float = 250):
    delay_s = delay_ms / 1000
    for i in range(count):
        key(ip, key_name)
        if i < count - 1:
            time.sleep(delay_s)
    output_ok(f"Pressed {key_name} x{count}")


def cmd_cec(ip: str, action: str):
    action = action.lower()
    if action == "on":
        key(ip, "wakeup")
        output_ok("CEC: Wake up / Power on")
    elif action == "off":
        key(ip, "sleep")
        output_ok("CEC: Sleep / Power off")
    elif action == "standby":
        key(ip, "power")
        output_ok("CEC: Standby toggle")
    else:
        output_err(f"Unknown CEC action: {action}. Use: on, off, standby")


def cmd_record_screen(ip: str, duration: int = 30, path: str = str(TMP_DIR / "formuler-recording.mp4")):
    remote = "/sdcard/recording.mp4"
    info(f"Recording for {duration}s... (video surfaces will be green due to DRM)")
    try:
        adb(ip, "shell", "screenrecord", "--time-limit", str(duration), remote,
            timeout=duration + 10)
    except Exception:
        pass
    code, out = adb(ip, "pull", remote, path, timeout=30)
    adb(ip, "shell", "rm", remote)
    if code != 0:
        output_err(f"Recording pull failed: {out}")
    else:
        output_ok(f"Recording saved to {path}", {"path": path})


_keep_connection = False  # set when a command (mirror) still needs the ADB connection after exit


def _mirror_running() -> bool:
    """True if a scrcpy mirror is running; it needs the ADB connection kept open."""
    if os.name == "nt":
        cmd = ["tasklist", "/FI", "IMAGENAME eq scrcpy.exe", "/NH"]
    else:
        cmd = ["pgrep", "-x", "scrcpy"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return False
    return "scrcpy" in out.lower() if os.name == "nt" else bool(out.strip())


def _disconnect(ip: str):
    """Disconnect ADB unless a mirror or the web remote server still uses it."""
    if _keep_connection or os.environ.get("FORMULER_KEEP_CONNECTION") or _mirror_running():
        return
    run_adb("disconnect", tgt(ip))


def _find_scrcpy() -> str | None:
    found = shutil.which("scrcpy")
    if found or os.name != "nt":
        return found
    # winget adds the alias to PATH only for new shells; look in its package dir
    pkgs = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages"
    for exe in sorted(pkgs.glob("Genymobile.scrcpy*/scrcpy-*/scrcpy.exe"), reverse=True):
        return str(exe)
    return None


def cmd_mirror(ip: str, tv_audio: bool = False, fullscreen: bool = False):
    """Mirror the device screen to this computer with scrcpy.

    By default audio is forwarded to this computer. On Android 11 (Z11 Pro MAX)
    capturing audio mutes the TV, since --audio-dup needs Android 13, so
    tv_audio=True mirrors video only and keeps the sound on the TV.
    """
    global _keep_connection
    exe = _find_scrcpy()
    if not exe:
        hint = "winget install Genymobile.scrcpy" if os.name == "nt" else "https://github.com/Genymobile/scrcpy"
        output_err(f"scrcpy not found. Install it: {hint}")
        return

    args = [exe, "--serial", tgt(ip), "--window-title", "Formuler", "--max-size", "1920",
            "--video-bit-rate", "12M", "--no-power-on"]
    if tv_audio:
        args.append("--no-audio")
    if fullscreen:
        args.append("--fullscreen")

    # Use the same adb as this CLI: two adb versions kill each other's server
    env = dict(os.environ)
    if shutil.which("adb"):
        env["ADB"] = shutil.which("adb")

    log_path = TMP_DIR / "formuler-mirror.log"
    if DRY_RUN:
        print(f"[dry-run] {' '.join(args)}", file=sys.stderr)
        return
    log = open(log_path, "w", encoding="utf-8")
    # Run independently of this command so the window outlives it. On Windows use a
    # hidden console (CREATE_NO_WINDOW): with DETACHED_PROCESS the adb.exe that
    # scrcpy spawns fails to start (0xC0000142).
    if os.name == "nt":
        flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        proc = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT, env=env, creationflags=flags)
    else:
        proc = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT, env=env, start_new_session=True)

    # Started once the video decoder reports its texture; scrcpy exits on failure
    deadline = time.time() + 15
    started = False
    while time.time() < deadline and proc.poll() is None:
        time.sleep(0.5)
        if "Texture" in log_path.read_text(encoding="utf-8", errors="replace"):
            started = True
            break
    if not started:
        if proc.poll() is None:
            proc.kill()
        log.close()
        tail = log_path.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-3:]
        output_err(f"scrcpy did not start: {' | '.join(tail)}", {"log": str(log_path)})
        return

    _keep_connection = True
    audio = "on the TV" if tv_audio else "on this computer (TV is muted while mirroring)"
    output_ok(f"Mirroring started, audio {audio}. Close the window to stop.",
              {"pid": proc.pid, "tv_audio": tv_audio, "log": str(log_path)})


# ══════════════════════════════════════════════════════════════
#  CONVENIENCE COMMANDS
# ══════════════════════════════════════════════════════════════

def cmd_wake(ip: str):
    key(ip, "wakeup")
    wait(LOAD_DELAY)
    ensure_mytv(ip)
    output_ok("Device awake, MyTV running")


def cmd_power_off(ip: str):
    key(ip, "sleep")
    wait(NAV_DELAY * 2)
    run_adb("disconnect", tgt(ip))
    output_ok("CEC sleep sent, disconnected")


def cmd_history(count: int = 20):
    history_file = CACHE_DIR / "history"
    if not history_file.exists():
        output_err("No command history found.")
        return
    lines = history_file.read_text(encoding="utf-8").splitlines()
    recent = lines[-count:] if count < len(lines) else lines
    if JSON_MODE:
        output(recent)
    else:
        for i, line in enumerate(recent, 1):
            print(f"  {i:3d}  {line}")


def cmd_watch(ip: str, interval: float = 5.0, directory: str = str(TMP_DIR)):
    """Take screenshots at regular intervals until Ctrl-C."""
    info(f"Watching every {interval}s to {directory}/ (Ctrl-C to stop)")
    try:
        n = 0
        while True:
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            path = f"{directory}/formuler-watch-{ts}.png"
            screenshot(ip, path)
            n += 1
            time.sleep(interval)
    except KeyboardInterrupt:
        output_ok(f"Watch stopped after {n} screenshots")


# ══════════════════════════════════════════════════════════════
#  M3U EXPORT
# ══════════════════════════════════════════════════════════════

def cmd_export_m3u(ip: str, path: str = "channels.m3u"):
    """Export full channel cache as M3U playlist (metadata only)."""
    channels = get_full_channels(ip)
    if not channels:
        output_err("No channels in cache. Run 'refresh-all' first.")
        return

    lines = ["#EXTM3U"]
    for ch in channels:
        num = ch.get("number", "0")
        title = ch.get("title", "Unknown")
        logo = ch.get("logo", "")
        extinf = f'#EXTINF:-1 tvg-chno="{num}"'
        if logo:
            extinf += f' tvg-logo="{logo}"'
        extinf += f",{title}"
        lines.append(extinf)
        # No stream URL available — CLI uses intents, not direct streams
        lines.append(f"# Channel {num}: {title} (no stream URL available)")

    content = "\n".join(lines) + "\n"
    Path(path).write_text(content, encoding="utf-8")
    output_ok(f"Exported {len(channels)} channels to {path}", {"path": path, "count": len(channels)})


# ══════════════════════════════════════════════════════════════
#  MISC
# ══════════════════════════════════════════════════════════════

def go_to_channel_number(ip: str, number: str):
    for d in number:
        key(ip, d)
    key(ip, "ok")


def screenshot(ip: str, path: str = str(TMP_DIR / "formuler-screenshot.png")):
    remote = "/sdcard/screenshot.png"
    code, out = adb(ip, "shell", "screencap", "-p", remote)
    if code != 0:
        output_err(f"Screenshot capture failed: {out}")
        return
    code, out = run_adb("-s", tgt(ip), "pull", remote, path)
    if code != 0:
        output_err(f"Screenshot pull failed: {out}")
        return
    adb(ip, "shell", "rm", remote)
    output_ok(f"Screenshot saved to {path}", {"path": path})


def cmd_ping(ip: str):
    code, out = adb(ip, "shell", "echo", "pong", timeout=5)
    if code == 0 and "pong" in out:
        output_ok("Device reachable", {"ip": ip, "port": ADB_PORT})
    else:
        output_err(f"Device unreachable: {out}")


def cmd_status(ip: str):
    code, model = adb(ip, "shell", "getprop", "ro.product.model")
    _, version = adb(ip, "shell", "getprop", "ro.build.version.release")
    _, uptime = adb(ip, "shell", "uptime")
    _, fg = adb(ip, "shell", "dumpsys", "activity", "recents", timeout=5)

    top_app = "unknown"
    for line in fg.splitlines():
        if "Recent #0" in line or "topActivity" in line:
            m = re.search(r"com\.\S+|tv\.\S+", line)
            if m:
                top_app = m.group(0).split("/")[0]
            break

    data = {
        "model": model.strip() if code == 0 else "unknown",
        "android": version.strip(),
        "uptime": uptime.strip(),
        "foreground_app": top_app,
        "ip": ip,
    }
    if JSON_MODE:
        output(data)
    else:
        print(f"  {_C.BOLD}Model{_C.RESET}: {data['model']}")
        print(f"  {_C.BOLD}Android{_C.RESET}: {data['android']}")
        print(f"  {_C.BOLD}Uptime{_C.RESET}: {data['uptime']}")
        print(f"  {_C.BOLD}Foreground{_C.RESET}: {data['foreground_app']}")
        print(f"  {_C.BOLD}IP{_C.RESET}: {data['ip']}:{ADB_PORT}")


# ══════════════════════════════════════════════════════════════
#  HELP
# ══════════════════════════════════════════════════════════════

HELP_TEXT = f"""
{_C.BOLD}Commands:{_C.RESET}
  {_C.CYAN}NAVIGATION{_C.RESET}       up, down, left, right, ok, back, home, menu
  {_C.CYAN}PLAYBACK{_C.RESET}         play, pause, play-pause, stop, rewind, fast-forward
  {_C.CYAN}CHANNELS{_C.RESET}         channel-up, channel-down, channel <number>
  {_C.CYAN}VOLUME{_C.RESET}           volume-up, volume-down, mute

  {_C.CYAN}SECTIONS{_C.RESET}
    section <name>                  switch section (live/vod/series/radio/recordings)
    browse <section>                open section for manual browsing

  {_C.CYAN}LIVE TV{_C.RESET}
    tune <name|number>              tune by name or channel number
    last                            retune the last tuned channel
    prev                            swap to previous channel (recall)
    tune-history [count]            show recent tune history
    search <query>                  search local DB + device provider
    list [filter]                   list channels (e.g. 'list BE')
    list-all [filter]               list all enumerated channels
    channel-info <number>           show channel details
    country [code] [filter]         countries, or one country's channels (nl be us ca au nz)
    tune-id <unique_id>             tune by provider channel id
    categories                      show category counts

  {_C.CYAN}VOD (Movies){_C.RESET}
    search-vod <query>              search movies on device UI
    play-movie <query>              search & play first match
    stop-vod                        stop with confirmation (Up + OK)

  {_C.CYAN}SERIES (TV Shows){_C.RESET}
    search-series <query>           search series on device UI
    play-series <query> [S] [E]     search & play episode (default S1E1)
    episodes <query>                open episode browser + screenshot
    stop-vod                        stop with confirmation

  {_C.CYAN}BROWSING{_C.RESET}
    resume [vod|series|live]        resume last watched
    info <query>                    show details from local database
    vod-history [count] [filter]    show VOD/series watch history

  {_C.CYAN}EPG{_C.RESET}
    epg [channel]                   show EPG info overlay + screenshot
    guide                           open full EPG guide + screenshot
    search-epg <query>              search EPG program listings
    now-playing                     capture player log entries

  {_C.CYAN}FAVORITES{_C.RESET}
    star                            toggle star on current detail page
    star-vod <query>                find movie & toggle star
    star-series <query>             find series & toggle star

  {_C.CYAN}MACROS & TIMERS{_C.RESET}
    macro <name>                    run a saved macro
    macros                          list available macros
    at <HH:MM> <command>            schedule a command
    timers                          list pending timers
    cancel-timer <index>            cancel a timer
    sleep-timer <minutes>           auto-off after N minutes
    sleep-at <HH:MM>               auto-off at specific time
    sleep-cancel                    cancel sleep timer

  {_C.CYAN}ADVANCED{_C.RESET}
    repeat <key> <count>            press key N times
    cec <on|off|standby>            TV power via CEC
    record [duration] [path]        screen record (default 30s)
    mirror [--no-audio] [--fullscreen]
                                    mirror screen here via scrcpy (--no-audio: sound stays on TV)
    serve [port] [--local]          web remote for phone/tablet/laptop (default port 8765)

  {_C.CYAN}EXPORT{_C.RESET}
    export-m3u [path]               export channel lineup as M3U file

  {_C.CYAN}CONVENIENCE{_C.RESET}
    wake                            CEC wakeup + launch MyTVOnline
    power-off                       CEC sleep + disconnect
    history [count]                 show recent command history
    watch [interval] [dir]          screenshot every N seconds (Ctrl-C to stop)

  {_C.CYAN}GENERAL{_C.RESET}
    type <text>                     type text on device
    open <app>                      launch app (youtube/netflix/prime/mytv/...)
    apps                            list installed apps
    screenshot [path]               capture screen
    status                          show device info
    ping                            check device connectivity
    reboot                          reboot device
    refresh                         reload favorites/history cache
    refresh-all                     rebuild full channel database
    commands                        list all commands as JSON schema
    keys                            list all key names
    help                            show this help
    quit / exit                     disconnect

  {_C.CYAN}FLAGS{_C.RESET}
    --json                          structured JSON output
    --yes                           skip confirmation prompts
    --first                         auto-select first match
    --wait <N>                      sleep N seconds after command
    --timeout <N>                   override ADB timeout (default 10s)
    --verbose / --debug             print ADB commands to stderr
    --dry-run                       show ADB commands without executing
    -h / --help                     show usage and exit
"""


# ══════════════════════════════════════════════════════════════
#  UNIFIED DISPATCH
# ══════════════════════════════════════════════════════════════

def _parse_series_args(args: list[str]) -> tuple[str, int, int]:
    season, episode = 1, 1
    query_parts = list(args)
    if len(query_parts) >= 2 and query_parts[-1].isdigit() and query_parts[-2].isdigit():
        episode = int(query_parts.pop())
        season = int(query_parts.pop())
    elif len(query_parts) >= 1 and query_parts[-1].isdigit():
        season = int(query_parts.pop())
    return " ".join(query_parts), season, episode


def dispatch(ip: str, raw: str) -> bool:
    """Execute a single command. Returns False to quit REPL."""
    parts = raw.strip().split()
    if not parts:
        return True
    cmd = parts[0].lower()
    args = parts[1:]
    atxt = " ".join(args)

    # Quit
    if cmd in ("quit", "exit", "q"):
        return False

    # Help / meta
    elif cmd == "help":
        if JSON_MODE:
            output(COMMANDS)
        else:
            print(HELP_TEXT)
    elif cmd == "commands":
        output(COMMANDS)
    elif cmd == "keys":
        if JSON_MODE:
            output(sorted(KEYS.keys()))
        else:
            print(", ".join(sorted(KEYS.keys())))

    # Sections
    elif cmd == "section" and atxt:
        go_to_section(ip, atxt)
    elif cmd == "browse" and atxt:
        cmd_browse(ip, atxt)

    # Live TV
    elif cmd == "channel" and args:
        go_to_channel_number(ip, args[0])
    elif cmd == "tune" and atxt:
        cmd_tune(ip, atxt)
    elif cmd == "last":
        cmd_last(ip)
    elif cmd == "prev":
        cmd_prev(ip)
    elif cmd == "tune-history":
        cmd_tune_history(int(args[0]) if args and args[0].isdigit() else 10)
    elif cmd == "search" and atxt:
        cmd_search(ip, atxt)
    elif cmd == "list":
        cmd_list(ip, atxt)
    elif cmd == "list-all":
        cmd_list_all(ip, atxt)
    elif cmd == "channel-info" and args:
        cmd_channel_info(ip, args[0])
    elif cmd == "country":
        cmd_country(ip, args[0] if args else "", " ".join(args[1:]))
    elif cmd == "tune-id" and args:
        cmd_tune_id(ip, args[0])
    elif cmd == "categories":
        cmd_categories(ip)
    elif cmd == "refresh":
        get_channels(ip, force_refresh=True)
    elif cmd == "refresh-all":
        get_full_channels(ip, force_refresh=True)

    # VOD
    elif cmd == "search-vod" and atxt:
        do_search(ip, atxt, section="vod")
    elif cmd == "play-movie" and atxt:
        play_movie(ip, atxt)
    elif cmd == "stop-vod":
        stop_playback(ip)

    # Series
    elif cmd == "search-series" and atxt:
        do_search(ip, atxt, section="series")
    elif cmd == "play-series" and args:
        q, s, e = _parse_series_args(args)
        play_series(ip, q, s, e)
    elif cmd == "episodes" and atxt:
        cmd_episodes(ip, atxt)

    # Browsing
    elif cmd == "resume":
        cmd_resume(ip, args[0] if args else "vod")
    elif cmd == "info" and atxt:
        cmd_info(ip, atxt)
    elif cmd == "vod-history":
        cnt = int(args[0]) if args and args[0].isdigit() else 20
        filt = " ".join(args[1:]) if len(args) > 1 else (" ".join(args) if args and not args[0].isdigit() else "")
        cmd_vod_history(ip, cnt, filt)

    # EPG
    elif cmd == "epg":
        cmd_epg(ip, atxt)
    elif cmd == "guide":
        cmd_guide(ip)
    elif cmd == "search-epg" and atxt:
        cmd_search_epg(ip, atxt)
    elif cmd == "now-playing":
        cmd_now_playing(ip)

    # Favorites
    elif cmd == "star" and not args:
        toggle_star(ip)
    elif cmd == "star-vod" and atxt:
        star_search(ip, atxt, "vod")
    elif cmd == "star-series" and atxt:
        star_search(ip, atxt, "series")

    # Macros & Timers
    elif cmd == "macro" and atxt:
        run_macro(ip, atxt)
    elif cmd == "macros":
        cmd_macros(ip)
    elif cmd == "at" and len(args) >= 2:
        cmd_at(ip, args[0], " ".join(args[1:]))
    elif cmd == "timers":
        cmd_timers()
    elif cmd == "cancel-timer" and args and args[0].isdigit():
        cmd_cancel_timer(int(args[0]) - 1)
    elif cmd == "sleep-timer" and args:
        try:
            cmd_sleep_timer(ip, float(args[0]))
        except ValueError:
            output_err("Usage: sleep-timer <minutes>")
    elif cmd == "sleep-at" and args:
        cmd_sleep_at(ip, args[0])
    elif cmd == "sleep-cancel":
        cmd_sleep_cancel()

    # Advanced
    elif cmd == "repeat" and len(args) >= 2 and args[1].isdigit():
        cmd_repeat(ip, args[0], int(args[1]))
    elif cmd == "cec" and args:
        cmd_cec(ip, args[0])
    elif cmd == "record":
        dur = int(args[0]) if args and args[0].isdigit() else 30
        path = args[1] if len(args) > 1 else str(TMP_DIR / "formuler-recording.mp4")
        cmd_record_screen(ip, dur, path)
    elif cmd == "mirror":
        opts = {a.lower().lstrip("-") for a in args}
        cmd_mirror(ip, tv_audio=bool(opts & {"no-audio", "tv-audio", "tv"}),
                   fullscreen=bool(opts & {"fullscreen", "fs"}))
    elif cmd == "serve":
        port = next((int(a) for a in args if a.isdigit()), 8765)
        cmd_serve(ip, port=port, local_only="--local" in args)

    # Text / direct key
    elif cmd == "type" and args:
        text_input(ip, " ".join(args))
    elif cmd == "key" and args:
        key(ip, args[0])

    # Apps
    elif cmd == "open" and args:
        open_app(ip, KNOWN_APPS.get(args[0].lower(), args[0]))
    elif cmd == "apps":
        list_apps(ip)

    # Export
    elif cmd == "export-m3u":
        cmd_export_m3u(ip, args[0] if args else "channels.m3u")

    # Convenience
    elif cmd == "wake":
        cmd_wake(ip)
    elif cmd == "power-off":
        cmd_power_off(ip)
    elif cmd == "history":
        cmd_history(int(args[0]) if args and args[0].isdigit() else 20)
    elif cmd == "watch":
        interval = float(args[0]) if args and args[0].replace(".", "").isdigit() else 5.0
        directory = args[1] if len(args) > 1 else str(TMP_DIR)
        cmd_watch(ip, interval, directory)

    # Connectivity
    elif cmd == "ping":
        cmd_ping(ip)

    # Misc
    elif cmd == "screenshot":
        screenshot(ip, args[0] if args else str(TMP_DIR / "formuler-screenshot.png"))
    elif cmd == "status":
        cmd_status(ip)
    elif cmd == "reboot":
        if AUTO_YES:
            adb(ip, "shell", "reboot")
            output_ok("Rebooting...")
        else:
            try:
                if input("Reboot device? [y/N] ").strip().lower() == "y":
                    adb(ip, "shell", "reboot")
                    output_ok("Rebooting...")
            except (EOFError, KeyboardInterrupt):
                pass

    # Direct key (any known key name)
    elif cmd in KEYS:
        key(ip, cmd)

    # Macro shortcut (check if command matches a macro name)
    elif cmd in get_macros():
        run_macro(ip, cmd)

    else:
        error(f"Unknown: {cmd}. Type 'help'.")

    return True


def dispatch_chain(ip: str, raw: str) -> bool:
    """Dispatch a command string that may contain ; separated commands."""
    if ";" not in raw:
        return dispatch(ip, raw)
    for part in raw.split(";"):
        part = part.strip()
        if not part:
            continue
        if part.startswith("wait "):
            try:
                wait(float(part.split()[1]))
            except (ValueError, IndexError):
                wait(1)
        elif not dispatch(ip, part):
            return False
    return True


# ══════════════════════════════════════════════════════════════
#  WEB REMOTE
# ══════════════════════════════════════════════════════════════

# Commands the web page may run (first word of each ';' part). Long-running or
# destructive ones (serve, reboot, watch, record) are left out on purpose.
_WEB_ALLOWED = {
    "tune", "tune-id", "last", "prev", "channel", "section", "play-movie", "play-series", "resume",
    "open", "mirror", "screenshot", "status", "ping", "wake", "power-off", "sleep-timer",
    "sleep-at", "sleep-cancel", "epg", "guide", "star", "stop-vod", "cec", "wait", "type",
}

_WEB_KEYS = {"up", "down", "left", "right", "ok", "back", "home", "menu", "info", "guide",
             "volume-up", "volume-down", "mute", "channel-up", "channel-down",
             "play-pause", "stop", "rewind", "fast-forward"}


def _parse_json_stream(text: str) -> list[dict]:
    """Parse the concatenated JSON objects the CLI prints in --json mode."""
    decoder, objs, i = json.JSONDecoder(), [], 0
    text = text.strip()
    while i < len(text):
        try:
            obj, end = decoder.raw_decode(text, i)
        except json.JSONDecodeError:
            break
        objs.append(obj)
        i = end
        while i < len(text) and text[i].isspace():
            i += 1
    return objs


def _lan_ip() -> str:
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))  # UDP connect sends nothing; picks the LAN interface
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def cmd_serve(ip: str, port: int = 8765, local_only: bool = False):
    """Serve a web remote control page plus a small JSON API.

    Key presses go straight to adb (fast). Other commands run through this CLI
    in a subprocess, one at a time. Every request needs the random token that
    is printed at startup, so others on the network can't control the TV.
    """
    import secrets
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import parse_qs, urlparse

    global _keep_connection
    _keep_connection = True
    # Persist the token so a bookmark on the phone keeps working across restarts;
    # delete the file to revoke old links.
    token_file = CACHE_DIR / "web_token"
    try:
        token = token_file.read_text(encoding="utf-8").strip()
    except OSError:
        token = ""
    if len(token) < 12:
        token = secrets.token_urlsafe(12)
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        token_file.write_text(token, encoding="utf-8")
    cmd_lock = threading.Lock()
    script = str(Path(__file__).resolve())
    env = {**os.environ, "FORMULER_KEEP_CONNECTION": "1", "PYTHONIOENCODING": "utf-8"}

    def run_cli(command: str) -> dict:
        parts = [p.strip() for p in command.split(";") if p.strip()]
        bad = [p.split()[0] for p in parts if p.split()[0].lower() not in _WEB_ALLOWED]
        if not parts or bad:
            return {"ok": False, "error": f"Not allowed: {', '.join(bad) or 'empty command'}"}
        if not cmd_lock.acquire(timeout=1):
            return {"ok": False, "error": "Busy with another command", "busy": True}
        try:
            proc = subprocess.run(
                [sys.executable, script, "--json", "--first", ip, command],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=300, env=env)
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "Command timed out"}
        finally:
            cmd_lock.release()
        results = _parse_json_stream(proc.stdout)
        errors = [r.get("error") for r in results if not r.get("ok", True)]
        messages = [r.get("message") for r in results if r.get("message")]
        return {"ok": proc.returncode == 0 and not errors, "messages": messages,
                "error": "; ".join(e for e in errors if e) or None, "results": results}

    # One long-lived `adb shell` for key presses: starting adb per key costs ~1s
    key_shell: dict = {"proc": None}
    key_lock = threading.Lock()

    def press(name: str) -> dict:
        if name not in _WEB_KEYS:
            return {"ok": False, "error": f"Unknown key: {name}"}
        line = f"input keyevent {KEYS[name]}\n".encode()
        with key_lock:
            for _ in range(2):
                proc = key_shell["proc"]
                if proc is None or proc.poll() is not None:
                    proc = key_shell["proc"] = subprocess.Popen(
                        ["adb", "-s", tgt(ip), "shell"], stdin=subprocess.PIPE,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                try:
                    proc.stdin.write(line)
                    proc.stdin.flush()
                    return {"ok": True, "error": None}
                except OSError:
                    key_shell["proc"] = None  # shell died (e.g. reconnect); start a new one
        code, out = adb(ip, "shell", "input", "keyevent", str(KEYS[name]), timeout=5)
        return {"ok": code == 0, "error": None if code == 0 else out}

    def screenshot_png() -> bytes | None:
        try:
            proc = subprocess.run(["adb", "-s", tgt(ip), "exec-out", "screencap", "-p"],
                                  capture_output=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return proc.stdout if proc.returncode == 0 and proc.stdout[:4] == b"\x89PNG" else None

    def channels(country: str) -> list[dict]:
        out = []
        for c in country_channels(ip, country):
            key, rank, label = _channel_key(c.get("title", ""))
            out.append({"number": c.get("number") or "", "title": c.get("title", ""), "logo": c.get("logo") or "",
                        "id": c["unique_id"], "kind": c.get("kind", "tv"), "key": key, "rank": rank, "q": label})
        return out

    # ── Live stream to the browser ──
    # scrcpy (no window) records the box's screen + audio to a growing MKV; ffmpeg
    # follows that file and cuts it into ~1s HLS segments without re-encoding.
    # scrcpy handles the device protocol and A/V timestamps, which raw H.264 lacks.
    live = {"scrcpy": None, "ffmpeg": None, "dir": TMP_DIR / "formuler-live", "logs": []}
    live_lock = threading.Lock()
    LIVE_MAX_BYTES = 1500 * 1024 * 1024  # restart before the MKV gets huge (~70 min)

    def _kill(proc):
        if proc is not None and proc.poll() is None:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass

    def _live_running() -> bool:
        return all(live[k] is not None and live[k].poll() is None for k in ("scrcpy", "ffmpeg"))

    def _live_stop_locked():
        _kill(live["ffmpeg"])
        _kill(live["scrcpy"])
        live["ffmpeg"] = live["scrcpy"] = None
        for log in live["logs"]:  # open handles would block deleting the dir on Windows
            log.close()
        live["logs"] = []
        # scrcpy's adb child can hold its log open a moment longer; retry briefly
        for _ in range(6):
            shutil.rmtree(live["dir"], ignore_errors=True)
            if not live["dir"].exists():
                break
            time.sleep(0.5)

    def _spawn(args, log_path):
        log = open(log_path, "w", encoding="utf-8")
        live["logs"].append(log)
        if os.name == "nt":
            # hidden console: scrcpy's adb.exe can't start without one (0xC0000142)
            return subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
        return subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                start_new_session=True)

    def _log_tail(name: str) -> str:
        try:
            lines = (live["dir"] / name).read_text(encoding="utf-8", errors="replace").strip().splitlines()
        except OSError:
            return ""
        return " | ".join(lines[-2:])

    def live_start() -> dict:
        with live_lock:
            if _live_running():
                return {"ok": True, "url": "/live/live.m3u8"}
            _live_stop_locked()
            exe, ffmpeg = _find_scrcpy(), shutil.which("ffmpeg")
            if not exe:
                return {"ok": False, "error": "scrcpy not found (winget install Genymobile.scrcpy)"}
            if not ffmpeg:
                return {"ok": False, "error": "ffmpeg not found (winget install Gyan.FFmpeg)"}
            d = live["dir"]
            (d / "hls").mkdir(parents=True, exist_ok=True)
            mkv = d / "live.mkv"
            env_adb = shutil.which("adb")
            if env_adb:
                os.environ["ADB"] = env_adb  # same adb as the CLI; inherited by scrcpy
            live["scrcpy"] = _spawn(
                [exe, "--serial", tgt(ip), "--no-window", "--no-audio-playback", "--no-control",
                 "--no-power-on", "--audio-codec=aac", "--max-size=1280", "--video-bit-rate=3M",
                 # a keyframe every second, so HLS can cut ~1s segments
                 "--video-codec-options=i-frame-interval:int=1", f"--record={mkv}"],
                d / "scrcpy.log")
            deadline = time.time() + 20
            while time.time() < deadline and live["scrcpy"].poll() is None:
                if mkv.exists() and mkv.stat().st_size > 0:
                    break
                time.sleep(0.5)
            else:
                err = _log_tail("scrcpy.log") or "no video/audio from the box"
                _live_stop_locked()
                return {"ok": False, "error": f"Stream did not start: {err}"}
            live["ffmpeg"] = _spawn(
                [ffmpeg, "-hide_banner", "-loglevel", "error", "-follow", "1", "-i", f"file:{mkv}",
                 # scrcpy's MKV leaves the keyframe flag off audio blocks; without
                 # -copyinkf the stream copy drops every audio packet
                 "-copyinkf", "-c", "copy", "-f", "hls", "-hls_time", "1", "-hls_list_size", "6",
                 "-hls_flags", "delete_segments+omit_endlist+independent_segments",
                 str(d / "hls" / "live.m3u8")],
                d / "ffmpeg.log")
            deadline = time.time() + 20
            while time.time() < deadline and _live_running():
                if (d / "hls" / "live.m3u8").exists():
                    return {"ok": True, "url": "/live/live.m3u8"}
                time.sleep(0.5)
            err = _log_tail("ffmpeg.log") or _log_tail("scrcpy.log") or "no segments produced"
            _live_stop_locked()
            return {"ok": False, "error": f"Stream did not start: {err}"}

    def live_stop() -> dict:
        with live_lock:
            _live_stop_locked()
        return {"ok": True}

    def live_watchdog():
        while True:
            time.sleep(30)
            with live_lock:
                running = _live_running()
                mkv = live["dir"] / "live.mkv"
                too_big = running and mkv.exists() and mkv.stat().st_size > LIVE_MAX_BYTES
            if too_big:
                live_stop()
                live_start()

    threading.Thread(target=live_watchdog, daemon=True).start()

    def status() -> dict:
        activity = _get_current_activity(ip)
        return {"ok": bool(activity), "activity": activity,
                # the stream's own scrcpy process would otherwise show up as a mirror
                "playing": bool(_mol3_media_players(ip)),
                "mirror": _mirror_running() and not _live_running(),
                "streaming": _live_running()}

    def live_file(name: str) -> tuple[bytes, str] | None:
        """HLS playlist (segment URLs get the token appended) or a segment."""
        hls = live["dir"] / "hls"
        if name == "live.m3u8":
            try:
                text = (hls / name).read_text(encoding="utf-8")
            except OSError:
                return None
            # Players fetch segments by relative URL without our header, so carry the token
            lines = [ln if not ln or ln.startswith("#") else f"{ln}?t={token}" for ln in text.splitlines()]
            return "\n".join(lines).encode("utf-8") + b"\n", "application/vnd.apple.mpegurl"
        if re.fullmatch(r"live\d+\.ts", name):
            try:
                return (hls / name).read_bytes(), "video/mp2t"
            except OSError:
                return None
        return None

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            if VERBOSE:
                super().log_message(fmt, *args)

        def _authorized(self) -> bool:
            q = parse_qs(urlparse(self.path).query)
            return secrets.compare_digest(self.headers.get("X-Token") or q.get("t", [""])[0], token)

        def _send(self, status: int, body: bytes, ctype: str):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, data: dict, status: int = 200):
            self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")

        def do_GET(self):
            if not self._authorized():
                return self._send(403, b"Forbidden: open the link printed by 'formuler-remote serve'",
                                  "text/plain; charset=utf-8")
            path = urlparse(self.path).path
            if path == "/":
                self._send(200, WEB_REMOTE_HTML.encode("utf-8"), "text/html; charset=utf-8")
            elif path == "/api/countries":
                self._json({"ok": True, "countries": [{"code": k, "name": v[0]} for k, v in COUNTRIES.items()]})
            elif path == "/api/channels":
                country = (parse_qs(urlparse(self.path).query).get("country") or ["nl"])[0].lower()
                if country not in COUNTRIES:
                    return self._json({"ok": False, "error": f"Unknown country '{country}'"}, 400)
                self._json({"ok": True, "country": country, "channels": channels(country)})
            elif path == "/api/status":
                self._json(status())
            elif path == "/api/screenshot":
                png = screenshot_png()
                if png:
                    self._send(200, png, "image/png")
                else:
                    self._json({"ok": False, "error": "Screenshot failed"}, 502)
            elif path.startswith("/live/"):
                found = live_file(path[len("/live/"):])
                if found:
                    self._send(200, *found)
                else:
                    self._json({"ok": False, "error": "Not found"}, 404)
            else:
                self._json({"ok": False, "error": "Not found"}, 404)

        def do_POST(self):
            if not self._authorized():
                return self._json({"ok": False, "error": "Forbidden"}, 403)
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, json.JSONDecodeError):
                return self._json({"ok": False, "error": "Bad request"}, 400)
            path = urlparse(self.path).path
            if path == "/api/key":
                self._json(press(str(body.get("key", ""))))
            elif path == "/api/cmd":
                self._json(run_cli(str(body.get("cmd", ""))))
            elif path == "/api/stream":
                action = body.get("action")
                if action == "start":
                    self._json(live_start())
                elif action == "stop":
                    self._json(live_stop())
                else:
                    self._json({"ok": False, "error": "action must be start or stop"}, 400)
            else:
                self._json({"ok": False, "error": "Not found"}, 404)

    host = "127.0.0.1" if local_only else "0.0.0.0"
    try:
        server = ThreadingHTTPServer((host, port), Handler)
    except OSError as e:
        output_err(f"Cannot listen on port {port}: {e}")
        return
    shown = "127.0.0.1" if local_only else _lan_ip()
    url = f"http://{shown}:{port}/?t={token}"
    output_ok(f"Web remote running at {url} (Ctrl-C to stop)", {"url": url, "port": port})
    if not JSON_MODE and not local_only:
        print("Open this link on your phone or tablet (same Wi-Fi). Windows may ask to allow "
              "Python through the firewall: allow it for private networks.")
    sys.stdout.flush()  # the URL must show up even when output is piped to a file
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        live_stop()  # gives the TV its sound back
        if key_shell["proc"] is not None:
            key_shell["proc"].kill()
        _keep_connection = False


WEB_REMOTE_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0f1115">
<title>Formuler Remote</title>
<style>
:root {
  --bg: #0f1115; --panel: #181b22; --panel-2: #20242d; --line: #2a2f3a;
  --text: #e8eaf0; --muted: #8b93a7; --accent: #4f8cff; --accent-2: #2d6bea;
  --ok: #34c77b; --err: #ff6b6b; --radius: 14px;
}
@media (prefers-color-scheme: light) {
  :root { --bg: #f3f4f7; --panel: #ffffff; --panel-2: #eef0f4; --line: #dde1e8;
          --text: #161a22; --muted: #5d6576; }
}
* { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
body { margin: 0; background: var(--bg); color: var(--text);
       font: 15px/1.4 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
header { position: sticky; top: 0; z-index: 5; display: flex; align-items: center; gap: 10px;
         padding: 12px 16px; background: color-mix(in srgb, var(--bg) 88%, transparent);
         backdrop-filter: blur(8px); border-bottom: 1px solid var(--line); }
header h1 { font-size: 17px; margin: 0; flex: 1; }
.pill { font-size: 12px; padding: 4px 10px; border-radius: 99px; background: var(--panel-2); color: var(--muted); }
.pill.on { color: var(--ok); }
main { max-width: 1100px; margin: 0 auto; padding: 16px; display: grid; gap: 16px; grid-template-columns: minmax(0, 1fr); }
@media (min-width: 900px) { main { grid-template-columns: 380px minmax(0, 1fr); align-items: start; } }
section { background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius); padding: 14px; }
section h2 { font-size: 13px; text-transform: uppercase; letter-spacing: .06em; color: var(--muted); margin: 0 0 12px; }
button { font: inherit; color: var(--text); background: var(--panel-2); border: 1px solid var(--line);
         border-radius: 12px; padding: 12px; cursor: pointer; touch-action: manipulation; user-select: none; }
button:active { transform: scale(.96); background: var(--line); }
button.primary { background: var(--accent); border-color: var(--accent-2); color: #fff; }
button:disabled { opacity: .5; }
.row { display: flex; gap: 8px; }
.row > * { flex: 1; }
.dpad { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; max-width: 300px; margin: 0 auto 12px; }
.dpad button { aspect-ratio: 1; font-size: 22px; border-radius: 50%; }
.dpad .ok { background: var(--accent); border-color: var(--accent-2); color: #fff; font-size: 16px; font-weight: 600; }
.rockers { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 8px; margin-top: 8px; }
.rocker { display: grid; gap: 6px; }
.rocker span { text-align: center; font-size: 12px; color: var(--muted); }
.stack { display: grid; gap: 16px; min-width: 0; }
.stack > section { min-width: 0; }  /* let the chip rows scroll instead of widening the page */
.preview { position: relative; background: #000; border-radius: 10px; overflow: hidden; aspect-ratio: 16/9; }
.preview img, .preview video { width: 100%; height: 100%; object-fit: contain; display: block; background: #000; }
.preview .empty { position: absolute; inset: 0; display: grid; place-items: center; color: var(--muted); font-size: 13px; }
[hidden] { display: none !important; }
label.toggle { display: flex; align-items: center; gap: 8px; color: var(--muted); font-size: 13px; }
input, select { font: inherit; color: var(--text); background: var(--panel-2); border: 1px solid var(--line);
                border-radius: 10px; padding: 10px 12px; width: 100%; min-width: 0; }
.channels { display: grid; grid-template-columns: repeat(auto-fill, minmax(96px, 1fr)); gap: 8px;
            max-height: 360px; overflow-y: auto; margin-top: 10px; padding-right: 2px; }
.chips { display: flex; gap: 6px; overflow-x: auto; margin-bottom: 8px; scrollbar-width: none; }
.chips button { flex: none; padding: 6px 12px; border-radius: 99px; font-size: 13px; }
.chips button[aria-pressed="true"] { background: var(--accent); border-color: var(--accent-2); color: #fff; }
.chips button[hidden] { display: none; }
.ch { display: grid; gap: 4px; justify-items: center; padding: 8px 6px; font-size: 12px; }
.ch img { width: 56px; height: 34px; object-fit: contain; }
.ch .num { color: var(--muted); font-size: 11px; }
.ch .name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 100%; }
.form { display: grid; grid-template-columns: 1fr 64px 64px; gap: 8px; margin-top: 8px; }
.form .full { grid-column: 1 / -1; }
#toast { position: fixed; left: 50%; bottom: 16px; transform: translateX(-50%); max-width: calc(100% - 32px);
         background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 10px 14px;
         box-shadow: 0 8px 30px rgba(0,0,0,.35); font-size: 14px; opacity: 0; transition: opacity .2s; pointer-events: none; }
#toast.show { opacity: 1; }
#toast.err { border-color: var(--err); color: var(--err); }
.hint { color: var(--muted); font-size: 12px; margin: 8px 0 0; }
</style>
</head>
<body>
<header>
  <h1>Formuler Remote</h1>
  <span id="state" class="pill">…</span>
</header>
<main>
  <div class="stack">
    <section aria-label="Remote">
      <h2>Remote</h2>
      <div class="row" style="margin-bottom:12px">
        <button data-cmd="wake">Power on</button>
        <button data-cmd="power-off">Power off</button>
      </div>
      <div class="dpad">
        <span></span><button data-key="up" aria-label="Up">▲</button><span></span>
        <button data-key="left" aria-label="Left">◀</button>
        <button data-key="ok" class="ok">OK</button>
        <button data-key="right" aria-label="Right">▶</button>
        <span></span><button data-key="down" aria-label="Down">▼</button><span></span>
      </div>
      <div class="row">
        <button data-key="back">Back</button>
        <button data-key="home">Home</button>
        <button data-key="menu">Menu</button>
      </div>
      <div class="rockers">
        <div class="rocker"><button data-key="volume-up">＋</button><span>Volume</span><button data-key="volume-down">－</button></div>
        <div class="rocker"><button data-key="mute">Mute</button><button data-key="play-pause">⏯</button><button data-key="info">Info</button></div>
        <div class="rocker"><button data-key="channel-up">＋</button><span>Channel</span><button data-key="channel-down">－</button></div>
      </div>
      <div class="row" style="margin-top:8px">
        <button data-cmd="prev">Previous ch.</button>
        <button data-cmd="guide">Guide</button>
      </div>
    </section>

    <section aria-label="Apps">
      <h2>Apps</h2>
      <div class="row">
        <button data-cmd="open mytv">MyTVOnline</button>
        <button data-cmd="open youtube">YouTube</button>
        <button data-cmd="open prime">Prime Video</button>
      </div>
    </section>
  </div>

  <div class="stack">
    <section aria-label="Watch">
      <h2>Watch here</h2>
      <div class="preview"><video id="live" playsinline controls hidden></video>
        <div id="live-empty" class="empty">Stream the TV picture and sound to this device</div></div>
      <div class="row" style="margin-top:10px">
        <button id="watch" class="primary">Watch here</button>
        <button id="unwatch" disabled>Stop</button>
      </div>
      <p class="hint">Runs a few seconds behind the TV. On this box (Android 11) the TV is muted while streaming; Stop gives the sound back.</p>
    </section>

    <section aria-label="Screen">
      <h2>Screen</h2>
      <div class="preview"><img id="shot" alt="TV screen" hidden><div id="shot-empty" class="empty">No screenshot yet</div></div>
      <div class="row" style="margin-top:10px; align-items:center">
        <button id="refresh">Refresh</button>
        <label class="toggle"><input id="auto" type="checkbox" style="width:auto"> Auto (5 s)</label>
      </div>
      <div class="row" style="margin-top:10px">
        <button data-cmd="mirror">Mirror (sound on laptop)</button>
        <button data-cmd="mirror --no-audio">Mirror (sound on TV)</button>
      </div>
      <p class="hint">Mirroring opens a window on the computer running the server.</p>
    </section>

    <section aria-label="Channels">
      <h2>Channels</h2>
      <div id="countries" class="chips" role="tablist" aria-label="Country"></div>
      <div id="kinds" class="chips" aria-label="Type">
        <button data-kind="" aria-pressed="true">All</button><button data-kind="tv" aria-pressed="false">TV</button>
        <button data-kind="24/7" aria-pressed="false">24/7</button><button data-kind="events" aria-pressed="false">Events</button>
        <button data-kind="radio" aria-pressed="false">Radio</button>
      </div>
      <input id="filter" type="search" placeholder="Filter channels (e.g. NPO, sport, 42)" autocomplete="off">
      <div id="channels" class="channels"></div>
      <div class="row" style="margin-top:8px; align-items:center">
        <p id="ch-hint" class="hint" style="margin:0"></p>
        <label class="toggle" style="flex:none"><input id="variants" type="checkbox" style="width:auto"> All variants</label>
      </div>
      <p class="hint">Variants of one channel (4K, FHD, HD, LQ…) show as one tile that tunes the best quality.</p>
    </section>

    <section aria-label="Play">
      <h2>Play a series or movie</h2>
      <div class="form">
        <select id="kind" class="full"><option value="series">Series</option><option value="movie">Movie</option></select>
        <input id="title" placeholder="Title, e.g. below deck adventure">
        <input id="season" type="number" min="1" value="1" aria-label="Season">
        <input id="episode" type="number" min="1" value="1" aria-label="Episode">
        <button id="play" class="primary full">Play</button>
      </div>
      <p class="hint">Searching and starting can take 1–2 minutes. Playback is checked before it reports success.</p>
    </section>
  </div>
</main>
<div id="toast" role="status" aria-live="polite"></div>
<script>
const token = new URLSearchParams(location.search).get("t") || "";
const $ = (s) => document.querySelector(s);
let toastTimer;
function toast(msg, err = false) {
  const t = $("#toast");
  t.textContent = msg; t.className = "show" + (err ? " err" : "");
  clearTimeout(toastTimer); toastTimer = setTimeout(() => t.className = "", err ? 6000 : 3000);
}
async function api(path, body) {
  const opts = { headers: { "X-Token": token } };
  if (body) { opts.method = "POST"; opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
  const r = await fetch(path, opts);
  return r.headers.get("Content-Type")?.startsWith("application/json") ? r.json() : r;
}
async function key(name) {
  if (navigator.vibrate) navigator.vibrate(10);
  const r = await api("/api/key", { key: name });
  if (!r.ok) toast(r.error || "Key failed", true);
}
async function run(cmd, btn) {
  if (btn) btn.disabled = true;
  toast("Working: " + cmd);
  try {
    const r = await api("/api/cmd", { cmd });
    if (r.ok) toast((r.messages || []).slice(-1)[0] || "Done");
    else toast(r.error || "Failed", true);
    setTimeout(refreshShot, 1500);
    return r;
  } catch (e) { toast("Server unreachable", true); }
  finally { if (btn) btn.disabled = false; }
}
document.addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  if (b.dataset.key) key(b.dataset.key);
  else if (b.dataset.cmd) run(b.dataset.cmd, b);
});
document.addEventListener("keydown", (e) => {
  if (e.target.matches("input, select")) return;
  const map = { ArrowUp: "up", ArrowDown: "down", ArrowLeft: "left", ArrowRight: "right",
                Enter: "ok", Backspace: "back", Escape: "back", h: "home", m: "mute",
                "+": "volume-up", "-": "volume-down", PageUp: "channel-up", PageDown: "channel-down" };
  if (map[e.key]) { e.preventDefault(); key(map[e.key]); }
});
async function refreshShot() {
  const r = await fetch("/api/screenshot", { headers: { "X-Token": token } });
  if (!r.ok) return;
  const url = URL.createObjectURL(await r.blob());
  const img = $("#shot"); const old = img.src;
  img.src = url; img.hidden = false; $("#shot-empty").hidden = true;
  if (old.startsWith("blob:")) URL.revokeObjectURL(old);
}
$("#refresh").onclick = refreshShot;
let autoTimer;
$("#auto").onchange = (e) => {
  clearInterval(autoTimer);
  if (e.target.checked) { refreshShot(); autoTimer = setInterval(refreshShot, 5000); }
};
let allChannels = [], country = "nl", kind = "";
try { country = localStorage.getItem("country") || "nl"; } catch (e) {}
const QUALITY = /\s*\b(ULTRA\s+)?(4K|UHD|FHD|HD|HEVC|H265|SD|LQ|HQ|50FPS)\b|[ᴬ-ᵡ◉²]+/gi;
function renderChannels() {
  const q = $("#filter").value.trim().toLowerCase();
  const all = $("#variants").checked;
  const match = c => c.title.toLowerCase().includes(q) || String(c.number) === q;
  // Group variants of one channel (NPO1 4K / NPO 1 FHD / NPO1 LQ) unless "all variants" is on
  let groups = [];
  if (all) groups = allChannels.map(c => [c]);
  else {
    const byKey = new Map();
    for (const c of allChannels) {
      const k = c.kind + "|" + c.key;
      if (!byKey.has(k)) byKey.set(k, []);
      byKey.get(k).push(c);
    }
    groups = [...byKey.values()].map(g => g.sort((a, b) => b.rank - a.rank || a.number - b.number));
    groups.sort((a, b) => Math.min(...a.map(c => +c.number || 1e9)) - Math.min(...b.map(c => +c.number || 1e9)));
  }
  const list = groups.filter(g => (!kind || g[0].kind === kind) && (!q || g.some(match)));
  const box = $("#channels"); box.innerHTML = "";
  for (const g of list.slice(0, 300)) {
    const c = g[0], b = document.createElement("button");
    b.className = "ch"; b.dataset.cmd = "tune-id " + c.id;
    b.title = g.length > 1 ? `${c.title} (best of ${g.length}: ${g.map(v => v.q || "SD").join(", ")})` : c.title;
    let name = c.title.replace(/^[A-Z.]{2,4}\s*\|\s*/, "");
    if (!all) name = name.replace(QUALITY, "").trim() || name;
    const logo = g.find(v => v.logo)?.logo;
    if (logo) { const i = document.createElement("img"); i.src = logo; i.alt = ""; i.loading = "lazy";
                i.onerror = () => i.remove(); b.append(i); }
    const n = document.createElement("span"); n.className = "num";
    n.textContent = c.number + (!all && c.q ? " · " + c.q : "") + (g.length > 1 ? ` · +${g.length - 1}` : "");
    const t = document.createElement("span"); t.className = "name"; t.textContent = name;
    b.append(n, t); box.append(b);
  }
  const total = groups.filter(g => !kind || g[0].kind === kind).length;
  $("#ch-hint").textContent = allChannels.length
    ? `${list.length} of ${total} channels` + (all ? "" : ` (${allChannels.filter(c => !kind || c.kind === kind).length} incl. variants)`)
      + (list.length > 300 ? ", first 300 shown, filter to narrow" : "")
    : "No channels found for this country.";
  for (const k of $("#kinds").children) k.hidden = !!k.dataset.kind && !allChannels.some(c => c.kind === k.dataset.kind);
}
$("#filter").oninput = renderChannels;
$("#variants").onchange = renderChannels;
$("#kinds").onclick = (e) => {
  const b = e.target.closest("button"); if (!b) return;
  kind = b.dataset.kind;
  for (const k of $("#kinds").children) k.setAttribute("aria-pressed", String(k === b));
  renderChannels();
};
async function loadChannels() {
  for (const b of $("#countries").children) b.setAttribute("aria-pressed", String(b.dataset.country === country));
  $("#channels").innerHTML = ""; $("#ch-hint").textContent = "Loading channels…";
  const r = await api("/api/channels?country=" + encodeURIComponent(country));
  if (!r.ok) { $("#ch-hint").textContent = r.error || "Could not load channels"; return; }
  allChannels = r.channels || [];
  if (kind && !allChannels.some(c => c.kind === kind)) $("#kinds").children[0].click();
  else renderChannels();
}
async function loadCountries() {
  const r = await api("/api/countries");
  const list = r.countries || [];
  if (!list.some(c => c.code === country)) country = list[0]?.code || "nl";
  $("#countries").replaceChildren(...list.map(c => {
    const b = document.createElement("button");
    b.dataset.country = c.code; b.textContent = c.name; return b;
  }));
  await loadChannels();
}
$("#countries").onclick = (e) => {
  const b = e.target.closest("button"); if (!b || b.dataset.country === country) return;
  country = b.dataset.country;
  try { localStorage.setItem("country", country); } catch (err) {}
  loadChannels();
};
$("#play").onclick = () => {
  const title = $("#title").value.trim();
  if (!title) return toast("Enter a title", true);
  const cmd = $("#kind").value === "series"
    ? `play-series ${title} ${$("#season").value || 1} ${$("#episode").value || 1}`
    : `play-movie ${title}`;
  run(cmd, $("#play"));
};
$("#kind").onchange = () => {
  const movie = $("#kind").value === "movie";
  $("#season").hidden = $("#episode").hidden = movie;
};
let hls = null;
function loadScript(src) {
  return new Promise((ok, fail) => { const s = document.createElement("script");
    s.src = src; s.onload = ok; s.onerror = fail; document.head.append(s); });
}
async function attachStream(url) {
  const v = $("#live");
  v.hidden = false; $("#live-empty").hidden = true;
  // Prefer hls.js (MSE): Chrome's experimental native HLS stalls on this live stream.
  // Fall back to native HLS for browsers without MSE (older iPhones).
  if (!window.Hls) await loadScript("https://cdn.jsdelivr.net/npm/hls.js@1.5.17/dist/hls.min.js").catch(() => {});
  if (window.Hls && Hls.isSupported()) {
    hls = new Hls({ liveSyncDurationCount: 2, liveMaxLatencyDurationCount: 6, maxLiveSyncPlaybackRate: 1.2 });
    hls.loadSource(url); hls.attachMedia(v);
  } else if (v.canPlayType("application/vnd.apple.mpegurl")) {
    v.src = url;
  } else {
    throw new Error("This browser can't play the stream");
  }
  await v.play().catch(() => {});  // started from a click, so sound is allowed
}
function detachStream() {
  const v = $("#live");
  if (hls) { hls.destroy(); hls = null; }
  v.pause(); v.removeAttribute("src"); v.load();
  v.hidden = true; $("#live-empty").hidden = false;
}
$("#watch").onclick = async () => {
  const b = $("#watch"); b.disabled = true;
  toast("Starting stream… (about 10 seconds)");
  try {
    const r = await api("/api/stream", { action: "start" });
    if (!r.ok) { toast(r.error || "Stream failed", true); b.disabled = false; return; }
    await attachStream(r.url + "?t=" + encodeURIComponent(token));
    $("#unwatch").disabled = false;
    toast("Streaming. The TV is muted until you press Stop.");
  } catch (e) { toast(e.message || "Stream failed", true); b.disabled = false; }
};
$("#unwatch").onclick = async () => {
  detachStream();
  await api("/api/stream", { action: "stop" });
  $("#watch").disabled = false; $("#unwatch").disabled = true;
  toast("Stream stopped, sound is back on the TV");
};
async function refreshState() {
  try {
    const s = await api("/api/status");
    const el = $("#state");
    const label = s.activity === "LiveActivity" ? "Live TV" : (s.activity || "Offline").replace(/Activity$/, "");
    el.textContent = (s.playing ? "▶ " : "") + label + (s.mirror ? " · mirroring" : "") + (s.streaming ? " · streaming" : "");
    if (s.streaming) $("#unwatch").disabled = false;  // also when started from another device
    el.className = "pill" + (s.playing ? " on" : "");
  } catch (e) { $("#state").textContent = "Offline"; }
}
loadCountries(); refreshState(); setInterval(refreshState, 10000);
</script>
</body>
</html>
"""


# ══════════════════════════════════════════════════════════════
#  TAB COMPLETION
# ══════════════════════════════════════════════════════════════

def _make_completer(ip: str):
    commands = [
        "tune", "search", "list", "list-all", "channel-info", "country", "tune-id", "categories",
        "section", "browse", "channel",
        "search-vod", "play-movie", "stop-vod",
        "search-series", "play-series", "episodes",
        "resume", "info", "vod-history",
        "epg", "guide", "search-epg", "now-playing",
        "star", "star-vod", "star-series",
        "macro", "macros", "at", "timers", "cancel-timer",
        "repeat", "cec", "record",
        "type", "key", "open", "apps",
        "last", "prev", "tune-history",
        "sleep-timer", "sleep-at", "sleep-cancel",
        "export-m3u",
        "wake", "power-off", "history", "watch",
        "screenshot", "status", "ping", "reboot", "refresh", "refresh-all",
        "keys", "help", "quit", "exit",
    ]
    commands += list(KEYS.keys())
    commands += list(get_macros().keys())
    all_commands = sorted(set(commands))

    section_names = sorted(set(MOL3_SECTIONS.keys()))
    app_names = sorted(KNOWN_APPS.keys())

    # Load channel names for tune completion
    channel_names: list[str] = []
    if CHANNELS_CACHE.exists():
        try:
            with open(CHANNELS_CACHE, encoding="utf-8") as f:
                channel_names = list(set(c["title"] for c in json.load(f)))
        except (json.JSONDecodeError, KeyError):
            pass

    def completer(text: str, state: int):
        line = readline.get_line_buffer().lstrip()
        parts = line.split()
        text_lower = text.lower()

        if len(parts) <= 1 and not line.endswith(" "):
            options = [c for c in all_commands if c.startswith(text_lower)]
        elif parts[0] in ("tune", "search"):
            options = [n for n in channel_names if n.lower().startswith(text_lower)]
        elif parts[0] == "open":
            options = [a for a in app_names if a.startswith(text_lower)]
        elif parts[0] in ("section", "browse"):
            options = [s for s in section_names if s.startswith(text_lower)]
        elif parts[0] == "macro":
            options = [m for m in get_macros() if m.startswith(text_lower)]
        elif parts[0] == "cec":
            options = [a for a in ("on", "off", "standby") if a.startswith(text_lower)]
        elif parts[0] == "resume":
            options = [t for t in ("vod", "series", "live") if t.startswith(text_lower)]
        elif parts[0] in ("key", "repeat"):
            options = [k for k in KEYS if k.startswith(text_lower)]
        else:
            options = []

        return options[state] if state < len(options) else None

    return completer


# ══════════════════════════════════════════════════════════════
#  INTERACTIVE REPL
# ══════════════════════════════════════════════════════════════

def interactive(ip: str):
    channels = get_channels(ip)
    if channels:
        print(f"Channel database: {_C.BOLD}{len(channels)}{_C.RESET} entries")

    history_file = CACHE_DIR / "history"
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if readline:
        # Setup tab completion
        readline.set_completer(_make_completer(ip))
        readline.parse_and_bind("tab: complete")
        readline.set_completer_delims(" ")

        # Setup history file
        try:
            readline.read_history_file(history_file)
        except (FileNotFoundError, OSError):
            pass

    print(HELP_TEXT)

    prompt = f"{_C.BLUE}remote>{_C.RESET} "
    while True:
        try:
            raw = input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not raw:
            continue
        if not dispatch_chain(ip, raw):
            break

    if readline:
        try:
            readline.write_history_file(history_file)
        except OSError:
            pass

    # Cancel pending timers
    for t in _timers:
        t["timer"].cancel()

    _disconnect(ip)
    info("Disconnected.")


# ══════════════════════════════════════════════════════════════
#  SHELL COMPLETIONS
# ══════════════════════════════════════════════════════════════

def _generate_completions(shell: str):
    all_words = sorted(set(list(COMMANDS.keys()) + list(KEYS.keys())))
    words_str = " ".join(all_words)
    if shell == "bash":
        print(f"""_formuler_remote_completions() {{
  local cur="${{COMP_WORDS[COMP_CWORD]}}"
  COMPREPLY=($(compgen -W "{words_str}" -- "$cur"))
}}
complete -F _formuler_remote_completions formuler-remote
complete -F _formuler_remote_completions formuler-remote.py""")
    elif shell == "zsh":
        print(f"""#compdef formuler-remote formuler-remote.py
_formuler_remote() {{
  local -a commands=({words_str})
  _describe 'command' commands
}}
compdef _formuler_remote formuler-remote formuler-remote.py""")
    elif shell == "fish":
        for w in all_words:
            desc = COMMANDS.get(w, {}).get("desc", "key")
            print(f"complete -c formuler-remote -f -a '{w}' -d '{desc}'")
    else:
        print(f"Unknown shell: {shell}. Use: bash, zsh, fish", file=sys.stderr)
        sys.exit(1)


# ══════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════

def main():
    global JSON_MODE, AUTO_YES, AUTO_FIRST, VERBOSE, ADB_TIMEOUT

    if not shutil.which("adb"):
        if os.name == "nt":
            print("Error: adb not found. Install with: winget install Google.PlatformTools")
        else:
            print("Error: adb not found. Install with: sudo pacman -S android-tools")
        sys.exit(1)

    # Parse flags (including value-bearing ones)
    simple_flags = {"--json", "--yes", "--first", "--verbose", "--debug", "--dry-run"}
    argv = []
    post_wait = 0.0
    i = 1
    while i < len(sys.argv):
        arg = sys.argv[i]
        if arg in simple_flags:
            if arg == "--json":
                JSON_MODE = True
            elif arg == "--yes":
                AUTO_YES = True
            elif arg == "--first":
                AUTO_FIRST = True
            elif arg in ("--verbose", "--debug"):
                VERBOSE = True
            elif arg == "--dry-run":
                DRY_RUN = True
                VERBOSE = True  # dry-run implies verbose
        elif arg == "--wait" and i + 1 < len(sys.argv):
            i += 1
            try:
                post_wait = float(sys.argv[i])
            except ValueError:
                print("Error: --wait requires a numeric argument", file=sys.stderr)
                sys.exit(1)
        elif arg == "--timeout" and i + 1 < len(sys.argv):
            i += 1
            try:
                ADB_TIMEOUT = int(sys.argv[i])
            except ValueError:
                print("Error: --timeout requires an integer argument", file=sys.stderr)
                sys.exit(1)
        elif arg in ("--help", "-h"):
            print(__doc__)
            print(HELP_TEXT)
            sys.exit(0)
        elif arg in ("--version", "-V"):
            print(f"formuler-remote {__version__}")
            sys.exit(0)
        elif arg == "--completions" and i + 1 < len(sys.argv):
            i += 1
            _generate_completions(sys.argv[i])
            sys.exit(0)
        else:
            argv.append(arg)
        i += 1

    ip = DEFAULT_IP
    off = 0
    # Accept IP address or hostname as first positional arg
    if argv and re.match(r"^[\w.\-]+$", argv[0]) and not argv[0] in KEYS and "." in argv[0]:
        ip = argv[0]
        off = 1

    if not ip:
        if JSON_MODE:
            _json_out({"ok": False, "error": "No device IP/hostname specified",
                        "hint": "Set FORMULER_IP env var, .env file, config file, or pass as first argument"})
        else:
            print("Error: No device IP/hostname specified.")
            print("Provide it via: CLI argument, FORMULER_IP env var, .env file, or config file.")
            print(f"Config: {CONFIG_FILE}")
        sys.exit(1)

    # Acquire lock to prevent concurrent instances
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _lock_file = open(CACHE_DIR / ".lock", "w", encoding="utf-8")
    try:
        import fcntl
        # Commands run by the web remote are children of the server, which holds the lock
        if not os.environ.get("FORMULER_KEEP_CONNECTION"):
            fcntl.flock(_lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except ImportError:
        pass  # fcntl not available on Windows — skip locking
    except OSError:
        msg = "Another formuler-remote instance is running"
        if JSON_MODE:
            _json_out({"ok": False, "error": msg})
        else:
            print(f"Error: {msg}")
        sys.exit(1)

    if not connect(ip, quiet=JSON_MODE):
        sys.exit(1)

    remaining = argv[off:]
    if not remaining:
        # Batch/pipe mode: read commands from stdin when not a TTY
        if not sys.stdin.isatty():
            for line in sys.stdin:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                dispatch_chain(ip, line)
                if post_wait > 0:
                    time.sleep(post_wait)
            _disconnect(ip)
            sys.exit(_exit_code)
        else:
            interactive(ip)
            return

    cmd_str = " ".join(remaining)
    dispatch_chain(ip, cmd_str)
    if post_wait > 0:
        time.sleep(post_wait)
    _disconnect(ip)
    sys.exit(_exit_code)


if __name__ == "__main__":
    main()
