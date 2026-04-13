#!/usr/bin/env python3
"""
Claude Khud Dashboard -- full visibility into what's happening behind the scenes.

Two pages: X (Twitter) and LinkedIn.
Left side: live process activity.
Right side: every decision in detail.

Run: python3 khud-dashboard.py
Open: http://localhost:8421
"""

from flask import Flask, jsonify, render_template_string
import sqlite3, json, os
from datetime import datetime, timezone, timedelta
from pathlib import Path

app = Flask(__name__)

DB_PATH = Path(os.path.expanduser(
    "~/.openclaw/agents/aria/workspace/memory/aria.db"))


def get_db():
    db = sqlite3.connect(str(DB_PATH), timeout=5)
    db.row_factory = sqlite3.Row
    return db


def now_utc():
    return datetime.now(timezone.utc)


def ist_now():
    return now_utc() + timedelta(hours=5, minutes=30)


# ── API ENDPOINTS ──────────────────────────────────────────

@app.route("/api/x")
def api_x():
    db = get_db()
    now = now_utc()
    ist = ist_now()
    cutoff_24h = (now - timedelta(hours=24)).isoformat()
    cutoff_7d = (now - timedelta(days=7)).isoformat()
    ist_midnight = ist.replace(hour=0, minute=0, second=0, microsecond=0)
    utc_start = (ist_midnight - timedelta(hours=5, minutes=30)).isoformat()

    data = {}

    # ── services status ──
    import subprocess
    try:
        out = subprocess.check_output(
            ["launchctl", "list"], text=True, timeout=5)
        services = {}
        for svc in ["com.aria.brain", "com.aria.hands", "com.aria.khud",
                     "com.aria.watchdog", "com.aria.retro"]:
            services[svc.replace("com.aria.", "")] = svc in out
        data["services"] = services
    except Exception:
        data["services"] = {}

    # ── actions today ──
    actions_today = db.execute(
        "SELECT COUNT(*) as c FROM engine_log WHERE process='hands' "
        "AND message LIKE 'action=%' AND ts > ?", (utc_start,)
    ).fetchone()["c"]
    data["actions_today"] = actions_today
    data["daily_cap"] = 60
    data["time_ist"] = ist.strftime("%I:%M %p IST, %A")

    # ── tweets posted today ──
    tweets = db.execute(
        "SELECT id, text, territory, tweet_url, posted_at, scores_json, image_type "
        "FROM posted WHERE posted_at > ? ORDER BY posted_at DESC",
        (utc_start,)
    ).fetchall()
    data["tweets_today"] = []
    for t in tweets:
        scores = {}
        try:
            scores = json.loads(t["scores_json"] or "{}")
        except Exception:
            pass
        data["tweets_today"].append({
            "id": t["id"][:10],
            "text": t["text"],
            "territory": t["territory"],
            "url": t["tweet_url"],
            "posted_at": t["posted_at"][:16],
            "composite": scores.get("composite"),
            "hook_pattern": scores.get("hook_pattern"),
            "image_type": t["image_type"],
        })

    # ── replies posted today ──
    replies = db.execute(
        "SELECT target_handle, reply_text, target_tweet_url, posted_at "
        "FROM reply_drafts WHERE status='posted' AND posted_at > ? "
        "ORDER BY posted_at DESC", (utc_start,)
    ).fetchall()
    data["replies_today"] = [{
        "target": r["target_handle"],
        "text": r["reply_text"],
        "tweet_url": r["target_tweet_url"],
        "posted_at": r["posted_at"][:16],
    } for r in replies]

    # ── failed replies today ──
    failed = db.execute(
        "SELECT target_handle, reply_text, generated_at "
        "FROM reply_drafts WHERE status='failed' AND generated_at > ? "
        "ORDER BY generated_at DESC", (utc_start,)
    ).fetchall()
    data["failed_replies"] = [{
        "target": f["target_handle"],
        "text": f["reply_text"][:120],
        "at": f["generated_at"][:16],
    } for f in failed]

    # ── queue ──
    queued = db.execute(
        "SELECT id, text, territory, image_type, card_text, scores_json "
        "FROM queue WHERE status='queued' ORDER BY "
        "json_extract(scores_json, '$.composite') DESC"
    ).fetchall()
    data["queue"] = []
    for q in queued:
        scores = {}
        try:
            scores = json.loads(q["scores_json"] or "{}")
        except Exception:
            pass
        data["queue"].append({
            "id": q["id"][:10],
            "text": q["text"],
            "territory": q["territory"],
            "composite": scores.get("composite"),
            "image_type": q["image_type"],
            "card_text": q["card_text"] or "",
        })

    ready_replies = db.execute(
        "SELECT target_handle, reply_text FROM reply_drafts "
        "WHERE status='ready' ORDER BY generated_at"
    ).fetchall()
    data["ready_replies"] = [{
        "target": r["target_handle"],
        "text": r["reply_text"][:120],
    } for r in ready_replies]

    # ── territory distribution 7d ──
    terr = db.execute(
        "SELECT territory, COUNT(*) as c FROM posted WHERE posted_at > ? "
        "GROUP BY territory", (cutoff_7d,)
    ).fetchall()
    data["territory_7d"] = {t["territory"]: t["c"] for t in terr}

    # ── follows ──
    follows = db.execute(
        "SELECT target_handle, performed_at FROM engagements "
        "WHERE action='follow' ORDER BY performed_at DESC"
    ).fetchall()
    data["follows"] = [{
        "handle": f["target_handle"],
        "at": f["performed_at"][:16],
    } for f in follows]

    # ── errors (6h) ──
    cutoff_6h = (now - timedelta(hours=6)).isoformat()
    errors = db.execute(
        "SELECT ts, message FROM engine_log WHERE level='error' "
        "AND ts > ? ORDER BY id DESC LIMIT 10", (cutoff_6h,)
    ).fetchall()
    data["errors"] = [{
        "ts": e["ts"][:16], "msg": e["message"][:200],
    } for e in errors]

    # ── decision ledger ──
    ledger = db.execute(
        "SELECT ts, actor, decision_type, before_state, decision, outcome "
        "FROM decision_ledger ORDER BY id DESC LIMIT 50"
    ).fetchall()
    data["decisions"] = []
    for d in ledger:
        before = {}
        try:
            before = json.loads(d["before_state"] or "{}")
        except Exception:
            before = {"raw": d["before_state"]}
        data["decisions"].append({
            "ts": d["ts"][:16],
            "actor": d["actor"],
            "type": d["decision_type"],
            "before": before,
            "decision": d["decision"] or "",
            "outcome": d["outcome"] or "",
        })

    # ── khud reflections ──
    refs = db.execute(
        "SELECT reflection, category, ts FROM reflections_x ORDER BY id DESC"
    ).fetchall()
    data["reflections"] = [{
        "thought": r["reflection"],
        "category": r["category"],
        "ts": r["ts"][:16],
    } for r in refs]

    # ── khud guidance (current) ──
    guidance = {}
    for key in ["khud.tweet_guidance", "khud.reply_guidance",
                "khud.reply_targets", "khud.tweet_count",
                "khud.last_run", "khud.last_summary"]:
        val = db.execute(
            "SELECT value FROM state WHERE key=?", (key,)
        ).fetchone()
        guidance[key.replace("khud.", "")] = val["value"] if val else ""
    data["guidance"] = guidance

    # ── learned knowledge ──
    sem = db.execute(
        "SELECT knowledge, confidence, ts FROM memory_semantic_x ORDER BY id DESC"
    ).fetchall()
    data["learned"] = [{
        "knowledge": s["knowledge"],
        "confidence": s["confidence"],
        "ts": s["ts"][:16],
    } for s in sem]

    # ── procedural skills ──
    proc = db.execute(
        "SELECT skill_name, skill_type, template, ts FROM memory_procedural_x ORDER BY id DESC"
    ).fetchall()
    data["skills"] = [{
        "name": p["skill_name"],
        "type": p["skill_type"],
        "template": p["template"],
        "ts": p["ts"][:16],
    } for p in proc]

    # ── recent engine log (substantive) ──
    logs = db.execute(
        "SELECT ts, process, level, message FROM engine_log "
        "WHERE ts > ? AND message NOT LIKE '%cycle starting%' "
        "AND message NOT LIKE '%cycle done%' "
        "AND message NOT LIKE '%skip%' "
        "ORDER BY id DESC LIMIT 30", (cutoff_24h,)
    ).fetchall()
    data["activity_log"] = [{
        "ts": l["ts"][:19],
        "process": l["process"],
        "level": l["level"],
        "msg": l["message"][:200],
    } for l in logs]

    db.close()
    return jsonify(data)


@app.route("/api/linkedin")
def api_linkedin():
    db = get_db()
    now = now_utc()
    ist = ist_now()
    ist_midnight = ist.replace(hour=0, minute=0, second=0, microsecond=0)
    utc_start = (ist_midnight - timedelta(hours=5, minutes=30)).isoformat()
    cutoff_7d = (now - timedelta(days=7)).isoformat()

    data = {
        "time_ist": ist.strftime("%I:%M %p IST, %A"),
    }

    # ── services status ──
    import subprocess
    try:
        out = subprocess.check_output(
            ["launchctl", "list"], text=True, timeout=5)
        services = {}
        for svc in ["com.aria.khud-li"]:
            services[svc.replace("com.aria.", "")] = svc in out
        data["services"] = services
    except Exception:
        data["services"] = {}

    # ── linkedin queue ──
    try:
        queued = db.execute(
            "SELECT content, territory, scores_json FROM linkedin_queue "
            "WHERE status='queued' ORDER BY id DESC"
        ).fetchall()
        data["queue"] = []
        for q in queued:
            scores = {}
            try:
                scores = json.loads(q["scores_json"] or "{}")
            except Exception:
                pass
            data["queue"].append({
                "text": q["content"],
                "territory": q["territory"],
                "composite": scores.get("composite"),
            })
    except Exception:
        data["queue"] = []

    # ── linkedin posted ──
    try:
        posted = db.execute(
            "SELECT content, territory, post_url, posted_at, scores_json "
            "FROM linkedin_posted ORDER BY posted_at DESC LIMIT 30"
        ).fetchall()
        data["posts_today"] = []
        data["posts_all"] = []
        for p in posted:
            scores = {}
            try:
                scores = json.loads(p["scores_json"] or "{}")
            except Exception:
                pass
            entry = {
                "text": p["content"][:300],
                "territory": p["territory"],
                "url": p["post_url"],
                "posted_at": (p["posted_at"] or "")[:16],
                "composite": scores.get("composite"),
            }
            data["posts_all"].append(entry)
            if p["posted_at"] and p["posted_at"] > utc_start:
                data["posts_today"].append(entry)
    except Exception:
        data["posts_today"] = []
        data["posts_all"] = []

    # ── territory distribution 7d ──
    try:
        terr = db.execute(
            "SELECT territory, COUNT(*) as c FROM linkedin_posted "
            "WHERE posted_at > ? GROUP BY territory", (cutoff_7d,)
        ).fetchall()
        data["territory_7d"] = {t["territory"]: t["c"] for t in terr}
    except Exception:
        data["territory_7d"] = {}

    # ── stats summary ──
    data["posts_today_count"] = len(data["posts_today"])
    data["posts_total"] = len(data["posts_all"])
    data["queue_count"] = len(data["queue"])

    # ── linkedin reflections ──
    try:
        refs = db.execute(
            "SELECT reflection, category, ts FROM reflections_li ORDER BY id DESC"
        ).fetchall()
        data["reflections"] = [{
            "thought": r["reflection"],
            "category": r["category"],
            "ts": r["ts"][:16],
        } for r in refs]
    except Exception:
        data["reflections"] = []

    # ── linkedin khud guidance ──
    guidance = {}
    for key in ["khud_li.post_guidance", "khud_li.post_count",
                "khud_li.last_run", "khud_li.last_summary"]:
        val = db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        guidance[key.replace("khud_li.", "")] = val["value"] if val else ""
    data["guidance"] = guidance

    # ── linkedin learned knowledge ──
    try:
        sem = db.execute(
            "SELECT knowledge, confidence, ts FROM memory_semantic_li ORDER BY id DESC"
        ).fetchall()
        data["learned"] = [{
            "knowledge": s["knowledge"],
            "confidence": s["confidence"],
            "ts": s["ts"][:16],
        } for s in sem]
    except Exception:
        data["learned"] = []

    # ── linkedin episodic memory ──
    try:
        epi = db.execute(
            "SELECT content, category, importance, ts "
            "FROM memory_episodic_li ORDER BY id DESC LIMIT 20"
        ).fetchall()
        data["episodic"] = [{
            "content": e["content"],
            "category": e["category"],
            "importance": e["importance"],
            "ts": e["ts"][:16],
        } for e in epi]
    except Exception:
        data["episodic"] = []

    # ── linkedin khud actions ──
    try:
        actions = db.execute(
            "SELECT action_type, action_detail, result, ts "
            "FROM khud_actions_li ORDER BY id DESC LIMIT 30"
        ).fetchall()
        data["khud_actions"] = [{
            "type": a["action_type"],
            "detail": a["action_detail"],
            "result": a["result"],
            "ts": a["ts"][:16],
        } for a in actions]
    except Exception:
        data["khud_actions"] = []

    # ── linkedin decisions ──
    try:
        ledger = db.execute(
            "SELECT ts, actor, decision_type, before_state, decision, outcome "
            "FROM decision_ledger WHERE actor='khud_li' ORDER BY id DESC LIMIT 50"
        ).fetchall()
        data["decisions"] = []
        for d in ledger:
            before = {}
            try:
                before = json.loads(d["before_state"] or "{}")
            except Exception:
                before = {"raw": d["before_state"]}
            data["decisions"].append({
                "ts": d["ts"][:16],
                "actor": d["actor"],
                "type": d["decision_type"],
                "before": before,
                "decision": d["decision"] or "",
                "outcome": d["outcome"] or "",
            })
    except Exception:
        data["decisions"] = []

    db.close()
    return jsonify(data)


# ── HTML TEMPLATE ──────────────────────────────────────────

TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Claude Khud Dashboard</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: 'DM Sans', -apple-system, sans-serif; background: #0d0d0d; color: #e0e0e0; }

.tabs { display: flex; gap: 0; border-bottom: 1px solid #2a2a2a; background: #111; position: sticky; top: 0; z-index: 10; }
.tab { padding: 14px 32px; cursor: pointer; font-size: 14px; font-weight: 600; letter-spacing: 0.5px;
       color: #666; border-bottom: 2px solid transparent; transition: all 0.2s; }
.tab:hover { color: #999; }
.tab.active { color: #6B8F71; border-bottom-color: #6B8F71; }

.page { display: none; }
.page.active { display: flex; }

.split { display: flex; width: 100%; min-height: calc(100vh - 48px); }
.left, .right { flex: 1; padding: 20px; overflow-y: auto; max-height: calc(100vh - 48px); }
.left { border-right: 1px solid #1a1a1a; }

h2 { font-size: 13px; font-weight: 700; letter-spacing: 1px; text-transform: uppercase;
     color: #6B8F71; margin: 24px 0 12px; }
h2:first-child { margin-top: 0; }

.stat-row { display: flex; gap: 16px; margin-bottom: 16px; flex-wrap: wrap; }
.stat { background: #161616; border: 1px solid #222; border-radius: 8px; padding: 12px 16px; min-width: 120px; }
.stat-label { font-size: 11px; color: #666; text-transform: uppercase; letter-spacing: 0.5px; }
.stat-value { font-size: 22px; font-weight: 700; color: #e0e0e0; margin-top: 4px; }
.stat-value.green { color: #6B8F71; }
.stat-value.red { color: #c44; }
.stat-value.orange { color: #d4a03c; }

.card { background: #161616; border: 1px solid #222; border-radius: 8px; padding: 14px;
        margin-bottom: 10px; font-size: 13px; line-height: 1.5; }
.card .meta { font-size: 11px; color: #555; margin-top: 6px; }
.card .tag { display: inline-block; background: #1e2a20; color: #6B8F71; padding: 2px 8px;
             border-radius: 4px; font-size: 11px; font-weight: 600; margin-right: 6px; }
.card .tag.red { background: #2a1a1a; color: #c44; }
.card .tag.orange { background: #2a2418; color: #d4a03c; }
.card .tag.blue { background: #182028; color: #4a9cc4; }

.decision { background: #161616; border: 1px solid #222; border-radius: 8px; padding: 14px;
            margin-bottom: 10px; font-size: 13px; line-height: 1.6; border-left: 3px solid #6B8F71; }
.decision .actor { font-size: 11px; font-weight: 700; color: #6B8F71; text-transform: uppercase; }
.decision .type { font-size: 11px; color: #888; margin-left: 8px; }
.decision .text { margin-top: 6px; color: #ccc; }
.decision .before { margin-top: 6px; font-size: 12px; color: #666; font-style: italic; }
.decision .outcome { margin-top: 4px; font-size: 12px; color: #999; }

.knowledge { background: #1a2218; border: 1px solid #2a3a28; border-radius: 8px; padding: 14px;
             margin-bottom: 10px; font-size: 13px; line-height: 1.6; }
.knowledge .confidence { font-size: 11px; color: #6B8F71; font-weight: 700; }

.reflection { background: #161616; border-left: 3px solid #d4a03c; border-radius: 0 8px 8px 0;
              padding: 12px 14px; margin-bottom: 10px; font-size: 13px; line-height: 1.6; color: #bbb; }
.reflection .cat { font-size: 11px; font-weight: 700; color: #d4a03c; text-transform: uppercase; }

.error-card { background: #1a1212; border: 1px solid #2a1a1a; border-radius: 8px; padding: 10px;
              margin-bottom: 8px; font-size: 12px; color: #c44; }

.log-entry { font-size: 12px; color: #888; padding: 4px 0; border-bottom: 1px solid #1a1a1a; }
.log-entry .proc { color: #6B8F71; font-weight: 600; }
.log-entry .err { color: #c44; }

.empty { color: #444; font-style: italic; font-size: 13px; padding: 12px 0; }

.refresh-bar { position: fixed; bottom: 0; left: 0; right: 0; background: #111;
               border-top: 1px solid #222; padding: 8px 20px; font-size: 11px; color: #444;
               display: flex; justify-content: space-between; z-index: 10; }
.refresh-bar button { background: #222; color: #888; border: 1px solid #333; border-radius: 4px;
                      padding: 4px 12px; cursor: pointer; font-size: 11px; }
.refresh-bar button:hover { background: #333; color: #ccc; }

.svc-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; }
.svc-dot.on { background: #6B8F71; }
.svc-dot.off { background: #c44; }
.svc-item { display: inline-flex; align-items: center; margin-right: 16px; font-size: 12px; color: #888; }

/* linkedin placeholder */
.li-placeholder { text-align: center; padding: 80px 40px; color: #444; }
.li-placeholder h3 { font-size: 18px; color: #555; margin-bottom: 12px; }
</style>
</head>
<body>

<div class="tabs">
  <div class="tab active" onclick="switchTab('x')">X (Twitter)</div>
  <div class="tab" onclick="switchTab('linkedin')">LinkedIn</div>
</div>

<!-- ── X PAGE ── -->
<div id="page-x" class="page active">
<div class="split">
<div class="left" id="x-left">
  <h2>loading...</h2>
</div>
<div class="right" id="x-right">
  <h2>loading...</h2>
</div>
</div>
</div>

<!-- ── LINKEDIN PAGE ── -->
<div id="page-linkedin" class="page">
<div class="split">
<div class="left" id="li-left">
  <h2>loading...</h2>
</div>
<div class="right" id="li-right">
  <h2>loading...</h2>
</div>
</div>
</div>

<div class="refresh-bar">
  <span id="refresh-status">loading...</span>
  <button onclick="refresh()">refresh now</button>
</div>

<script>
function switchTab(tab) {
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.getElementById('page-' + tab).classList.add('active');
  document.querySelectorAll('.tab').forEach(t => {
    if ((tab === 'x' && t.textContent.includes('X')) ||
        (tab === 'linkedin' && t.textContent.includes('LinkedIn')))
      t.classList.add('active');
  });
}

function esc(s) { if (s === null || s === undefined) return ''; s = String(s); const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }

function renderX(d) {
  // ── LEFT: live activity ──
  let left = '';

  // services
  left += '<h2>Services</h2><div style="margin-bottom:16px">';
  for (const [name, on] of Object.entries(d.services || {})) {
    left += `<span class="svc-item"><span class="svc-dot ${on ? 'on' : 'off'}"></span>${name}</span>`;
  }
  left += '</div>';

  // stats
  const pct = Math.round((d.actions_today / d.daily_cap) * 100);
  const capColor = pct > 90 ? 'red' : pct > 70 ? 'orange' : 'green';
  left += '<div class="stat-row">';
  left += `<div class="stat"><div class="stat-label">Actions Today</div><div class="stat-value ${capColor}">${d.actions_today}/${d.daily_cap}</div></div>`;
  left += `<div class="stat"><div class="stat-label">Tweets Posted</div><div class="stat-value">${d.tweets_today.length}</div></div>`;
  left += `<div class="stat"><div class="stat-label">Replies Landed</div><div class="stat-value green">${d.replies_today.length}</div></div>`;
  left += `<div class="stat"><div class="stat-label">Failed</div><div class="stat-value red">${d.failed_replies.length}</div></div>`;
  left += `<div class="stat"><div class="stat-label">Queue</div><div class="stat-value">${d.queue.length}</div></div>`;
  left += `<div class="stat"><div class="stat-label">Follows</div><div class="stat-value">${d.follows.length}</div></div>`;
  left += '</div>';

  // territory
  left += '<h2>Territory Distribution (7d)</h2><div class="stat-row">';
  for (const [t, c] of Object.entries(d.territory_7d || {})) {
    left += `<div class="stat"><div class="stat-label">${esc(t)}</div><div class="stat-value">${c}</div></div>`;
  }
  left += '</div>';

  // tweets posted today
  left += '<h2>Tweets Posted Today</h2>';
  if (!d.tweets_today.length) left += '<div class="empty">nothing posted yet today</div>';
  for (const t of d.tweets_today) {
    left += `<div class="card">
      <span class="tag">${esc(t.territory)}</span>
      <span class="tag blue">${esc(t.hook_pattern || '?')}</span>
      <span class="tag">${t.composite || '?'}</span>
      ${t.image_type !== 'none' ? `<span class="tag orange">${esc(t.image_type)}</span>` : ''}
      <div style="margin-top:8px">${esc(t.text)}</div>
      <div class="meta">${t.posted_at} ${t.url ? `<a href="${esc(t.url)}" target="_blank" style="color:#4a9cc4">view</a>` : '(no url)'}</div>
    </div>`;
  }

  // replies posted
  left += '<h2>Replies Landed Today</h2>';
  if (!d.replies_today.length) left += '<div class="empty">no replies yet</div>';
  for (const r of d.replies_today) {
    left += `<div class="card">
      <span class="tag">@${esc(r.target)}</span>
      <div style="margin-top:8px">${esc(r.text)}</div>
      <div class="meta">${r.posted_at} <a href="${esc(r.tweet_url || '')}" target="_blank" style="color:#4a9cc4">tweet</a></div>
    </div>`;
  }

  // failed
  if (d.failed_replies.length) {
    left += '<h2>Failed Replies</h2>';
    for (const f of d.failed_replies) {
      left += `<div class="error-card"><span class="tag red">@${esc(f.target)}</span> ${esc(f.text)}<div class="meta">${f.at}</div></div>`;
    }
  }

  // queue
  left += '<h2>Queue (next up)</h2>';
  if (!d.queue.length) left += '<div class="empty">queue empty</div>';
  for (const q of d.queue) {
    left += `<div class="card">
      <span class="tag">${esc(q.territory)}</span>
      <span class="tag">${q.composite || '?'}</span>
      ${q.image_type !== 'none' ? `<span class="tag orange">${esc(q.image_type)}</span>` : ''}
      ${q.card_text ? `<span class="tag blue">card: ${esc(q.card_text).substring(0,40)}</span>` : ''}
      <div style="margin-top:8px">${esc(q.text)}</div>
    </div>`;
  }

  // errors
  if (d.errors.length) {
    left += '<h2>Recent Errors</h2>';
    for (const e of d.errors) {
      left += `<div class="error-card">${e.ts} ${esc(e.msg)}</div>`;
    }
  }

  document.getElementById('x-left').innerHTML = left;

  // ── RIGHT: decisions ──
  let right = '';

  // khud guidance (current)
  right += '<h2>Khud Current Guidance</h2>';
  const g = d.guidance || {};
  if (g.tweet_guidance) right += `<div class="card"><b>Tweet direction:</b> ${esc(g.tweet_guidance)}</div>`;
  if (g.reply_guidance) right += `<div class="card"><b>Reply direction:</b> ${esc(g.reply_guidance)}</div>`;
  if (g.reply_targets) right += `<div class="card"><b>Priority targets:</b> ${esc(g.reply_targets)}</div>`;
  if (g.last_run) right += `<div class="card"><b>Last Khud run:</b> ${esc(g.last_run)}</div>`;

  // learned knowledge
  right += '<h2>Learned Knowledge (permanent)</h2>';
  if (!d.learned.length) right += '<div class="empty">no learned knowledge yet</div>';
  for (const k of d.learned) {
    right += `<div class="knowledge">
      <span class="confidence">${k.confidence} confidence</span> <span style="color:#555;font-size:11px">${k.ts}</span>
      <div style="margin-top:6px">${esc(k.knowledge)}</div>
    </div>`;
  }

  // reflections
  right += '<h2>Khud Reflections</h2>';
  if (!d.reflections.length) right += '<div class="empty">no reflections yet</div>';
  for (const r of d.reflections) {
    right += `<div class="reflection">
      <span class="cat">${esc(r.category)}</span> <span style="color:#555;font-size:11px">${r.ts}</span>
      <div style="margin-top:6px">${esc(r.thought)}</div>
    </div>`;
  }

  // decision ledger
  right += '<h2>Decision Ledger</h2>';
  if (!d.decisions.length) right += '<div class="empty">no decisions logged yet (will populate from next cycle)</div>';
  for (const dec of d.decisions) {
    const beforeStr = typeof dec.before === 'object' ? Object.entries(dec.before).map(([k,v]) => `${k}: ${v}`).join(' | ') : '';
    right += `<div class="decision">
      <span class="actor">${esc(dec.actor)}</span>
      <span class="type">${esc(dec.type)}</span>
      <span style="color:#555;font-size:11px;margin-left:8px">${dec.ts}</span>
      <div class="text">${esc(dec.decision)}</div>
      ${beforeStr ? `<div class="before">context: ${esc(beforeStr)}</div>` : ''}
      ${dec.outcome ? `<div class="outcome">outcome: ${esc(dec.outcome)}</div>` : ''}
    </div>`;
  }

  // activity log
  right += '<h2>Activity Log</h2>';
  for (const l of d.activity_log || []) {
    const cls = l.level === 'error' ? 'err' : 'proc';
    right += `<div class="log-entry"><span style="color:#444">${l.ts.substring(11)}</span> <span class="${cls}">[${l.process}]</span> ${esc(l.msg)}</div>`;
  }

  document.getElementById('x-right').innerHTML = right;
}

function renderLinkedIn(d) {
  // ── LEFT: live activity ──
  let left = '';

  // services
  left += '<h2>Services</h2><div style="margin-bottom:16px">';
  for (const [name, on] of Object.entries(d.services || {})) {
    left += `<span class="svc-item"><span class="svc-dot ${on ? 'on' : 'off'}"></span>${name}</span>`;
  }
  if (!Object.keys(d.services || {}).length) left += '<span style="color:#555;font-size:12px">no services configured</span>';
  left += '</div>';

  // stats
  left += '<div class="stat-row">';
  left += `<div class="stat"><div class="stat-label">Posts Today</div><div class="stat-value green">${d.posts_today_count || 0}</div></div>`;
  left += `<div class="stat"><div class="stat-label">Total Posts</div><div class="stat-value">${d.posts_total || 0}</div></div>`;
  left += `<div class="stat"><div class="stat-label">Queue</div><div class="stat-value">${d.queue_count || 0}</div></div>`;
  left += '</div>';

  // territory 7d
  if (Object.keys(d.territory_7d || {}).length) {
    left += '<h2>Territory Distribution (7d)</h2><div class="stat-row">';
    for (const [t, c] of Object.entries(d.territory_7d)) {
      left += `<div class="stat"><div class="stat-label">${esc(t)}</div><div class="stat-value">${c}</div></div>`;
    }
    left += '</div>';
  }

  // posts today
  left += '<h2>Posts Today</h2>';
  if (!(d.posts_today || []).length) left += '<div class="empty">nothing posted yet today</div>';
  for (const p of (d.posts_today || [])) {
    left += `<div class="card">
      <span class="tag">${esc(p.territory)}</span>
      ${p.composite ? `<span class="tag blue">${p.composite}</span>` : ''}
      <div style="margin-top:8px">${esc(p.text)}</div>
      <div class="meta">${p.posted_at || ''} ${p.url ? `<a href="${esc(p.url)}" target="_blank" style="color:#4a9cc4">view</a>` : ''}</div>
    </div>`;
  }

  // queue
  left += '<h2>Queue (next up)</h2>';
  if (!(d.queue || []).length) left += '<div class="empty">queue empty</div>';
  for (const q of (d.queue || [])) {
    left += `<div class="card">
      <span class="tag">${esc(q.territory)}</span>
      ${q.composite ? `<span class="tag blue">${q.composite}</span>` : ''}
      <div style="margin-top:8px">${esc(q.text)}</div>
    </div>`;
  }

  // all posts (history)
  if ((d.posts_all || []).length > (d.posts_today || []).length) {
    left += '<h2>Recent Posts (all time)</h2>';
    for (const p of (d.posts_all || [])) {
      left += `<div class="card">
        <span class="tag">${esc(p.territory)}</span>
        <div style="margin-top:8px">${esc(p.text)}</div>
        <div class="meta">${p.posted_at || ''} ${p.url ? `<a href="${esc(p.url)}" target="_blank" style="color:#4a9cc4">view</a>` : ''}</div>
      </div>`;
    }
  }

  document.getElementById('li-left').innerHTML = left;

  // ── RIGHT: decisions + brain ──
  let right = '';

  // khud guidance
  right += '<h2>Khud Current Guidance</h2>';
  const g = d.guidance || {};
  if (g.post_guidance) right += `<div class="card"><b>Post direction:</b> ${esc(g.post_guidance)}</div>`;
  if (g.post_count) right += `<div class="card"><b>Post count target:</b> ${esc(g.post_count)}</div>`;
  if (g.last_summary) right += `<div class="card"><b>Summary:</b> ${esc(g.last_summary)}</div>`;
  if (g.last_run) right += `<div class="card"><b>Last Khud run:</b> ${esc(g.last_run)}</div>`;
  if (!g.post_guidance && !g.last_run) right += '<div class="empty">no guidance yet (khud hasn\'t run)</div>';

  // learned knowledge
  right += '<h2>Learned Knowledge (permanent)</h2>';
  if (!(d.learned || []).length) right += '<div class="empty">no learned knowledge yet</div>';
  for (const k of (d.learned || [])) {
    right += `<div class="knowledge">
      <span class="confidence">${k.confidence} confidence</span> <span style="color:#555;font-size:11px">${k.ts}</span>
      <div style="margin-top:6px">${esc(k.knowledge)}</div>
    </div>`;
  }

  // reflections
  right += '<h2>Khud Reflections</h2>';
  if (!(d.reflections || []).length) right += '<div class="empty">no reflections yet</div>';
  for (const r of (d.reflections || [])) {
    right += `<div class="reflection">
      <span class="cat">${esc(r.category)}</span> <span style="color:#555;font-size:11px">${r.ts}</span>
      <div style="margin-top:6px">${esc(r.thought)}</div>
    </div>`;
  }

  // episodic memory
  if ((d.episodic || []).length) {
    right += '<h2>Episodic Memory</h2>';
    for (const e of d.episodic) {
      right += `<div class="card" style="border-left:3px solid #4a9cc4">
        <span class="tag blue">${esc(e.category)}</span>
        <span style="color:#555;font-size:11px;margin-left:6px">importance: ${e.importance}</span>
        <span style="color:#555;font-size:11px;margin-left:6px">${e.ts}</span>
        <div style="margin-top:6px">${esc(e.content)}</div>
      </div>`;
    }
  }

  // decision ledger
  right += '<h2>Decision Ledger</h2>';
  if (!(d.decisions || []).length) right += '<div class="empty">no decisions logged yet</div>';
  for (const dec of (d.decisions || [])) {
    const beforeStr = typeof dec.before === 'object' ? Object.entries(dec.before).map(([k,v]) => `${k}: ${v}`).join(' | ') : '';
    right += `<div class="decision">
      <span class="actor">${esc(dec.actor)}</span>
      <span class="type">${esc(dec.type)}</span>
      <span style="color:#555;font-size:11px;margin-left:8px">${dec.ts}</span>
      <div class="text">${esc(dec.decision)}</div>
      ${beforeStr ? `<div class="before">context: ${esc(beforeStr)}</div>` : ''}
      ${dec.outcome ? `<div class="outcome">outcome: ${esc(dec.outcome)}</div>` : ''}
    </div>`;
  }

  // khud actions log
  if ((d.khud_actions || []).length) {
    right += '<h2>Khud Actions Log</h2>';
    for (const a of d.khud_actions) {
      right += `<div class="log-entry">
        <span style="color:#444">${(a.ts || '').substring(11)}</span>
        <span class="proc">[${esc(a.type)}]</span> ${esc(a.detail)}
        ${a.result ? `<span style="color:#666"> -> ${esc(a.result).substring(0,100)}</span>` : ''}
      </div>`;
    }
  }

  document.getElementById('li-right').innerHTML = right;
}

async function refresh() {
  document.getElementById('refresh-status').textContent = 'refreshing...';
  try {
    const [xRes, liRes] = await Promise.all([
      fetch('/api/x').then(r => r.json()),
      fetch('/api/linkedin').then(r => r.json()),
    ]);
    renderX(xRes);
    renderLinkedIn(liRes);
    document.getElementById('refresh-status').textContent =
      `last updated: ${new Date().toLocaleTimeString()} | ${xRes.time_ist} | auto-refresh 30s`;
  } catch(e) {
    document.getElementById('refresh-status').textContent = 'error: ' + e.message;
  }
}

refresh();
setInterval(refresh, 30000);
</script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(TEMPLATE)


@app.route("/x")
def page_x():
    return render_template_string(TEMPLATE)


@app.route("/linkedin")
def page_linkedin():
    return render_template_string(TEMPLATE)


if __name__ == "__main__":
    print("Claude Khud Dashboard: http://localhost:8421")
    print("  /x        -> X (Twitter) decisions")
    print("  /linkedin -> LinkedIn decisions")
    app.run(host="0.0.0.0", port=8421, debug=False)
