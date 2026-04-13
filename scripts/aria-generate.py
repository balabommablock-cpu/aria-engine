#!/usr/bin/env python3
"""
aria-generate.py v2.1 -- ARIA Voice Engine: Generate & Gate (Full Engine)

Now includes:
  - Signal-fed generation (reads signals.jsonl for live topic context)
  - Algo scoring (reply provocation, bookmark-worthy, hook strength, length bands)
  - Image decision (should this tweet have a visual?)
  - Variety enforcement (no territory can dominate)
  - Composite scoring (voice + algo = final rank)

Usage:
    python3 aria-generate.py                 # normal run
    python3 aria-generate.py --dry-run       # generate + gate, don't queue
    python3 aria-generate.py --territory ai  # force territory
    python3 aria-generate.py --volume 10     # generate more candidates
"""

import json, os, sys, re, random, time, argparse, hashlib
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections import Counter

# --- anti-detection: random startup delay (0-15 min) ---
if "--dry-run" not in sys.argv:
    _jitter = random.randint(30, 900)
    print(f"[jitter] sleeping {_jitter}s before generate run")
    time.sleep(_jitter)

WORKSPACE = Path(os.environ.get("ARIA_WORKSPACE", os.path.expanduser("~/.openclaw/agents/aria/workspace")))
VOICE_PATH = WORKSPACE / "voice.json"
QUEUE_PATH = WORKSPACE / "memory" / "queue.jsonl"
POSTED_PATH = WORKSPACE / "memory" / "posted.jsonl"
SIGNALS_PATH = WORKSPACE / "memory" / "signals.jsonl"
LOG_PATH = WORKSPACE / "logs" / "generate.log"

OLLAMA_BASE = os.environ.get("OLLAMA_BASE", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "gemma4:26b")

DEFAULT_CANDIDATES = 10


def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")


def load_json(path):
    if not path.exists():
        return None
    with open(path) as f:
        return json.load(f)


def load_jsonl(path):
    if not path.exists():
        return []
    items = []
    with open(path) as f:
        for line in f:
            try:
                items.append(json.loads(line.strip()))
            except json.JSONDecodeError:
                pass
    return items


def call_ollama(prompt, temperature=0.9, max_tokens=200):
    import urllib.request
    url = f"{OLLAMA_BASE}/v1/chat/completions"
    body = json.dumps({
        "model": OLLAMA_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False
    }).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode())
            return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        log(f"ollama error: {e}")
        return None


# ---- VARIETY ENFORCEMENT ----

def check_variety(territory, voice, posted):
    """Check if posting this territory would violate variety constraints."""
    variety = voice.get("variety", {})
    max_consec = variety.get("max_consecutive_same_territory", 2)
    max_pct = variety.get("max_territory_pct_weekly", 0.40)

    # Check consecutive
    recent = sorted(posted, key=lambda p: p.get("posted_at", ""), reverse=True)
    consec = 0
    for p in recent[:max_consec]:
        if p.get("territory") == territory:
            consec += 1
    if consec >= max_consec:
        return False, f"would be {consec + 1} consecutive {territory} tweets"

    # Check weekly percentage
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    week_posts = [p for p in posted if p.get("posted_at", "") > week_ago]
    if week_posts:
        territory_count = sum(1 for p in week_posts if p.get("territory") == territory)
        pct = (territory_count + 1) / (len(week_posts) + 1)
        if pct > max_pct:
            return False, f"{territory} would be {pct:.0%} of weekly posts (max {max_pct:.0%})"

    return True, "ok"


def pick_territory(voice, posted, forced=None):
    """Pick territory respecting variety constraints."""
    if forced and forced in voice["territory_weights"]:
        return forced

    weights = voice["territory_weights"]
    territories = list(weights.keys())
    random.shuffle(territories)  # break ties randomly

    # Try weighted random, respecting variety
    for _ in range(20):
        pick = random.choices(territories, weights=[weights[t] for t in territories], k=1)[0]
        ok, reason = check_variety(pick, voice, posted)
        if ok:
            return pick
        log(f"  variety skip: {reason}")

    # Fallback: pick least-used territory this week
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    week_counts = Counter(p.get("territory") for p in posted if p.get("posted_at", "") > week_ago)
    return min(territories, key=lambda t: week_counts.get(t, 0))


# ---- SIGNAL INTEGRATION ----

def get_relevant_signals(territory):
    """Get recent signals relevant to the territory."""
    signals = load_jsonl(SIGNALS_PATH)
    relevant = [s for s in signals if s.get("territory") == territory or s.get("territory") == "general"]
    # Sort by recency
    relevant.sort(key=lambda s: s.get("scraped_at", ""), reverse=True)
    return relevant[:5]


def build_signal_context(signals):
    """Format signals into context for the generation prompt."""
    if not signals:
        return ""
    lines = ["here's what's being discussed right now in this space:"]
    for s in signals[:3]:
        title = s.get("title", "")
        summary = s.get("summary", "")[:100]
        if title:
            lines.append(f"- {title}" + (f" ({summary})" if summary else ""))
    lines.append("\nyou don't have to reference these directly. they're context for what's on people's minds today. write YOUR observation, not a reaction to these.")
    return "\n".join(lines)


# ---- PROMPT BUILDING ----

def pick_golden_examples(voice, territory, n=3):
    same = [g for g in voice["golden_tweets"] if g["territory"] == territory]
    diff = [g for g in voice["golden_tweets"] if g["territory"] != territory]
    picks = []
    if same:
        picks.append(random.choice(same))
    pool = [g for g in (same + diff) if g not in picks]
    picks.extend(random.sample(pool, min(n - len(picks), len(pool))))
    return picks


def build_prompt(voice, territory, examples, signal_context=""):
    examples_text = "\n".join([f"- {e['text']}" for e in examples])
    territory_prompt = voice["territory_prompts"][territory]
    structure = voice["structure_rules"]
    bans_words = ", ".join(voice["hard_bans"]["words"][:20])
    bans_phrases = ", ".join([f'"{p}"' for p in voice["hard_bans"]["phrases"][:10]])

    signal_block = f"\n{signal_context}\n" if signal_context else ""

    return f"""you are writing tweets for a specific voice. study these examples carefully:

{examples_text}

rules:
- {structure['primary']}
- maximum {structure['max_sentences']} sentences. usually 1-2.
- {structure['case']}
- maximum 280 characters.
- never use em dashes, hashtags, emojis, or exclamation marks.
- never give advice, tips, lessons, or frameworks.
- never use these words: {bans_words}
- never use these phrases: {bans_phrases}
- never mention any company, title, credential, or person by name.
- tone: observer, not teacher. anthropologist inside a broken system. deadpan.
- the tweet should provoke smart people to reply, disagree, or extend the thought.
- the tweet should feel worth bookmarking or screenshotting.

topic: {territory_prompt}
{signal_block}
write exactly ONE tweet. nothing else. no quotes, no prefix, no explanation. just the tweet text."""


# ---- HARD GATE ----

def hard_gate(text, voice):
    bans = voice["hard_bans"]
    if len(text) > 280: return False, f"too long ({len(text)})"
    if len(text) < 20: return False, f"too short ({len(text)})"

    alpha = [c for c in text if c.isalpha()]
    if alpha and sum(1 for c in alpha if c.isupper()) / len(alpha) > 0.05:
        return False, "not lowercase"

    for ch in bans.get("characters", []):
        if ch in text: return False, f"banned char: {repr(ch)}"
    if "!" in text: return False, "exclamation"
    if "#" in text: return False, "hashtag"

    text_lower = text.lower()
    for w in bans.get("words", []):
        if w.lower() in text_lower: return False, f"banned word: {w}"
    for p in bans.get("phrases", []):
        if p.lower() in text_lower: return False, f"banned phrase: {p}"
    for pat in bans.get("patterns", []):
        try:
            if re.search(pat, text): return False, f"banned pattern: {pat}"
        except re.error:
            pass

    for rl in ["mofsl", "motilal", "oswal", "iim", "bhilai"]:
        if rl in text_lower: return False, f"red line: {rl}"

    sentences = [s.strip() for s in re.split(r'[.!?]+', text) if s.strip()]
    if len(sentences) > 4: return False, f"too many sentences ({len(sentences)})"

    return True, "passed"


def similarity_check(text, voice, posted_items):
    def word_set(t):
        return set(re.findall(r'[a-z]+', t.lower()))
    def jaccard(a, b):
        if not a or not b: return 0.0
        return len(a & b) / len(a | b)

    cw = word_set(text)
    for g in voice["golden_tweets"]:
        if jaccard(cw, word_set(g["text"])) > 0.6:
            return False, f"too similar to golden {g['id']}"
    for p in posted_items:
        if jaccard(cw, word_set(p.get("text", ""))) > 0.55:
            return False, "too similar to posted tweet"
    return True, "unique"


# ---- ALGO SCORING ----

def score_algo(text, voice, examples):
    """Score candidate on algorithm-optimized dimensions."""
    algo = voice.get("algo_scoring", {})

    # Voice match (AI scored)
    voice_score = ai_score(text, examples, "voice match",
        "does this tweet match the voice of the examples? same structure, tone, observer-not-teacher? 0-10.")

    # Reply provocation (AI scored)
    provocation = ai_score(text, examples, "reply provocation",
        "would a smart person feel compelled to reply to this tweet? disagree, extend, or add their take? 0-10. a tweet that just states a fact = low. a tweet that redefines something familiar = high.")

    # Hook strength (AI scored)
    hook = ai_score(text, examples, "hook strength",
        "do the first 7-10 words make you stop scrolling? does the opening create immediate tension or curiosity? 0-10.")

    # Bookmark-worthy (rule-based + AI)
    bookmark_bonus = 0
    # Aphoristic tweets (redefinitions) tend to be bookmark-worthy
    if len(text) < 150 and ". " in text:
        bookmark_bonus = algo.get("bookmark_worthy_bonus", 1.5)
    elif "the question" in text.lower() or "the difference" in text.lower() or "the only" in text.lower():
        bookmark_bonus = algo.get("bookmark_worthy_bonus", 1.5) * 0.7

    # Length band bonus
    length_bands = algo.get("length_bands", {})
    length_bonus = length_bands.get("default", {}).get("bonus", 0)
    tlen = len(text)
    for band_name, band in length_bands.items():
        if isinstance(band, dict) and "min" in band and "max" in band:
            if band["min"] <= tlen <= band["max"]:
                length_bonus = band.get("bonus", 0)
                break
    if tlen > 260:
        length_bonus = length_bands.get("over_260", {}).get("bonus", -0.5)

    composite = (
        voice_score * 1.0 +
        provocation * algo.get("reply_provocation_weight", 1.0) +
        hook * algo.get("hook_strength_weight", 1.0) +
        bookmark_bonus +
        length_bonus
    )

    return {
        "voice": voice_score,
        "provocation": provocation,
        "hook": hook,
        "bookmark_bonus": bookmark_bonus,
        "length_bonus": length_bonus,
        "composite": round(composite, 1)
    }


def ai_score(text, examples, dimension, criteria):
    """Generic AI scoring function."""
    examples_text = "\n".join([f"- {e['text']}" for e in examples[:3]])
    prompt = f"""rate this tweet 0-10 on: {criteria}

example tweets (the target voice):
{examples_text}

tweet to rate:
- {text}

respond with ONLY a single number 0-10. nothing else."""

    result = call_ollama(prompt, temperature=0.1, max_tokens=5)
    if result:
        nums = re.findall(r'\d+', result)
        if nums:
            return min(max(int(nums[0]), 0), 10)
    return 5


# ---- IMAGE DECISION ----

def decide_image(text, territory, voice):
    """Decide if this tweet should have an image and what type."""
    img_rules = voice.get("images", {}).get("rules", {})
    style_map = {
        "aphorism_reframe": "quote_card",
        "builder_confession": "terminal_screenshot",
        "organizational_observation": "none",
        "ai_observation": "none",
        "declaration": "quote_card"
    }

    # Detect tweet style
    if len(text) < 120 and ". " in text and any(w in text.lower() for w in ["the ", "is the ", "isn't"]):
        style = "aphorism_reframe"
    elif "i " in text.lower()[:10] and (territory == "building" or "built" in text.lower()):
        style = "builder_confession"
    elif territory == "building" and any(w in text.lower() for w in ["dangerous", "builder", "weapon"]):
        style = "declaration"
    elif territory == "organizations":
        style = "organizational_observation"
    else:
        style = "ai_observation"

    image_type = img_rules.get(style, style_map.get(style, "none"))
    return image_type, style


# ---- MAIN GENERATION LOOP ----

def generate_candidates(voice, territory, examples, signal_context, n):
    prompt = build_prompt(voice, territory, examples, signal_context)
    candidates = []
    for i in range(n):
        log(f"  generating {i+1}/{n}...")
        result = call_ollama(prompt, temperature=0.82 + (i * 0.04))
        if result:
            clean = result.strip().strip('"').strip("'")
            clean = re.sub(r'^(tweet|here|okay|sure)[:\s]*', '', clean, flags=re.IGNORECASE).strip()
            clean = clean.strip('"').strip("'")
            if clean:
                candidates.append(clean)
        time.sleep(1)
    return candidates


def gate_and_score(candidates, voice, examples, posted_items):
    survivors = []
    min_composite = voice.get("algo_scoring", {}).get("min_composite_to_queue", 22)

    for i, text in enumerate(candidates):
        log(f"  gating [{i+1}]: \"{text[:60]}...\"")

        ok, reason = hard_gate(text, voice)
        if not ok:
            log(f"    HARD FAIL: {reason}")
            continue

        ok, reason = similarity_check(text, voice, posted_items)
        if not ok:
            log(f"    SIMILARITY FAIL: {reason}")
            continue

        scores = score_algo(text, voice, examples)
        log(f"    scores: voice={scores['voice']} prov={scores['provocation']} "
            f"hook={scores['hook']} bm={scores['bookmark_bonus']} "
            f"len={scores['length_bonus']} COMPOSITE={scores['composite']}")

        if scores["voice"] < voice.get("algo_scoring", {}).get("voice_match_threshold", 7):
            log(f"    VOICE FAIL: {scores['voice']}")
            continue

        if scores["composite"] < min_composite:
            log(f"    COMPOSITE FAIL: {scores['composite']} < {min_composite}")
            continue

        image_type, tweet_style = decide_image(text, None, voice)

        survivors.append({
            "text": text,
            "scores": scores,
            "image_type": image_type,
            "tweet_style": tweet_style,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "golden_examples_used": [e["id"] for e in examples],
            "char_count": len(text)
        })
        log(f"    PASSED (composite={scores['composite']}, image={image_type})")

    survivors.sort(key=lambda s: s["scores"]["composite"], reverse=True)
    return survivors


def queue_survivors(survivors, territory, dry_run=False):
    if dry_run:
        log(f"  DRY RUN: {len(survivors)} survivors")
        for s in survivors:
            log(f"    [{s['scores']['composite']}] [{s['image_type']}] {s['text']}")
        return

    QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(QUEUE_PATH, "a") as f:
        for s in survivors:
            s["territory"] = territory
            s["status"] = "queued"
            s["id"] = hashlib.md5(s["text"].encode()).hexdigest()[:12]
            f.write(json.dumps(s) + "\n")
    log(f"  queued {len(survivors)}")


def main():
    parser = argparse.ArgumentParser(description="ARIA v2.1 Generate")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--territory", type=str)
    parser.add_argument("--volume", type=int, default=DEFAULT_CANDIDATES)
    args = parser.parse_args()

    log("=" * 60)
    log("ARIA v2.1 generate starting")

    voice = load_json(VOICE_PATH)
    if not voice:
        log("ERROR: voice.json not found")
        sys.exit(1)

    posted = load_jsonl(POSTED_PATH)
    territory = pick_territory(voice, posted, forced=args.territory)
    log(f"territory: {territory}")

    examples = pick_golden_examples(voice, territory)
    log(f"golden examples: {[e['id'] for e in examples]}")

    signals = get_relevant_signals(territory)
    signal_context = build_signal_context(signals)
    if signals:
        log(f"signals: {len(signals)} ({', '.join(s['title'][:30] for s in signals[:3])})")

    log(f"generating {args.volume} candidates...")
    candidates = generate_candidates(voice, territory, examples, signal_context, n=args.volume)
    log(f"raw candidates: {len(candidates)}")

    if not candidates:
        log("no candidates generated")
        return

    survivors = gate_and_score(candidates, voice, examples, posted)
    log(f"survivors: {len(survivors)}/{len(candidates)}")

    if survivors:
        queue_survivors(survivors, territory, dry_run=args.dry_run)

    log("generate complete")
    log("=" * 60)


if __name__ == "__main__":
    main()
