#!/usr/bin/env python3
"""
aria-linkedin-db.py -- LinkedIn engine database schema.

All tables for the 55-loop LinkedIn growth engine:
  - Comment opportunities, posted comments, received comments
  - Post metrics, follower tracking
  - Connection tracking
  - Signal storage
  - Learning/performance data

Call init_linkedin_tables(db) from any script that needs these.
"""

from __future__ import annotations


LINKEDIN_SCHEMA = """
-- ============================================================
-- COMMENT ENGINE (L01-L04, L55)
-- ============================================================

-- Posts found on target accounts that we could comment on
CREATE TABLE IF NOT EXISTS li_comment_opportunities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    creator_name TEXT NOT NULL,
    creator_slug TEXT NOT NULL,
    post_url TEXT NOT NULL,
    post_text TEXT NOT NULL,
    post_engagement_json TEXT,
    post_age_hours REAL,
    existing_themes_json TEXT,
    score INTEGER DEFAULT 0,
    status TEXT DEFAULT 'new'
        CHECK(status IN ('new','drafted','posting','posted','skipped','expired')),
    tier TEXT,
    territory TEXT,
    draft_text TEXT,
    draft_scores_json TEXT,
    found_at TEXT NOT NULL,
    drafted_at TEXT,
    posted_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_li_opp_status ON li_comment_opportunities(status);
CREATE INDEX IF NOT EXISTS idx_li_opp_found ON li_comment_opportunities(found_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_li_opp_url ON li_comment_opportunities(post_url);

-- Comments we have posted on others' LinkedIn posts
CREATE TABLE IF NOT EXISTS li_comments_posted (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    opportunity_id INTEGER,
    creator_name TEXT NOT NULL,
    creator_slug TEXT NOT NULL,
    post_url TEXT NOT NULL,
    comment_text TEXT NOT NULL,
    scores_json TEXT,
    territory TEXT,
    tier TEXT,
    posted_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_li_cpost_at ON li_comments_posted(posted_at);
CREATE INDEX IF NOT EXISTS idx_li_cpost_slug ON li_comments_posted(creator_slug);

-- Comments received on our own LinkedIn posts
CREATE TABLE IF NOT EXISTS li_comments_received (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    our_post_id INTEGER,
    our_post_url TEXT NOT NULL,
    commenter_name TEXT,
    commenter_slug TEXT,
    commenter_headline TEXT,
    comment_text TEXT NOT NULL,
    comment_type TEXT DEFAULT 'unknown'
        CHECK(comment_type IN ('substantive','agreement','question','disagreement','spam','unknown')),
    replied INTEGER DEFAULT 0,
    reply_text TEXT,
    reply_posted_at TEXT,
    found_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_li_crec_url ON li_comments_received(our_post_url);
CREATE INDEX IF NOT EXISTS idx_li_crec_replied ON li_comments_received(replied);

-- ============================================================
-- POST METRICS (L26-L29)
-- ============================================================

CREATE TABLE IF NOT EXISTS li_post_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id INTEGER,
    post_url TEXT,
    impressions INTEGER DEFAULT 0,
    likes INTEGER DEFAULT 0,
    comments INTEGER DEFAULT 0,
    shares INTEGER DEFAULT 0,
    saves INTEGER DEFAULT 0,
    scraped_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_li_metrics_post ON li_post_metrics(post_id);

CREATE TABLE IF NOT EXISTS li_profile_analytics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_views_7d INTEGER DEFAULT 0,
    search_appearances_7d INTEGER DEFAULT 0,
    post_impressions_7d INTEGER DEFAULT 0,
    top_viewer_titles_json TEXT,
    top_viewer_companies_json TEXT,
    checked_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS li_followers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    count INTEGER NOT NULL,
    new_7d INTEGER DEFAULT 0,
    top_industries_json TEXT,
    top_titles_json TEXT,
    checked_at TEXT NOT NULL
);

-- ============================================================
-- COMMENT QUALITY ANALYSIS (L29)
-- ============================================================

CREATE TABLE IF NOT EXISTS li_comment_quality (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id INTEGER,
    total_comments INTEGER DEFAULT 0,
    substantive_count INTEGER DEFAULT 0,
    agreement_count INTEGER DEFAULT 0,
    question_count INTEGER DEFAULT 0,
    debate_count INTEGER DEFAULT 0,
    spam_count INTEGER DEFAULT 0,
    avg_commenter_seniority REAL,
    thread_depth_avg REAL,
    analyzed_at TEXT NOT NULL
);

-- ============================================================
-- SIGNALS (L07-L11)
-- ============================================================

CREATE TABLE IF NOT EXISTS li_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    signal_type TEXT NOT NULL,
    title TEXT,
    content TEXT NOT NULL,
    author TEXT,
    engagement_json TEXT,
    territory TEXT,
    relevance_score REAL DEFAULT 0,
    scraped_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_li_signals_type ON li_signals(signal_type);
CREATE INDEX IF NOT EXISTS idx_li_signals_at ON li_signals(scraped_at);

-- ============================================================
-- CONNECTIONS (L05, L51)
-- ============================================================

CREATE TABLE IF NOT EXISTS li_connections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    slug TEXT,
    headline TEXT,
    source TEXT,
    score INTEGER DEFAULT 0,
    request_sent INTEGER DEFAULT 0,
    request_sent_at TEXT,
    request_accepted INTEGER DEFAULT 0,
    note_text TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_li_conn_slug ON li_connections(slug);

-- ============================================================
-- LEARNING (L30-L35)
-- ============================================================

CREATE TABLE IF NOT EXISTS li_format_performance (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    format_type TEXT NOT NULL,
    avg_impressions REAL DEFAULT 0,
    avg_comments REAL DEFAULT 0,
    avg_saves REAL DEFAULT 0,
    avg_shares REAL DEFAULT 0,
    sample_count INTEGER DEFAULT 0,
    weight REAL DEFAULT 0.1,
    computed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS li_hook_performance (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hook_type TEXT NOT NULL,
    avg_distribution_rate REAL DEFAULT 0,
    avg_comments REAL DEFAULT 0,
    sample_count INTEGER DEFAULT 0,
    computed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS li_comment_target_roi (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_slug TEXT NOT NULL,
    total_comments_sent INTEGER DEFAULT 0,
    likes_on_our_comments INTEGER DEFAULT 0,
    replies_to_our_comments INTEGER DEFAULT 0,
    profile_visits_estimated INTEGER DEFAULT 0,
    followers_gained_est REAL DEFAULT 0,
    computed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_li_roi_slug ON li_comment_target_roi(target_slug);

-- ============================================================
-- ENGAGEMENT TRACKING
-- ============================================================

CREATE TABLE IF NOT EXISTS li_engagements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL,
    target_slug TEXT,
    target_post_url TEXT,
    detail TEXT,
    performed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_li_engage_at ON li_engagements(performed_at);

-- ============================================================
-- SCANNER STATE (for round-robin scanning)
-- ============================================================

CREATE TABLE IF NOT EXISTS li_scanner_state (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT NOT NULL UNIQUE,
    tier TEXT NOT NULL,
    last_scanned_at TEXT,
    scan_count INTEGER DEFAULT 0,
    last_post_found_at TEXT
);
"""


def init_linkedin_tables(db):
    """Create all LinkedIn engine tables. Safe to call multiple times."""
    db.executescript(LINKEDIN_SCHEMA)
    db.commit()
