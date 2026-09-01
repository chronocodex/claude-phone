import concurrent.futures
import json
import logging
import os
import queue
import socket
import socketserver
import ssl
import struct
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import anthropic
import numpy as np
import soxr
import webrtcvad
import yaml
from anthropic import beta_tool
from dotenv import load_dotenv
from faster_whisper import WhisperModel
from piper import PiperVoice

load_dotenv()  # loads .env if present; harmless no-op if not (e.g. under
                # systemd with EnvironmentFile= already set)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("claude-phone.log")],
)
logging.getLogger("httpx").setLevel(logging.WARNING)

# ============================================================
# Config loading — everything here is OPTIONAL. With no
# config.yaml at all, Tier 1 (core conversation) still works.
# See config.example.yaml and README.md for the tier breakdown.
# ============================================================


def _load_config():
    path = Path("config.yaml")
    if not path.exists():
        logging.warning(
            "config.yaml not found - running Tier 1 only (core conversation). "
            "Copy config.example.yaml to config.yaml to enable monitoring, "
            "alerting, and optional tools."
        )
        return {}
    with path.open() as f:
        return yaml.safe_load(f) or {}


CONFIG = _load_config()


def cfg(*keys, default=None):
    """Safe nested config getter: cfg("ami", "host", default="")"""
    node = CONFIG
    for k in keys:
        if not isinstance(node, dict) or k not in node:
            return default
        node = node[k]
    return node


# ---------- core config ----------
SR_PHONE, SR_STT = 8000, 16000
FRAME_BYTES = 320
SILENCE_FRAMES_TO_END = cfg("silence_frames_to_end", default=60)
MIN_SPEECH_FRAMES = 10
CHECK_INTERVAL = cfg("check_interval_seconds", default=120)
GREETING = "Hello this is Claude. What can I do for you?"

CLAUDE_MODEL = cfg("model", default="claude-opus-5")
CLAUDE_EFFORT = cfg("effort", default="low")

# Deliberately NEUTRAL - these fire on any slow turn, not just searches.
FILLERS = [
    "One moment.",
    "Still working on it.",
    "Almost there.",
]
FILLER_FIRST = cfg("filler_first_seconds", default=3.0)
FILLER_REPEAT = cfg("filler_repeat_seconds", default=7.0)

# Deterministic message for genuine Anthropic-side (5xx) failures, so a
# caller never mistakes "Anthropic is overloaded" for "your setup is broken".
ANTHROPIC_DOWN_MESSAGE = (
    "That's an issue on Anthropic's servers right now, not your setup. "
    "Try again in a few minutes."
)

TYPE_HANGUP, TYPE_UUID, TYPE_AUDIO, TYPE_ERROR = 0x00, 0x01, 0x10, 0xFF

# ---------- Tier 3: alerting ----------
ALERT_UUID = os.environ.get("ALERT_UUID", "").replace("-", "").lower()
AMI_HOST = cfg("ami", "host", default="")
AMI_PORT = cfg("ami", "port", default=5038)
AMI_USER = cfg("ami", "user", default="claudephone")
AMI_SECRET = os.environ.get("AMI_SECRET", "")
ALERT_CHANNEL = cfg("ami", "alert_channel", default="PJSIP/100")
ALERT_CONTEXT = cfg("ami", "alert_context", default="claude-phone-alert")
ALERT_COOLDOWN = cfg("ami", "cooldown_seconds", default=900)
ALERTING_ENABLED = bool(AMI_HOST)

# ---------- Tier 4: Discord notifications ----------
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "")

_alert_lock = threading.Lock()
_pending_alert = None
_last_alert_time = 0.0

SYSTEM = (
    "You are a friendly voice assistant on a phone call. "
    "Your replies are spoken aloud, so keep them SHORT: one sentence when "
    "possible, two at most, and under 30 words total. Speak plainly and "
    "conversationally. Never use markdown, lists, bullet points, headers, "
    "emoji, or any symbol that doesn't read aloud naturally. "
    "Write scores, times and ranges as words - say 'twenty seven to seven', "
    "never '27-7' - because hyphens and slashes are read aloud literally. "
    "If the caller wants more detail, they will ask - do not pre-empt them. "
    "You have tools available for checking server/network status, recent "
    "outages, the time, and a household list, plus web search and any "
    "other tools you're given - use them rather than guessing. "
    "Only search the web when the answer genuinely depends on current "
    "information you cannot know - recent events, prices, live status, "
    "sports results, weather. For anything else, answer directly. "
    "Searching makes the caller wait several seconds, so do not do it reflexively. "
    "The phone system already tells the caller you are looking something up, "
    "so do not start your reply with 'let me check' or 'one moment' - just "
    "give them the answer."
)

ALERT_SYSTEM = SYSTEM + (
    " IMPORTANT CONTEXT: This is an OUTBOUND alert call that YOU placed. "
    "A background monitor detected a problem and told the phone system to "
    "ring the user. You do have the ability to call the user automatically "
    "when something goes down - never say you cannot call them."
)

# ---------- Tier 2: monitoring ----------
HOSTS = {name: tuple(v) for name, v in cfg("hosts", default={}).items()}

WAN_PROBE = (cfg("wan_probe", "host", default="1.1.1.1"),
             cfg("wan_probe", "port", default=53))
DNS_PROBE = cfg("dns_probe", default="api.anthropic.com")

LIST_FILE = Path("household-list.txt")
EVENTS_FILE = Path("homelab-events.log")

# ---------- optional tool: Pi-hole stats ----------
PIHOLE_HOST = cfg("pihole", "host", default="")
PIHOLE_APP_PASSWORD = os.environ.get("PIHOLE_APP_PASSWORD", "")
PIHOLE_ENABLED = bool(PIHOLE_HOST and PIHOLE_APP_PASSWORD)

_pihole_sid = None
_pihole_sid_expiry = 0.0

# ---------- optional tool: disk space (SSH, forced-command restricted key) ----------
DISKCHECK_KEY = cfg("disk_check", "ssh_key_path", default="")
DISKCHECK_KNOWN_HOSTS = cfg("disk_check", "known_hosts_path", default="")
DISK_HOSTS = {
    name: {
        "host": info.get("hostname", ""),
        "ssh_user": info.get("ssh_user", ""),
        "mounts": info.get("mounts", ["/"]),
    }
    for name, info in cfg("disk_check", "hosts", default={}).items()
}
DISK_CHECK_ENABLED = bool(DISKCHECK_KEY and DISK_HOSTS)

# ---------- optional tool: UniFi AP/switch monitoring ----------
UNIFI_HOST = cfg("unifi", "host", default="")
UNIFI_SITE_ID = cfg("unifi", "site_id", default="")
UNIFI_API_KEY = os.environ.get("UNIFI_API_KEY", "")
UNIFI_ENABLED = bool(UNIFI_HOST and UNIFI_SITE_ID and UNIFI_API_KEY)

WEB_SEARCH = {
    "type": "web_search_20260209",
    "name": "web_search",
    "max_uses": 3,
}


def _discord(text):
    """Fire-and-forget Discord post. Never blocks the caller.

    Runs on its own thread so a slow or dead webhook can't add latency to a
    phone turn - the list tool sits in the conversational hot path.
    """
    if not DISCORD_WEBHOOK:
        return

    def _post():
        try:
            req = urllib.request.Request(
                DISCORD_WEBHOOK,
                data=json.dumps({"content": text}).encode(),
                headers={
                    "Content-Type": "application/json",
                    # Discord's Cloudflare rejects the default Python-urllib UA
                    "User-Agent": "ClaudePhone/1.0 (github.com/chronocodex/claude-phone)",
                },
            )
            resp = urllib.request.urlopen(req, timeout=5)
            logging.info("  discord ok (%s)", resp.status)
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")[:300]
            logging.warning("  discord HTTP %s %s -> %s", e.code, e.reason, body)
        except Exception as e:
            logging.warning("  discord post failed: %r", e)

    threading.Thread(target=_post, daemon=True).start()


def _tcp_check(hostport, timeout=1.0):
    try:
        with socket.create_connection(hostport, timeout=timeout):
            return True
    except OSError:
        return False


def _dns_check(timeout=2.0):
    """Resolve a name through the system resolver.

    Run in a thread because gethostbyname has no timeout parameter and can
    block for 5s+ when DNS is dead - which is exactly when you'd be asking.
    """
    result = {}

    def _resolve():
        try:
            socket.gethostbyname(DNS_PROBE)
            result["ok"] = True
        except OSError:
            result["ok"] = False

    t = threading.Thread(target=_resolve, daemon=True)
    t.start()
    t.join(timeout)
    return result.get("ok", False)      # timed out == not working


def _unifi_check():
    """Query the UniFi Network API for AP/switch online state.

    Returns {device_name: is_online}. Empty dict if not configured, or on
    any failure - a UniFi API hiccup should never crash the whole status
    check. The UDM/gateway device itself is excluded: if it's down, this
    call can't succeed anyway (it IS the gateway), so including it would
    be meaningless.
    """
    if not UNIFI_ENABLED:
        return {}
    try:
        req = urllib.request.Request(
            f"https://{UNIFI_HOST}/proxy/network/integration/v1/sites/{UNIFI_SITE_ID}/devices",
            headers={"X-API-Key": UNIFI_API_KEY},
        )
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE  # most UniFi controllers use a self-signed local cert
        with urllib.request.urlopen(req, timeout=5, context=ctx) as resp:
            data = json.loads(resp.read())
        return {
            d["name"]: d["state"] == "ONLINE"
            for d in data.get("data", [])
            if "gateway" not in d.get("features", []) and d.get("model") != "UDM Pro"
        }
    except Exception as e:
        logging.warning("  unifi check error: %s", e)
        return {}


def _check_all(dns_known=None):
    """Configured hosts, UniFi APs/switches (if enabled), plus two
    pseudo-hosts: the internet, and DNS.

    Every entry in HOSTS should be a RAW IP (not a hostname) and WAN_PROBE
    defaults to 1.1.1.1 - so all of this works fine even with DNS down.
    """
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = dict(pool.map(lambda kv: (kv[0], _tcp_check(kv[1])), HOSTS.items()))
    results.update(_unifi_check())
    results["the internet"] = _tcp_check(WAN_PROBE, timeout=2.0)
    results["DNS"] = _dns_check() if dns_known is None else dns_known
    return results


def _local_fallback(dns_ok=None):
    """Spoken answer when the Claude API is unreachable.

    Everything here is a TCP check against a raw IP, so this gives LIVE
    status even with the resolver dead.
    """
    try:
        results = _check_all(dns_known=dns_ok)
        down = [n for n, ok in results.items() if not ok]
        up = [n for n, ok in results.items() if ok]
        if not down:
            return "I can't reach my brain right now, but everything else is up."
        msg = ("I can't reach my brain right now. Checking locally: "
               f"{' and '.join(down)} not responding.")
        if up:
            msg += f" Still up: {', '.join(up)}."
        return msg
    except Exception:
        return "I can't reach my brain right now, and the local check failed too."


def _ago(ts):
    delta = time.time() - ts
    if delta < 90:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)} minutes ago"
    if delta < 86400:
        h = int(delta // 3600)
        return f"{h} hour{'s' if h != 1 else ''} ago"
    d = int(delta // 86400)
    return f"{d} day{'s' if d != 1 else ''} ago"


def _ami_originate():
    """Tell Asterisk to ring the alert extension into the alert context."""
    if not ALERTING_ENABLED:
        return False
    if not AMI_SECRET:
        logging.error("ALERT: AMI_SECRET not set, cannot originate")
        return False
    try:
        with socket.create_connection((AMI_HOST, AMI_PORT), timeout=5) as s:
            s.recv(1024)
            s.sendall(f"Action: Login\r\nUsername: {AMI_USER}\r\n"
                      f"Secret: {AMI_SECRET}\r\n\r\n".encode())
            time.sleep(0.5)
            resp = s.recv(2048).decode(errors="replace")
            if "Success" not in resp:
                logging.error("ALERT: AMI login failed: %s", resp.strip()[:120])
                return False
            s.sendall(f"Action: Originate\r\n"
                      f"Channel: {ALERT_CHANNEL}\r\n"
                      f"Context: {ALERT_CONTEXT}\r\n"
                      f"Exten: s\r\n"
                      f"Priority: 1\r\n"
                      f"CallerID: Homelab Alert <999>\r\n"
                      f"Async: true\r\n\r\n".encode())
            time.sleep(0.5)
            s.sendall(b"Action: Logoff\r\n\r\n")
        logging.info("ALERT: originate sent to %s", ALERT_CHANNEL)
        return True
    except Exception as e:
        logging.error("ALERT: originate failed: %s", e)
        return False


def _monitor_loop():
    """Logs state changes; calls you when something goes DOWN (if alerting is enabled)."""
    global _pending_alert, _last_alert_time
    last = {}
    while True:
        try:
            now = _check_all()
            stamp = time.strftime("%Y-%m-%d %H:%M:%S")
            went_down = []

            for name, up in now.items():
                prev = last.get(name)
                if prev is not None and prev != up:
                    state = "UP" if up else "DOWN"
                    with EVENTS_FILE.open("a") as f:
                        f.write(f"{stamp}\t{name}\t{state}\n")
                    logging.info("MONITOR: %s went %s", name, state)
                    if not up:
                        went_down.append(name)
            last = now

            if (went_down and ALERTING_ENABLED
                    and (time.time() - _last_alert_time) > ALERT_COOLDOWN):
                if "the internet" in went_down:
                    msg = ("Heads up. The internet connection is down. "
                           "I can still reach you on the phone, but not much else.")
                elif "DNS" in went_down:
                    msg = ("Heads up. DNS has stopped resolving, but the internet "
                           "itself is fine.")
                else:
                    still_up = [n for n, ok in now.items() if ok]
                    msg = f"Heads up. {' and '.join(went_down)} stopped responding. "
                    msg += (f"The other {len(still_up)} services are still up."
                            if still_up else "Nothing else is responding either.")

                with _alert_lock:
                    _pending_alert = msg
                if _ami_originate():
                    _last_alert_time = time.time()

                # Also post to Discord - covers being away from the alert phone.
                # Won't reach you during a WAN outage, but nothing external can.
                _discord(f"🚨 **Homelab alert** — {msg}")

        except Exception as e:
            logging.error("monitor error: %s", e)
        time.sleep(CHECK_INTERVAL)


@beta_tool
def check_homelab() -> str:
    """Check whether configured servers, network gear, the internet, and DNS are working RIGHT NOW.

    Use this whenever the caller asks about current status, whether things
    are up, if anything is broken, or whether the internet/DNS/network
    devices are working. Checks everything at once.
    """
    results = _check_all()
    up = [n for n, ok in results.items() if ok]
    down = [n for n, ok in results.items() if not ok]
    if not down:
        return f"All {len(up)} checks passed: {', '.join(up)}."
    return f"NOT RESPONDING: {', '.join(down)}. Still up: {', '.join(up)}."


@beta_tool
def get_downtime_history(hours: int = 24) -> str:
    """Report which monitored services have gone down or come back recently.

    Use this when the caller asks about downtime, outages, whether anything
    has been down, or what happened while they were away.

    Args:
        hours: How far back to look. Defaults to 24.
    """
    now_str = time.strftime("%A %-I:%M %p")
    if not EVENTS_FILE.exists():
        return f"(It is currently {now_str}.) No outages recorded since monitoring started."

    cutoff = time.time() - hours * 3600
    events = []
    for line in EVENTS_FILE.read_text().splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        try:
            ts = time.mktime(time.strptime(parts[0], "%Y-%m-%d %H:%M:%S"))
        except ValueError:
            continue
        if ts >= cutoff:
            clock = time.strftime("%-I:%M %p", time.localtime(ts))
            events.append(f"{parts[1]} went {parts[2].lower()} {_ago(ts)} (at {clock})")

    if not events:
        return (f"(It is currently {now_str}.) No outages in the last {hours} hours - "
                "everything has been stable.")
    return f"(It is currently {now_str}.) " + "; ".join(events[-10:])


@beta_tool
def get_datetime() -> str:
    """Get the current date and time.

    Use whenever the caller asks what time it is, what day it is, or the date.
    """
    return time.strftime("%A, %B %-d, %Y at %-I:%M %p")


@beta_tool
def add_to_list(item: str) -> str:
    """Add an item to a household list (groceries, reminders, to-dos).

    Args:
        item: The thing to add, for example "milk" or "call the dentist".
    """
    item = item.strip()
    if not item:
        return "Nothing to add."
    with LIST_FILE.open("a") as f:
        f.write(item + "\n")
    _discord(f"🛒 Added to list: **{item}**")
    return f"Added '{item}'."


@beta_tool
def read_list() -> str:
    """Read back everything currently on the household list."""
    if not LIST_FILE.exists():
        return "The list is empty."
    items = [l.strip() for l in LIST_FILE.read_text().splitlines() if l.strip()]
    return f"The list has: {', '.join(items)}." if items else "The list is empty."


def _pihole_auth():
    """Get a valid Pi-hole API session ID, re-authenticating only when needed."""
    global _pihole_sid, _pihole_sid_expiry
    if _pihole_sid and time.time() < _pihole_sid_expiry:
        return _pihole_sid
    try:
        req = urllib.request.Request(
            f"http://{PIHOLE_HOST}/api/auth",
            data=json.dumps({"password": PIHOLE_APP_PASSWORD}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        session = data.get("session", {})
        if not session.get("valid"):
            logging.warning("  pihole auth failed: %s", session.get("message"))
            return None
        _pihole_sid = session["sid"]
        _pihole_sid_expiry = time.time() + session.get("validity", 1800) - 60
        return _pihole_sid
    except Exception as e:
        logging.warning("  pihole auth error: %s", e)
        return None


@beta_tool
def get_pihole_stats() -> str:
    """Check Pi-hole ad-blocking stats for today: queries blocked, percent blocked, active clients.

    Use this when the caller asks how many ads have been blocked, about DNS
    stats, or how Pi-hole is doing.
    """
    sid = _pihole_auth()
    if not sid:
        return "I couldn't authenticate with Pi-hole to get stats right now."
    try:
        req = urllib.request.Request(
            f"http://{PIHOLE_HOST}/api/stats/summary",
            headers={"sid": sid},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        q = data["queries"]
        total, blocked, pct = q["total"], q["blocked"], q["percent_blocked"]
        clients = data["clients"]["active"]
        return (f"Pi-hole has blocked {blocked} of {total} queries today, "
                f"about {pct:.0f} percent. {clients} devices are active right now.")
    except Exception as e:
        logging.warning("  pihole stats error: %s", e)
        return "I had trouble getting Pi-hole stats just now."


def _get_disk_usage(host_info):
    target = f"{host_info['ssh_user']}@{host_info['host']}"
    try:
        result = subprocess.run(
            ["ssh", "-T", "-i", DISKCHECK_KEY, "-o", "BatchMode=yes",
             "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=accept-new",
             "-o", f"UserKnownHostsFile={DISKCHECK_KNOWN_HOSTS}",
             target, "ignored"],  # command is forced server-side via authorized_keys
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            logging.warning("  disk check ssh failed (%s): rc=%s stderr=%s",
                            target, result.returncode, result.stderr.strip()[:200])
            return None
        return result.stdout
    except Exception as e:
        logging.warning("  disk check ssh error (%s): %s", target, e)
        return None


def _parse_df(df_output, mounts):
    """Extract use% for specific mount points from `df -h` output."""
    usage = {}
    for line in df_output.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 6:
            continue
        mount = parts[5]
        if mount in mounts:
            usage[mount] = parts[4].rstrip("%")
    return usage


@beta_tool
def get_disk_space() -> str:
    """Check disk space usage on configured servers.

    Use this when the caller asks about disk space, storage, whether a drive
    is full, or if there's room left on the servers.
    """
    reports, warnings = [], []
    for name, info in DISK_HOSTS.items():
        output = _get_disk_usage(info)
        if output is None:
            reports.append(f"{name} unreachable")
            continue
        usage = _parse_df(output, info["mounts"])
        if not usage:
            reports.append(f"{name}: couldn't read disk stats")
            continue
        reports.append(f"{name}: " + ", ".join(f"{p}% on {m}" for m, p in usage.items()))
        warnings += [f"{name} {m} at {p}%" for m, p in usage.items() if int(p) >= 85]

    summary = ". ".join(reports) + "."
    if warnings:
        summary += " Warning: " + ", ".join(warnings) + " getting full."
    return summary


# ============================================================
# Build the tool list based on what's actually configured.
# Tier 1 (check_homelab, downtime history, datetime, list, web
# search) is always present and needs zero config.
# ============================================================
TOOLS = [check_homelab, get_downtime_history, get_datetime, add_to_list, read_list]
if PIHOLE_ENABLED:
    TOOLS.append(get_pihole_stats)
if DISK_CHECK_ENABLED:
    TOOLS.append(get_disk_space)
TOOLS.append(WEB_SEARCH)

# ---------- load models ONCE at startup ----------
logging.info("loading models...")
stt = WhisperModel(cfg("whisper_model", default="base.en"), device="cpu", compute_type="int8")
tts = PiperVoice.load(cfg("piper_voice", default="voices/en_US-lessac-medium.onnx"))
client = anthropic.Anthropic(max_retries=1, timeout=15.0)
logging.info("models ready")

# ---------- startup summary: what's actually enabled ----------
logging.info("=== Startup summary ===")
logging.info("Model: %s (effort: %s)", CLAUDE_MODEL, CLAUDE_EFFORT)
logging.info("Tier 1 (core conversation): always on")
logging.info("Tier 2 (monitoring): %s (%d hosts configured, UniFi %s)",
             "on" if HOSTS or UNIFI_ENABLED else "hosts/internet/DNS only",
             len(HOSTS), "enabled" if UNIFI_ENABLED else "disabled")
logging.info("Tier 3 (alerting): %s", "ENABLED" if ALERTING_ENABLED else "disabled (ami.host not set in config.yaml)")
if ALERTING_ENABLED and not AMI_SECRET:
    logging.warning("  ami.host is set but AMI_SECRET is not - alerting will fail at call time")
if ALERTING_ENABLED and not ALERT_UUID:
    logging.warning("  ALERT_UUID not set - outbound alert calls will play the normal "
                    "greeting instead of the alert message")
logging.info("Tier 4 (Discord): %s", "enabled" if DISCORD_WEBHOOK else "disabled")
logging.info("Pi-hole stats tool: %s", "enabled" if PIHOLE_ENABLED else "disabled")
logging.info("Disk space tool: %s", "enabled" if DISK_CHECK_ENABLED else "disabled")
logging.info("=======================")


def pcm_to_float(b):
    return np.frombuffer(b, dtype=np.int16).astype(np.float32) / 32768.0


def transcribe(pcm8k):
    audio16 = soxr.resample(pcm_to_float(pcm8k), SR_PHONE, SR_STT)
    segments, _ = stt.transcribe(
        audio16, beam_size=1, language="en", condition_on_previous_text=False)
    return " ".join(s.text for s in segments).strip()


def synthesize(text):
    chunks = list(tts.synthesize(text))
    if not chunks:
        return b""
    pcm = b"".join(c.audio_int16_bytes for c in chunks)
    audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    out = soxr.resample(audio, chunks[0].sample_rate, SR_PHONE)
    return (np.clip(out * 0.9, -1.0, 1.0) * 32767).astype(np.int16).tobytes()


FILLER_PCM = [synthesize(t) for t in FILLERS]
logging.info("cached %d fillers", len(FILLER_PCM))

threading.Thread(target=_monitor_loop, daemon=True).start()
logging.info("monitor started (every %ds)", CHECK_INTERVAL)


def ask_claude(history, system=SYSTEM):
    runner = client.beta.messages.tool_runner(
        model=CLAUDE_MODEL,
        max_tokens=1024,
        system=system,
        output_config={"effort": CLAUDE_EFFORT},
        tools=TOOLS,
        messages=history,
    )

    final = None
    for message in runner:
        final = message
        history.append({"role": "assistant", "content": message.content})
        for block in message.content:
            if block.type == "tool_use":
                logging.info("  TOOL: %s(%s)", block.name, block.input)
            elif block.type == "server_tool_use":
                logging.info("  SEARCH: %s", block.input)
        tool_response = runner.generate_tool_call_response()
        if tool_response is not None:
            history.append(tool_response)

    if final is None:
        return "Sorry, something went wrong."
    if final.stop_reason == "refusal":
        return "Sorry, I can't help with that one."
    if final.stop_reason == "pause_turn":
        logging.warning("  search paused mid-turn")
        return "That search is taking a while - could you ask me again?"

    # JOIN every text block. With server-side tools the response contains
    # several, interleaved around the search results - taking only the first
    # truncates the answer to a fragment like "Yes -".
    return " ".join(b.text for b in final.content if b.type == "text").strip()


def read_exactly(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        logging.info("CALL START from %s", self.client_address)
        self.q = queue.Queue()
        self.playing = threading.Event()
        self.done = threading.Event()
        self.uuid_ready = threading.Event()
        self.is_alert = False
        self.system = SYSTEM

        worker = threading.Thread(target=self.brain, daemon=True)
        worker.start()

        while True:
            header = read_exactly(self.request, 3)
            if header is None:
                break
            msg_type, length = struct.unpack("!BH", header)
            payload = read_exactly(self.request, length) if length else b""
            if payload is None:
                break

            if msg_type == TYPE_AUDIO:
                if not self.playing.is_set():
                    self.q.put(payload)
            elif msg_type == TYPE_UUID:
                hexs = payload.hex().lower()
                self.is_alert = bool(ALERT_UUID) and (hexs == ALERT_UUID)
                self.uuid_ready.set()
                logging.info("  UUID %s%s", hexs[:16],
                             "  <<< ALERT CALL" if self.is_alert else "")
            elif msg_type == TYPE_HANGUP:
                break
            elif msg_type == TYPE_ERROR:
                logging.error("  asterisk error %s", payload.hex())

        self.done.set()
        self.uuid_ready.set()
        worker.join(timeout=5)
        logging.info("CALL END")

    def play(self, pcm):
        self.playing.set()
        try:
            PRIME_FRAMES = 15
            start = time.monotonic()
            sent = 0
            for i in range(0, len(pcm), FRAME_BYTES):
                chunk = pcm[i:i + FRAME_BYTES].ljust(FRAME_BYTES, b"\x00")
                self.request.sendall(struct.pack("!BH", TYPE_AUDIO, len(chunk)) + chunk)
                sent += 1
                if sent <= PRIME_FRAMES:
                    continue
                target = start + (sent - PRIME_FRAMES) * 0.020
                delay = target - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
            expected = len(pcm) / (SR_PHONE * 2)
            actual = time.monotonic() - start
            logging.info("  play: %.2fs audio in %.2fs wall (%.0f%%)",
                         expected, actual, 100 * actual / expected)
        except OSError:
            pass
        finally:
            self.playing.clear()

    def ask_with_fillers(self, history):
        """Run ask_claude in a worker; speak filler lines while we wait."""
        result = {}

        def _work():
            # Check DNS BEFORE dialling out. The SDK's own timeout doesn't
            # cover system-resolver failure, so a dead resolver can cost
            # 20+ seconds of retries before erroring.
            if not _dns_check(timeout=2.0):
                logging.warning("  DNS down - skipping API, answering locally")
                result["reply"] = _local_fallback(dns_ok=False)
                return
            try:
                result["reply"] = ask_claude(history, self.system)
            except anthropic.APIStatusError as e:
                if e.status_code >= 500:
                    logging.error("  ask_claude failed (Anthropic server error %s): %s",
                                 e.status_code, e)
                    result["reply"] = ANTHROPIC_DOWN_MESSAGE
                else:
                    logging.error("  ask_claude failed (client error %s): %s",
                                 e.status_code, e)
                    result["reply"] = _local_fallback()
            except Exception as e:
                logging.error("  ask_claude failed: %s", e)
                result["reply"] = _local_fallback()

        t = threading.Thread(target=_work, daemon=True)
        t.start()

        used = 0
        while True:
            t.join(timeout=FILLER_FIRST if used == 0 else FILLER_REPEAT)
            if not t.is_alive():
                break
            if self.done.is_set():          # caller hung up - stop talking
                break
            pcm = FILLER_PCM[min(used, len(FILLER_PCM) - 1)]
            logging.info("  FILLER: %s", FILLERS[min(used, len(FILLERS) - 1)])
            self.play(pcm)
            used += 1

        return result.get("reply", _local_fallback())

    def brain(self):
        global _pending_alert
        vad = webrtcvad.Vad(2)
        buf = bytearray()
        speech_frames = silence_frames = 0

        self.uuid_ready.wait(timeout=2.0)

        if self.is_alert:
            with _alert_lock:
                msg = _pending_alert or "Something went down, but I've lost the details."
                _pending_alert = None
            logging.info("  ALERT OPENER: %s", msg)
            self.system = ALERT_SYSTEM
            history = [
                {"role": "user", "content": "Call me if anything monitored goes down."},
                {"role": "assistant", "content": msg},
            ]
            self.play(synthesize(msg))
        else:
            self.system = SYSTEM
            history = []
            self.play(synthesize(GREETING))

        while not self.done.is_set():
            try:
                frame = self.q.get(timeout=0.5)
            except queue.Empty:
                continue
            if len(frame) != FRAME_BYTES:
                continue

            if vad.is_speech(frame, SR_PHONE):
                buf.extend(frame)
                speech_frames += 1
                silence_frames = 0
                continue

            if speech_frames == 0:
                continue
            silence_frames += 1
            buf.extend(frame)
            if silence_frames < SILENCE_FRAMES_TO_END:
                continue

            audio, enough = bytes(buf), speech_frames >= MIN_SPEECH_FRAMES
            buf, speech_frames, silence_frames = bytearray(), 0, 0
            if not enough:
                continue

            t0 = time.time()
            text = transcribe(audio)
            t_stt = time.time() - t0
            if not text:
                logging.info("  (empty transcript, ignoring)")
                continue
            logging.info("  HEARD [%.2fs]: %s", t_stt, text)

            history.append({"role": "user", "content": text})

            t0 = time.time()
            reply = self.ask_with_fillers(history)
            t_llm = time.time() - t0
            logging.info("  REPLY [%.2fs]: %s", t_llm, reply)

            if self.done.is_set():
                break

            t0 = time.time()
            pcm = synthesize(reply)
            t_tts = time.time() - t0
            logging.info("  spoke  [%.2fs]  (turn total %.2fs)",
                         t_tts, t_stt + t_llm + t_tts)
            self.play(pcm)

            while not self.q.empty():
                self.q.get_nowait()


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    logging.info("listening on 0.0.0.0:9000")
    Server(("0.0.0.0", 9000), Handler).serve_forever()
