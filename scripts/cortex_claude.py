#!/usr/bin/env python3
"""
Cortex Claude — Anthropic API integration for spike analysis and belief creation.

This is the module that actually calls Claude to analyze WHY spikes happened,
and creates world model beliefs from the analysis.

Usage:
    from cortex_claude import CortexClaude
    claude = CortexClaude(db)
    claude.analyze_pending_spikes()   # analyze all unanalyzed spikes
    claude.update_beliefs()           # update belief evidence from recent predictions

Or standalone:
    python3 cortex_claude.py --analyze    # analyze pending spikes
    python3 cortex_claude.py --beliefs    # show current beliefs
    python3 cortex_claude.py --test       # test API connectivity
"""

import sqlite3
import json
import os
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Dict, List

IST = timezone(timedelta(hours=5, minutes=30))
WORKSPACE = Path(os.environ.get(
    "ARIA_WORKSPACE",
    os.path.expanduser("~/.openclaw/agents/aria/workspace")
))
DB_PATH = WORKSPACE / "memory" / "aria.db"

log = logging.getLogger("cortex_claude")

# Cortex uses a smaller, cheaper model for analysis — not the generation model
ANALYSIS_MODEL = "claude-sonnet-4-20250514"
ANALYSIS_MAX_TOKENS = 1500

# Rate limiting: max N API calls per cortex cycle
MAX_ANALYSIS_CALLS_PER_CYCLE = 3


class CortexClaude:
    """
    Claude API integration for cortex.
    Analyzes spikes, creates beliefs, tests hypotheses.
    """

    def __init__(self, db: sqlite3.Connection):
        self.db = db
        self.api_key = self._load_api_key()

    def analyze_pending_spikes(self) -> int:
        """
        Find unanalyzed spike events, call Claude to analyze them,
        store results, and create world model beliefs.
        Returns number of spikes analyzed.
        """
        if not self.api_key:
            log.warning("No Anthropic API key — skipping spike analysis")
            return 0

        pending = self.db.execute(
            """SELECT se.*, p.territory, p.hook_pattern, p.hour_bucket,
                      p.confounders_json, p.features_json
               FROM spike_events se
               JOIN predictions p ON se.prediction_id = p.id
               WHERE se.analysis_response IS NULL
               ORDER BY se.spike_magnitude DESC
               LIMIT ?""",
            (MAX_ANALYSIS_CALLS_PER_CYCLE,)
        ).fetchall()

        if not pending:
            log.info("No pending spikes to analyze")
            return 0

        analyzed = 0
        for spike in pending:
            prompt = spike["analysis_prompt"]
            if not prompt:
                continue

            try:
                response = self._call_claude(prompt)
                if not response:
                    continue

                # Parse Claude's response as JSON
                analysis = self._parse_analysis(response)

                # Store analysis
                self.db.execute(
                    """UPDATE spike_events SET
                       analysis_response = ?,
                       identified_factors = ?,
                       replicable_factors = ?,
                       non_replicable_factors = ?
                       WHERE id = ?""",
                    (response,
                     json.dumps(analysis.get("identified_factors", [])),
                     json.dumps(analysis.get("replicable_factors", [])),
                     json.dumps(analysis.get("non_replicable_factors", [])),
                     spike["id"])
                )

                # Create world model beliefs from analysis
                self._create_beliefs_from_analysis(spike["id"], analysis)

                analyzed += 1
                log.info("Analyzed spike #%d (%.1fx): %d factors identified",
                        spike["id"], spike["spike_magnitude"],
                        len(analysis.get("identified_factors", [])))

                # Rate limit between calls
                if analyzed < len(pending):
                    time.sleep(2)

            except Exception as e:
                log.error("Error analyzing spike #%d: %s", spike["id"], e)
                continue

        self.db.commit()
        return analyzed

    def test_beliefs_against_recent(self, hours_back: int = 24) -> int:
        """
        Check recent prediction outcomes against active world model beliefs.
        Update belief evidence (supporting or contradicting).
        Returns number of belief-prediction matches tested.
        """
        beliefs = self.db.execute(
            "SELECT * FROM world_model WHERE status = 'active'"
        ).fetchall()

        if not beliefs:
            return 0

        recent_predictions = self.db.execute(
            """SELECT * FROM predictions
               WHERE actual_impressions IS NOT NULL
               AND measured_at > datetime('now', ?)""",
            (f"-{hours_back} hours",)
        ).fetchall()

        if not recent_predictions:
            return 0

        tested = 0
        for belief in beliefs:
            for pred in recent_predictions:
                relevance = self._check_belief_relevance(belief, pred)
                if relevance == "none":
                    continue

                z_score = pred["z_score"] or 0
                is_spike = pred["is_spike"] or 0

                # Does this prediction support or contradict the belief?
                if relevance == "supporting" and z_score > 1.0:
                    self._update_belief_evidence(belief["id"], pred["id"], supports=True)
                    tested += 1
                elif relevance == "supporting" and z_score < -1.0:
                    self._update_belief_evidence(belief["id"], pred["id"], supports=False)
                    tested += 1
                elif relevance == "contradicting" and z_score > 1.0:
                    self._update_belief_evidence(belief["id"], pred["id"], supports=False)
                    tested += 1
                elif relevance == "contradicting" and z_score < -1.0:
                    self._update_belief_evidence(belief["id"], pred["id"], supports=True)
                    tested += 1

        self.db.commit()
        if tested:
            log.info("Tested %d belief-prediction pairs", tested)
        return tested

    # --- Internal ---

    def _call_claude(self, prompt: str) -> Optional[str]:
        """Call Anthropic API. Returns response text or None."""
        import urllib.request

        body = json.dumps({
            "model": ANALYSIS_MODEL,
            "max_tokens": ANALYSIS_MAX_TOKENS,
            "messages": [
                {"role": "user", "content": prompt}
            ],
            "system": (
                "You are an analytical assistant helping a social media AI agent understand "
                "why certain tweets outperformed expectations. Be specific and concrete. "
                "Always respond with valid JSON matching the requested format. "
                "No markdown fences, no preamble — just the JSON object."
            ),
        }).encode("utf-8")

        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=body,
            headers={
                "Content-Type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
                content = data.get("content", [])
                text_parts = [c["text"] for c in content if c.get("type") == "text"]
                return "\n".join(text_parts) if text_parts else None
        except Exception as e:
            log.error("Claude API call failed: %s", e)
            return None

    def _parse_analysis(self, response: str) -> dict:
        """Parse Claude's JSON response, handling common formatting issues."""
        text = response.strip()

        # Strip markdown fences if present
        if text.startswith("```"):
            lines = text.split("\n")
            # Remove first and last lines if they're fences
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines)

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # Try to extract JSON from mixed text
            start = text.find("{")
            end = text.rfind("}") + 1
            if start >= 0 and end > start:
                try:
                    return json.loads(text[start:end])
                except json.JSONDecodeError:
                    pass

            log.warning("Could not parse Claude response as JSON: %s...", text[:200])
            return {
                "identified_factors": [],
                "replicable_factors": [],
                "non_replicable_factors": [],
                "audience_belief": "",
                "content_belief": "",
                "raw_response": text,
            }

    def _create_beliefs_from_analysis(self, spike_event_id: int, analysis: dict):
        """Create world model beliefs from spike analysis."""
        belief_ids = []

        audience_belief = analysis.get("audience_belief", "")
        if audience_belief and len(audience_belief) > 10:
            bid = self._insert_belief("audience_model", audience_belief, "spike_analysis")
            if bid:
                belief_ids.append(bid)

        content_belief = analysis.get("content_belief", "")
        if content_belief and len(content_belief) > 10:
            bid = self._insert_belief("content_model", content_belief, "spike_analysis")
            if bid:
                belief_ids.append(bid)

        # Also create beliefs from replicable factors
        replicable = analysis.get("replicable_factors", [])
        for factor in replicable[:2]:  # max 2 factor-beliefs per spike
            if factor and len(factor) > 10:
                bid = self._insert_belief(
                    "content_model",
                    f"Replicable success factor: {factor}",
                    "spike_analysis"
                )
                if bid:
                    belief_ids.append(bid)

        if belief_ids:
            self.db.execute(
                "UPDATE spike_events SET beliefs_created = ? WHERE id = ?",
                (json.dumps(belief_ids), spike_event_id)
            )
            self.db.commit()
            log.info("Created %d beliefs from spike #%d", len(belief_ids), spike_event_id)

    def _insert_belief(self, belief_type: str, belief: str, source: str) -> Optional[int]:
        """Insert a new belief if not duplicate."""
        # Check for near-duplicates
        existing = self.db.execute(
            "SELECT id, belief FROM world_model WHERE belief_type = ? AND status = 'active'",
            (belief_type,)
        ).fetchall()

        for e in existing:
            # Simple similarity: if >60% of words overlap, skip
            existing_words = set(e["belief"].lower().split())
            new_words = set(belief.lower().split())
            if existing_words and new_words:
                overlap = len(existing_words & new_words) / max(len(existing_words), len(new_words))
                if overlap > 0.6:
                    log.debug("Skipping near-duplicate belief: %s", belief[:60])
                    return None

        cur = self.db.execute(
            """INSERT INTO world_model
               (ts, belief_type, belief, belief_confidence, source, serves_identity, status)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (datetime.now(IST).strftime("%Y-%m-%dT%H:%M:%S"),
             belief_type, belief, 0.3, source, 1, "active")
        )
        self.db.commit()
        return cur.lastrowid

    def _check_belief_relevance(self, belief, prediction) -> str:
        """
        Check if a prediction outcome is relevant to a belief.
        Returns 'supporting', 'contradicting', or 'none'.

        Simple keyword matching for now. Could be upgraded to Claude-based matching.
        """
        belief_text = belief["belief"].lower()
        pred_territory = (prediction["territory"] or "").lower()
        pred_hook = (prediction["hook_pattern"] or "").lower()

        # Territory-related beliefs
        if pred_territory and pred_territory in belief_text:
            return "supporting"

        # Hook pattern beliefs
        if "inversion" in belief_text and pred_hook == "inversion":
            if prediction.get("z_score", 0) < 0:
                return "supporting"  # inversion underperformed, belief says it should
            else:
                return "contradicting"

        # Timing beliefs
        hour = prediction.get("hour_bucket")
        if hour and ("morning" in belief_text or "early" in belief_text):
            if hour < 10:
                return "supporting"

        # Specificity beliefs
        if "specific" in belief_text or "concrete" in belief_text:
            try:
                features = json.loads(prediction["features_json"] or "{}")
                if features.get("word_count", 0) > 30:  # longer = more specific
                    return "supporting"
            except (json.JSONDecodeError, TypeError):
                pass

        return "none"

    def _update_belief_evidence(self, belief_id: int, prediction_id: int, supports: bool):
        """Update a belief's evidence and confidence."""
        belief = self.db.execute(
            "SELECT * FROM world_model WHERE id = ?", (belief_id,)
        ).fetchone()
        if not belief:
            return

        try:
            supporting = json.loads(belief["supporting_evidence"] or "[]")
            contradicting = json.loads(belief["contradicting_evidence"] or "[]")
        except (json.JSONDecodeError, TypeError):
            supporting = []
            contradicting = []

        # Don't count same prediction twice
        if prediction_id in supporting or prediction_id in contradicting:
            return

        if supports:
            supporting.append(prediction_id)
        else:
            contradicting.append(prediction_id)

        total = len(supporting) + len(contradicting)
        evidence_ratio = len(supporting) / total if total > 0 else 0.5
        blended_confidence = belief["belief_confidence"] * 0.7 + evidence_ratio * 0.3

        self.db.execute(
            """UPDATE world_model SET
               supporting_evidence = ?, contradicting_evidence = ?,
               evidence_count = ?, belief_confidence = ?, last_tested = ?
               WHERE id = ?""",
            (json.dumps(supporting[-50:]), json.dumps(contradicting[-50:]),  # keep last 50
             total, blended_confidence,
             datetime.now(IST).strftime("%Y-%m-%dT%H:%M:%S"),
             belief_id)
        )

        # Deprecate if confidence is very low with enough evidence
        if blended_confidence < 0.2 and total >= 8:
            self.db.execute(
                """UPDATE world_model SET status = 'disproven',
                   deprecated_reason = ? WHERE id = ?""",
                (f"confidence {blended_confidence:.2f} after {total} tests", belief_id)
            )
            log.info("Belief #%d disproven: %s", belief_id, belief["belief"][:60])

    def _load_api_key(self) -> Optional[str]:
        """Load Anthropic API key from environment or config."""
        key = os.environ.get("ANTHROPIC_API_KEY")
        if key:
            return key

        key_paths = [
            WORKSPACE / "cortex" / "anthropic_key.txt",
            Path(os.path.expanduser("~/.anthropic_api_key")),
            WORKSPACE / "config" / "api_keys.json",
        ]
        for path in key_paths:
            if path.exists():
                try:
                    text = path.read_text().strip()
                    if text.startswith("sk-"):
                        return text
                    # Try JSON
                    data = json.loads(text)
                    return data.get("anthropic") or data.get("ANTHROPIC_API_KEY")
                except (json.JSONDecodeError, OSError):
                    continue

        return None


# ---------------------------------------------------------------------------
# Standalone
# ---------------------------------------------------------------------------

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Cortex Claude Integration")
    parser.add_argument("--analyze", action="store_true", help="Analyze pending spikes")
    parser.add_argument("--beliefs", action="store_true", help="Show current beliefs")
    parser.add_argument("--test", action="store_true", help="Test API connectivity")
    parser.add_argument("--update-beliefs", action="store_true", help="Update belief evidence")
    args = parser.parse_args()

    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    claude = CortexClaude(db)

    if args.test:
        print("Testing Anthropic API...")
        if claude.api_key:
            resp = claude._call_claude("Say 'cortex online' and nothing else.")
            if resp:
                print(f"  OK: {resp.strip()}")
            else:
                print("  FAIL: API call returned None")
        else:
            print("  SKIP: No API key found")

    elif args.analyze:
        n = claude.analyze_pending_spikes()
        print(f"Analyzed {n} spikes")

    elif args.update_beliefs:
        n = claude.test_beliefs_against_recent(hours_back=48)
        print(f"Tested {n} belief-prediction pairs")

    elif args.beliefs:
        beliefs = db.execute(
            "SELECT * FROM world_model WHERE status = 'active' ORDER BY belief_confidence DESC"
        ).fetchall()
        print(f"\nActive beliefs: {len(beliefs)}\n")
        for b in beliefs:
            supporting = len(json.loads(b["supporting_evidence"] or "[]"))
            contradicting = len(json.loads(b["contradicting_evidence"] or "[]"))
            print(f"  #{b['id']} [{b['belief_type']}] (conf {b['belief_confidence']:.2f}, "
                  f"+{supporting}/-{contradicting})")
            print(f"    {b['belief']}")
            print()

    else:
        parser.print_help()

    db.close()


if __name__ == "__main__":
    main()
