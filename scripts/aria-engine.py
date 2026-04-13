#!/usr/bin/env python3
"""
aria-engine.py v3 -- Single autonomous orchestrator

One script. One cron. Runs every 20 min.
Each cycle decides what needs doing and does it.

Pipeline per cycle:
  1. SIGNALS   -- refresh if stale (>3h)
  2. GENERATE  -- if queue < 3, generate via CLAUDE (brain)
  3. POST      -- if in window + gap ok, post top candidate
  4. ENGAGE    -- self-reply + reply-back on recent posts
  5. METRICS   -- scrape own analytics if >4h stale
  6. HEAL      -- check CDP alive, restart if dead

Claude = brain (content, strategy, voice).
Gemma  = muscle (quick scoring only, optional).
CDP    = hands (posting, scraping X).
"""

import json, os, sys, re, random, time, subprocess, hashlib, traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib import request as urllib_request
from xml.etree import ElementTree
from collections import Counter

# ============================================================
# CONFIG
# ============================================================

WORKSPACE = Path(os.environ.get("ARIA_WORKSPACE",
    os.path.expanduser("~/.openclaw/agents/aria/workspace")))

VOICE_PATH     = WORKSPACE / "voice.json"
QUEUE_PATH     = WORKSPACE / "memory" / "queue.jsonl"
POSTED_PATH    = WORKSPACE / "memory" / "posted.jsonl"
SIGNALS_PATH   = WORKSPACE / "memory" / "signals.jsonl"
ENGAGE_PATH    = WORKSPACE / "memory" / "engagements.jsonl"
FOLLOWERS_PATH = WORKSPACE / "memory" / "followers.jsonl"
STATE_PATH     = WORKSPACE / "memory" / "engine_state.json"
LOG_PATH       = WORKSPACE / "logs" / "engine.log"

POST_TWEET_JS  = Path(os.path.expanduser(
    "~/.openclaw/workspace/skills/x-twitter-poster/post_tweet.js"))

CLAUDE_CLI     = os.environ.get("CLAUDE_CLI",
    os.path.expanduser("~/.local/bin/claude"))

CDP_URL        = os.environ.get("CDP_URL", "http://127.0.0.1:28800")
CDP_PORT       = 28800
X_USERNAME     = os.environ.get("X_USERNAME", "BalabommaRao")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN",
    "8794849679:AAGUiW5aIKeGzChVeSOMzpMFeqw0g-gGqII")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "7353580848")

# Gemma for quick scoring only (muscle)
OLLAMA_BASE  = os.environ.get("OLLAMA_BASE", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "gemma4:26b")

# Thresholds
SIGNAL_STALE_HOURS   = 3
METRICS_STALE_HOURS  = 4
MIN_QUEUE_SIZE       = 3
GENERATE_BATCH       = 8
SELF_REPLY_WINDOW    = 12   # minutes
REPLY_BACK_WINDOW    = 60   # minutes
MAX_AUTO_REPLIES     = 3

DRY_RUN = "--dry-run" in sys.argv

# RSS feeds
RSS_FEEDS = [
    {"url": "https://blog.anthropic.com/rss.xml", "territory": "ai", "name": "Anthropic"},
    {"url": "https://openai.com/blog/rss.xml", "territory": "ai", "name": "OpenAI"},
    {"url": "https://blog.google/technology/ai/rss/", "territory": "ai", "name": "Google AI"},
    {"url": "https://lilianweng.github.io/index.xml", "territory": "ai", "name": "Lilian Weng"},
    {"url": "https://www.svpg.com/feed/", "territory": "organizations", "name": "SVPG"},
    {"url": "https://world.hey.com/jason/feed.atom", "territory": "taste_agency", "name": "Jason Fried"},
    {"url": "https://www.lennysnewsletter.com/feed", "territory": "organizations", "name": "Lenny"},
    {"url": "https://hnrss.org/frontpage?count=10", "territory": "building", "name": "HN Front"},
]


# ============================================================
# UTILS
# ============================================================

def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")


def load_voice():
    with open(VOICE_PATH) as f:
        return json.load(f)


def load_jsonl(path):
    if not path.exists():
        return []
    items = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    items.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return items


def save_jsonl(path, items):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        for item in items:
            f.write(json.dumps(item) + "\n")


def append_jsonl(path, item):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(item) + "\n")


def load_state():
    if STATE_PATH.exists():
        with open(STATE_PATH) as f:
            return json.load(f)
    return {}


def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)


def send_telegram(message):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        body = json.dumps({"chat_id": TELEGRAM_CHAT_ID, "text": message,
                           "parse_mode": "HTML"}).encode()
        req = urllib_request.Request(url, data=body,
                                    headers={"Content-Type": "application/json"})
        with urllib_request.urlopen(req, timeout=10):
            pass
    except Exception as e:
        log(f"telegram error: {e}")


def parse_ts(s):
    """Parse ISO timestamp, handling Z suffix."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except:
        return None


def now_utc():
    return datetime.now(timezone.utc)


def now_ist():
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        from backports.zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("Asia/Kolkata"))


# ============================================================
# CLAUDE BRAIN -- all content generation goes through here
# ============================================================

def call_claude(prompt, max_tokens=400):
    """Call Claude via CLI. Brain-tier work only."""
    try:
        proc = subprocess.run(
            [CLAUDE_CLI, "-p", "--model", "opus"],
            input=prompt,
            capture_output=True, text=True, timeout=120
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
        else:
            log(f"claude error: rc={proc.returncode} err={proc.stderr[:200]}")
            return None
    except subprocess.TimeoutExpired:
        log("claude timeout (120s)")
        return None
    except Exception as e:
        log(f"claude call failed: {e}")
        return None


def call_gemma(prompt, temperature=0.3):
    """Call Gemma via Ollama. Muscle-tier work only (scoring)."""
    url = f"{OLLAMA_BASE}/v1/chat/completions"
    body = json.dumps({
        "model": OLLAMA_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": 100,
        "stream": False
    }).encode()
    req = urllib_request.Request(url, data=body,
                                headers={"Content-Type": "application/json"})
    try:
        with urllib_request.urlopen(req, timeout=45) as resp:
            data = json.loads(resp.read().decode())
            return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        log(f"gemma error: {e}")
        return None


# ============================================================
# PHASE 1: SIGNALS
# ============================================================

def phase_signals(state, voice):
    last_signal = parse_ts(state.get("last_signal_at"))
    if last_signal and (now_utc() - last_signal).total_seconds() / 3600 < SIGNAL_STALE_HOURS:
        log(f"signals: fresh ({(now_utc() - last_signal).total_seconds()/3600:.1f}h old), skip")
        return

    log("signals: refreshing RSS feeds...")
    existing = load_jsonl(SIGNALS_PATH)
    existing_ids = {s.get("id") for s in existing}
    new_signals = []

    for feed in RSS_FEEDS:
        try:
            req = urllib_request.Request(feed["url"],
                headers={"User-Agent": "Mozilla/5.0"})
            with urllib_request.urlopen(req, timeout=15) as resp:
                root = ElementTree.fromstring(resp.read())
        except Exception as e:
            log(f"  RSS error ({feed['name']}): {e}")
            continue

        items = root.findall(".//{http://www.w3.org/2005/Atom}entry") or \
                root.findall(".//item")
        count = 0
        for item in items[:5]:
            title_el = item.find("{http://www.w3.org/2005/Atom}title") or item.find("title")
            link_el  = item.find("{http://www.w3.org/2005/Atom}link") or item.find("link")

            title = title_el.text.strip() if title_el is not None and title_el.text else ""
            if link_el is not None:
                link = link_el.get("href", "") or (link_el.text or "").strip()
            else:
                link = ""

            if not title:
                continue

            sig_id = hashlib.md5(f"{feed['name']}:{title}".encode()).hexdigest()[:12]
            if sig_id in existing_ids:
                continue

            new_signals.append({
                "id": sig_id,
                "source": feed["name"],
                "territory": feed["territory"],
                "title": title,
                "url": link,
                "scraped_at": now_utc().isoformat()
            })
            existing_ids.add(sig_id)
            count += 1

        if count > 0:
            log(f"  {feed['name']}: {count} new")

    if new_signals:
        for s in new_signals:
            append_jsonl(SIGNALS_PATH, s)
        log(f"signals: {len(new_signals)} new signals saved")
    else:
        log("signals: no new signals")

    # Cleanup: cap signals at 200 (keep newest)
    all_signals = load_jsonl(SIGNALS_PATH)
    if len(all_signals) > 200:
        trimmed = all_signals[-200:]
        save_jsonl(SIGNALS_PATH, trimmed)
        log(f"signals: trimmed from {len(all_signals)} to 200")

    state["last_signal_at"] = now_utc().isoformat()


# ============================================================
# PHASE 2: GENERATE (Claude brain)
# ============================================================

def phase_generate(state, voice):
    queue = [c for c in load_jsonl(QUEUE_PATH) if c.get("status") == "queued"]

    # Expire stale candidates (>48h old)
    fresh_queue = []
    for c in queue:
        gen_ts = parse_ts(c.get("generated_at"))
        if gen_ts and (now_utc() - gen_ts).total_seconds() > 48 * 3600:
            log(f"generate: expired stale candidate \"{c.get('text','')[:40]}...\"")
        else:
            fresh_queue.append(c)
    if len(fresh_queue) != len(queue):
        save_jsonl(QUEUE_PATH, fresh_queue)
        queue = fresh_queue

    if len(queue) >= MIN_QUEUE_SIZE:
        log(f"generate: queue has {len(queue)} candidates, skip")
        return

    log(f"generate: queue low ({len(queue)}), generating via Claude batch...")

    # Gather context
    signals = load_jsonl(SIGNALS_PATH)[-20:]
    signal_context = "\n".join([f"- [{s['territory']}] {s['title']}" for s in signals[-10:]])

    posted = load_jsonl(POSTED_PATH)
    recent_texts = [p["text"] for p in posted[-10:]]
    existing_queue_texts = [c["text"] for c in queue]
    avoid_texts = recent_texts + existing_queue_texts
    avoid_context = "\n".join([f"- {t}" for t in avoid_texts]) if avoid_texts else "(none yet)"

    golden = voice["golden_tweets"]
    golden_text = "\n".join([f"- {g['text']}" for g in golden])

    territory_prompts = voice.get("territory_prompts", {})
    hard_bans = voice.get("hard_bans", {})
    structure = voice.get("structure_rules", {})

    ban_words = ", ".join(hard_bans.get("words", []))
    ban_phrases = ", ".join(hard_bans.get("phrases", []))

    # Pick 4 territories for this batch (weighted, ensure variety)
    weights = voice["territory_weights"]
    batch_territories = []
    for _ in range(4):
        t = random.choices(list(weights.keys()), list(weights.values()))[0]
        batch_territories.append(t)
    # Ensure at least 2 unique territories
    if len(set(batch_territories)) == 1:
        others = [t for t in weights.keys() if t != batch_territories[0]]
        if others:
            batch_territories[1] = random.choice(others)

    territory_directions = "\n".join([
        f"{i+1}. TERRITORY: {t}\n   DIRECTION: {territory_prompts.get(t, f'write about {t}')}"
        for i, t in enumerate(batch_territories)
    ])

    # BATCH GENERATE: one Claude call for multiple tweets + scoring
    prompt = f"""you are ghostwriting tweets for @BalabommaRao. match his voice exactly.

GOLDEN TWEETS (the gold standard):
{golden_text}

WRITE 4 TWEETS, one for each territory below:
{territory_directions}

RECENT SIGNALS (for topical awareness, don't quote directly):
{signal_context}

ALREADY POSTED OR QUEUED (don't repeat these angles):
{avoid_context}

STRUCTURE RULES:
- {structure.get('primary', 'redefine a familiar concept, reveal the uncomfortable implication')}
- max {structure.get('max_sentences', 2)} sentences, max {structure.get('max_chars', 280)} chars
- {structure.get('case', 'all lowercase')}
- {structure.get('punctuation', 'periods only')}

HARD BANS:
- words: {ban_words}
- phrases: {ban_phrases}
- no em dashes, no en dashes, no hashtags, no emojis, no exclamation marks, no hyphens as formatting

FOR EACH TWEET, also rate it on these 3 dimensions (1-10):
- reply_provocation: how likely someone replies to argue or add their take
- bookmark_worthy: would someone save this to think about later
- hook_strength: does the opening make you stop scrolling

RESPOND IN THIS EXACT FORMAT (4 blocks, nothing else):
---
territory: [territory name]
tweet: [the tweet text, no quotes]
reply_provocation: [1-10]
bookmark_worthy: [1-10]
hook_strength: [1-10]
---
territory: [territory name]
tweet: [the tweet text, no quotes]
reply_provocation: [1-10]
bookmark_worthy: [1-10]
hook_strength: [1-10]
---
(and so on for all 4)"""

    # Retry up to 2 times
    result = None
    for attempt in range(2):
        result = call_claude(prompt)
        if result:
            break
        log(f"generate: claude attempt {attempt+1} failed, retrying...")
        time.sleep(5)

    if not result:
        log("generate: claude failed after 2 attempts")
        return

    # Parse batch response
    candidates = []
    blocks = re.split(r'---+', result)
    algo = voice.get("algo_scoring", {})

    for block in blocks:
        block = block.strip()
        if not block:
            continue

        # Extract fields
        territory_m = re.search(r'territory:\s*(.+)', block, re.IGNORECASE)
        tweet_m = re.search(r'tweet:\s*(.+?)(?:\n|reply_provocation)', block, re.IGNORECASE | re.DOTALL)
        prov_m = re.search(r'reply_provocation:\s*(\d+)', block, re.IGNORECASE)
        book_m = re.search(r'bookmark_worthy:\s*(\d+)', block, re.IGNORECASE)
        hook_m = re.search(r'hook_strength:\s*(\d+)', block, re.IGNORECASE)

        if not tweet_m:
            continue

        territory = territory_m.group(1).strip() if territory_m else "building"
        text = tweet_m.group(1).strip().strip('"').strip("'")

        # Clean preamble
        for prefix in ["here's", "here is", "tweet:", "how about:"]:
            if text.lower().startswith(prefix):
                text = text[len(prefix):].strip().strip('"').strip("'").strip(':').strip()

        # Enforce hard bans
        skip = False
        for banned in hard_bans.get("characters", []):
            if banned in text:
                text = text.replace(banned, "")
        for banned in hard_bans.get("words", []):
            if banned.lower() in text.lower():
                skip = True
                break
        for banned in hard_bans.get("phrases", []):
            if banned.lower() in text.lower():
                skip = True
                break
        if skip:
            log(f"  banned content found, skip")
            continue

        # Enforce lowercase
        if structure.get("case", "").startswith("all lowercase"):
            text = text.lower()

        # Length check
        if len(text) > 280 or len(text) < 30:
            log(f"  length {len(text)}, skip")
            continue

        # Dedup against existing queue + posted
        if any(text[:50].lower() == existing[:50].lower() for existing in avoid_texts):
            log(f"  duplicate angle, skip")
            continue

        # Score from Claude's own ratings (brain scores itself)
        provocation = min(10, max(1, int(prov_m.group(1)))) if prov_m else 5
        bookmark = min(10, max(1, int(book_m.group(1)))) if book_m else 5
        hook = min(10, max(1, int(hook_m.group(1)))) if hook_m else 5

        # Length bonus
        tlen = len(text)
        length_bonus = 0
        if 71 <= tlen <= 100:
            length_bonus = 1.5
        elif 240 <= tlen <= 259:
            length_bonus = 1.0
        elif tlen > 260:
            length_bonus = -0.5

        composite = round(
            provocation * algo.get("reply_provocation_weight", 1.0) +
            bookmark * algo.get("bookmark_worthy_bonus", 1.5) +
            hook * algo.get("hook_strength_weight", 1.0) +
            length_bonus, 1
        )

        scores = {
            "provocation": provocation,
            "bookmark": bookmark,
            "hook": hook,
            "length_bonus": length_bonus,
            "composite": composite
        }

        cid = hashlib.md5(f"{text}{time.time()}".encode()).hexdigest()[:10]
        candidate = {
            "id": cid,
            "text": text,
            "territory": territory,
            "status": "queued",
            "scores": scores,
            "image_type": pick_image_type(text, territory, voice),
            "generated_at": now_utc().isoformat(),
            "generator": "claude-opus"
        }

        candidates.append(candidate)
        avoid_texts.append(text)  # prevent intra-batch dupes
        log(f"  [{territory}] composite={composite} p={provocation} b={bookmark} h={hook} \"{text[:60]}...\"")

    # Save to queue
    if candidates:
        for c in candidates:
            append_jsonl(QUEUE_PATH, c)
        log(f"generate: {len(candidates)} candidates queued")
        state["last_generate_at"] = now_utc().isoformat()
    else:
        log("generate: no candidates passed filters")


def pick_image_type(text, territory, voice):
    """Decide image type based on content."""
    rules = voice.get("images", {}).get("rules", {})

    # Simple heuristic based on territory/style
    if len(text) < 100 and territory in ("taste_agency", "ai"):
        return "quote_card"
    if territory == "building" and any(w in text for w in ["built", "automated", "cron", "deploy", "ship"]):
        return "terminal_screenshot"
    return random.choice(["quote_card", "none", "none"])  # 1/3 chance of image


# ============================================================
# PHASE 3: POST
# ============================================================

def phase_post(state, voice):
    # Timing check
    ist = now_ist()
    current_time = ist.strftime("%H:%M")
    windows = voice.get("timing", {}).get("windows_ist", [])
    in_window = False
    window_name = ""

    for w in windows:
        if w["start"] <= current_time <= w["end"]:
            in_window = True
            window_name = w["name"]
            break

    if not in_window:
        next_w = [w for w in windows if w["start"] > current_time]
        if next_w:
            log(f"post: outside window. next: {next_w[0]['name']} at {next_w[0]['start']} IST")
        else:
            log(f"post: past all windows today")
        return

    # Pacing check
    min_gap = voice.get("timing", {}).get("min_gap_hours", 2)
    posted = load_jsonl(POSTED_PATH)
    if posted:
        latest_ts = max(
            (parse_ts(p.get("posted_at")) for p in posted if p.get("posted_at")),
            default=None
        )
        if latest_ts:
            gap_h = (now_utc() - latest_ts).total_seconds() / 3600
            if gap_h < min_gap:
                log(f"post: pacing hold. last post {gap_h:.1f}h ago (min {min_gap}h)")
                return

    # Load queue
    queue = [c for c in load_jsonl(QUEUE_PATH) if c.get("status") == "queued"]
    if not queue:
        log("post: queue empty, need generate first")
        return

    # Pick best by composite score
    candidate = max(queue, key=lambda c: c.get("scores", {}).get("composite", 0))
    scores = candidate.get("scores", {})
    log(f"post: selected [{scores.get('composite','?')}] \"{candidate['text'][:70]}...\"")
    log(f"post: territory={candidate.get('territory')} window={window_name} time={current_time} IST")

    if DRY_RUN:
        log(f"DRY RUN would post: {candidate['text']}")
        return

    # Anti-detection: random pre-post delay (30s - 3min)
    delay = random.randint(30, 180)
    log(f"post: human delay {delay}s before posting...")
    time.sleep(delay)

    # Post via CDP
    success, result = do_post_tweet(candidate["text"])

    if success:
        log(f"post: POSTED -- {result}")

        # Remove from queue
        all_queue = load_jsonl(QUEUE_PATH)
        remaining = [c for c in all_queue if c.get("id") != candidate["id"]]
        save_jsonl(QUEUE_PATH, remaining)

        # Add to posted
        entry = {
            "id": candidate["id"],
            "text": candidate["text"],
            "territory": candidate.get("territory"),
            "scores": candidate.get("scores"),
            "image_type": candidate.get("image_type", "none"),
            "posted_at": now_utc().isoformat(),
            "tweet_url": result,
            "status": "live",
            "self_replied": False,
            "replied_to_users": [],
            "metrics": {}
        }
        append_jsonl(POSTED_PATH, entry)

        state["last_post_at"] = now_utc().isoformat()
        state["total_posted"] = state.get("total_posted", 0) + 1

        send_telegram(
            f"<b>ARIA posted</b>\n\n"
            f"{candidate['text']}\n\n"
            f"composite: {scores.get('composite', '?')} | "
            f"territory: {candidate.get('territory')}\n"
            f"{result}"
        )

        # SELF-REPLY IMMEDIATELY (not in engage phase -- can't wait 20 min)
        # The 150x algo signal requires replying within 5-10 min
        if result and not result.startswith("posted"):
            self_reply_delay = random.randint(90, 300)  # 1.5 - 5 min
            log(f"post: scheduling self-reply in {self_reply_delay}s...")
            time.sleep(self_reply_delay)

            golden = voice["golden_tweets"]
            examples = random.sample(golden, min(3, len(golden)))
            examples_text = "\n".join([f"- {e['text']}" for e in examples])

            sr_prompt = f"""you posted this tweet as @BalabommaRao:
"{candidate['text']}"

write a self-reply that adds a second angle. not a continuation, not an explanation. a new observation related to the same territory.

voice examples:
{examples_text}

rules:
- lowercase, 1-2 sentences, max 200 chars
- no em dashes, no hashtags, no emojis, no hyphens as formatting
- don't explain the original tweet
- don't start with "also" or "and" or "to add"
- same deadpan observer tone
- must stand alone as a good tweet even without the original

write ONLY the reply text. nothing else."""

            sr_text = call_claude(sr_prompt)
            if sr_text:
                clean = sr_text.strip().strip('"').strip("'").lower()
                clean = re.sub(r'^(reply|tweet|also|and)[:\s]*', '', clean, flags=re.IGNORECASE).strip()
                log(f"post: self-reply: \"{clean[:80]}\"")

                sr_success = do_post_reply(result, clean)
                if sr_success:
                    entry["self_replied"] = True
                    entry["self_reply_text"] = clean
                    log("post: self-reply POSTED")
                    append_jsonl(ENGAGE_PATH, {
                        "post_id": entry["id"],
                        "action": "self_reply",
                        "text": clean,
                        "timestamp": now_utc().isoformat()
                    })
                    # Update the posted entry
                    all_posted = load_jsonl(POSTED_PATH)
                    for p in all_posted:
                        if p.get("id") == entry["id"]:
                            p["self_replied"] = True
                            p["self_reply_text"] = clean
                    save_jsonl(POSTED_PATH, all_posted)
                else:
                    log("post: self-reply FAILED")
            else:
                log("post: self-reply generation failed")
    else:
        log(f"post: FAILED -- {result}")
        send_telegram(f"<b>ARIA post failed</b>\n{result}")
        # Track consecutive failures for heal
        state["consecutive_post_failures"] = state.get("consecutive_post_failures", 0) + 1


def do_post_tweet(text, image_path=None):
    """Post tweet via post_tweet.js CDP script."""
    if not POST_TWEET_JS.exists():
        return False, "post_tweet.js not found"

    env = os.environ.copy()
    env["X_USERNAME"] = X_USERNAME
    env["CDP_URL"] = CDP_URL

    cmd = ["node", str(POST_TWEET_JS), text]
    if image_path:
        cmd.extend(["--image", image_path])

    try:
        result = subprocess.run(
            cmd, env=env, cwd=str(POST_TWEET_JS.parent),
            capture_output=True, text=True, timeout=120
        )
        if result.returncode == 0:
            tweet_url = None
            for line in result.stdout.split("\n"):
                if "twitter.com" in line or "x.com" in line:
                    tweet_url = line.strip()
                    break
            return True, tweet_url or "posted (url not captured)"
        else:
            return False, result.stderr.strip()[:300] or "unknown error"
    except subprocess.TimeoutExpired:
        return False, "timeout (120s)"
    except Exception as e:
        return False, str(e)


# ============================================================
# PHASE 4: ENGAGE
# ============================================================

def phase_engage(state, voice):
    """Monitor incoming replies on recent posts.
    Self-reply is now handled in phase_post immediately after posting.
    This phase handles: catching missed self-replies + future reply-back monitoring."""
    posted = load_jsonl(POSTED_PATH)
    now = now_utc()

    # Find posts in engagement window
    targets = []
    for post in posted:
        if post.get("status") != "live":
            continue
        ts = parse_ts(post.get("posted_at"))
        if not ts:
            continue
        age_min = (now - ts).total_seconds() / 60
        if age_min < REPLY_BACK_WINDOW:
            post["_age_min"] = age_min
            targets.append(post)

    if not targets:
        log("engage: no recent posts in window")
        return

    log(f"engage: {len(targets)} posts in engagement window")
    updated = False

    for post in targets:
        age = post.get("_age_min", 999)
        tweet_url = post.get("tweet_url", "")
        log(f"  post [{post.get('id','?')}] age={age:.0f}min self_replied={post.get('self_replied', False)}")

        # Safety net: if self-reply was missed in post phase (e.g. crash), catch it here
        if age < 30 and not post.get("self_replied") and not DRY_RUN:
            log("  MISSED self-reply, catching up...")
            golden = voice["golden_tweets"]
            examples = random.sample(golden, min(3, len(golden)))
            examples_text = "\n".join([f"- {e['text']}" for e in examples])

            prompt = f"""you posted this tweet as @BalabommaRao:
"{post['text']}"

write a self-reply that adds a second angle. not a continuation, not an explanation. a new observation.

voice examples:
{examples_text}

rules:
- lowercase, 1-2 sentences, max 200 chars
- no em dashes, no hashtags, no emojis
- same deadpan observer tone

write ONLY the reply text."""

            sr = call_claude(prompt)
            if sr and tweet_url and not tweet_url.startswith("posted"):
                clean = sr.strip().strip('"').strip("'").lower()
                time.sleep(random.randint(20, 60))
                if do_post_reply(tweet_url, clean):
                    post["self_replied"] = True
                    post["self_reply_text"] = clean
                    updated = True
                    log(f"  catchup self-reply POSTED: \"{clean[:60]}\"")
                    append_jsonl(ENGAGE_PATH, {
                        "post_id": post.get("id"), "action": "self_reply_catchup",
                        "text": clean, "timestamp": now_utc().isoformat()
                    })

    if updated:
        save_jsonl(POSTED_PATH, posted)
        log("engage: posted.jsonl updated")


def do_post_reply(tweet_url, reply_text):
    """Post a reply via CDP."""
    script = f"""
const CDP = require('chrome-remote-interface');
(async () => {{
    const client = await CDP({{port: {CDP_PORT}}});
    const {{Page, Runtime}} = client;
    await Page.enable();

    await Page.navigate({{url: '{tweet_url}'}});
    await new Promise(r => setTimeout(r, {4000 + random.randint(1000, 6000)}));

    // Scroll like a human
    await Runtime.evaluate({{expression: 'window.scrollBy(0, {random.randint(50, 200)})'}});
    await new Promise(r => setTimeout(r, {random.randint(1000, 3000)}));

    const clicked = await Runtime.evaluate({{
        expression: `(function() {{
            const replyBtn = document.querySelector('[data-testid="reply"]');
            if (replyBtn) {{ replyBtn.click(); return true; }}
            return false;
        }})()`
        , returnByValue: true
    }});

    if (!clicked.result.value) {{
        console.error('reply button not found');
        process.exit(1);
    }}

    await new Promise(r => setTimeout(r, {random.randint(1500, 4000)}));

    const replyText = {json.dumps(reply_text)};
    await Runtime.evaluate({{
        expression: `(function() {{
            const editor = document.querySelector('[data-testid="tweetTextarea_0"]');
            if (editor) {{
                editor.focus();
                document.execCommand('insertText', false, ${{JSON.stringify(replyText)}});
                return true;
            }}
            return false;
        }})()`
        , returnByValue: true
    }});

    await new Promise(r => setTimeout(r, {random.randint(800, 2500)}));

    await Runtime.evaluate({{
        expression: `(function() {{
            const btns = document.querySelectorAll('[data-testid="tweetButton"], [data-testid="tweetButtonInline"]');
            for (const btn of btns) {{
                if (!btn.disabled) {{ btn.click(); return true; }}
            }}
            return false;
        }})()`
        , returnByValue: true
    }});

    await new Promise(r => setTimeout(r, 3000));
    console.log('reply posted');
    await client.close();
}})().catch(e => {{ console.error(e.message); process.exit(1); }});
"""
    script_path = WORKSPACE / "scripts" / "_post_reply.js"
    script_path.parent.mkdir(parents=True, exist_ok=True)
    with open(script_path, "w") as f:
        f.write(script)

    try:
        result = subprocess.run(
            ["node", str(script_path)],
            capture_output=True, text=True, timeout=60,
            cwd=str(WORKSPACE)
        )
        return result.returncode == 0
    except Exception as e:
        log(f"reply error: {e}")
        return False


# ============================================================
# PHASE 5: METRICS
# ============================================================

def phase_metrics(state, voice):
    last_metrics = parse_ts(state.get("last_metrics_at"))
    if last_metrics and (now_utc() - last_metrics).total_seconds() / 3600 < METRICS_STALE_HOURS:
        log(f"metrics: fresh ({(now_utc() - last_metrics).total_seconds()/3600:.1f}h old), skip")
        return

    posted = load_jsonl(POSTED_PATH)
    live = [p for p in posted if p.get("status") == "live"]
    if not live:
        log("metrics: no live posts to scrape")
        return

    log(f"metrics: would scrape {len(live)} posts (CDP scrape)")
    # Note: full metrics scraping is in aria-metrics.py
    # For now the engine just ensures it runs periodically
    try:
        subprocess.run(
            [sys.executable, str(WORKSPACE / "scripts" / "aria-metrics.py")],
            cwd=str(WORKSPACE), timeout=300,
            capture_output=True, text=True
        )
        state["last_metrics_at"] = now_utc().isoformat()
    except Exception as e:
        log(f"metrics: error running aria-metrics.py: {e}")


# ============================================================
# PHASE 6: HEAL
# ============================================================

def phase_heal(state):
    """Self-healing checks."""
    issues = []

    # Check CDP is alive
    try:
        req = urllib_request.Request(f"http://127.0.0.1:{CDP_PORT}/json/version")
        with urllib_request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
            log(f"heal: CDP alive ({data.get('Browser', 'unknown')})")
    except:
        issues.append("CDP chrome not responding")
        log("heal: CDP NOT RESPONDING")
        # Try to restart
        start_script = WORKSPACE / "scripts" / "start-cdp-chrome.sh"
        if start_script.exists():
            log("heal: attempting CDP restart...")
            try:
                subprocess.run(["bash", str(start_script)], timeout=30,
                              capture_output=True, text=True)
                time.sleep(5)
                # Verify
                urllib_request.urlopen(
                    urllib_request.Request(f"http://127.0.0.1:{CDP_PORT}/json/version"),
                    timeout=5
                )
                log("heal: CDP restarted successfully")
            except:
                log("heal: CDP restart FAILED")
                issues.append("CDP restart failed")

    # Check ollama is alive (for scoring)
    try:
        req = urllib_request.Request(f"{OLLAMA_BASE}/api/tags")
        with urllib_request.urlopen(req, timeout=5):
            pass
        log("heal: ollama alive")
    except:
        issues.append("ollama not responding")
        log("heal: ollama NOT RESPONDING")

    # Check Claude CLI reachable
    try:
        proc = subprocess.run(
            [CLAUDE_CLI, "-p", "--model", "haiku"],
            input="respond with only the word ok",
            capture_output=True, text=True, timeout=30
        )
        if proc.returncode == 0 and proc.stdout.strip():
            log("heal: claude CLI alive")
        else:
            issues.append("claude CLI not responding")
            log("heal: claude CLI NOT RESPONDING")
    except:
        issues.append("claude CLI unreachable")
        log("heal: claude CLI UNREACHABLE")

    # Check disk space for logs
    try:
        log_dir = WORKSPACE / "logs"
        total_size = sum(f.stat().st_size for f in log_dir.glob("*") if f.is_file())
        if total_size > 50 * 1024 * 1024:  # 50MB
            log("heal: log dir > 50MB, rotating...")
            for f in sorted(log_dir.glob("*.log"), key=lambda x: x.stat().st_mtime):
                if f.stat().st_size > 5 * 1024 * 1024:
                    f.write_text("")
                    log(f"heal: cleared {f.name}")
    except:
        pass

    # Check for consecutive post failures
    if state.get("consecutive_post_failures", 0) >= 3:
        issues.append(f"3+ consecutive post failures")
        log("heal: 3+ post failures, CDP may need restart")

    # Queue health: warn if queue has been empty for 2+ cycles
    queue = load_jsonl(QUEUE_PATH)
    queued = [c for c in queue if c.get("status") == "queued"]
    if not queued:
        empty_since = parse_ts(state.get("queue_empty_since"))
        if empty_since:
            empty_h = (now_utc() - empty_since).total_seconds() / 3600
            if empty_h > 1:
                issues.append(f"queue empty for {empty_h:.1f}h")
        else:
            state["queue_empty_since"] = now_utc().isoformat()
    else:
        state.pop("queue_empty_since", None)

    if issues:
        send_telegram(f"<b>ARIA heal issues</b>\n" + "\n".join(f"- {i}" for i in issues))

    state["last_heal_at"] = now_utc().isoformat()
    state["heal_issues"] = issues


# ============================================================
# MAIN ORCHESTRATOR
# ============================================================

def main():
    # Anti-detection: random startup jitter (30s - 5min)
    if not DRY_RUN:
        jitter = random.randint(30, 300)
        log(f"engine: jitter sleep {jitter}s")
        time.sleep(jitter)

    log("=" * 60)
    log("ARIA engine v3 starting")
    log(f"mode={'DRY RUN' if DRY_RUN else 'LIVE'}")

    voice = load_voice()
    state = load_state()

    try:
        # Phase 1: Signals
        log("\n--- PHASE 1: SIGNALS ---")
        phase_signals(state, voice)
        save_state(state)

        # Phase 2: Generate
        log("\n--- PHASE 2: GENERATE ---")
        phase_generate(state, voice)
        save_state(state)

        # Phase 3: Post
        log("\n--- PHASE 3: POST ---")
        phase_post(state, voice)
        save_state(state)

        # Phase 4: Engage
        log("\n--- PHASE 4: ENGAGE ---")
        phase_engage(state, voice)
        save_state(state)

        # Phase 5: Metrics (less frequent, engine handles timing)
        log("\n--- PHASE 5: METRICS ---")
        phase_metrics(state, voice)
        save_state(state)

        # Phase 6: Heal
        log("\n--- PHASE 6: HEAL ---")
        phase_heal(state)
        save_state(state)

    except Exception as e:
        log(f"ENGINE ERROR: {e}")
        log(traceback.format_exc())
        send_telegram(f"<b>ARIA engine error</b>\n{str(e)[:300]}")
        state["last_error"] = str(e)
        state["last_error_at"] = now_utc().isoformat()
        save_state(state)

    log("\nengine cycle complete")
    log("=" * 60)


if __name__ == "__main__":
    main()
