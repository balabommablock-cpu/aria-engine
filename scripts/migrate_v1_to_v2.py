#!/usr/bin/env python3
"""
Migrate ARIA Cortex v1 schema to v2.
Safe to run multiple times — only adds columns that don't exist.
Run BEFORE starting cortex v2 for the first time.

Usage:
    python3 migrate_v1_to_v2.py
    python3 migrate_v1_to_v2.py --dry-run
"""

import sqlite3
import os
import sys
import argparse
from pathlib import Path

WORKSPACE = Path(os.environ.get(
    "ARIA_WORKSPACE",
    os.path.expanduser("~/.openclaw/agents/aria/workspace")
))
DB_PATH = WORKSPACE / "memory" / "aria.db"
SCHEMA_V2_PATH = WORKSPACE / "cortex" / "aria-cortex-schema-v2.sql"


def get_existing_columns(db: sqlite3.Connection, table: str) -> set:
    """Get set of column names for a table."""
    try:
        cols = db.execute(f"PRAGMA table_info({table})").fetchall()
        return {c[1] for c in cols}
    except sqlite3.OperationalError:
        return set()


def table_exists(db: sqlite3.Connection, table: str) -> bool:
    row = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


# All v2 column additions, grouped by table.
# Format: (table, column_name, column_definition, default_value_or_None)
MIGRATIONS = [
    # predictions table
    ("predictions", "predicted_variance", "REAL", None),
    ("predictions", "z_score", "REAL", None),
    ("predictions", "is_spike", "INTEGER DEFAULT 0", None),
    ("predictions", "prediction_method", "TEXT DEFAULT 'baseline'", None),
    ("predictions", "confounders_json", "TEXT", None),

    # learned_constraints table
    ("learned_constraints", "target_field_2", "TEXT", None),
    ("learned_constraints", "target_value_2", "TEXT", None),
    ("learned_constraints", "is_compound", "INTEGER DEFAULT 0", None),
    ("learned_constraints", "source", "TEXT DEFAULT 'statistical'", None),
    ("learned_constraints", "spike_event_id", "INTEGER", None),
    ("learned_constraints", "world_model_id", "INTEGER", None),

    # cognitive_state table
    ("cognitive_state", "current_strategy", "TEXT", None),
    ("cognitive_state", "strategy_performance", "REAL", None),
    ("cognitive_state", "strategy_duration_cycles", "INTEGER", None),
    ("cognitive_state", "should_pivot", "INTEGER DEFAULT 0", None),
    ("cognitive_state", "identity_drift_score", "REAL DEFAULT 0", None),
    ("cognitive_state", "drift_details", "TEXT", None),

    # knob_experiments table
    ("knob_experiments", "mythos_belief_id", "INTEGER", None),
    ("knob_experiments", "metric_before_stddev", "REAL", None),
    ("knob_experiments", "metric_before_n", "INTEGER", None),
    ("knob_experiments", "metric_after_stddev", "REAL", None),
    ("knob_experiments", "metric_after_n", "INTEGER", None),
    ("knob_experiments", "t_statistic", "REAL", None),
    ("knob_experiments", "p_value", "REAL", None),
    ("knob_experiments", "effect_size", "REAL", None),
    ("knob_experiments", "power", "REAL", None),

    # prediction_performance table
    ("prediction_performance", "mean_z_score", "REAL", None),
    ("prediction_performance", "prediction_stddev", "REAL", None),
    ("prediction_performance", "actual_stddev", "REAL", None),
    ("prediction_performance", "correlation", "REAL", None),
    ("prediction_performance", "trend_method", "TEXT DEFAULT 'half_split'", None),
]

# New tables that don't exist in v1
NEW_TABLES = [
    "confounder_snapshots",
    "world_model",
    "spike_events",
    "spike_replication_scorecard",
]

# New indexes for existing tables
NEW_INDEXES = [
    ("idx_predictions_spikes", "CREATE INDEX IF NOT EXISTS idx_predictions_spikes ON predictions(is_spike) WHERE is_spike = 1"),
    ("idx_predictions_territory_ts", "CREATE INDEX IF NOT EXISTS idx_predictions_territory_ts ON predictions(territory, ts)"),
    ("idx_constraints_compound", "CREATE INDEX IF NOT EXISTS idx_constraints_compound ON learned_constraints(scope, target_field, target_value, target_field_2, target_value_2) WHERE active = 1 AND is_compound = 1"),
]


def run_migration(dry_run: bool = False):
    if not DB_PATH.exists():
        print(f"Database not found at {DB_PATH}")
        print("Run cortex v2 normally — it will create the schema from scratch.")
        return

    db = sqlite3.connect(str(DB_PATH))
    db.execute("PRAGMA journal_mode=WAL")

    print(f"Migrating {DB_PATH}")
    print(f"Dry run: {dry_run}\n")

    operations = []

    # Step 1: Add missing columns to existing tables
    for table, column, col_type, default in MIGRATIONS:
        if not table_exists(db, table):
            continue
        existing = get_existing_columns(db, table)
        if column not in existing:
            sql = f"ALTER TABLE {table} ADD COLUMN {column} {col_type}"
            operations.append(("ALTER", sql))

    # Step 2: Create new tables
    if SCHEMA_V2_PATH.exists():
        schema_sql = SCHEMA_V2_PATH.read_text()
        for table_name in NEW_TABLES:
            if not table_exists(db, table_name):
                # Extract the CREATE TABLE statement for this table from schema
                # Simple approach: just run the whole schema — CREATE TABLE IF NOT EXISTS is safe
                operations.append(("CREATE_TABLE", table_name))
    else:
        print(f"WARNING: Schema file not found at {SCHEMA_V2_PATH}")
        print("New tables (confounder_snapshots, world_model, spike_events, spike_replication_scorecard) will not be created.")

    # Step 3: Create new indexes
    for idx_name, idx_sql in NEW_INDEXES:
        operations.append(("INDEX", idx_sql))

    # Execute or print
    if not operations:
        print("Nothing to migrate — schema is already v2.")
        db.close()
        return

    print(f"Found {len(operations)} operations:\n")

    for op_type, op_sql in operations:
        if op_type == "CREATE_TABLE":
            print(f"  CREATE TABLE {op_sql}")
        else:
            print(f"  {op_sql}")

    if dry_run:
        print(f"\nDry run complete. Run without --dry-run to apply.")
        db.close()
        return

    print(f"\nApplying...")

    # Apply column additions
    for op_type, op_sql in operations:
        if op_type == "ALTER":
            try:
                db.execute(op_sql)
                print(f"  OK: {op_sql}")
            except sqlite3.OperationalError as e:
                if "duplicate column" in str(e).lower():
                    print(f"  SKIP (already exists): {op_sql}")
                else:
                    print(f"  ERROR: {op_sql} — {e}")
        elif op_type == "INDEX":
            try:
                db.execute(op_sql)
                print(f"  OK: {op_sql}")
            except sqlite3.OperationalError as e:
                print(f"  SKIP: {op_sql} — {e}")

    # Apply new table creation via full schema (IF NOT EXISTS makes this safe)
    new_tables_needed = any(op[0] == "CREATE_TABLE" for op in operations)
    if new_tables_needed and SCHEMA_V2_PATH.exists():
        try:
            schema_sql = SCHEMA_V2_PATH.read_text()
            db.executescript(schema_sql)
            print(f"  OK: Ran full v2 schema (CREATE TABLE IF NOT EXISTS)")
        except sqlite3.OperationalError as e:
            print(f"  ERROR running schema: {e}")

    db.commit()
    db.close()

    print(f"\nMigration complete.")

    # Verify
    db = sqlite3.connect(str(DB_PATH))
    tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    print(f"\nTables in DB: {sorted(tables)}")

    for table_name in NEW_TABLES:
        if table_exists(db, table_name):
            print(f"  ✓ {table_name}")
        else:
            print(f"  ✗ {table_name} MISSING")

    db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Migrate Cortex v1 schema to v2")
    parser.add_argument("--dry-run", action="store_true", help="Show operations without executing")
    args = parser.parse_args()
    run_migration(dry_run=args.dry_run)
