#!/usr/bin/env python3
"""
aria live observability dashboard — stdlib-only http server.

serves:
  GET /                 → index.html
  GET /healthz          → {"ok": true}
  GET /api/state        → live JSON snapshot of everything aria is doing
  GET /api/pipeline     → static architecture map (named components + how they connect)
  GET /api/roadmap      → planned vs shipped features
  GET /api/events       → gateway log tail (parsed)

port: 28889 (override via ARIA_DASHBOARD_PORT env var)
read-only: never mutates aria state. CORS open for all origins (dashboard
is consumed by boredfolio.com via a cloudflared tunnel).
"""
from __future__ import annotations
import json
import os
import socket
import subprocess
import urllib.request
import http.server
import socketserver
import datetime
import pathlib
import traceback
import threading
import time
from typing import Any

HOME = os.path.expanduser("~")
ARIA = f"{HOME}/.openclaw/agents/aria/workspace"
OPENCLAW_HOME = f"{HOME}/.openclaw"
DASHBOARD_DIR = f"{ARIA}/dashboard"

PENDING_PATH = f"{ARIA}/memory/pending-cards.jsonl"
INBOX_PATH = f"{ARIA}/memory/target-inbox.jsonl"
APPROVED_PATH = f"{ARIA}/memory/approved-drafts.jsonl"
EVENTS_PATH = f"{ARIA}/memory/events.jsonl"
CRON_PATH = f"{OPENCLAW_HOME}/cron/jobs.json"
OPENCLAW_CONFIG_PATH = f"{OPENCLAW_HOME}/openclaw.json"
GATEWAY_LOG_DIR = "/tmp/openclaw"

PORT = int(os.environ.get("ARIA_DASHBOARD_PORT", "28889"))

# ========= architecture map (static) =========
# this defines what aria IS — the named components, the pipeline,
# what each thing does, and whether it's live or deferred.

PIPELINE_MAP = {
    "name": "aria v4 — the reply + post loop",
    "goal": "autonomous growth from ~400 to 1M followers via high-signal replies, voice-preserving drafts, and human-gated posting",
    "owner": "rishabh (@BalabommaRao)",
    "components": [
        # Tier 0 — humans + identity
        {
            "id": "rishabh",
            "tier": "human",
            "name": "Rishabh",
            "role": "the creative — approves, edits, steers",
            "tech": "human in the loop via telegram DM",
            "status": "live",
            "emoji": "🧠",
        },
        {
            "id": "balabommarao",
            "tier": "identity",
            "name": "@BalabommaRao",
            "role": "rishabh's X handle — the account aria writes for",
            "tech": "X.com account",
            "status": "live",
            "emoji": "🐦",
        },
        # Tier 1 — the brains
        {
            "id": "claude",
            "tier": "brain",
            "name": "Claude (Opus 4.6)",
            "role": "the architect — plans, builds infra, writes skills, debugs the stack",
            "tech": "anthropic api · model id claude-opus-4-6",
            "status": "live",
            "detail": "you're looking at its output. this very dashboard was built by claude in one session.",
            "emoji": "🧭",
        },
        {
            "id": "gemma",
            "tier": "brain",
            "name": "gemma4:26b",
            "role": "the drafter — writes aria's replies + daily angles in rishabh's voice",
            "tech": "ollama local · 25.8B params · Q4_K_M · ~20 GB VRAM · 32k context",
            "status": "live",
            "detail": "runs on the mac studio GPU. pinned via OLLAMA_KEEP_ALIVE=-1. voice-check loop enforces rishabh's tone.",
            "emoji": "🦭",
        },
        # Tier 2 — runtime + orchestration
        {
            "id": "openclaw",
            "tier": "runtime",
            "name": "OpenClaw",
            "role": "the agent runtime — hosts aria, runs sessions, fires cron jobs, routes tools",
            "tech": "nodejs · loopback gateway · ws://127.0.0.1:18789",
            "status": "live",
            "emoji": "🐙",
        },
        {
            "id": "aria",
            "tier": "agent",
            "name": "aria",
            "role": "the agent identity — has memory, skills, voice rules, standing orders",
            "tech": "openclaw agent · model ollama/gemma4:26b · workspace at ~/.openclaw/agents/aria/workspace/",
            "status": "live",
            "detail": "reads AGENTS.md on every turn. state lives in memory/*.jsonl files.",
            "emoji": "✨",
        },
        {
            "id": "cron",
            "tier": "orchestration",
            "name": "openclaw cron",
            "role": "scheduler — fires reply-pipeline every 40m, daily-angles at 08:57 IST",
            "tech": "openclaw cron · store at ~/.openclaw/cron/jobs.json",
            "status": "live",
            "emoji": "⏱️",
        },
        # Tier 3 — state files (the filesystem as message bus)
        {
            "id": "target-inbox",
            "tier": "state",
            "name": "target-inbox.jsonl",
            "role": "queue of tweets aria should consider replying to",
            "tech": "jsonl · memory/target-inbox.jsonl · statuses: new, processing, pending-approval, approved, skipped",
            "status": "live",
            "emoji": "📥",
        },
        {
            "id": "pending-cards",
            "tier": "state",
            "name": "pending-cards.jsonl",
            "role": "draft cards aria has written and is waiting on rishabh to approve",
            "tech": "jsonl · memory/pending-cards.jsonl",
            "status": "live",
            "emoji": "🎴",
        },
        {
            "id": "approved-drafts",
            "tier": "state",
            "name": "approved-drafts.jsonl",
            "role": "audit trail of every draft rishabh approved and aria posted",
            "tech": "jsonl · memory/approved-drafts.jsonl",
            "status": "live",
            "emoji": "🗂️",
        },
        # Tier 4 — delivery + action
        {
            "id": "telegram",
            "tier": "messaging",
            "name": "Telegram Bot API",
            "role": "the only channel between aria and rishabh — cards, approvals, confirmations",
            "tech": "@Boredubot · bot id 8794849679 · chat id 7353580848",
            "status": "live",
            "emoji": "💬",
        },
        {
            "id": "tg-reply",
            "tier": "helper",
            "name": "tg-reply.py",
            "role": "reliable replacement for the broken `message` tool — urllib → telegram api",
            "tech": "python stdlib · scripts/tg-reply.py",
            "status": "live",
            "emoji": "📨",
        },
        {
            "id": "approve-post",
            "tier": "helper",
            "name": "approve-post.py",
            "role": "atomic orchestrator — finds pending card, calls poster, updates 3 state files",
            "tech": "python stdlib · scripts/approve-post.py · subprocess argv (zero shell escaping)",
            "status": "live",
            "emoji": "✅",
        },
        {
            "id": "start-cdp",
            "tier": "helper",
            "name": "start-cdp-chrome.sh",
            "role": "idempotent chrome launcher — ensures CDP is live before posting",
            "tech": "bash · scripts/start-cdp-chrome.sh",
            "status": "live",
            "emoji": "🔌",
        },
        {
            "id": "x-twitter-poster",
            "tier": "skill",
            "name": "x-twitter-poster",
            "role": "the posting skill — types the tweet into x.com via a controlled browser",
            "tech": "clawhub skill · node post_tweet.js · playwright",
            "status": "live",
            "emoji": "📮",
        },
        {
            "id": "playwright",
            "tier": "automation",
            "name": "Playwright",
            "role": "browser automation library — drives chrome via CDP",
            "tech": "node · chromium.connectOverCDP",
            "status": "live",
            "emoji": "🎭",
        },
        {
            "id": "cdp",
            "tier": "automation",
            "name": "Chrome DevTools Protocol",
            "role": "the wire protocol playwright uses to control a real chrome",
            "tech": "127.0.0.1:28800 · JSON over websocket",
            "status": "live",
            "emoji": "🕸️",
        },
        {
            "id": "chrome",
            "tier": "automation",
            "name": "Chrome 147",
            "role": "the real browser that x.com sees — signed in as @BalabommaRao",
            "tech": "isolated profile at ~/.openclaw/agents/aria/chrome-profile/ (survives reboot)",
            "status": "live",
            "emoji": "🌐",
        },
        {
            "id": "x",
            "tier": "platform",
            "name": "x.com",
            "role": "the destination platform — where tweets actually live",
            "tech": "posted via browser automation (no API yet)",
            "status": "live",
            "emoji": "✖️",
        },
    ],
    "stages": [
        {"id": "s1", "name": "1. intake", "components": ["target-inbox"],
         "description": "rishabh seeds real tweets (for now — streams 1-6 deferred)"},
        {"id": "s2", "name": "2. draft", "components": ["cron", "aria", "gemma"],
         "description": "cron fires aria · aria runs the reply-opportunity-pipeline program · gemma writes the draft in rishabh's voice"},
        {"id": "s3", "name": "3. card", "components": ["pending-cards", "telegram"],
         "description": "aria writes the card to pending-cards.jsonl and returns the card body as final text · cron delivery layer sends it to telegram"},
        {"id": "s4", "name": "4. approve", "components": ["rishabh", "telegram", "aria"],
         "description": "rishabh replies approve / edit / skip in DM · aria parses + routes to the right handler"},
        {"id": "s5", "name": "5. post", "components": ["approve-post", "start-cdp", "x-twitter-poster", "playwright", "cdp", "chrome", "x"],
         "description": "APPROVE handler → start-cdp-chrome.sh → approve-post.py → node post_tweet.js → playwright types into x.com → tweet live"},
        {"id": "s6", "name": "6. confirm", "components": ["approve-post", "tg-reply", "telegram", "approved-drafts"],
         "description": "approve-post returns result JSON · tg-reply.py DMs rishabh ✓ posted · approved-drafts appended for audit"},
    ],
}

ROADMAP = {
    "shipped": [
        {"id": "mvp-loop", "name": "MVP reply → approve → post loop", "date": "2026-04-11",
         "detail": "hello world + seed-001 both shipped. full pipeline proven."},
        {"id": "auto-post", "name": "auto-post on approve", "date": "2026-04-11 ~17:30 IST",
         "detail": "APPROVE handler bash-only · chrome profile persistent · approve-post.py atomic"},
        {"id": "tg-reply", "name": "tg-reply.py replaces broken message tool", "date": "2026-04-11 ~17:30 IST",
         "detail": "DM-session handlers all converted off the message tool. urllib + bot api."},
        {"id": "voice-check", "name": "voice-rules enforcement", "date": "2026-04-11",
         "detail": "inline voice rules in AGENTS.md · banned words · §18 test · humor calibration memory atoms"},
        {"id": "dashboard-v1", "name": "this dashboard", "date": "2026-04-11",
         "detail": "live observability. stdlib python http server. no external deps."},
    ],
    "active_crons": [
        {"id": "aria-reply-pipeline", "schedule": "every 40m",
         "status": "enabled", "purpose": "draft replies to target-inbox entries"},
        {"id": "aria-daily-angles", "schedule": "08:57 Asia/Kolkata daily",
         "status": "enabled", "purpose": "morning angle suggestions in rishabh's voice"},
    ],
    "deferred": [
        {"id": "x-api-streams", "name": "X API intelligence streams 1-6", "blocker": "needs X developer account",
         "unblock": "apply for X dev account · add API keys to openclaw.json"},
        {"id": "linkedin", "name": "LinkedIn intake + posting", "blocker": "needs cookies or paste fallback",
         "unblock": "either clone linkedin session cookies or accept paste-in flow"},
        {"id": "claude-drafts", "name": "Claude/Anthropic-based draft generation (higher quality)", "blocker": "needs anthropic api key in aria's config",
         "unblock": "add anthropic provider to openclaw.json · switch aria.model"},
        {"id": "stream-7", "name": "interest graph monitor", "blocker": "needs X API + ≥30d posting history",
         "unblock": "after streams 1-6 + 30d history"},
        {"id": "stream-8", "name": "negative signal monitor", "blocker": "needs posting history",
         "unblock": "after ≥100 replies shipped"},
        {"id": "stream-9", "name": "engagement curve analyzer", "blocker": "needs X API impressions",
         "unblock": "after X dev account + impressions access"},
        {"id": "stream-10", "name": "follower quality scorer", "blocker": "needs xurl + X API",
         "unblock": "after xurl install + API keys"},
        {"id": "domain-10", "name": "platform health monitor", "blocker": "needs ≥30d posting history + baseline accounts",
         "unblock": "after 30d + baseline list configured"},
    ],
    "next_up": [
        "seed more real tweets into target-inbox (rishabh manual, for now)",
        "let daily-angles fire tomorrow morning at 08:57 IST and see the output",
        "wire cloudflared tunnel for public access at boredfolio.com/aria",
        "add draft history visualization (last N drafts with voice-check scores)",
    ],
}

# ========= live state gatherers =========

def _http_get_json(url: str, timeout: float = 1.5) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def check_cdp() -> dict:
    data = _http_get_json("http://127.0.0.1:28800/json/version")
    if not data:
        return {"alive": False, "name": "Chrome CDP", "detail": "no response on :28800"}
    return {
        "alive": True,
        "name": "Chrome CDP",
        "browser": data.get("Browser", "?"),
        "webkit_version": data.get("WebKit-Version", "?"),
        "user_agent": (data.get("User-Agent", "") or "")[:120],
    }


def check_ollama() -> dict:
    data = _http_get_json("http://127.0.0.1:11434/api/ps")
    if data is None:
        return {"alive": False, "name": "Ollama", "models_loaded": []}
    models = []
    for m in data.get("models", []):
        details = m.get("details", {}) or {}
        models.append({
            "name": m.get("name", "?"),
            "size_bytes": m.get("size", 0),
            "size_vram_bytes": m.get("size_vram", 0),
            "expires_at": m.get("expires_at", "?"),
            "context_length": m.get("context_length"),
            "quantization": details.get("quantization_level"),
            "parameter_size": details.get("parameter_size"),
            "family": details.get("family"),
        })
    return {"alive": True, "name": "Ollama", "models_loaded": models}


def check_openclaw() -> dict:
    try:
        with socket.create_connection(("127.0.0.1", 18789), timeout=1.0):
            return {"alive": True, "name": "OpenClaw Gateway", "port": 18789, "dashboard_url": "http://127.0.0.1:18789/"}
    except Exception as e:
        return {"alive": False, "name": "OpenClaw Gateway", "port": 18789, "detail": str(e)}


def check_tg_bot() -> dict:
    """ping telegram getMe via tg-reply.py --test (cached for 30s)."""
    now = time.time()
    cached = _tg_cache.get("data")
    cached_at = _tg_cache.get("at", 0)
    if cached and now - cached_at < 30:
        return cached
    try:
        r = subprocess.run(
            ["python3", f"{ARIA}/scripts/tg-reply.py", "--test"],
            capture_output=True, text=True, timeout=5,
        )
        ok = r.returncode == 0
        data = {
            "alive": ok,
            "name": "Telegram Bot",
            "detail": (r.stdout or r.stderr).strip()[:200],
        }
    except Exception as e:
        data = {"alive": False, "name": "Telegram Bot", "detail": str(e)}
    _tg_cache["data"] = data
    _tg_cache["at"] = now
    return data


_tg_cache: dict = {}


def load_cron_jobs() -> list[dict]:
    try:
        with open(CRON_PATH, "r") as f:
            data = json.load(f)
    except Exception:
        return []
    jobs = data.get("jobs") if isinstance(data, dict) else data
    if not isinstance(jobs, list):
        return []
    out = []
    for j in jobs:
        state = j.get("state", {}) or {}
        sched = j.get("schedule", {}) or {}
        if sched.get("kind") == "every":
            sched_str = f"every {int(sched.get('everyMs', 0)) // 60000}m"
        else:
            sched_str = f"{sched.get('expr', '?')} @ {sched.get('tz', 'UTC')}"
        out.append({
            "id": j.get("id", "?"),
            "name": j.get("name", "?"),
            "description": j.get("description", ""),
            "enabled": j.get("enabled", False),
            "schedule": sched_str,
            "last_run_ms": state.get("lastRunAtMs"),
            "next_run_ms": state.get("nextRunAtMs"),
            "last_status": state.get("lastRunStatus") or state.get("lastStatus"),
            "last_duration_ms": state.get("lastDurationMs"),
            "last_delivered": state.get("lastDelivered"),
            "delivery_channel": (j.get("delivery") or {}).get("channel"),
            "delivery_mode": (j.get("delivery") or {}).get("mode"),
        })
    return out


def load_jsonl(path: str, limit: int = 50) -> list[dict]:
    rows = []
    try:
        with open(path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    rows.append({"_parse_error": line[:120]})
    except FileNotFoundError:
        return []
    except Exception as e:
        return [{"_error": str(e)}]
    return list(reversed(rows[-limit:]))


def inbox_counts(rows: list[dict]) -> dict:
    counts = {"new": 0, "processing": 0, "pending-approval": 0, "approved": 0, "skipped": 0, "example": 0, "other": 0}
    for r in rows:
        if r.get("_example") or r.get("_comment"):
            counts["example"] += 1
            continue
        s = r.get("status", "other")
        if s in counts:
            counts[s] += 1
        else:
            counts["other"] += 1
    return counts


def pending_summary(rows: list[dict]) -> dict:
    active = [r for r in rows if r.get("status") == "sent"]
    return {
        "total": len(rows),
        "active": len(active),
        "rows": rows,
    }


def approved_summary(rows: list[dict]) -> dict:
    return {
        "total": len(rows),
        "rows": rows,
    }


def tail_gateway_log(max_lines: int = 80) -> list[dict]:
    today = datetime.date.today().strftime("%Y-%m-%d")
    log_path = f"{GATEWAY_LOG_DIR}/openclaw-{today}.log"
    if not os.path.exists(log_path):
        try:
            candidates = sorted(
                [f for f in os.listdir(GATEWAY_LOG_DIR) if f.startswith("openclaw-") and f.endswith(".log")],
                reverse=True,
            )
            if not candidates:
                return [{"ts": "", "level": "WARN", "msg": f"(no gateway log found)", "module": ""}]
            log_path = f"{GATEWAY_LOG_DIR}/{candidates[0]}"
        except Exception as e:
            return [{"ts": "", "level": "WARN", "msg": f"(log dir unreadable: {e})", "module": ""}]
    try:
        with open(log_path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            read_size = min(size, 256 * 1024)
            f.seek(size - read_size, 0)
            data = f.read().decode("utf-8", errors="replace")
        raw_lines = data.splitlines()[-max_lines:]
    except Exception as e:
        return [{"ts": "", "level": "WARN", "msg": f"(log read error: {e})", "module": ""}]

    out = []
    for line in raw_lines:
        try:
            obj = json.loads(line)
            ts = obj.get("time") or obj.get("_meta", {}).get("date", "")
            lvl = obj.get("_meta", {}).get("logLevelName", "INFO")
            # message assembly: logs use numeric keys 0, 1, 2...
            parts = []
            for k in sorted(obj.keys()):
                if k.isdigit():
                    v = obj[k]
                    if isinstance(v, (dict, list)):
                        parts.append(json.dumps(v, default=str)[:400])
                    else:
                        parts.append(str(v)[:400])
            msg = " · ".join(parts)[:600]
            module = ""
            # try to extract module from obj[0] if it's a json-shaped string
            first = obj.get("0", "")
            if isinstance(first, str) and first.startswith("{") and "module" in first:
                try:
                    module = json.loads(first).get("module", "")
                except Exception:
                    pass
            out.append({
                "ts": ts,
                "level": lvl,
                "module": module,
                "msg": msg,
            })
        except Exception:
            out.append({"ts": "", "level": "INFO", "module": "", "msg": line[:600]})
    return out


def _row_platform(row: dict) -> str:
    """Normalize platform for any inbox row. manual/seed rows count as 'x'."""
    if row.get("platform") in ("x", "linkedin"):
        return row["platform"]
    src = (row.get("source") or "").lower()
    if "linkedin" in src:
        return "linkedin"
    if "x-scraper" in src or src == "":
        return "x"
    return "x"


def _count_handles() -> dict:
    """Read the two target-handle files and count how many handles each scraper tracks."""
    ws = os.path.expanduser("~/.openclaw/agents/aria/workspace")
    x_count, li_count = 0, 0
    try:
        with open(os.path.join(ws, "memory/target-handles.json")) as f:
            d = json.load(f)
            x_count = len(d.get("handles", []))
    except Exception:
        pass
    try:
        with open(os.path.join(ws, "memory/target-handles-linkedin.json")) as f:
            d = json.load(f)
            li_count = len(d.get("people", [])) + len(d.get("companies", []))
    except Exception:
        pass
    return {"x": x_count, "linkedin": li_count}


def scraper_state_snapshot() -> dict:
    """Read the persistent state blobs the scrapers write after every run."""
    ws = os.path.expanduser("~/.openclaw/agents/aria/workspace")
    out = {
        "x": None,
        "linkedin": None,
        "inbox_by_source": {"x-scraper": 0, "linkedin-scraper": 0, "manual": 0},
        "inbox_by_platform": {"x": 0, "linkedin": 0},
        "inbox_by_status": {},
        "targets": _count_handles(),
    }
    for key, path in (
        ("x", os.path.join(ws, "memory/scraper-state.json")),
        ("linkedin", os.path.join(ws, "memory/linkedin-scraper-state.json")),
    ):
        try:
            with open(path) as f:
                out[key] = json.load(f)
        except Exception:
            pass

    # aggregate inbox stats — skip examples/comments
    inbox = load_jsonl(INBOX_PATH, limit=500)
    for row in inbox:
        if row.get("_example") or row.get("_comment"):
            continue
        src = row.get("source") or "manual"
        out["inbox_by_source"][src] = out["inbox_by_source"].get(src, 0) + 1
        plat = _row_platform(row)
        out["inbox_by_platform"][plat] = out["inbox_by_platform"].get(plat, 0) + 1
        status = row.get("status") or "unknown"
        out["inbox_by_status"][status] = out["inbox_by_status"].get(status, 0) + 1
    return out


def _parse_iso(s: Any) -> float:
    """Return unix ms for an ISO-8601 string, or 0 on failure."""
    if not s or not isinstance(s, str):
        return 0.0
    try:
        s2 = s.replace("Z", "+00:00")
        return datetime.datetime.fromisoformat(s2).timestamp() * 1000
    except Exception:
        return 0.0


def compute_metrics(inbox: list[dict], pending: list[dict], approved: list[dict],
                    cron: list[dict], scrapers: dict) -> dict:
    """Compute operational metrics for the top-of-page band."""
    now_ms = time.time() * 1000
    day_ms = 24 * 60 * 60 * 1000

    # filter out non-real rows
    inbox_real = [r for r in inbox if not (r.get("_example") or r.get("_comment"))]
    pending_real = [r for r in pending if not (r.get("_example") or r.get("_comment"))]
    approved_real = [r for r in approved if not (r.get("_example") or r.get("_comment"))]

    # inbox split
    inbox_by_platform = {"x": 0, "linkedin": 0}
    scraped_24h = {"x": 0, "linkedin": 0}
    for r in inbox_real:
        p = _row_platform(r)
        inbox_by_platform[p] = inbox_by_platform.get(p, 0) + 1
        scraped_at = _parse_iso(r.get("scraped_at") or r.get("timestamp"))
        if scraped_at and now_ms - scraped_at < day_ms:
            scraped_24h[p] = scraped_24h.get(p, 0) + 1

    # drafts + approval rate
    drafts_total = len(pending_real)
    drafts_24h = sum(
        1 for r in pending_real
        if _parse_iso(r.get("sent_at")) and now_ms - _parse_iso(r.get("sent_at")) < day_ms
    )
    pending_review = sum(1 for r in pending_real if r.get("status") == "sent")
    posted_total = len(approved_real)
    posted_24h = sum(
        1 for r in approved_real
        if _parse_iso(r.get("approved_at") or r.get("posted_at")) and
        now_ms - _parse_iso(r.get("approved_at") or r.get("posted_at")) < day_ms
    )
    skipped = sum(1 for r in pending_real if r.get("status") == "skipped")
    decided = posted_total + skipped
    approval_rate = (posted_total / decided) if decided > 0 else None

    # next cron
    next_cron = None
    for j in cron:
        if not j.get("enabled"):
            continue
        nr = j.get("next_run_ms")
        if not nr:
            continue
        if next_cron is None or nr < next_cron["next_run_ms"]:
            next_cron = {
                "id": j.get("id"),
                "name": j.get("name"),
                "next_run_ms": nr,
                "schedule": j.get("schedule"),
            }

    return {
        "inbox_total": len(inbox_real),
        "inbox_by_platform": inbox_by_platform,
        "scraped_24h": scraped_24h,
        "scraped_24h_total": scraped_24h["x"] + scraped_24h["linkedin"],
        "drafts_total": drafts_total,
        "drafts_24h": drafts_24h,
        "posted_total": posted_total,
        "posted_24h": posted_24h,
        "pending_review": pending_review,
        "approval_rate": approval_rate,
        "next_cron": next_cron,
        "targets": scrapers.get("targets", {"x": 0, "linkedin": 0}),
    }


def compute_now(pending: list[dict], approved: list[dict], scrapers: dict,
                cron: list[dict], heartbeat: dict) -> dict:
    """Infer what aria is doing RIGHT NOW and the last/next actions."""
    now_ms = time.time() * 1000

    # 1. if any pending card is waiting for approval → AWAITING APPROVAL
    awaiting = [
        r for r in pending
        if r.get("status") == "sent" and not (r.get("_example") or r.get("_comment"))
    ]
    if awaiting:
        card = awaiting[0]
        status = "awaiting approval"
        detail = f"{len(awaiting)} card{'s' if len(awaiting) != 1 else ''} in rishabh's telegram · reply approve/edit/skip"
        mood = "warn"
    else:
        # 2. if next cron is < 30s → ABOUT TO DRAFT
        next_cron_ms = None
        next_cron_name = None
        for j in cron:
            if not j.get("enabled"):
                continue
            nr = j.get("next_run_ms")
            if not nr:
                continue
            if next_cron_ms is None or nr < next_cron_ms:
                next_cron_ms = nr
                next_cron_name = j.get("name") or j.get("id")
        if next_cron_ms and (next_cron_ms - now_ms) < 30_000:
            status = "about to fire"
            detail = f"{next_cron_name} cron triggers in <30s"
            mood = "live"
        else:
            status = "idle"
            detail = "factory running · waiting for next cron fire"
            mood = "live"

    # last action — max timestamp across pending.sent_at, approved.approved_at, scraper.last_run_iso
    last_ts = 0.0
    last_label = "no activity recorded"
    for r in pending:
        t = _parse_iso(r.get("sent_at"))
        if t > last_ts:
            last_ts = t
            last_label = f"drafted card → {r.get('source_id', '?')}"
    for r in approved:
        t = _parse_iso(r.get("approved_at") or r.get("posted_at"))
        if t > last_ts:
            last_ts = t
            last_label = f"posted approved draft → {r.get('source_id', '?')}"
    for key in ("x", "linkedin"):
        blob = scrapers.get(key) or {}
        t = _parse_iso(blob.get("last_run_iso"))
        if t > last_ts:
            last_ts = t
            added = blob.get("last_added", 0)
            last_label = f"{key}-scraper run · +{added} new rows"

    # next action — next cron fire + its id/name
    next_action = None
    for j in cron:
        if not j.get("enabled"):
            continue
        nr = j.get("next_run_ms")
        if not nr:
            continue
        if next_action is None or nr < next_action["at_ms"]:
            next_action = {
                "at_ms": nr,
                "label": j.get("name") or j.get("id"),
                "schedule": j.get("schedule"),
            }

    # heartbeat degraded? reflect in mood
    any_dead = any(not (heartbeat.get(k) or {}).get("alive") for k in ("openclaw", "ollama", "cdp", "telegram"))
    if any_dead and mood != "warn":
        mood = "warn"
        status = "degraded"
        detail = "one or more subsystems are DOWN — check heartbeat"

    return {
        "status": status,
        "detail": detail,
        "mood": mood,
        "last_action_ms": last_ts or None,
        "last_action_label": last_label,
        "next_action": next_action,
    }


def _tail_text_file(path: str, max_bytes: int = 256 * 1024) -> list[str]:
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            read = min(size, max_bytes)
            f.seek(size - read, 0)
            return f.read().decode("utf-8", errors="replace").splitlines()
    except Exception:
        return []


def _parse_scraper_log_ts(line: str) -> float:
    if not line.startswith("["):
        return 0.0
    end = line.find("]")
    if end < 0:
        return 0.0
    try:
        return datetime.datetime.fromisoformat(line[1:end]).timestamp() * 1000
    except Exception:
        return 0.0


def parse_scraper_progress(platform: str, max_events: int = 40) -> list[dict]:
    """Extract per-handle progress events from the scraper log files so the
    live feed shows 'x-scraper scraping @Nithin0dha' mid-run. events.jsonl
    only has start/done; this fills in the middle."""
    today = datetime.date.today().strftime("%Y-%m-%d")
    prefix = "x-scraper" if platform == "x" else "linkedin-scraper"
    tag = "x-scraper" if platform == "x" else "li-scraper"
    path = f"{ARIA}/logs/{prefix}-{today}.log"
    lines = _tail_text_file(path)
    events: list[dict] = []
    last_ts_ms = 0.0
    actor_emoji = "✖️" if platform == "x" else "💼"

    for raw in lines:
        raw = raw.rstrip()
        if not raw:
            continue
        ts = _parse_scraper_log_ts(raw)
        if ts:
            last_ts_ms = ts
            body = raw[raw.find("]")+1:].strip() if "]" in raw else raw
            if "post-reply cooldown" in body:
                events.append({
                    "ts_ms": ts, "ts_iso": datetime.datetime.fromtimestamp(ts/1000).isoformat(timespec='seconds'),
                    "stream": "scraper", "stage": f"{tag}-cooldown",
                    "level": "warn", "actor": f"{actor_emoji} {tag}",
                    "title": f"{tag} respecting post-reply cooldown",
                    "detail": body, "data": {},
                })
            elif body.startswith("jitter"):
                events.append({
                    "ts_ms": ts, "ts_iso": datetime.datetime.fromtimestamp(ts/1000).isoformat(timespec='seconds'),
                    "stream": "scraper", "stage": f"{tag}-jitter",
                    "level": "info", "actor": f"{actor_emoji} {tag}",
                    "title": f"{tag} {body}",
                    "detail": "anti-bot humanization", "data": {},
                })
            continue

        # unstamped [tag] detail line — inherit last_ts
        stripped = raw.strip()
        if not stripped.startswith(f"[{tag}]"):
            continue
        body = stripped[len(f"[{tag}]"):].strip()
        if not body or not last_ts_ms:
            continue

        title, detail, level, stage = None, "", "info", f"{tag}-progress"
        if body.startswith("→"):
            handle = body[1:].strip()
            title = f"{tag} scraping {handle}"
            stage = f"{tag}-target"
        elif "extracted" in body and "candidate" in body:
            # "extracted 3 candidate(s)"
            title = f"{tag} {body}"
            stage = f"{tag}-extract"
            level = "ok" if "0 candidate" not in body else "info"
        elif "waiting" in body and "before next" in body:
            title = f"{tag} {body.lstrip('. ')}"
            stage = f"{tag}-wait"
        elif "inbox has" in body:
            title = f"{tag} {body}"
            stage = f"{tag}-dedupe"
        elif "run=" in body and "selected" in body:
            title = f"{tag} {body}"
            stage = f"{tag}-plan"
        elif body.startswith("SKIP"):
            title = f"{tag} SKIP · {body}"
            level = "warn"
            stage = f"{tag}-skip"
        else:
            title = f"{tag} {body[:100]}"

        events.append({
            "ts_ms": last_ts_ms,
            "ts_iso": datetime.datetime.fromtimestamp(last_ts_ms/1000).isoformat(timespec='seconds'),
            "stream": "scraper",
            "stage": stage,
            "level": level,
            "actor": f"{actor_emoji} {tag}",
            "title": title,
            "detail": detail,
            "data": {},
        })

    return events[-max_events:]


def parse_gateway_errors(max_events: int = 20) -> list[dict]:
    """Pull cron timeouts, model failovers, and other critical errors from the
    gateway log into the unified feed — they're otherwise invisible."""
    today = datetime.date.today().strftime("%Y-%m-%d")
    log_path = f"{GATEWAY_LOG_DIR}/openclaw-{today}.log"
    if not os.path.exists(log_path):
        return []
    lines = _tail_text_file(log_path, max_bytes=384 * 1024)
    out: list[dict] = []
    for raw in lines:
        if not raw.strip():
            continue
        try:
            obj = json.loads(raw)
        except Exception:
            continue
        m = obj.get("_meta", {}) or {}
        date = m.get("date", "")
        try:
            ts_ms = datetime.datetime.fromisoformat(date.replace("Z", "+00:00")).timestamp() * 1000
        except Exception:
            continue

        v1 = obj.get("1")
        v2 = obj.get("2")

        if isinstance(v1, dict):
            if v1.get("event") == "embedded_run_failover_decision":
                out.append({
                    "ts_ms": ts_ms,
                    "ts_iso": datetime.datetime.fromtimestamp(ts_ms/1000).isoformat(timespec='seconds'),
                    "stream": "model",
                    "stage": "failover",
                    "level": "error" if v1.get("decision") == "surface_error" else "warn",
                    "actor": "🦭 gemma4:26b",
                    "title": f"ollama failover · {v1.get('decision','?')}",
                    "detail": f"reason={v1.get('failoverReason','?')} · provider={v1.get('provider')} · model={v1.get('model')} · timedOut={v1.get('timedOut')}",
                    "data": {},
                })
                continue
            if "jobId" in v1 and "jobName" in v1 and v2:
                if "timed out" in str(v2):
                    out.append({
                        "ts_ms": ts_ms,
                        "ts_iso": datetime.datetime.fromtimestamp(ts_ms/1000).isoformat(timespec='seconds'),
                        "stream": "cron",
                        "stage": "cron-timeout",
                        "level": "error",
                        "actor": f"⏱️ {v1.get('jobName')}",
                        "title": f"cron {v1.get('jobName')} TIMED OUT",
                        "detail": f"timeout {v1.get('timeoutMs',0)//1000}s · jobId {v1.get('jobId','?')[:8]}",
                        "data": {},
                    })
                    continue
                if "error backoff" in str(v2):
                    out.append({
                        "ts_ms": ts_ms,
                        "ts_iso": datetime.datetime.fromtimestamp(ts_ms/1000).isoformat(timespec='seconds'),
                        "stream": "cron",
                        "stage": "cron-backoff",
                        "level": "warn",
                        "actor": f"⏱️ {v1.get('jobName','cron')}",
                        "title": f"cron applying error backoff",
                        "detail": f"consecutive_errors={v1.get('consecutiveErrors',0)} · backoff={v1.get('backoffMs',0)//1000}s",
                        "data": {},
                    })
                    continue
        # ollama keepalive timeouts
        if isinstance(v1, str) and "timed out" in v1 and "ollama" in v1.lower():
            out.append({
                "ts_ms": ts_ms,
                "ts_iso": datetime.datetime.fromtimestamp(ts_ms/1000).isoformat(timespec='seconds'),
                "stream": "model",
                "stage": "ollama-timeout",
                "level": "error",
                "actor": "🦭 ollama",
                "title": v1[:140],
                "detail": "",
                "data": {},
            })

    return out[-max_events:]


def detect_in_flight() -> list[dict]:
    """Detect scrapers or crons that are currently mid-run and expose what
    they're actively doing. The NOW hero surfaces this live."""
    out = []
    today = datetime.date.today().strftime("%Y-%m-%d")
    now_ms = time.time() * 1000

    # Scraper in-flight detection: log was written to recently AND the last
    # `=== run starting` has no matching `output:` after it.
    for plat, prefix, tag, emoji in (
        ("x", "x-scraper", "x-scraper", "✖️"),
        ("linkedin", "linkedin-scraper", "li-scraper", "💼"),
    ):
        log_path = f"{ARIA}/logs/{prefix}-{today}.log"
        try:
            mt_ms = os.path.getmtime(log_path) * 1000
        except Exception:
            continue
        if now_ms - mt_ms > 120_000:
            continue  # no recent writes — not in flight
        lines = _tail_text_file(log_path, max_bytes=64 * 1024)
        if not lines:
            continue
        last_start_idx = -1
        last_output_idx = -1
        for i, ln in enumerate(lines):
            if "=== run starting" in ln:
                last_start_idx = i
            if "output:" in ln and ln.strip().startswith("["):
                last_output_idx = i
        if last_start_idx > last_output_idx:
            # still mid-run — find last → line for current target
            current_target = ""
            last_action = ""
            for ln in lines[last_start_idx:]:
                s = ln.strip()
                if s.startswith(f"[{tag}] →"):
                    current_target = s.split("→", 1)[1].strip()
                if s.startswith(f"[{tag}]"):
                    last_action = s[len(f"[{tag}]"):].strip()
            out.append({
                "kind": "scraper",
                "platform": plat,
                "actor": f"{emoji} {tag}",
                "label": f"{plat}-scraper running",
                "current": current_target or "starting",
                "last_action": last_action or "—",
                "started_ms": _parse_scraper_log_ts(lines[last_start_idx]) or mt_ms,
            })
    return out


def merged_live_feed(limit: int = 150) -> dict:
    """Merge the canonical events.jsonl stream with supplementary streams that
    aren't captured there: scraper per-handle progress + critical gateway
    errors. This is what the dashboard renders in the big activity panel."""
    canonical = load_events(limit=limit)
    supplementary: list[dict] = []
    supplementary.extend(parse_scraper_progress("x", max_events=50))
    supplementary.extend(parse_scraper_progress("linkedin", max_events=50))
    supplementary.extend(parse_gateway_errors(max_events=25))

    # dedupe — drop supplementary events that fall within 500ms of any canonical
    # event with the same stream. canonical wins because it has richer data.
    canonical_ts = {(e.get("ts_ms", 0), e.get("stream", "")) for e in canonical}
    merged = list(canonical)
    for e in supplementary:
        key = (e.get("ts_ms", 0), e.get("stream", ""))
        if any(abs(e.get("ts_ms", 0) - ct) < 500 and cs == e.get("stream") for ct, cs in canonical_ts):
            continue
        merged.append(e)

    merged.sort(key=lambda e: e.get("ts_ms", 0), reverse=True)
    merged = merged[:limit]

    by_stream: dict = {}
    by_level: dict = {}
    for e in merged:
        by_stream[e.get("stream", "?")] = by_stream.get(e.get("stream", "?"), 0) + 1
        by_level[e.get("level", "?")] = by_level.get(e.get("level", "?"), 0) + 1

    return {
        "generated_at_ms": int(time.time() * 1000),
        "count": len(merged),
        "by_stream": by_stream,
        "by_level": by_level,
        "in_flight": detect_in_flight(),
        "events": merged,
    }


def load_events(since_ms: int = 0, limit: int = 200) -> list[dict]:
    """Read events.jsonl tail, newest-first, filtering by ts_ms > since_ms.

    Cheap for <5k rows (default cap). If the file grows, callers should
    pass since_ms to narrow the window.
    """
    if not os.path.exists(EVENTS_PATH):
        return []
    out: list[dict] = []
    try:
        # Read the full file — we cap writes at ~5000 lines via emit-event.py,
        # and each line is ~200 bytes, so worst case is ~1MB. Acceptable.
        with open(EVENTS_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if since_ms and obj.get("ts_ms", 0) <= since_ms:
                    continue
                out.append(obj)
    except Exception:
        return []
    # newest first, cap
    out.reverse()
    return out[:limit]


def compute_analytics(days: int = 7) -> dict:
    """Day-by-day rollup for the last N days (IST).

    Sources:
      - approved-drafts.jsonl → posts per day (by platform)
      - pending-cards.jsonl → drafts written per day + skipped count
      - scraper-state.json / linkedin-scraper-state.json → last run per day
      - events.jsonl → scraper new_items per day
    """
    now = datetime.datetime.now()
    # Build date keys (YYYY-MM-DD) for the last `days` days, oldest first.
    day_keys = []
    for i in range(days - 1, -1, -1):
        d = (now - datetime.timedelta(days=i)).date()
        day_keys.append(d.isoformat())

    def _empty_day(k):
        return {
            "date": k,
            "day_of_week": datetime.date.fromisoformat(k).strftime("%a"),
            "posts": {"x": 0, "linkedin": 0, "total": 0},
            "drafts_written": 0,
            "drafts_skipped": 0,
            "scraped_new": {"x": 0, "linkedin": 0, "total": 0},
            "pipeline_runs": 0,
            "pipeline_failures": 0,
        }

    buckets = {k: _empty_day(k) for k in day_keys}

    def _date_key_from_iso(s):
        if not s or not isinstance(s, str):
            return None
        try:
            return datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).date().isoformat()
        except Exception:
            return None

    # posts (approved-drafts.jsonl)
    try:
        with open(APPROVED_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                k = _date_key_from_iso(r.get("timestamp") or r.get("approved_at") or r.get("posted_at"))
                if k not in buckets:
                    continue
                plat = (r.get("platform") or "x").lower()
                if plat not in ("x", "linkedin"):
                    plat = "x"
                buckets[k]["posts"][plat] = buckets[k]["posts"].get(plat, 0) + 1
                buckets[k]["posts"]["total"] = buckets[k]["posts"]["total"] + 1
    except FileNotFoundError:
        pass

    # drafts written / skipped (pending-cards.jsonl)
    try:
        with open(PENDING_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                k = _date_key_from_iso(r.get("sent_at") or r.get("created_at"))
                if k not in buckets:
                    continue
                buckets[k]["drafts_written"] += 1
                if r.get("status") in ("skipped", "deleted"):
                    buckets[k]["drafts_skipped"] += 1
    except FileNotFoundError:
        pass

    # pipeline + scraper rollups via events.jsonl
    try:
        with open(EVENTS_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                ts_ms = ev.get("ts_ms", 0)
                if not ts_ms:
                    continue
                d = datetime.datetime.fromtimestamp(ts_ms / 1000).date().isoformat()
                if d not in buckets:
                    continue
                stage = ev.get("stage", "")
                if stage in ("pick-start",):
                    buckets[d]["pipeline_runs"] += 1
                if ev.get("level") == "error" and stage.startswith("pick-") is False:
                    buckets[d]["pipeline_failures"] += 1
                if stage == "x-done":
                    n = int((ev.get("data") or {}).get("new_items", 0))
                    buckets[d]["scraped_new"]["x"] += n
                    buckets[d]["scraped_new"]["total"] += n
                if stage == "li-done":
                    n = int((ev.get("data") or {}).get("new_items", 0))
                    buckets[d]["scraped_new"]["linkedin"] += n
                    buckets[d]["scraped_new"]["total"] += n
    except FileNotFoundError:
        pass

    # rollup totals
    totals = {
        "posts_total": sum(b["posts"]["total"] for b in buckets.values()),
        "posts_x": sum(b["posts"].get("x", 0) for b in buckets.values()),
        "posts_linkedin": sum(b["posts"].get("linkedin", 0) for b in buckets.values()),
        "drafts_written": sum(b["drafts_written"] for b in buckets.values()),
        "drafts_skipped": sum(b["drafts_skipped"] for b in buckets.values()),
        "scraped_new": sum(b["scraped_new"]["total"] for b in buckets.values()),
        "pipeline_runs": sum(b["pipeline_runs"] for b in buckets.values()),
        "pipeline_failures": sum(b["pipeline_failures"] for b in buckets.values()),
    }

    return {
        "days": days,
        "generated_at": now.isoformat(timespec="seconds"),
        "buckets": [buckets[k] for k in day_keys],
        "totals": totals,
    }


def state_snapshot() -> dict:
    inbox = load_jsonl(INBOX_PATH, limit=500)
    pending = load_jsonl(PENDING_PATH, limit=50)
    approved = load_jsonl(APPROVED_PATH, limit=50)
    cron = load_cron_jobs()
    scrapers = scraper_state_snapshot()
    heartbeat = {
        "openclaw": check_openclaw(),
        "ollama": check_ollama(),
        "cdp": check_cdp(),
        "telegram": check_tg_bot(),
    }
    metrics = compute_metrics(inbox, pending, approved, cron, scrapers)
    now = compute_now(pending, approved, scrapers, cron, heartbeat)

    # tag every inbox row with normalized platform so the UI can filter cheap
    for r in inbox:
        if not (r.get("_example") or r.get("_comment")):
            r["_platform"] = _row_platform(r)

    return {
        "timestamp": datetime.datetime.now().isoformat(),
        "server_time_ms": int(time.time() * 1000),
        "heartbeat": heartbeat,
        "now": now,
        "metrics": metrics,
        "inbox": {
            "counts": inbox_counts(inbox),
            "rows": inbox[:100],  # cap rows sent to client
        },
        "pending": pending_summary(pending),
        "approved": approved_summary(approved),
        "cron": cron,
        "scrapers": scrapers,
        "daily_goals": _load_daily_goals(),
        "follower_counts": _latest_follower_counts(),
    }


def _latest_follower_counts() -> dict:
    """Read latest follower counts from follower-history.jsonl."""
    path = f"{ARIA}/memory/follower-history.jsonl"
    latest = {"x": 0, "linkedin": 0}
    try:
        with open(path, "r") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                plat = (row.get("platform") or "").lower()
                fc = row.get("followers")
                if plat in latest and isinstance(fc, (int, float)):
                    latest[plat] = int(fc)
    except FileNotFoundError:
        pass
    return latest


def _load_daily_goals() -> dict:
    """Load daily goal progress from the daily-goals module."""
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "daily_goals",
            os.path.join(ARIA, "scripts", "daily-goals.py"),
        )
        if spec and spec.loader:
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod.get_progress()
    except Exception:
        pass
    return {}


# ========= http handlers =========

class AriaDashboardHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A002
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        try:
            import sys
            sys.stderr.write(f"[{ts}] {self.address_string()} {format % args}\n")
        except Exception:
            pass

    def _cors_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):  # noqa: N802
        self.send_response(204)
        self._cors_headers()
        self.end_headers()

    def _send_json(self, obj: Any, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self._cors_headers()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: str, content_type: str) -> None:
        try:
            with open(path, "rb") as f:
                body = f.read()
        except FileNotFoundError:
            self.send_error(404, f"not found: {path}")
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self._cors_headers()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):  # noqa: N802
        # respond to HEAD the same way as GET but without the body. keeps
        # curl -I and upstream health probes from getting 501s.
        path = self.path.split("?", 1)[0]
        try:
            if path in ("/", "/index.html", "/aria", "/aria/", "/api/state",
                        "/api/pipeline", "/api/roadmap", "/api/events",
                        "/api/live", "/api/micro", "/api/analytics",
                        "/api/scrapers", "/healthz"):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self._cors_headers()
                self.end_headers()
                return
            self.send_error(404, f"unknown path: {path}")
        except BrokenPipeError:
            pass

    def _handle_sse(self) -> None:
        """Server-Sent Events endpoint: tails events.jsonl and pushes new events
        to the client in real-time. Also sends periodic heartbeats + state snapshots."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self._cors_headers()
        self.end_headers()

        # Start from the current end of events.jsonl
        try:
            pos = os.path.getsize(EVENTS_PATH)
        except Exception:
            pos = 0

        last_state_ms = 0
        try:
            while True:
                # Check for new events
                try:
                    cur_size = os.path.getsize(EVENTS_PATH)
                except Exception:
                    cur_size = pos

                if cur_size > pos:
                    with open(EVENTS_PATH, "r", encoding="utf-8") as f:
                        f.seek(pos)
                        new_data = f.read()
                    pos = cur_size
                    for line in new_data.strip().split("\n"):
                        if not line.strip():
                            continue
                        try:
                            evt = json.loads(line)
                            self.wfile.write(f"data: {json.dumps(evt, ensure_ascii=False)}\n\n".encode("utf-8"))
                            self.wfile.flush()
                        except Exception:
                            pass
                elif cur_size < pos:
                    # File was truncated/rotated
                    pos = 0

                # Send state snapshot every 5 seconds
                now_ms = int(time.time() * 1000)
                if now_ms - last_state_ms > 5000:
                    try:
                        snap = state_snapshot()
                        self.wfile.write(f"event: state\ndata: {json.dumps(snap, ensure_ascii=False, default=str)}\n\n".encode("utf-8"))
                        self.wfile.flush()
                    except Exception:
                        pass
                    last_state_ms = now_ms

                # Heartbeat every 15s to keep connection alive
                self.wfile.write(": heartbeat\n\n".encode("utf-8"))
                self.wfile.flush()
                time.sleep(1)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass  # Client disconnected

    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        try:
            if path in ("/", "/index.html", "/aria", "/aria/"):
                self._send_file(f"{DASHBOARD_DIR}/index.html", "text/html; charset=utf-8")
                return
            if path == "/api/state":
                self._send_json(state_snapshot())
                return
            if path == "/api/pipeline":
                self._send_json(PIPELINE_MAP)
                return
            if path == "/api/roadmap":
                self._send_json(ROADMAP)
                return
            if path == "/api/events":
                self._send_json({
                    "timestamp": datetime.datetime.now().isoformat(),
                    "lines": tail_gateway_log(max_lines=80),
                })
                return
            if path == "/api/live":
                # Parse query string for since_ms + limit
                qs = ""
                if "?" in self.path:
                    qs = self.path.split("?", 1)[1]
                params = {}
                for part in qs.split("&"):
                    if "=" in part:
                        k, v = part.split("=", 1)
                        params[k] = v
                since_ms = int(params.get("since", "0") or "0")
                limit = int(params.get("limit", "200") or "200")
                events = load_events(since_ms=since_ms, limit=limit)
                self._send_json({
                    "timestamp": datetime.datetime.now().isoformat(),
                    "server_time_ms": int(time.time() * 1000),
                    "count": len(events),
                    "events": events,
                })
                return
            if path == "/api/micro":
                # merged micro-activity stream: events.jsonl + scraper logs +
                # gateway errors + in-flight detection. this is what surfaces
                # "every micro detail" of what openclaw is doing right now.
                qs = ""
                if "?" in self.path:
                    qs = self.path.split("?", 1)[1]
                params = {}
                for part in qs.split("&"):
                    if "=" in part:
                        k, v = part.split("=", 1)
                        params[k] = v
                limit = int(params.get("limit", "150") or "150")
                limit = max(10, min(limit, 500))
                feed = merged_live_feed(limit=limit)
                feed["timestamp"] = datetime.datetime.now().isoformat()
                self._send_json(feed)
                return
            if path == "/api/analytics":
                qs = ""
                if "?" in self.path:
                    qs = self.path.split("?", 1)[1]
                params = {}
                for part in qs.split("&"):
                    if "=" in part:
                        k, v = part.split("=", 1)
                        params[k] = v
                days = int(params.get("days", "7") or "7")
                days = max(1, min(days, 30))
                self._send_json(compute_analytics(days=days))
                return
            if path == "/api/scrapers":
                self._send_json(scraper_state_snapshot())
                return
            if path == "/healthz":
                self._send_json({"ok": True, "port": PORT, "aria": "alive"})
                return
            if path == "/api/stream":
                self._handle_sse()
                return
            self.send_error(404, f"unknown path: {path}")
        except BrokenPipeError:
            pass
        except Exception:
            err = traceback.format_exc()
            try:
                self._send_json({"error": err}, status=500)
            except Exception:
                pass


class ReusableTCPServer(socketserver.ThreadingTCPServer):
    # so restart-after-crash doesn't wait 60s for TIME_WAIT to clear
    allow_reuse_address = True
    daemon_threads = True


def main() -> None:
    pathlib.Path(DASHBOARD_DIR).mkdir(parents=True, exist_ok=True)
    with ReusableTCPServer(("0.0.0.0", PORT), AriaDashboardHandler) as httpd:
        print(f"aria dashboard → http://127.0.0.1:{PORT}/")
        print(f"  GET /api/state     live snapshot")
        print(f"  GET /api/pipeline  architecture map")
        print(f"  GET /api/roadmap   shipped + deferred")
        print(f"  GET /api/events    log tail (legacy)")
        print(f"  GET /api/live      event stream (aria narration)")
        print(f"  GET /api/micro     merged micro-activity (events + scrapers + gateway)")
        print(f"  GET /api/analytics day-wise rollup")
        print(f"  GET /api/scrapers  scraper throughput + inbox aggregates")
        httpd.serve_forever()


if __name__ == "__main__":
    main()
