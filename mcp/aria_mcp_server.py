#!/usr/bin/env python3
"""
aria MCP server — stdio JSON-RPC interface for Claude Desktop.

Claude Desktop connects to this over stdio. This server exposes tools
that let Claude read aria's state (drafts, inbox, memory files, cron,
metrics) and write back strategic updates (polish drafts, update the
playbook, add scraper targets, send rishabh a telegram).

Why this matters:
  Gemma4:26b drafts everything in the factory. Claude Desktop is the
  strategic oversight layer that rishabh invokes on demand — not on
  every post, not via metered API, just when he wants claude's eyes on
  things. Claude's influence enters via the rules gemma follows and via
  direct polish/playbook updates made through these tools.

Protocol:
  JSON-RPC 2.0 over stdio.
  MCP spec version 2024-11-05.
  Stdlib only — no pip dependencies.

Install into Claude Desktop:
  Edit ~/Library/Application Support/Claude/claude_desktop_config.json
  and add:
    {"mcpServers": {"aria": {
      "command": "python3",
      "args": ["/Users/boredfolio/.openclaw/agents/aria/workspace/mcp/aria_mcp_server.py"]
    }}}
  Then quit and relaunch Claude Desktop. The `aria` toolkit will appear
  in the tools panel.
"""
from __future__ import annotations
import json
import os
import sys
import datetime
import subprocess
import traceback
import urllib.request
import socket

HOME = os.path.expanduser("~")
ARIA = f"{HOME}/.openclaw/agents/aria/workspace"
OPENCLAW_HOME = f"{HOME}/.openclaw"

MEMORY_DIR = f"{ARIA}/memory"
PENDING_PATH = f"{MEMORY_DIR}/pending-cards.jsonl"
INBOX_PATH = f"{MEMORY_DIR}/target-inbox.jsonl"
APPROVED_PATH = f"{MEMORY_DIR}/approved-drafts.jsonl"
PLAYBOOK_PATH = f"{MEMORY_DIR}/playbook.md"
TARGET_LIST_PATH = f"{MEMORY_DIR}/target-list.json"
CRON_PATH = f"{OPENCLAW_HOME}/cron/jobs.json"
SCRIPTS_DIR = f"{ARIA}/scripts"

# ---------- helpers ----------

def _now_iso() -> str:
    return datetime.datetime.now().astimezone().isoformat()


def _load_jsonl(path: str) -> list:
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
                    pass
    except FileNotFoundError:
        pass
    return rows


def _append_jsonl(path: str, row: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _write_jsonl(path: str, rows: list) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def _read_file_safe(path: str, max_bytes: int = 200_000) -> str:
    try:
        with open(path, "rb") as f:
            data = f.read(max_bytes)
        return data.decode("utf-8", errors="replace")
    except FileNotFoundError:
        return f"(file not found: {path})"
    except Exception as e:
        return f"(read error: {e})"


def _log_line(msg: str) -> None:
    """stderr log so we don't pollute stdio which is the JSON-RPC channel."""
    try:
        sys.stderr.write(f"[aria-mcp {datetime.datetime.now().strftime('%H:%M:%S')}] {msg}\n")
        sys.stderr.flush()
    except Exception:
        pass


def _http_get_json(url: str, timeout: float = 1.5):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def _check_openclaw() -> dict:
    try:
        with socket.create_connection(("127.0.0.1", 18789), timeout=0.8):
            return {"alive": True, "port": 18789}
    except Exception as e:
        return {"alive": False, "detail": str(e)}


def _check_ollama() -> dict:
    data = _http_get_json("http://127.0.0.1:11434/api/ps")
    if data is None:
        return {"alive": False}
    models = [m.get("name") for m in data.get("models", [])]
    return {"alive": True, "models_loaded": models}


def _check_cdp() -> dict:
    data = _http_get_json("http://127.0.0.1:28800/json/version")
    if data is None:
        return {"alive": False}
    return {"alive": True, "browser": data.get("Browser")}


# ---------- tools ----------

TOOLS = [
    {
        "name": "aria_overview",
        "description": (
            "Return a short plain-text summary of aria's current state: "
            "heartbeat, inbox counts, pending cards awaiting approval, and "
            "recent activity. Call this FIRST in every claude desktop "
            "session to ground yourself."
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "aria_read_state",
        "description": (
            "Return a full JSON snapshot of aria's live state: heartbeat "
            "(openclaw, ollama, CDP chrome), target inbox counts and rows, "
            "pending cards, approved drafts, and cron jobs. Use this when "
            "you need detailed state for analysis."
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "aria_read_pending_drafts",
        "description": (
            "Return the full list of pending cards (drafts aria has written "
            "and is waiting on rishabh to approve). Each row includes the "
            "source_id, draft text, hook pattern, emotional truth, and "
            "timestamps. Use this when rishabh asks you to review or "
            "polish what's waiting."
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "aria_read_approved_drafts",
        "description": (
            "Return the audit trail of every draft rishabh has approved "
            "and aria has posted. Use this to understand what's been "
            "shipped, spot patterns, and inform strategy."
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "aria_read_target_inbox",
        "description": (
            "Return the target-inbox.jsonl rows — the tweets aria is "
            "considering replying to. Includes author context, original "
            "tweet text, status (new/processing/pending-approval/approved/"
            "skipped)."
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "aria_read_memory_file",
        "description": (
            "Read a specific file from aria's memory/ directory or from the "
            "workspace root. Use this to inspect voice rules, humor "
            "calibration, emotional truths, AGENTS.md, playbook, etc."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "filename": {
                    "type": "string",
                    "description": (
                        "Relative path under aria's workspace. Examples: "
                        "'memory/voice-rules.md', 'memory/humor-calibration.md', "
                        "'memory/playbook.md', 'AGENTS.md', 'SOUL.md'. "
                        "Path must not escape the workspace dir."
                    ),
                }
            },
            "required": ["filename"],
        },
    },
    {
        "name": "aria_list_memory_files",
        "description": "List every file available under aria's workspace (memory + root markdown files). Use this when you don't know the exact filename.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "aria_polish_pending_draft",
        "description": (
            "Replace the draft text of a pending card with a claude-polished "
            "version. This overwrites the draft in pending-cards.jsonl and "
            "marks the card as 'polished-by-claude'. Rishabh still has to "
            "approve before aria posts. Use this to up-level the top 5% of "
            "drafts with your direct input."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "source_id": {
                    "type": "string",
                    "description": "The source_id of the pending card to polish (match what aria_read_pending_drafts returned).",
                },
                "polished_draft": {
                    "type": "string",
                    "description": "Your polished draft text. Respect the voice rules (lowercase, structural humor, <240 chars unless threading).",
                },
                "rationale": {
                    "type": "string",
                    "description": "1-2 lines explaining what you changed and why. Saved with the polish for later review.",
                },
            },
            "required": ["source_id", "polished_draft", "rationale"],
        },
    },
    {
        "name": "aria_update_playbook",
        "description": (
            "Append or replace a section in memory/playbook.md — aria's "
            "strategic playbook. This is how you encode your strategy "
            "decisions so gemma can follow them in every future cron run. "
            "Sections are identified by heading (e.g., '## weekly theme', "
            "'## killer hooks'). If a section with the same heading exists, "
            "it's replaced; otherwise appended."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "section_heading": {
                    "type": "string",
                    "description": "Markdown heading including the '#' prefix. Example: '## weekly theme 2026-w15'",
                },
                "body": {
                    "type": "string",
                    "description": "Markdown body for the section.",
                },
            },
            "required": ["section_heading", "body"],
        },
    },
    {
        "name": "aria_read_target_list",
        "description": "Return the list of X handles the scraper is polling (target-list.json).",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "aria_add_target_handle",
        "description": (
            "Add a handle to the scraper target list. The scraper will "
            "begin polling their tweets on the next cron run."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "handle": {
                    "type": "string",
                    "description": "X handle, with or without the @ prefix. Example: '@nithin0dha' or 'naval'.",
                },
                "priority": {
                    "type": "integer",
                    "description": "1-10 — how high this account ranks vs others for reply opportunities.",
                },
                "themes": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Topic tags this account posts about. Helps the scraper rank relevance.",
                },
                "reason": {
                    "type": "string",
                    "description": "Why this account is worth engaging (for audit).",
                },
            },
            "required": ["handle"],
        },
    },
    {
        "name": "aria_remove_target_handle",
        "description": "Remove a handle from the scraper target list (e.g., if it's not producing quality engagement).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "handle": {"type": "string", "description": "X handle with or without @"},
                "reason": {"type": "string", "description": "Why it's being removed."},
            },
            "required": ["handle"],
        },
    },
    {
        "name": "aria_send_telegram",
        "description": (
            "Send a telegram message to rishabh from the aria bot. Use this "
            "to report a strategic recommendation, ask for input, or "
            "alert about something you noticed during analysis."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "The message body. Keep under 3500 chars.",
                }
            },
            "required": ["text"],
        },
    },
    {
        "name": "aria_performance_summary",
        "description": (
            "Summarize aria's performance so far: drafts written, drafts "
            "approved, drafts skipped, hit rate, most-used hook patterns, "
            "posting cadence. No external metrics yet (streams 7-10 "
            "deferred) — this is factory output stats only."
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
]


# ---------- tool implementations ----------

def tool_aria_overview() -> str:
    inbox = _load_jsonl(INBOX_PATH)
    pending = _load_jsonl(PENDING_PATH)
    approved = _load_jsonl(APPROVED_PATH)
    real_inbox = [r for r in inbox if not (r.get("_example") or r.get("_comment"))]
    new_count = sum(1 for r in real_inbox if r.get("status") == "new")
    pending_active = sum(1 for r in pending if r.get("status") == "sent")

    oc = _check_openclaw()
    ol = _check_ollama()
    cdp = _check_cdp()

    lines = [
        "ARIA OVERVIEW",
        f"timestamp: {_now_iso()}",
        "",
        "heartbeat:",
        f"  openclaw gateway: {'UP' if oc.get('alive') else 'DOWN'}",
        f"  ollama:           {'UP' if ol.get('alive') else 'DOWN'}"
        + (f" (loaded: {', '.join(ol.get('models_loaded', []))})" if ol.get('alive') else ""),
        f"  chrome CDP:       {'UP' if cdp.get('alive') else 'DOWN'}"
        + (f" ({cdp.get('browser', '')})" if cdp.get('alive') else ""),
        "",
        "state:",
        f"  target-inbox: {len(real_inbox)} real rows, {new_count} with status=new",
        f"  pending:      {pending_active} awaiting rishabh's approval",
        f"  approved:     {len(approved)} drafts shipped total",
        "",
        "cron:",
    ]
    try:
        with open(CRON_PATH, "r") as f:
            data = json.load(f)
        for j in data.get("jobs", []):
            name = j.get("name", "?")
            enabled = j.get("enabled", False)
            status = "enabled" if enabled else "disabled"
            lines.append(f"  {name}: {status}")
    except Exception:
        lines.append("  (cron jobs file unreadable)")

    return "\n".join(lines)


def tool_aria_read_state() -> dict:
    inbox = _load_jsonl(INBOX_PATH)
    pending = _load_jsonl(PENDING_PATH)
    approved = _load_jsonl(APPROVED_PATH)
    real_inbox = [r for r in inbox if not (r.get("_example") or r.get("_comment"))]
    return {
        "timestamp": _now_iso(),
        "heartbeat": {
            "openclaw": _check_openclaw(),
            "ollama": _check_ollama(),
            "cdp": _check_cdp(),
        },
        "inbox": {
            "total_real": len(real_inbox),
            "by_status": _count_by_key(real_inbox, "status"),
            "rows": real_inbox,
        },
        "pending": {
            "count": len(pending),
            "active": sum(1 for r in pending if r.get("status") == "sent"),
            "rows": pending,
        },
        "approved": {
            "count": len(approved),
            "rows": approved,
        },
    }


def _count_by_key(rows: list, key: str) -> dict:
    counts: dict = {}
    for r in rows:
        v = r.get(key, "other")
        counts[v] = counts.get(v, 0) + 1
    return counts


def tool_aria_read_pending_drafts() -> list:
    return _load_jsonl(PENDING_PATH)


def tool_aria_read_approved_drafts() -> list:
    return _load_jsonl(APPROVED_PATH)


def tool_aria_read_target_inbox() -> list:
    return [r for r in _load_jsonl(INBOX_PATH) if not (r.get("_example") or r.get("_comment"))]


def tool_aria_read_memory_file(filename: str) -> str:
    # safety: resolve within workspace, refuse path escapes
    safe = os.path.normpath(os.path.join(ARIA, filename))
    if not safe.startswith(ARIA):
        return "(denied: path escapes workspace)"
    return _read_file_safe(safe)


def tool_aria_list_memory_files() -> list:
    out: list = []
    for root, _, files in os.walk(ARIA):
        rel_root = os.path.relpath(root, ARIA)
        if rel_root.startswith((".", "node_modules", "chrome-profile", "dashboard")):
            continue
        if rel_root.startswith("scripts") and rel_root != "scripts":
            continue
        for name in files:
            if name.endswith(".md") or name.endswith(".json") or name.endswith(".jsonl"):
                rel = os.path.relpath(os.path.join(root, name), ARIA)
                out.append(rel)
    return sorted(out)


def tool_aria_polish_pending_draft(source_id: str, polished_draft: str, rationale: str) -> dict:
    rows = _load_jsonl(PENDING_PATH)
    target_idx = None
    for i, r in enumerate(rows):
        if r.get("source_id") == source_id and r.get("status") == "sent":
            target_idx = i
            break
    if target_idx is None:
        return {"ok": False, "reason": f"no sent pending card with source_id={source_id}"}
    original = rows[target_idx].get("draft", "")
    rows[target_idx]["draft"] = polished_draft
    rows[target_idx]["original_draft"] = original
    rows[target_idx]["polished_by"] = "claude-desktop"
    rows[target_idx]["polished_at"] = _now_iso()
    rows[target_idx]["polish_rationale"] = rationale
    rows[target_idx]["status"] = "polished-by-claude"
    _write_jsonl(PENDING_PATH, rows)
    return {
        "ok": True,
        "source_id": source_id,
        "before": original,
        "after": polished_draft,
        "rationale": rationale,
    }


def tool_aria_update_playbook(section_heading: str, body: str) -> dict:
    heading = section_heading.strip()
    if not heading.startswith("#"):
        heading = "## " + heading
    os.makedirs(MEMORY_DIR, exist_ok=True)
    existing = ""
    if os.path.exists(PLAYBOOK_PATH):
        with open(PLAYBOOK_PATH, "r") as f:
            existing = f.read()
    else:
        existing = "# aria playbook\n\nclaude's strategic direction lives here. every new entry stamps date + reason.\n\n"

    lines = existing.splitlines(keepends=False)
    # find section
    start = -1
    end = len(lines)
    for i, line in enumerate(lines):
        if line.strip() == heading:
            start = i
            break
    if start >= 0:
        # find next heading at same or shallower level
        level = len(heading) - len(heading.lstrip("#"))
        for j in range(start + 1, len(lines)):
            ln = lines[j].strip()
            if ln.startswith("#"):
                lvl = len(ln) - len(ln.lstrip("#"))
                if lvl <= level:
                    end = j
                    break
        new_section = [heading, f"*updated {_now_iso()}*", "", body.rstrip(), ""]
        new_lines = lines[:start] + new_section + lines[end:]
        action = "replaced"
    else:
        new_lines = lines + ["", heading, f"*added {_now_iso()}*", "", body.rstrip(), ""]
        action = "appended"
    with open(PLAYBOOK_PATH, "w") as f:
        f.write("\n".join(new_lines) + "\n")
    return {"ok": True, "action": action, "heading": heading, "path": PLAYBOOK_PATH}


def _load_target_list() -> dict:
    if not os.path.exists(TARGET_LIST_PATH):
        return {"version": 1, "last_updated": _now_iso(), "accounts": []}
    try:
        with open(TARGET_LIST_PATH, "r") as f:
            return json.load(f)
    except Exception:
        return {"version": 1, "last_updated": _now_iso(), "accounts": []}


def _save_target_list(data: dict) -> None:
    data["last_updated"] = _now_iso()
    os.makedirs(os.path.dirname(TARGET_LIST_PATH), exist_ok=True)
    with open(TARGET_LIST_PATH, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def tool_aria_read_target_list() -> dict:
    return _load_target_list()


def _normalize_handle(h: str) -> str:
    h = h.strip()
    if not h.startswith("@"):
        h = "@" + h
    return h


def tool_aria_add_target_handle(handle: str, priority: int = 5, themes=None, reason: str = "") -> dict:
    handle = _normalize_handle(handle)
    data = _load_target_list()
    for a in data.get("accounts", []):
        if a.get("handle", "").lower() == handle.lower():
            return {"ok": False, "reason": "already in list", "handle": handle}
    data.setdefault("accounts", []).append({
        "handle": handle,
        "priority": int(priority) if priority else 5,
        "themes": themes or [],
        "reason": reason,
        "added_at": _now_iso(),
        "added_by": "claude-desktop",
    })
    _save_target_list(data)
    return {"ok": True, "handle": handle, "total_accounts": len(data["accounts"])}


def tool_aria_remove_target_handle(handle: str, reason: str = "") -> dict:
    handle = _normalize_handle(handle)
    data = _load_target_list()
    before = len(data.get("accounts", []))
    data["accounts"] = [a for a in data.get("accounts", []) if a.get("handle", "").lower() != handle.lower()]
    after = len(data["accounts"])
    if before == after:
        return {"ok": False, "reason": "not in list", "handle": handle}
    _save_target_list(data)
    return {"ok": True, "handle": handle, "removed": True, "remaining": after, "why": reason}


def tool_aria_send_telegram(text: str) -> dict:
    try:
        r = subprocess.run(
            ["python3", f"{SCRIPTS_DIR}/tg-reply.py"],
            input=text, capture_output=True, text=True, timeout=8,
        )
        return {
            "ok": r.returncode == 0,
            "stdout": r.stdout.strip()[:300],
            "stderr": r.stderr.strip()[:300],
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def tool_aria_performance_summary() -> dict:
    pending = _load_jsonl(PENDING_PATH)
    approved = _load_jsonl(APPROVED_PATH)
    inbox = [r for r in _load_jsonl(INBOX_PATH) if not (r.get("_example") or r.get("_comment"))]

    hooks: dict = {}
    for r in pending + approved:
        h = r.get("hook_pattern", "unknown")
        hooks[h] = hooks.get(h, 0) + 1

    status_counts = _count_by_key(inbox, "status")
    return {
        "drafts_written": len(pending),
        "drafts_approved_posted": len(approved),
        "drafts_in_pending": sum(1 for r in pending if r.get("status") in ("sent", "polished-by-claude")),
        "drafts_skipped": status_counts.get("skipped", 0),
        "hit_rate": (len(approved) / len(pending)) if pending else 0.0,
        "hook_distribution": hooks,
        "inbox_status_counts": status_counts,
        "generated_at": _now_iso(),
    }


# ---------- tool dispatcher ----------

DISPATCH = {
    "aria_overview": lambda a: {"kind": "text", "value": tool_aria_overview()},
    "aria_read_state": lambda a: {"kind": "json", "value": tool_aria_read_state()},
    "aria_read_pending_drafts": lambda a: {"kind": "json", "value": tool_aria_read_pending_drafts()},
    "aria_read_approved_drafts": lambda a: {"kind": "json", "value": tool_aria_read_approved_drafts()},
    "aria_read_target_inbox": lambda a: {"kind": "json", "value": tool_aria_read_target_inbox()},
    "aria_read_memory_file": lambda a: {"kind": "text", "value": tool_aria_read_memory_file(a.get("filename", ""))},
    "aria_list_memory_files": lambda a: {"kind": "json", "value": tool_aria_list_memory_files()},
    "aria_polish_pending_draft": lambda a: {"kind": "json", "value": tool_aria_polish_pending_draft(a.get("source_id", ""), a.get("polished_draft", ""), a.get("rationale", ""))},
    "aria_update_playbook": lambda a: {"kind": "json", "value": tool_aria_update_playbook(a.get("section_heading", ""), a.get("body", ""))},
    "aria_read_target_list": lambda a: {"kind": "json", "value": tool_aria_read_target_list()},
    "aria_add_target_handle": lambda a: {"kind": "json", "value": tool_aria_add_target_handle(a.get("handle", ""), a.get("priority", 5), a.get("themes", []), a.get("reason", ""))},
    "aria_remove_target_handle": lambda a: {"kind": "json", "value": tool_aria_remove_target_handle(a.get("handle", ""), a.get("reason", ""))},
    "aria_send_telegram": lambda a: {"kind": "json", "value": tool_aria_send_telegram(a.get("text", ""))},
    "aria_performance_summary": lambda a: {"kind": "json", "value": tool_aria_performance_summary()},
}


def _call_tool(name: str, arguments: dict) -> dict:
    if name not in DISPATCH:
        return {
            "content": [{"type": "text", "text": f"unknown tool: {name}"}],
            "isError": True,
        }
    try:
        result = DISPATCH[name](arguments or {})
        if result["kind"] == "text":
            return {"content": [{"type": "text", "text": result["value"]}], "isError": False}
        return {
            "content": [{"type": "text", "text": json.dumps(result["value"], ensure_ascii=False, indent=2, default=str)}],
            "isError": False,
        }
    except Exception:
        return {
            "content": [{"type": "text", "text": f"tool error: {traceback.format_exc()}"}],
            "isError": True,
        }


# ---------- JSON-RPC loop ----------

def _make_response(req_id, result=None, error=None) -> dict:
    if error is not None:
        return {"jsonrpc": "2.0", "id": req_id, "error": error}
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def handle_request(req: dict) -> dict | None:
    method = req.get("method", "")
    params = req.get("params") or {}
    req_id = req.get("id")
    is_notification = "id" not in req

    if method == "initialize":
        return _make_response(req_id, {
            "protocolVersion": "2024-11-05",
            "capabilities": {
                "tools": {},
                "logging": {},
            },
            "serverInfo": {
                "name": "aria",
                "version": "0.1.0",
                "description": "ARIA v4 observability + strategy tools for Claude Desktop",
            },
        })

    if method == "notifications/initialized":
        _log_line("client initialized")
        return None  # notification, no response

    if method == "tools/list":
        return _make_response(req_id, {"tools": TOOLS})

    if method == "tools/call":
        name = params.get("name", "")
        arguments = params.get("arguments") or {}
        _log_line(f"tools/call {name}")
        return _make_response(req_id, _call_tool(name, arguments))

    if method == "ping":
        return _make_response(req_id, {})

    if is_notification:
        return None  # silently ignore other notifications

    return _make_response(req_id, error={"code": -32601, "message": f"method not found: {method}"})


def main() -> None:
    _log_line(f"aria MCP server starting · workspace={ARIA}")
    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            continue
        try:
            resp = handle_request(req)
            if resp is not None:
                sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
                sys.stdout.flush()
        except Exception:
            _log_line(f"ERROR {traceback.format_exc()}")


if __name__ == "__main__":
    main()
