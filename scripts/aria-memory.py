#!/usr/bin/env python3
"""
aria-memory.py -- Memory system for Claude Khud.

Three memory layers:
  1. EPISODIC: what happened (reflections, observations per thought cycle)
  2. SEMANTIC: what I know (learned patterns, developed taste, rules)
  3. PROCEDURAL: what works (proven structures, reply styles, tweet templates)

Uses nomic-embed-text via Ollama for semantic search.
Each platform (X, LinkedIn) has its own memory space.
"""

from __future__ import annotations

import json, os, subprocess, sqlite3, hashlib, sys
from pathlib import Path
from datetime import datetime, timezone

WORKSPACE = Path(os.environ.get("ARIA_WORKSPACE",
    str(Path.home() / ".openclaw/agents/aria/workspace")))
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))


# ============================================================
# EMBEDDING: local via nomic-embed-text
# ============================================================

def embed_text(text: str) -> list[float] | None:
    """Get embedding vector from nomic-embed-text via Ollama. Free, local, fast."""
    try:
        result = subprocess.run(
            ["ollama", "run", "nomic-embed-text", text[:2000]],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0 and result.stdout.strip():
            # parse the JSON array of floats
            vec = json.loads(result.stdout.strip())
            if isinstance(vec, list) and len(vec) > 0:
                return vec
    except (subprocess.TimeoutExpired, json.JSONDecodeError, Exception):
        pass
    return None


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


# ============================================================
# MEMORY TABLES: platform-specific
# ============================================================

def init_memory_tables(db, platform: str = "x"):
    """Create memory tables for a specific platform."""
    p = platform  # x or li

    # Episodic: what happened
    db.execute(f"""
        CREATE TABLE IF NOT EXISTS memory_episodic_{p} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            content TEXT NOT NULL,
            category TEXT DEFAULT 'observation',
            embedding_json TEXT,
            importance REAL DEFAULT 0.5,
            access_count INTEGER DEFAULT 0,
            last_accessed TEXT
        )
    """)

    # Semantic: what I know (graduated from episodic when pattern confirmed)
    db.execute(f"""
        CREATE TABLE IF NOT EXISTS memory_semantic_{p} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            knowledge TEXT NOT NULL,
            confidence REAL DEFAULT 0.5,
            evidence_count INTEGER DEFAULT 1,
            embedding_json TEXT,
            last_confirmed TEXT,
            source_episodic_ids TEXT
        )
    """)

    # Procedural: what works (reusable skills/templates)
    db.execute(f"""
        CREATE TABLE IF NOT EXISTS memory_procedural_{p} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            skill_name TEXT NOT NULL,
            skill_type TEXT NOT NULL,
            template TEXT NOT NULL,
            success_count INTEGER DEFAULT 1,
            fail_count INTEGER DEFAULT 0,
            embedding_json TEXT,
            example_urls TEXT,
            last_used TEXT
        )
    """)

    db.commit()


# ============================================================
# MEMORY OPERATIONS
# ============================================================

def store_episodic(db, platform: str, content: str, category: str = "observation",
                   importance: float = 0.5) -> int:
    """Store an episodic memory (what happened)."""
    ts = datetime.now(timezone.utc).isoformat()
    embedding = embed_text(content)
    embedding_json = json.dumps(embedding) if embedding else None

    cursor = db.execute(
        f"INSERT INTO memory_episodic_{platform} "
        f"(ts, content, category, embedding_json, importance) VALUES (?,?,?,?,?)",
        (ts, content, category, embedding_json, importance)
    )
    db.commit()
    return cursor.lastrowid


def store_semantic(db, platform: str, knowledge: str, confidence: float = 0.5,
                   source_ids: list[int] = None) -> int:
    """Store a semantic memory (what I know). Graduated from episodic."""
    ts = datetime.now(timezone.utc).isoformat()
    embedding = embed_text(knowledge)
    embedding_json = json.dumps(embedding) if embedding else None
    source_str = json.dumps(source_ids) if source_ids else "[]"

    cursor = db.execute(
        f"INSERT INTO memory_semantic_{platform} "
        f"(ts, knowledge, confidence, embedding_json, source_episodic_ids) VALUES (?,?,?,?,?)",
        (ts, knowledge, confidence, embedding_json, source_str)
    )
    db.commit()
    return cursor.lastrowid


def store_procedural(db, platform: str, skill_name: str, skill_type: str,
                     template: str, example_urls: list[str] = None) -> int:
    """Store a procedural memory (what works). A reusable skill."""
    ts = datetime.now(timezone.utc).isoformat()
    embedding = embed_text(f"{skill_name}: {template}")
    embedding_json = json.dumps(embedding) if embedding else None
    urls_str = json.dumps(example_urls) if example_urls else "[]"

    cursor = db.execute(
        f"INSERT INTO memory_procedural_{platform} "
        f"(ts, skill_name, skill_type, template, embedding_json, example_urls) VALUES (?,?,?,?,?,?)",
        (ts, skill_name, skill_type, template, embedding_json, urls_str)
    )
    db.commit()
    return cursor.lastrowid


# ============================================================
# MEMORY RETRIEVAL: semantic search
# ============================================================

def recall_episodic(db, platform: str, query: str, limit: int = 5,
                    min_importance: float = 0.0) -> list[dict]:
    """Recall episodic memories most relevant to a query."""
    query_emb = embed_text(query)
    if not query_emb:
        # fallback: return most recent
        rows = db.execute(
            f"SELECT id, content, category, importance, ts FROM memory_episodic_{platform} "
            f"WHERE importance >= ? ORDER BY id DESC LIMIT ?",
            (min_importance, limit)
        ).fetchall()
        return [dict(r) for r in rows]

    # get all memories with embeddings
    rows = db.execute(
        f"SELECT id, content, category, importance, ts, embedding_json "
        f"FROM memory_episodic_{platform} WHERE embedding_json IS NOT NULL "
        f"AND importance >= ?",
        (min_importance,)
    ).fetchall()

    # score by similarity
    scored = []
    for r in rows:
        try:
            emb = json.loads(r["embedding_json"])
            sim = cosine_similarity(query_emb, emb)
            scored.append({
                "id": r["id"], "content": r["content"],
                "category": r["category"], "importance": r["importance"],
                "ts": r["ts"], "relevance": sim
            })
        except (json.JSONDecodeError, TypeError):
            pass

    # sort by relevance, return top N
    scored.sort(key=lambda x: x["relevance"], reverse=True)

    # update access counts
    for item in scored[:limit]:
        db.execute(
            f"UPDATE memory_episodic_{platform} SET access_count = access_count + 1, "
            f"last_accessed = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), item["id"])
        )
    db.commit()

    return scored[:limit]


def recall_semantic(db, platform: str, query: str, limit: int = 5) -> list[dict]:
    """Recall semantic knowledge most relevant to a query."""
    query_emb = embed_text(query)
    if not query_emb:
        rows = db.execute(
            f"SELECT id, knowledge, confidence, evidence_count, ts "
            f"FROM memory_semantic_{platform} ORDER BY confidence DESC LIMIT ?",
            (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    rows = db.execute(
        f"SELECT id, knowledge, confidence, evidence_count, ts, embedding_json "
        f"FROM memory_semantic_{platform} WHERE embedding_json IS NOT NULL"
    ).fetchall()

    scored = []
    for r in rows:
        try:
            emb = json.loads(r["embedding_json"])
            sim = cosine_similarity(query_emb, emb)
            scored.append({
                "id": r["id"], "knowledge": r["knowledge"],
                "confidence": r["confidence"],
                "evidence_count": r["evidence_count"],
                "ts": r["ts"], "relevance": sim
            })
        except (json.JSONDecodeError, TypeError):
            pass

    scored.sort(key=lambda x: x["relevance"], reverse=True)
    return scored[:limit]


def recall_procedural(db, platform: str, query: str, limit: int = 3) -> list[dict]:
    """Recall procedural skills most relevant to a query."""
    query_emb = embed_text(query)
    if not query_emb:
        rows = db.execute(
            f"SELECT id, skill_name, skill_type, template, success_count, fail_count "
            f"FROM memory_procedural_{platform} "
            f"ORDER BY (success_count - fail_count) DESC LIMIT ?",
            (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    rows = db.execute(
        f"SELECT id, skill_name, skill_type, template, success_count, fail_count, "
        f"embedding_json FROM memory_procedural_{platform} "
        f"WHERE embedding_json IS NOT NULL"
    ).fetchall()

    scored = []
    for r in rows:
        try:
            emb = json.loads(r["embedding_json"])
            sim = cosine_similarity(query_emb, emb)
            scored.append({
                "id": r["id"], "skill_name": r["skill_name"],
                "skill_type": r["skill_type"], "template": r["template"],
                "success_count": r["success_count"],
                "fail_count": r["fail_count"],
                "relevance": sim
            })
        except (json.JSONDecodeError, TypeError):
            pass

    scored.sort(key=lambda x: x["relevance"], reverse=True)
    return scored[:limit]


# ============================================================
# MEMORY GRADUATION: episodic -> semantic
# ============================================================

def graduate_to_semantic(db, platform: str, episodic_ids: list[int],
                         knowledge: str, confidence: float = 0.7):
    """When multiple episodic memories confirm a pattern, graduate to semantic knowledge."""
    return store_semantic(db, platform, knowledge, confidence, episodic_ids)


def graduate_to_procedural(db, platform: str, skill_name: str, skill_type: str,
                           template: str, example_urls: list[str] = None):
    """When a specific approach repeatedly works, codify it as a reusable skill."""
    return store_procedural(db, platform, skill_name, skill_type, template, example_urls)


# ============================================================
# CONTEXT BUILDER: what Khud sees from memory
# ============================================================

def build_memory_context(db, platform: str, current_situation: str) -> str:
    """Build a rich memory context string for Khud's prompt.
    Uses the current situation to retrieve the most relevant memories."""

    context_parts = []

    # relevant episodic memories
    episodes = recall_episodic(db, platform, current_situation, limit=5)
    if episodes:
        context_parts.append("RELEVANT PAST OBSERVATIONS:")
        for ep in episodes:
            rel = f" (relevance: {ep.get('relevance', 0):.2f})" if 'relevance' in ep else ""
            context_parts.append(f"  [{ep['category']}] {ep['content'][:150]}{rel}")

    # relevant knowledge
    knowledge = recall_semantic(db, platform, current_situation, limit=3)
    if knowledge:
        context_parts.append("\nTHINGS I KNOW (confirmed patterns):")
        for k in knowledge:
            context_parts.append(
                f"  [{k['confidence']:.1f} confidence, {k['evidence_count']}x confirmed] "
                f"{k['knowledge'][:150]}"
            )

    # relevant skills
    skills = recall_procedural(db, platform, current_situation, limit=3)
    if skills:
        context_parts.append("\nSKILLS THAT WORK:")
        for s in skills:
            win_rate = s['success_count'] / max(s['success_count'] + s['fail_count'], 1)
            context_parts.append(
                f"  [{s['skill_name']}] {s['template'][:120]} "
                f"(win rate: {win_rate:.0%})"
            )

    return "\n".join(context_parts) if context_parts else "(no memories yet -- this is a fresh start)"
