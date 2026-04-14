#!/usr/bin/env python3
"""
Cortex Regression — Actual regression model for predictions.

Replaces the weighted-average "prediction engine" with OLS/Ridge regression
over real features. Falls back to weighted averages when sample size is too small.

Features: territory (one-hot), hour_bucket, day_of_week, has_image, word_count,
          follower_count, session_tokens, minutes_since_last_post

Usage:
    from cortex_regression import CortexRegressor
    reg = CortexRegressor(db)
    reg.fit()                                    # train on historical predictions
    pred = reg.predict(features_dict)            # predict for new tweet
    importance = reg.feature_importance()         # which features explain variance

Or standalone:
    python3 cortex_regression.py --fit           # fit model and show results
    python3 cortex_regression.py --importance    # show feature importance
    python3 cortex_regression.py --validate      # train/test split validation
"""

import sqlite3
import json
import os
import sys
import math
import statistics
import pickle
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Dict, List, Tuple

IST = timezone(timedelta(hours=5, minutes=30))

WORKSPACE = Path(os.environ.get(
    "ARIA_WORKSPACE",
    os.path.expanduser("~/.openclaw/agents/aria/workspace")
))
DB_PATH = WORKSPACE / "memory" / "aria.db"
MODEL_PATH = WORKSPACE / "cortex" / "regression_model.json"

# Minimum predictions needed before we trust regression over weighted average
MIN_SAMPLES_FOR_REGRESSION = 30
MIN_SAMPLES_FOR_VALIDATION = 50

# Known territories and hook patterns for one-hot encoding
TERRITORIES = ["building", "organizations", "ai", "taste_agency"]
HOOK_PATTERNS = ["observation", "question", "inversion", "reframe", "story",
                 "provocation", "bookmark", "hook"]


class CortexRegressor:
    """
    Ridge regression for tweet/reply performance prediction.
    Pure Python implementation (no scikit-learn dependency).
    """

    def __init__(self, db: sqlite3.Connection):
        self.db = db
        self.weights = None
        self.bias = 0.0
        self.feature_names = []
        self.fitted = False
        self.stats = {}  # training stats
        self._try_load()

    def fit(self, target: str = "impressions", alpha: float = 1.0) -> dict:
        """
        Fit Ridge regression on historical prediction data.

        target: 'impressions' or 'engagement_rate'
        alpha: regularization strength (higher = more regularization)

        Returns training stats dict.
        """
        rows = self.db.execute(
            """SELECT territory, hook_pattern, hour_bucket, day_of_week,
                      features_json, confounders_json,
                      actual_impressions, actual_engagement_rate
               FROM predictions
               WHERE actual_impressions IS NOT NULL AND actual_impressions > 0
               ORDER BY ts ASC"""
        ).fetchall()

        if len(rows) < MIN_SAMPLES_FOR_REGRESSION:
            self.stats = {
                "status": "insufficient_data",
                "n": len(rows),
                "min_required": MIN_SAMPLES_FOR_REGRESSION,
            }
            return self.stats

        # Build feature matrix and target vector
        X, y, feature_names = self._build_matrix(rows, target)

        if not X or not y:
            self.stats = {"status": "empty_matrix"}
            return self.stats

        # Ridge regression: w = (X^T X + alpha * I)^{-1} X^T y
        n_features = len(X[0])
        n_samples = len(X)

        # Standardize features
        means = [0.0] * n_features
        stds = [1.0] * n_features
        for j in range(n_features):
            col = [X[i][j] for i in range(n_samples)]
            means[j] = statistics.mean(col)
            stds[j] = statistics.stdev(col) if len(col) > 1 and statistics.stdev(col) > 0 else 1.0

        X_std = []
        for i in range(n_samples):
            X_std.append([(X[i][j] - means[j]) / stds[j] for j in range(n_features)])

        y_mean = statistics.mean(y)
        y_std = statistics.stdev(y) if len(y) > 1 else 1.0
        y_centered = [(yi - y_mean) for yi in y]

        # X^T X
        XtX = [[0.0] * n_features for _ in range(n_features)]
        for i in range(n_samples):
            for j in range(n_features):
                for k in range(n_features):
                    XtX[j][k] += X_std[i][j] * X_std[i][k]

        # Add Ridge penalty
        for j in range(n_features):
            XtX[j][j] += alpha * n_samples

        # X^T y
        Xty = [0.0] * n_features
        for i in range(n_samples):
            for j in range(n_features):
                Xty[j] += X_std[i][j] * y_centered[i]

        # Solve via Gaussian elimination
        weights_std = self._solve_linear_system(XtX, Xty)

        if weights_std is None:
            self.stats = {"status": "solve_failed"}
            return self.stats

        # Unstandardize weights
        self.weights = [weights_std[j] / stds[j] for j in range(n_features)]
        self.bias = y_mean - sum(self.weights[j] * means[j] for j in range(n_features))
        self.feature_names = feature_names

        # Compute training error
        predictions = []
        for i in range(n_samples):
            pred = self.bias + sum(self.weights[j] * X[i][j] for j in range(n_features))
            predictions.append(pred)

        residuals = [y[i] - predictions[i] for i in range(n_samples)]
        mse = statistics.mean([r ** 2 for r in residuals])
        mae = statistics.mean([abs(r) for r in residuals])

        # R-squared
        ss_res = sum(r ** 2 for r in residuals)
        ss_tot = sum((yi - y_mean) ** 2 for yi in y)
        r_squared = 1 - (ss_res / ss_tot) if ss_tot > 0 else 0

        self.fitted = True
        self.stats = {
            "status": "fitted",
            "n": n_samples,
            "n_features": n_features,
            "target": target,
            "alpha": alpha,
            "r_squared": r_squared,
            "mse": mse,
            "mae": mae,
            "rmse": math.sqrt(mse),
            "y_mean": y_mean,
            "y_std": y_std,
        }

        self._save()
        return self.stats

    def predict(self, features: dict) -> Optional[float]:
        """
        Predict for a new tweet given features dict.
        Returns predicted impressions/engagement_rate, or None if model not fitted.
        """
        if not self.fitted or not self.weights:
            return None

        x = self._encode_features(features)
        if len(x) != len(self.weights):
            return None

        return self.bias + sum(self.weights[j] * x[j] for j in range(len(x)))

    def feature_importance(self) -> List[Tuple[str, float]]:
        """
        Return feature importance as (name, |weight|) sorted descending.
        This directly answers: "which features explain variance?"
        """
        if not self.fitted or not self.weights:
            return []

        importance = list(zip(self.feature_names, [abs(w) for w in self.weights]))
        importance.sort(key=lambda x: x[1], reverse=True)
        return importance

    def validate(self, test_ratio: float = 0.2) -> dict:
        """
        Train/test split validation.
        Returns test error metrics.
        """
        rows = self.db.execute(
            """SELECT territory, hook_pattern, hour_bucket, day_of_week,
                      features_json, confounders_json,
                      actual_impressions, actual_engagement_rate
               FROM predictions
               WHERE actual_impressions IS NOT NULL AND actual_impressions > 0
               ORDER BY ts ASC"""
        ).fetchall()

        if len(rows) < MIN_SAMPLES_FOR_VALIDATION:
            return {
                "status": "insufficient_data",
                "n": len(rows),
                "min_required": MIN_SAMPLES_FOR_VALIDATION,
            }

        split = int(len(rows) * (1 - test_ratio))
        train_rows = rows[:split]
        test_rows = rows[split:]

        # Build matrices
        X_train, y_train, feature_names = self._build_matrix(train_rows, "impressions")
        X_test, y_test, _ = self._build_matrix(test_rows, "impressions")

        if not X_train or not X_test:
            return {"status": "empty_matrix"}

        n_features = len(X_train[0])

        # Fit on training data (simplified inline Ridge)
        n = len(X_train)
        means = [statistics.mean([X_train[i][j] for i in range(n)]) for j in range(n_features)]
        stds = [statistics.stdev([X_train[i][j] for i in range(n)]) or 1.0 for j in range(n_features)]

        X_std = [[(X_train[i][j] - means[j]) / stds[j] for j in range(n_features)] for i in range(n)]
        y_mean = statistics.mean(y_train)
        y_c = [yi - y_mean for yi in y_train]

        XtX = [[sum(X_std[i][j] * X_std[i][k] for i in range(n)) for k in range(n_features)]
               for j in range(n_features)]
        for j in range(n_features):
            XtX[j][j] += 1.0 * n

        Xty = [sum(X_std[i][j] * y_c[i] for i in range(n)) for j in range(n_features)]

        w_std = self._solve_linear_system(XtX, Xty)
        if w_std is None:
            return {"status": "solve_failed"}

        w = [w_std[j] / stds[j] for j in range(n_features)]
        b = y_mean - sum(w[j] * means[j] for j in range(n_features))

        # Evaluate on test data
        test_preds = [b + sum(w[j] * X_test[i][j] for j in range(n_features))
                      for i in range(len(X_test))]
        test_errors = [abs(y_test[i] - test_preds[i]) for i in range(len(X_test))]

        within_50 = sum(1 for i in range(len(X_test))
                       if abs(y_test[i] - test_preds[i]) < 0.5 * max(y_test[i], test_preds[i], 1))

        return {
            "status": "validated",
            "train_n": len(X_train),
            "test_n": len(X_test),
            "test_mae": statistics.mean(test_errors),
            "test_within_50pct": within_50 / len(X_test) if X_test else 0,
            "overfit_risk": "low" if len(X_train) > n_features * 10 else "high",
        }

    # --- Internal ---

    def _build_matrix(self, rows, target: str) -> Tuple[list, list, list]:
        """Build feature matrix X and target vector y from prediction rows."""
        feature_names = (
            [f"territory_{t}" for t in TERRITORIES] +
            ["hour_sin", "hour_cos", "is_weekend",
             "has_image", "word_count", "follower_count_log",
             "session_tokens_log", "minutes_since_last_log"]
        )

        X = []
        y = []

        for row in rows:
            territory = row["territory"] or "unknown"
            hour = row["hour_bucket"] or 12
            dow = row["day_of_week"] or 0

            features = {}
            confounders = {}
            try:
                features = json.loads(row["features_json"] or "{}")
            except (json.JSONDecodeError, TypeError):
                pass
            try:
                confounders = json.loads(row["confounders_json"] or "{}")
            except (json.JSONDecodeError, TypeError):
                pass

            x = []
            # Territory one-hot
            for t in TERRITORIES:
                x.append(1.0 if territory == t else 0.0)

            # Cyclical hour encoding
            x.append(math.sin(2 * math.pi * hour / 24))
            x.append(math.cos(2 * math.pi * hour / 24))

            # Weekend
            x.append(1.0 if dow >= 5 else 0.0)

            # Content features
            x.append(1.0 if features.get("has_image") else 0.0)
            x.append(math.log1p(features.get("word_count", 20)))

            # Confounder features
            x.append(math.log1p(confounders.get("follower_count", 0)))
            x.append(math.log1p(confounders.get("session_token_count", 0)))
            x.append(math.log1p(confounders.get("minutes_since_last_post", 60)))

            X.append(x)

            if target == "impressions":
                y.append(math.log1p(row["actual_impressions"] or 0))  # log-transform for better regression
            else:
                y.append(row["actual_engagement_rate"] or 0)

        return X, y, feature_names

    def _encode_features(self, features: dict) -> list:
        """Encode a single feature dict into vector matching training format."""
        territory = features.get("territory", "unknown")
        hour = features.get("hour_bucket", 12)
        dow = features.get("day_of_week", 0)

        x = []
        for t in TERRITORIES:
            x.append(1.0 if territory == t else 0.0)

        x.append(math.sin(2 * math.pi * hour / 24))
        x.append(math.cos(2 * math.pi * hour / 24))
        x.append(1.0 if dow >= 5 else 0.0)
        x.append(1.0 if features.get("has_image") else 0.0)
        x.append(math.log1p(features.get("word_count", 20)))
        x.append(math.log1p(features.get("follower_count", 0)))
        x.append(math.log1p(features.get("session_token_count", 0)))
        x.append(math.log1p(features.get("minutes_since_last_post", 60)))

        return x

    @staticmethod
    def _solve_linear_system(A: list, b: list) -> Optional[list]:
        """
        Solve Ax = b using Gaussian elimination with partial pivoting.
        Returns x or None if singular.
        """
        n = len(b)
        # Augmented matrix
        M = [row[:] + [b[i]] for i, row in enumerate(A)]

        for col in range(n):
            # Partial pivoting
            max_row = col
            max_val = abs(M[col][col])
            for row in range(col + 1, n):
                if abs(M[row][col]) > max_val:
                    max_val = abs(M[row][col])
                    max_row = row
            if max_val < 1e-12:
                return None
            M[col], M[max_row] = M[max_row], M[col]

            # Eliminate
            pivot = M[col][col]
            for row in range(col + 1, n):
                factor = M[row][col] / pivot
                for j in range(col, n + 1):
                    M[row][j] -= factor * M[col][j]

        # Back substitution
        x = [0.0] * n
        for i in range(n - 1, -1, -1):
            if abs(M[i][i]) < 1e-12:
                return None
            x[i] = (M[i][n] - sum(M[i][j] * x[j] for j in range(i + 1, n))) / M[i][i]

        return x

    def _save(self):
        """Save model to JSON."""
        if not self.fitted:
            return
        MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "weights": self.weights,
            "bias": self.bias,
            "feature_names": self.feature_names,
            "stats": self.stats,
            "fitted_at": datetime.now(IST).isoformat(),
        }
        MODEL_PATH.write_text(json.dumps(data, indent=2))

    def _try_load(self):
        """Try to load a previously fitted model."""
        if not MODEL_PATH.exists():
            return
        try:
            data = json.loads(MODEL_PATH.read_text())
            self.weights = data["weights"]
            self.bias = data["bias"]
            self.feature_names = data["feature_names"]
            self.stats = data.get("stats", {})
            self.fitted = True
        except (json.JSONDecodeError, KeyError, OSError):
            pass


# ---------------------------------------------------------------------------
# Standalone
# ---------------------------------------------------------------------------

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Cortex Regression Model")
    parser.add_argument("--fit", action="store_true", help="Fit model on historical data")
    parser.add_argument("--importance", action="store_true", help="Show feature importance")
    parser.add_argument("--validate", action="store_true", help="Train/test split validation")
    parser.add_argument("--alpha", type=float, default=1.0, help="Ridge regularization strength")
    args = parser.parse_args()

    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    reg = CortexRegressor(db)

    if args.fit:
        print("Fitting regression model...")
        stats = reg.fit(alpha=args.alpha)
        print(json.dumps(stats, indent=2))

        if stats.get("status") == "fitted":
            print(f"\nModel fitted: R²={stats['r_squared']:.3f}, "
                  f"MAE={stats['mae']:.2f}, RMSE={stats['rmse']:.2f}")
            print(f"  (n={stats['n']}, features={stats['n_features']})")

            print("\nFeature importance:")
            for name, weight in reg.feature_importance()[:10]:
                bar = "#" * max(1, int(weight * 10))
                print(f"  {name:30s} {weight:8.4f} {bar}")

    elif args.importance:
        if not reg.fitted:
            print("Model not fitted. Run --fit first.")
        else:
            print("Feature importance:")
            for name, weight in reg.feature_importance():
                bar = "#" * max(1, int(weight * 10))
                print(f"  {name:30s} {weight:8.4f} {bar}")

    elif args.validate:
        print("Running train/test validation...")
        result = reg.validate()
        print(json.dumps(result, indent=2))

    else:
        parser.print_help()

    db.close()


if __name__ == "__main__":
    main()
