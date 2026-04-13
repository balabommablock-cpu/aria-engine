#!/usr/bin/env python3
"""
aria-dashboard.py v3 -- ARIA Engine Dashboard

Real-time monitoring dashboard for the autonomous engine.
Auto-refreshes via AJAX every 10s. No page reload flicker.
"""

import json, os, sys, argparse
from datetime import datetime, timezone, timedelta
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from collections import defaultdict

WORKSPACE = Path(os.environ.get("ARIA_WORKSPACE",
    os.path.expanduser("~/.openclaw/agents/aria/workspace")))
PORT = 28889


def load_jsonl(path):
    if not path.exists():
        return []
    items = []
    with open(path) as f:
        for line in f:
            try: items.append(json.loads(line.strip()))
            except: pass
    return items


def load_json(path):
    if not path.exists():
        return {}
    with open(path) as f:
        return json.load(f)


def tail_file(path, n=20):
    if not path.exists():
        return []
    with open(path) as f:
        lines = f.readlines()
    return [l.rstrip() for l in lines[-n:]]


def get_dashboard_data():
    voice = load_json(WORKSPACE / "voice.json")
    queue = load_jsonl(WORKSPACE / "memory" / "queue.jsonl")
    posted = load_jsonl(WORKSPACE / "memory" / "posted.jsonl")
    signals = load_jsonl(WORKSPACE / "memory" / "signals.jsonl")
    followers = load_jsonl(WORKSPACE / "memory" / "followers.jsonl")
    engagements = load_jsonl(WORKSPACE / "memory" / "engagements.jsonl")
    state = load_json(WORKSPACE / "memory" / "engine_state.json")

    queued = [c for c in queue if c.get("status") == "queued"]
    live = [p for p in posted if p.get("status") == "live"]

    # Territory stats
    territory_stats = defaultdict(lambda: {"count": 0, "total_algo": 0, "total_imp": 0})
    for p in live:
        t = p.get("territory", "unknown")
        m = p.get("metrics", {})
        territory_stats[t]["count"] += 1
        if m.get("impressions") is not None:
            algo = (m.get("likes",0)*1 + m.get("retweets",0)*20 +
                    m.get("replies",0)*13.5 + m.get("bookmarks",0)*10)
            territory_stats[t]["total_algo"] += algo
            territory_stats[t]["total_imp"] += m.get("impressions", 0)

    # Top tweets
    scored_tweets = []
    for p in live:
        m = p.get("metrics", {})
        algo = 0
        if m.get("impressions") is not None:
            algo = (m.get("likes",0)*1 + m.get("retweets",0)*20 +
                    m.get("replies",0)*13.5 + m.get("bookmarks",0)*10)
        scored_tweets.append({
            "text": p["text"],
            "algo_score": algo,
            "territory": p.get("territory", "?"),
            "impressions": m.get("impressions", 0),
            "replies": m.get("replies", 0),
            "bookmarks": m.get("bookmarks", 0),
            "posted_at": p.get("posted_at", ""),
            "tweet_url": p.get("tweet_url", ""),
            "self_replied": p.get("self_replied", False)
        })
    scored_tweets.sort(key=lambda t: t["algo_score"], reverse=True)

    # Engine log tail
    log_lines = tail_file(WORKSPACE / "logs" / "engine.log", 30)

    # Next posting window
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        from backports.zoneinfo import ZoneInfo
    now_ist = datetime.now(ZoneInfo("Asia/Kolkata"))
    current_time = now_ist.strftime("%H:%M")
    windows = voice.get("timing", {}).get("windows_ist", [])
    current_window = None
    next_window = None
    for w in windows:
        if w["start"] <= current_time <= w["end"]:
            current_window = w
            break
    if not current_window:
        future = [w for w in windows if w["start"] > current_time]
        next_window = future[0] if future else (windows[0] if windows else None)

    # CDP health
    cdp_alive = False
    try:
        from urllib import request as urllib_request
        req = urllib_request.Request("http://127.0.0.1:28800/json/version")
        with urllib_request.urlopen(req, timeout=2):
            cdp_alive = True
    except:
        pass

    # Ollama health
    ollama_alive = False
    try:
        from urllib import request as urllib_request
        req = urllib_request.Request("http://127.0.0.1:11434/api/tags")
        with urllib_request.urlopen(req, timeout=2):
            ollama_alive = True
    except:
        pass

    return {
        "voice_version": voice.get("version", "?"),
        "golden_count": len(voice.get("golden_tweets", [])),
        "territory_weights": voice.get("territory_weights", {}),
        "queue_count": len(queued),
        "queue_top": sorted(queued, key=lambda c: c.get("scores",{}).get("composite",0), reverse=True)[:8],
        "posted_count": len(live),
        "territory_stats": dict(territory_stats),
        "top_tweets": scored_tweets[:10],
        "follower_history": followers[-30:],
        "signal_count": len(signals),
        "engagement_count": len(engagements),
        "updated_at": voice.get("updated_at", "never"),
        "engine_state": state,
        "log_tail": log_lines,
        "ist_time": now_ist.strftime("%H:%M:%S"),
        "ist_date": now_ist.strftime("%Y-%m-%d"),
        "current_window": current_window,
        "next_window": next_window,
        "cdp_alive": cdp_alive,
        "ollama_alive": ollama_alive,
        "total_posted": state.get("total_posted", len(live)),
        "last_post_at": state.get("last_post_at"),
        "last_error": state.get("last_error"),
        "last_error_at": state.get("last_error_at"),
    }


def build_html():
    return """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<title>ARIA Engine</title>
<style>
:root {
  --bg: #0d1117;
  --surface: #161b22;
  --surface2: #1c2333;
  --border: #30363d;
  --text: #e6edf3;
  --text2: #8b949e;
  --text3: #484f58;
  --green: #3fb950;
  --green-dim: #238636;
  --red: #f85149;
  --orange: #d29922;
  --blue: #58a6ff;
  --purple: #bc8cff;
  --cyan: #39d2c0;
  --font: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  --mono: 'SF Mono', 'Fira Code', 'Cascadia Code', monospace;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: var(--font); background: var(--bg); color: var(--text); padding: 20px 28px; min-height: 100vh; }

/* Header */
.header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; padding-bottom: 16px; border-bottom: 1px solid var(--border); }
.header h1 { font-size: 18px; font-weight: 600; letter-spacing: -0.3px; }
.header h1 span { color: var(--green); }
.header-right { display: flex; align-items: center; gap: 16px; font-size: 12px; color: var(--text2); }
.clock { font-family: var(--mono); font-size: 13px; color: var(--text); }

/* Health dots */
.health { display: flex; gap: 12px; align-items: center; }
.health-dot { display: flex; align-items: center; gap: 5px; font-size: 11px; color: var(--text2); }
.dot { width: 7px; height: 7px; border-radius: 50%; }
.dot.up { background: var(--green); box-shadow: 0 0 6px var(--green); }
.dot.down { background: var(--red); box-shadow: 0 0 6px var(--red); }

/* Stats row */
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 10px; margin-bottom: 20px; }
.stat { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 14px 16px; }
.stat-val { font-size: 26px; font-weight: 600; font-family: var(--mono); }
.stat-label { font-size: 10px; color: var(--text2); text-transform: uppercase; letter-spacing: 0.5px; margin-top: 3px; }

/* Window indicator */
.window-bar { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 12px 16px; margin-bottom: 20px; display: flex; align-items: center; gap: 12px; font-size: 13px; }
.window-bar .label { color: var(--text2); font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; }
.window-badge { padding: 3px 10px; border-radius: 12px; font-size: 11px; font-weight: 500; }
.window-badge.active { background: rgba(63,185,80,0.15); color: var(--green); border: 1px solid var(--green-dim); }
.window-badge.waiting { background: rgba(210,153,34,0.15); color: var(--orange); border: 1px solid rgba(210,153,34,0.3); }

/* Grid layout */
.grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 20px; }
@media (max-width: 900px) { .grid { grid-template-columns: 1fr; } }
.full-width { grid-column: 1 / -1; }

/* Panels */
.panel { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; overflow: hidden; }
.panel-header { padding: 12px 16px; border-bottom: 1px solid var(--border); display: flex; justify-content: space-between; align-items: center; }
.panel-title { font-size: 12px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; color: var(--text2); }
.panel-badge { font-size: 10px; padding: 2px 8px; border-radius: 10px; background: var(--surface2); color: var(--text2); }
.panel-body { padding: 0; }

/* Tables */
table { width: 100%; border-collapse: collapse; font-size: 12px; }
th { text-align: left; padding: 8px 12px; color: var(--text3); font-size: 10px; font-weight: 500; text-transform: uppercase; letter-spacing: 0.5px; border-bottom: 1px solid var(--border); }
td { padding: 10px 12px; border-bottom: 1px solid rgba(48,54,61,0.5); color: var(--text); vertical-align: top; }
tr:last-child td { border-bottom: none; }
tr:hover td { background: rgba(88,166,255,0.03); }
.tweet-text { max-width: 500px; line-height: 1.45; }
.territory-tag { font-size: 10px; padding: 2px 8px; border-radius: 10px; font-weight: 500; }
.t-building { background: rgba(63,185,80,0.12); color: var(--green); }
.t-organizations { background: rgba(88,166,255,0.12); color: var(--blue); }
.t-ai { background: rgba(188,140,255,0.12); color: var(--purple); }
.t-taste_agency { background: rgba(57,210,192,0.12); color: var(--cyan); }
.score { font-family: var(--mono); font-size: 12px; font-weight: 500; }
.empty-state { padding: 24px; text-align: center; color: var(--text3); font-size: 12px; }

/* Log viewer */
.log-viewer { padding: 12px 16px; max-height: 280px; overflow-y: auto; font-family: var(--mono); font-size: 11px; line-height: 1.6; color: var(--text2); white-space: pre-wrap; word-break: break-all; }
.log-viewer .log-phase { color: var(--blue); }
.log-viewer .log-ok { color: var(--green); }
.log-viewer .log-err { color: var(--red); }
.log-viewer .log-skip { color: var(--text3); }
.log-viewer .log-time { color: var(--text3); }

/* Footer */
.footer { text-align: center; padding: 16px; font-size: 11px; color: var(--text3); }

/* Pulse animation for live indicator */
@keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.4; } }
.live-dot { animation: pulse 2s ease-in-out infinite; }
</style>
</head>
<body>

<div class="header">
  <h1>ARIA <span>Engine</span></h1>
  <div class="header-right">
    <div class="health" id="health"></div>
    <div class="clock" id="clock"></div>
  </div>
</div>

<div class="stats" id="stats"></div>
<div id="window-bar"></div>
<div class="grid" id="main-grid"></div>
<div class="footer" id="footer"></div>

<script>
const TERRITORY_COLORS = {building:'green',organizations:'blue',ai:'purple',taste_agency:'cyan'};

function territoryTag(t) {
  const cls = TERRITORY_COLORS[t] || 'green';
  return '<span class="territory-tag t-'+t+'">' + (t||'?') + '</span>';
}

function timeAgo(isoStr) {
  if (!isoStr) return 'never';
  const d = new Date(isoStr.replace('Z','+00:00'));
  const mins = Math.floor((Date.now() - d.getTime()) / 60000);
  if (mins < 1) return 'just now';
  if (mins < 60) return mins + 'm ago';
  const hrs = Math.floor(mins/60);
  if (hrs < 24) return hrs + 'h ' + (mins%60) + 'm ago';
  return Math.floor(hrs/24) + 'd ago';
}

function colorLog(line) {
  line = line.replace(/\\[([\\d-]+ [\\d:]+)\\]/g, '<span class="log-time">[$1]</span>');
  if (line.includes('PHASE')) return '<span class="log-phase">' + line + '</span>';
  if (line.includes('POSTED') || line.includes('alive') || line.includes('queued') || line.includes('saved'))
    return '<span class="log-ok">' + line + '</span>';
  if (line.includes('FAILED') || line.includes('ERROR') || line.includes('error'))
    return '<span class="log-err">' + line + '</span>';
  if (line.includes('skip') || line.includes('outside'))
    return '<span class="log-skip">' + line + '</span>';
  return line;
}

function render(d) {
  // Health
  document.getElementById('health').innerHTML =
    '<div class="health-dot"><div class="dot ' + (d.cdp_alive?'up':'down') + '"></div>CDP</div>' +
    '<div class="health-dot"><div class="dot ' + (d.ollama_alive?'up':'down') + '"></div>Gemma</div>' +
    '<div class="health-dot"><div class="dot up live-dot"></div>Engine</div>';

  // Clock
  document.getElementById('clock').textContent = d.ist_time + ' IST';

  // Stats
  const lastPost = d.last_post_at ? timeAgo(d.last_post_at) : 'never';
  document.getElementById('stats').innerHTML =
    stat(d.posted_count, 'posted', '--green') +
    stat(d.queue_count, 'in queue', '--blue') +
    stat(d.signal_count, 'signals', '--purple') +
    stat(d.engagement_count, 'engagements', '--cyan') +
    stat(d.golden_count, 'golden tweets', '--orange') +
    stat(d.follower_history.length ? d.follower_history[d.follower_history.length-1].count : '?', 'followers', '--text') +
    stat(lastPost, 'last post', '--text2');

  // Window bar
  const wb = document.getElementById('window-bar');
  if (d.current_window) {
    wb.innerHTML = '<div class="window-bar"><span class="label">posting window</span>' +
      '<span class="window-badge active">' + d.current_window.name + ' (' + d.current_window.start + ' - ' + d.current_window.end + ')</span>' +
      '<span style="color:var(--green);font-size:12px">ready to post</span></div>';
  } else if (d.next_window) {
    wb.innerHTML = '<div class="window-bar"><span class="label">posting window</span>' +
      '<span class="window-badge waiting">next: ' + d.next_window.name + ' at ' + d.next_window.start + '</span>' +
      '<span style="color:var(--text3);font-size:12px">waiting</span></div>';
  }

  // Main grid
  let html = '';

  // Queue panel
  html += '<div class="panel"><div class="panel-header"><span class="panel-title">Queue</span><span class="panel-badge">' + d.queue_count + ' candidates</span></div><div class="panel-body">';
  if (d.queue_top.length) {
    html += '<table><tr><th>tweet</th><th>score</th><th>territory</th></tr>';
    d.queue_top.forEach(c => {
      const sc = c.scores || {};
      html += '<tr><td class="tweet-text">' + esc(c.text||'') + '</td>' +
        '<td class="score">' + (sc.composite||'?') + '</td>' +
        '<td>' + territoryTag(c.territory) + '</td></tr>';
    });
    html += '</table>';
  } else {
    html += '<div class="empty-state">queue empty. engine will generate next cycle.</div>';
  }
  html += '</div></div>';

  // Engine log
  html += '<div class="panel"><div class="panel-header"><span class="panel-title">Engine Log</span><span class="panel-badge">live</span></div>';
  html += '<div class="log-viewer" id="log-viewer">';
  if (d.log_tail.length) {
    html += d.log_tail.map(l => colorLog(esc(l))).join('\\n');
  } else {
    html += 'no log entries yet';
  }
  html += '</div></div>';

  // Posted tweets (full width)
  html += '<div class="panel full-width"><div class="panel-header"><span class="panel-title">Posted Tweets</span><span class="panel-badge">' + d.posted_count + ' live</span></div><div class="panel-body">';
  if (d.top_tweets.length) {
    html += '<table><tr><th>tweet</th><th>territory</th><th>impressions</th><th>replies</th><th>bookmarks</th><th>self-reply</th><th>posted</th></tr>';
    d.top_tweets.forEach(t => {
      const url = t.tweet_url && !t.tweet_url.startsWith('posted') ? t.tweet_url : '';
      const textHtml = url ? '<a href="'+esc(url)+'" target="_blank" style="color:var(--text);text-decoration:none;border-bottom:1px dashed var(--border)">'+esc(t.text)+'</a>' : esc(t.text);
      html += '<tr><td class="tweet-text">' + textHtml + '</td>' +
        '<td>' + territoryTag(t.territory) + '</td>' +
        '<td class="score">' + (t.impressions||'-') + '</td>' +
        '<td class="score">' + (t.replies||'-') + '</td>' +
        '<td class="score">' + (t.bookmarks||'-') + '</td>' +
        '<td>' + (t.self_replied?'<span style="color:var(--green)">yes</span>':'<span style="color:var(--text3)">no</span>') + '</td>' +
        '<td style="color:var(--text2);white-space:nowrap">' + timeAgo(t.posted_at) + '</td></tr>';
    });
    html += '</table>';
  } else {
    html += '<div class="empty-state">no posts yet. first post goes out at next window.</div>';
  }
  html += '</div></div>';

  // Territory performance (full width)
  const tw = d.territory_weights || {};
  const ts = d.territory_stats || {};
  const territories = [...new Set([...Object.keys(tw), ...Object.keys(ts)])];
  if (territories.length) {
    html += '<div class="panel full-width"><div class="panel-header"><span class="panel-title">Territory Performance</span></div><div class="panel-body">';
    html += '<table><tr><th>territory</th><th>weight</th><th>posts</th><th>avg algo</th><th>impressions</th></tr>';
    territories.forEach(t => {
      const s = ts[t] || {count:0, total_algo:0, total_imp:0};
      const avg = s.count ? Math.round(s.total_algo/s.count) : '-';
      html += '<tr><td>' + territoryTag(t) + '</td>' +
        '<td class="score">' + (tw[t]||0).toFixed(2) + '</td>' +
        '<td class="score">' + s.count + '</td>' +
        '<td class="score">' + avg + '</td>' +
        '<td class="score">' + (s.total_imp||'-') + '</td></tr>';
    });
    html += '</table></div></div>';
  }

  document.getElementById('main-grid').innerHTML = html;

  // Footer
  const err = d.last_error ? '<span style="color:var(--red)">last error: ' + esc(d.last_error).substring(0,80) + ' (' + timeAgo(d.last_error_at) + ')</span> | ' : '';
  document.getElementById('footer').innerHTML = err +
    'v' + d.voice_version + ' | claude opus brain | gemma muscle | refreshes every 10s | ' + d.ist_date;

  // Auto-scroll log
  const lv = document.getElementById('log-viewer');
  if (lv) lv.scrollTop = lv.scrollHeight;
}

function stat(val, label, colorVar) {
  return '<div class="stat"><div class="stat-val" style="color:var('+colorVar+')">' + val + '</div><div class="stat-label">' + label + '</div></div>';
}

function esc(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

async function refresh() {
  try {
    const r = await fetch('/api/data');
    const d = await r.json();
    render(d);
  } catch(e) {
    console.error('refresh failed:', e);
  }
}

refresh();
setInterval(refresh, 10000);
</script>
</body></html>"""



def build_arch_html():
    return """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<title>ARIA Engine</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0d1117; color: #e6edf3; }
.p { max-width: 880px; margin: 0 auto; padding: 56px 28px 40px; }

/* Hero */
.hero { text-align: center; margin-bottom: 64px; }
.hero h1 { font-size: 42px; font-weight: 700; letter-spacing: -1.5px; margin-bottom: 12px; }
.hero h1 em { font-style: normal; color: #3fb950; }
.hero p { font-size: 16px; color: #8b949e; line-height: 1.7; max-width: 560px; margin: 0 auto 24px; }
.pills { display: flex; justify-content: center; gap: 8px; flex-wrap: wrap; }
.pill { font-size: 11px; padding: 5px 14px; border-radius: 20px; font-weight: 500; }
.pg { background: rgba(63,185,80,.12); color: #3fb950; border: 1px solid rgba(63,185,80,.25); }
.pb { background: rgba(88,166,255,.12); color: #58a6ff; border: 1px solid rgba(88,166,255,.25); }
.pp { background: rgba(188,140,255,.12); color: #bc8cff; border: 1px solid rgba(188,140,255,.25); }

/* Section titles */
.st { font-size: 10px; text-transform: uppercase; letter-spacing: 2px; color: #3fb950; font-weight: 600; margin-bottom: 6px; }
.sh { font-size: 24px; font-weight: 600; letter-spacing: -0.5px; margin-bottom: 8px; }
.sp { font-size: 14px; color: #8b949e; line-height: 1.6; margin-bottom: 32px; max-width: 640px; }
.sec { margin-bottom: 64px; }

/* Pipeline steps */
.pipe { display: flex; flex-direction: column; gap: 0; }
.step { display: grid; grid-template-columns: 56px 1fr; gap: 0; }
.step-num { display: flex; flex-direction: column; align-items: center; }
.num { width: 36px; height: 36px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 14px; font-weight: 600; font-family: 'SF Mono', monospace; flex-shrink: 0; }
.line { width: 2px; flex: 1; min-height: 20px; }
.step-body { background: #161b22; border: 1px solid #30363d; border-radius: 10px; padding: 18px 22px; margin-bottom: 12px; }
.step-body h3 { font-size: 14px; font-weight: 600; margin-bottom: 4px; }
.step-body p { font-size: 13px; color: #8b949e; line-height: 1.55; }
.step-body .who { font-size: 11px; margin-top: 8px; padding: 3px 10px; border-radius: 8px; display: inline-block; font-weight: 500; }

.c-green .num { background: rgba(63,185,80,.15); color: #3fb950; border: 1px solid rgba(63,185,80,.3); }
.c-green .line { background: rgba(63,185,80,.2); }
.c-green h3 { color: #3fb950; }
.c-blue .num { background: rgba(88,166,255,.15); color: #58a6ff; border: 1px solid rgba(88,166,255,.3); }
.c-blue .line { background: rgba(88,166,255,.2); }
.c-blue h3 { color: #58a6ff; }
.c-purple .num { background: rgba(188,140,255,.15); color: #bc8cff; border: 1px solid rgba(188,140,255,.3); }
.c-purple .line { background: rgba(188,140,255,.2); }
.c-purple h3 { color: #bc8cff; }
.c-cyan .num { background: rgba(57,210,192,.15); color: #39d2c0; border: 1px solid rgba(57,210,192,.3); }
.c-cyan .line { background: rgba(57,210,192,.2); }
.c-cyan h3 { color: #39d2c0; }
.c-orange .num { background: rgba(210,153,34,.15); color: #d29922; border: 1px solid rgba(210,153,34,.3); }
.c-orange .line { background: rgba(210,153,34,.2); }
.c-orange h3 { color: #d29922; }
.c-pink .num { background: rgba(247,120,186,.15); color: #f778ba; border: 1px solid rgba(247,120,186,.3); }
.c-pink .line { background: rgba(247,120,186,.2); }
.c-pink h3 { color: #f778ba; }
.w-purple { background: rgba(188,140,255,.1); color: #bc8cff; }
.w-orange { background: rgba(210,153,34,.1); color: #d29922; }
.w-green { background: rgba(63,185,80,.1); color: #3fb950; }

/* 3 columns */
.cols3 { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 14px; margin-bottom: 20px; }
.card { background: #161b22; border: 1px solid #30363d; border-radius: 10px; padding: 24px 20px; text-align: center; }
.card-icon { font-size: 28px; margin-bottom: 10px; }
.card h3 { font-size: 15px; font-weight: 600; margin-bottom: 2px; }
.card .sub { font-size: 10px; text-transform: uppercase; letter-spacing: 1px; color: #484f58; margin-bottom: 14px; }
.card ul { list-style: none; text-align: left; font-size: 12px; color: #8b949e; }
.card li { padding: 5px 0; border-bottom: 1px solid rgba(48,54,61,.5); }
.card li:last-child { border: none; }

/* Stats */
.stats4 { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; }
.stat { background: #161b22; border: 1px solid #30363d; border-radius: 10px; padding: 18px; text-align: center; }
.sv { font-size: 28px; font-weight: 600; font-family: 'SF Mono', monospace; }
.sl { font-size: 10px; color: #8b949e; text-transform: uppercase; letter-spacing: .5px; margin-top: 4px; }

/* Footer */
.foot { text-align: center; padding: 24px 0; margin-top: 40px; border-top: 1px solid #30363d; }
.foot a { color: #3fb950; text-decoration: none; font-size: 13px; }
</style>
</head>
<body>
<div class="p">

<!-- HERO -->
<div class="hero">
  <h1>ARIA <em>Engine</em></h1>
  <p>A fully autonomous content engine for X. It researches trends, writes original tweets in a consistent voice, posts them, replies to itself for algorithmic boost, tracks performance, and self-heals when things break. No human touches the keyboard after deployment.</p>
  <div class="pills">
    <span class="pill pg">zero intervention</span>
    <span class="pill pb">claude opus writes everything</span>
    <span class="pill pp">anti-bot detection built in</span>
  </div>
</div>

<!-- HOW IT WORKS -->
<div class="sec">
  <div class="st">How it works</div>
  <div class="sh">One engine. Six steps. Every 20 minutes.</div>
  <p class="sp">A single script wakes up every 20 minutes. It walks through six steps in order. Each step checks if there's work to do. If not, it skips instantly. If yes, it acts.</p>

  <div class="pipe">

    <div class="step c-green">
      <div class="step-num"><div class="num">1</div><div class="line"></div></div>
      <div class="step-body">
        <h3>Research</h3>
        <p>Reads 8 RSS feeds (Anthropic, OpenAI, Hacker News, Lenny's Newsletter, Jason Fried, etc.) for what the tech world is talking about right now. This gives tweets topical awareness without being reactions.</p>
      </div>
    </div>

    <div class="step c-blue">
      <div class="step-num"><div class="num">2</div><div class="line"></div></div>
      <div class="step-body">
        <h3>Write</h3>
        <p>If less than 3 tweets are ready in the queue, asks Claude Opus to write 4 new ones across different topic territories (building, organizations, AI, taste). Claude also rates each tweet on how likely it is to get replies, bookmarks, and stop the scroll.</p>
        <span class="who w-purple">Claude Opus</span>
      </div>
    </div>

    <div class="step c-purple">
      <div class="step-num"><div class="num">3</div><div class="line"></div></div>
      <div class="step-body">
        <h3>Post</h3>
        <p>Picks the highest-scored tweet from the queue. Checks three gates: is it the right time of day in India? Has it been at least 2 hours since the last post? Is the queue not empty? If all pass, it posts via browser automation.</p>
        <span class="who w-green">CDP Browser</span>
      </div>
    </div>

    <div class="step c-cyan">
      <div class="step-num"><div class="num">4</div><div class="line"></div></div>
      <div class="step-body">
        <h3>Self-Reply</h3>
        <p>Within 2-5 minutes of posting, Claude writes a second-angle reply to the original tweet. This triggers X's algorithm to boost the post (the "author replied" signal is weighted 150x by X's ranking system).</p>
        <span class="who w-purple">Claude Opus</span>
      </div>
    </div>

    <div class="step c-orange">
      <div class="step-num"><div class="num">5</div><div class="line"></div></div>
      <div class="step-body">
        <h3>Measure</h3>
        <p>Every 4 hours, navigates to the X profile and reads impressions, likes, replies, retweets, and bookmarks for each live tweet. This data feeds back into the system so it learns what works.</p>
        <span class="who w-green">CDP Browser</span>
      </div>
    </div>

    <div class="step c-pink">
      <div class="step-num"><div class="num">6</div><div class="line"></div></div>
      <div class="step-body">
        <h3>Self-Heal</h3>
        <p>Every cycle checks if the browser, the AI models, and the posting system are alive. If something is down, it tries to restart it. If 3 posts fail in a row, it sends a Telegram alert. Logs auto-rotate. Stale content auto-expires.</p>
      </div>
    </div>

  </div>
</div>

<!-- THREE LAYERS -->
<div class="sec">
  <div class="st">Architecture</div>
  <div class="sh">Brain. Muscle. Hands.</div>
  <p class="sp">Three layers with strict separation. The brain never touches the browser. The hands never make creative decisions.</p>

  <div class="cols3">
    <div class="card" style="border-color: rgba(188,140,255,.3)">
      <div class="card-icon" style="color:#bc8cff">&#9672;</div>
      <h3 style="color:#bc8cff">Claude Opus</h3>
      <div class="sub">The Brain</div>
      <ul>
        <li>Writes all tweet content</li>
        <li>Scores quality and virality</li>
        <li>Generates self-replies in voice</li>
        <li>All creative decisions</li>
      </ul>
    </div>
    <div class="card" style="border-color: rgba(210,153,34,.3)">
      <div class="card-icon" style="color:#d29922">&#9881;</div>
      <h3 style="color:#d29922">Gemma 4</h3>
      <div class="sub">The Muscle</div>
      <ul>
        <li>Runs locally, not cloud</li>
        <li>Backup numeric scoring</li>
        <li>Zero creative authority</li>
        <li>Fast, cheap, disposable</li>
      </ul>
    </div>
    <div class="card" style="border-color: rgba(63,185,80,.3)">
      <div class="card-icon" style="color:#3fb950">&#9741;</div>
      <h3 style="color:#3fb950">CDP Chrome</h3>
      <div class="sub">The Hands</div>
      <ul>
        <li>Posts tweets via browser</li>
        <li>Scrapes own analytics</li>
        <li>Posts self-replies</li>
        <li>No decision-making</li>
      </ul>
    </div>
  </div>
</div>

<!-- VOICE -->
<div class="sec">
  <div class="st">Voice Control</div>
  <div class="sh">11 golden tweets define the entire personality.</div>
  <p class="sp">Every generated tweet is measured against 11 hand-picked examples that define tone, structure, and energy. 25+ words are permanently banned to prevent AI-sounding output (no "delve", no "landscape", no hashtags, forced lowercase). Four content territories are weighted to control the topic mix.</p>

  <div class="stats4">
    <div class="stat"><div class="sv" style="color:#3fb950">4</div><div class="sl">territories</div></div>
    <div class="stat"><div class="sv" style="color:#58a6ff">11</div><div class="sl">golden tweets</div></div>
    <div class="stat"><div class="sv" style="color:#bc8cff">25+</div><div class="sl">banned words</div></div>
    <div class="stat"><div class="sv" style="color:#39d2c0">280</div><div class="sl">max chars</div></div>
  </div>
</div>

<!-- SAFETY -->
<div class="sec">
  <div class="st">Safety</div>
  <div class="sh">Designed to be undetectable as automated.</div>
  <p class="sp">Every action has randomized timing. No two cycles produce the same pattern. The system posts 4-6 tweets per day during natural Indian hours, with human-like pauses before every browser interaction.</p>

  <div class="stats4">
    <div class="stat"><div class="sv" style="color:#d29922">30s-5m</div><div class="sl">startup jitter</div></div>
    <div class="stat"><div class="sv" style="color:#d29922">30s-3m</div><div class="sl">pre-post delay</div></div>
    <div class="stat"><div class="sv" style="color:#d29922">2h+</div><div class="sl">min gap</div></div>
    <div class="stat"><div class="sv" style="color:#d29922">4-6</div><div class="sl">posts / day</div></div>
  </div>
</div>

<!-- NUMBERS -->
<div class="sec">
  <div class="st">By the numbers</div>
  <div class="sh">The full system.</div>
  <div class="stats4" style="margin-top:20px">
    <div class="stat"><div class="sv" style="color:#3fb950">1</div><div class="sl">script</div></div>
    <div class="stat"><div class="sv" style="color:#58a6ff">1</div><div class="sl">cron job</div></div>
    <div class="stat"><div class="sv" style="color:#bc8cff">6</div><div class="sl">phases / cycle</div></div>
    <div class="stat"><div class="sv" style="color:#39d2c0">~90s</div><div class="sl">to write 4 tweets</div></div>
  </div>
</div>

<div class="foot">
  <a href="/">View Live Dashboard &rarr;</a>
</div>

</div>
</body></html>"""


class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            html = build_html()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode())
        elif self.path == "/arch" or self.path == "/architecture":
            html = build_arch_html()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode())
        elif self.path == "/api/data":
            data = get_dashboard_data()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(json.dumps(data, default=str).encode())
        else:
            self.send_error(404)

    def log_message(self, format, *args):
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()

    print(f"ARIA dashboard: http://localhost:{args.port}")
    server = HTTPServer(("127.0.0.1", args.port), DashboardHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
