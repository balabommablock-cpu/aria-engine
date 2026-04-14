-- ARIA Cortex Schema v2
-- Rebuilt: prediction with confounders, mythos world model, spike replication, statistical rigor
-- Run against aria.db. Safe to run alongside v1 tables (additive).

-- ============================================================
-- 1. PREDICTIONS (v2 — expanded features, variance tracking)
-- ============================================================

CREATE TABLE IF NOT EXISTS predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    action_type TEXT NOT NULL,
    action_ref TEXT,
    territory TEXT,
    hook_pattern TEXT,
    target_handle TEXT,
    hour_bucket INTEGER,
    day_of_week INTEGER,

    -- prediction
    predicted_impressions REAL,
    predicted_engagements REAL,
    predicted_engagement_rate REAL,
    predicted_variance REAL,             -- NEW: expected variance (how uncertain we are)
    confidence REAL DEFAULT 0.5,
    reasoning TEXT,
    features_json TEXT,
    prediction_method TEXT DEFAULT 'baseline',  -- NEW: baseline, regression, mythos_guided

    -- confounders snapshot at prediction time
    confounders_json TEXT,               -- NEW: follower_count, session_tokens, time_since_last_post, etc.

    -- outcome
    actual_impressions REAL,
    actual_engagements REAL,
    actual_engagement_rate REAL,
    measured_at TEXT,

    -- learning signal
    error_impressions REAL,
    error_engagements REAL,
    surprise_score REAL,
    z_score REAL,                        -- NEW: how many stddevs from expected (accounts for variance)
    lesson TEXT,
    is_spike INTEGER DEFAULT 0           -- NEW: 1 if this was a top-5% outlier
);

CREATE INDEX IF NOT EXISTS idx_predictions_action ON predictions(action_type, ts);
CREATE INDEX IF NOT EXISTS idx_predictions_unmeasured ON predictions(actual_impressions) WHERE actual_impressions IS NULL;
CREATE INDEX IF NOT EXISTS idx_predictions_spikes ON predictions(is_spike) WHERE is_spike = 1;
CREATE INDEX IF NOT EXISTS idx_predictions_territory_ts ON predictions(territory, ts);


-- ============================================================
-- 2. CONFOUNDERS LOG
-- External variables that affect outcomes but aren't content features.
-- Snapshot taken at prediction time, stored in predictions.confounders_json
-- but also tracked independently for trend analysis.
-- ============================================================

CREATE TABLE IF NOT EXISTS confounder_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    prediction_id INTEGER,

    -- Claude generation quality proxies
    session_token_count INTEGER,         -- tokens used in current brain.py session
    candidates_generated_this_session INTEGER,
    session_age_minutes REAL,            -- how long current Claude session has been running
    generation_model TEXT,               -- which Claude model was used

    -- Account state
    follower_count INTEGER,
    following_count INTEGER,
    account_age_days INTEGER,

    -- Posting context
    minutes_since_last_post REAL,
    posts_last_24h INTEGER,
    posts_last_7d INTEGER,
    avg_engagement_last_7d REAL,         -- rolling baseline

    -- Reply context (NULL for tweets)
    parent_tweet_age_minutes REAL,       -- how old was the tweet when we replied
    parent_tweet_impressions REAL,       -- parent tweet's impressions at reply time
    parent_tweet_velocity REAL,          -- parent's engagement per minute
    parent_author_follower_count INTEGER,

    -- External context
    is_weekend INTEGER,
    is_indian_holiday INTEGER,
    trending_topics_overlap INTEGER,     -- how many of our territories match current trends

    FOREIGN KEY (prediction_id) REFERENCES predictions(id)
);

CREATE INDEX IF NOT EXISTS idx_confounders_pred ON confounder_snapshots(prediction_id);


-- ============================================================
-- 3. MYTHOS — World Model
-- Narrative hypotheses about HOW and WHY things work.
-- Not numeric constraints. Causal beliefs that guide strategy.
-- Updated by Claude analyzing spikes and surprises.
-- ============================================================

CREATE TABLE IF NOT EXISTS world_model (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),

    -- The belief
    belief_type TEXT NOT NULL,            -- audience_model, platform_model, content_model, identity_model
    belief TEXT NOT NULL,                 -- "My audience responds to contrarian takes on mainstream AI narratives because they're practitioners who see through hype"
    belief_confidence REAL DEFAULT 0.5,  -- 0-1, updated by evidence

    -- Evidence
    supporting_evidence TEXT,             -- JSON: prediction IDs that support this
    contradicting_evidence TEXT,          -- JSON: prediction IDs that contradict this
    evidence_count INTEGER DEFAULT 0,
    last_tested TEXT,                     -- when was this belief last relevant to a prediction

    -- Lifecycle
    source TEXT NOT NULL,                -- seed, spike_analysis, surprise_analysis, hypothesis_test, human
    parent_belief_id INTEGER,            -- if this evolved from another belief
    status TEXT DEFAULT 'active',        -- active, tested, revised, deprecated, disproven
    deprecated_reason TEXT,

    -- Identity guard: does this belief serve the account's identity?
    serves_identity INTEGER DEFAULT 1,   -- 1 = yes, 0 = neutral, -1 = conflicts with identity
    identity_note TEXT,                  -- "optimizing for rage-bait would get engagement but kill credibility"

    FOREIGN KEY (parent_belief_id) REFERENCES world_model(id)
);

CREATE INDEX IF NOT EXISTS idx_world_model_active ON world_model(status, belief_type);
CREATE INDEX IF NOT EXISTS idx_world_model_confidence ON world_model(belief_confidence DESC);


-- ============================================================
-- 4. SPIKE EVENTS — Outlier success analysis
-- When something works 3x+ better than expected, WHY?
-- This is where the actual learning happens.
-- ============================================================

CREATE TABLE IF NOT EXISTS spike_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    prediction_id INTEGER NOT NULL,

    -- What happened
    actual_impressions REAL,
    expected_impressions REAL,
    spike_magnitude REAL,                -- actual / expected
    z_score REAL,                        -- how many stddevs above mean

    -- Analysis (filled by Claude)
    analysis_prompt TEXT,                 -- what we asked Claude
    analysis_response TEXT,              -- what Claude said
    identified_factors TEXT,             -- JSON: ["contrarian angle on trending topic", "posted within 15min of trend", ...]
    replicable_factors TEXT,             -- JSON: subset of factors we can actually replicate
    non_replicable_factors TEXT,         -- JSON: factors that were lucky/contextual

    -- Replication tracking
    replication_attempted INTEGER DEFAULT 0,
    replication_count INTEGER DEFAULT 0, -- how many times we tried to replicate
    replication_success_count INTEGER DEFAULT 0, -- how many replications also spiked
    replication_success_rate REAL,
    avg_replication_magnitude REAL,      -- avg spike magnitude of replications

    -- Link to world model
    beliefs_created TEXT,                -- JSON: world_model IDs created from this spike
    beliefs_reinforced TEXT,             -- JSON: world_model IDs this spike supports

    FOREIGN KEY (prediction_id) REFERENCES predictions(id)
);

CREATE INDEX IF NOT EXISTS idx_spikes_magnitude ON spike_events(spike_magnitude DESC);
CREATE INDEX IF NOT EXISTS idx_spikes_replication ON spike_events(replication_success_rate DESC);


-- ============================================================
-- 5. KNOB STATE + EXPERIMENTS (v2 — statistical rigor)
-- ============================================================

CREATE TABLE IF NOT EXISTS knob_state (
    knob_name TEXT PRIMARY KEY,
    current_value REAL NOT NULL,
    default_value REAL NOT NULL,
    min_value REAL NOT NULL,
    max_value REAL NOT NULL,
    value_type TEXT DEFAULT 'float',
    description TEXT,
    last_modified TEXT,
    modified_by TEXT DEFAULT 'default'
);

CREATE TABLE IF NOT EXISTS knob_experiments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    knob_name TEXT NOT NULL,
    old_value REAL NOT NULL,
    new_value REAL NOT NULL,
    hypothesis TEXT NOT NULL,
    rationale TEXT,
    mythos_belief_id INTEGER,            -- NEW: which world model belief motivated this

    status TEXT DEFAULT 'proposed',
    approved_at TEXT,
    started_at TEXT,
    duration_hours REAL DEFAULT 48,

    -- Measurement (v2: with variance)
    metric_name TEXT DEFAULT 'engagement_rate',
    metric_before REAL,
    metric_before_stddev REAL,           -- NEW: stddev of baseline
    metric_before_n INTEGER,             -- NEW: sample size of baseline
    metric_after REAL,
    metric_after_stddev REAL,            -- NEW: stddev of experiment period
    metric_after_n INTEGER,              -- NEW: sample size of experiment

    -- Statistical test
    t_statistic REAL,                    -- NEW: Welch's t-test
    p_value REAL,                        -- NEW: statistical significance
    effect_size REAL,                    -- NEW: Cohen's d
    power REAL,                          -- NEW: statistical power estimate

    verdict TEXT,
    verdict_reasoning TEXT,
    concluded_at TEXT,

    FOREIGN KEY (knob_name) REFERENCES knob_state(knob_name),
    FOREIGN KEY (mythos_belief_id) REFERENCES world_model(id)
);

CREATE INDEX IF NOT EXISTS idx_experiments_status ON knob_experiments(status);
CREATE INDEX IF NOT EXISTS idx_experiments_knob ON knob_experiments(knob_name, ts);


-- ============================================================
-- 6. LEARNED CONSTRAINTS (v2 — compound constraints)
-- ============================================================

CREATE TABLE IF NOT EXISTS learned_constraints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    constraint_type TEXT NOT NULL,
    scope TEXT NOT NULL,

    -- Single field match (backward compat)
    target_field TEXT NOT NULL,
    target_value TEXT NOT NULL,

    -- NEW: Compound constraints (optional second dimension)
    target_field_2 TEXT,                 -- e.g., "hour_bucket"
    target_value_2 TEXT,                 -- e.g., "9"
    is_compound INTEGER DEFAULT 0,       -- 1 if both fields must match

    modifier REAL NOT NULL,
    reason TEXT NOT NULL,
    evidence_ids TEXT,
    evidence_strength REAL DEFAULT 0.5,
    min_observations INTEGER DEFAULT 5,
    observation_count INTEGER DEFAULT 0,
    active INTEGER DEFAULT 0,
    deprecated_at TEXT,
    deprecated_reason TEXT,

    -- NEW: Source tracking
    source TEXT DEFAULT 'statistical',   -- statistical, spike_analysis, mythos, human
    spike_event_id INTEGER,              -- if learned from a spike
    world_model_id INTEGER,              -- if derived from a world model belief

    FOREIGN KEY (spike_event_id) REFERENCES spike_events(id),
    FOREIGN KEY (world_model_id) REFERENCES world_model(id)
);

CREATE INDEX IF NOT EXISTS idx_constraints_active ON learned_constraints(active, scope);
CREATE INDEX IF NOT EXISTS idx_constraints_lookup ON learned_constraints(scope, target_field, target_value) WHERE active = 1;
CREATE INDEX IF NOT EXISTS idx_constraints_compound ON learned_constraints(scope, target_field, target_value, target_field_2, target_value_2) WHERE active = 1 AND is_compound = 1;


-- ============================================================
-- 7. COGNITIVE CONTINUITY (v2 — behavioral, not just logging)
-- ============================================================

CREATE TABLE IF NOT EXISTS cognitive_state (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    cycle_number INTEGER NOT NULL,

    active_goals TEXT NOT NULL,           -- JSON: DYNAMIC goals, not hardcoded
    active_experiments TEXT,
    attention_focus TEXT,
    pending_questions TEXT,
    working_hypotheses TEXT,
    recent_surprises TEXT,

    -- Metacognition
    confidence_level REAL DEFAULT 0.5,
    exploration_vs_exploitation REAL DEFAULT 0.5,
    narrative TEXT,

    -- NEW: Strategy state
    current_strategy TEXT,               -- JSON: what strategy is cortex currently executing
    strategy_performance REAL,           -- how well is current strategy doing
    strategy_duration_cycles INTEGER,    -- how long have we been on this strategy
    should_pivot INTEGER DEFAULT 0,      -- 1 if performance suggests strategy change

    -- NEW: Identity coherence
    identity_drift_score REAL DEFAULT 0, -- 0 = on brand, 1 = fully off brand
    drift_details TEXT,                  -- what's drifting and why

    -- Continuity
    previous_state_id INTEGER,
    state_delta TEXT,
    FOREIGN KEY (previous_state_id) REFERENCES cognitive_state(id)
);

CREATE INDEX IF NOT EXISTS idx_cognitive_latest ON cognitive_state(ts DESC);


-- ============================================================
-- 8. AGGREGATE TRACKING (v2)
-- ============================================================

CREATE TABLE IF NOT EXISTS prediction_performance (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    window_label TEXT NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    action_type TEXT,

    prediction_count INTEGER,
    mean_abs_error REAL,
    median_abs_error REAL,
    mean_surprise REAL,
    accuracy_within_50pct REAL,
    mean_z_score REAL,                   -- NEW

    -- NEW: variance tracking
    prediction_stddev REAL,              -- stddev of predicted values
    actual_stddev REAL,                  -- stddev of actual values
    correlation REAL,                    -- Pearson r between predicted and actual

    trend TEXT,
    trend_slope REAL,
    trend_method TEXT DEFAULT 'half_split', -- NEW: half_split, linear_regression
    notes TEXT
);

-- NEW: Spike replication scorecard
CREATE TABLE IF NOT EXISTS spike_replication_scorecard (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    window_label TEXT NOT NULL,

    spikes_detected INTEGER,
    spikes_analyzed INTEGER,
    replications_attempted INTEGER,
    replications_succeeded INTEGER,
    replication_rate REAL,               -- THE key metric: can we reproduce success?

    -- Post-spike performance
    avg_performance_10_after_spike REAL,  -- avg engagement of 10 tweets after a spike
    avg_performance_baseline REAL,        -- normal baseline
    post_spike_lift REAL,                 -- ratio: does a spike improve subsequent performance?

    notes TEXT
);

CREATE TABLE IF NOT EXISTS cortex_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    component TEXT NOT NULL,
    event_type TEXT NOT NULL,
    message TEXT NOT NULL,
    data_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_cortex_log_ts ON cortex_log(ts DESC);
CREATE INDEX IF NOT EXISTS idx_cortex_log_component ON cortex_log(component, ts DESC);
