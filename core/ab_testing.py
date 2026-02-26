"""A/B Testing Framework — systematic experimentation for optimal content.

Experiments can test:
- Tone variations (e.g. helpful_casual vs creator_mentor in r/NewTubers)
- Content length (short vs long responses)
- Promotional vs organic ratios

Lightweight: no extra HTTP requests, just changes content generation params.
"""

import logging
from typing import Dict, List, Optional, Tuple

from core.database import Database

logger = logging.getLogger(__name__)

SIGNIFICANCE_THRESHOLD = 0.15  # 15% difference to declare winner
MAX_EXPERIMENT_DAYS = 14  # Cancel experiments older than this


class ABTestingEngine:
    """Manages A/B experiments and assigns variants.

    Usage:
        engine = ABTestingEngine(db)
        engine.create_experiment("my_project", "tone_NewTubers", "tone",
                                "helpful_casual", "creator_mentor")
        exp_id, variant, value = engine.get_variant("my_project", "tone")
        engine.record_result(exp_id, action_id, variant, engagement=2.5)
        engine.evaluate_experiments()  # during learning cycle
    """

    def __init__(self, db: Database):
        self.db = db

    def create_experiment(
        self, project: str, name: str, variable: str,
        variant_a: str, variant_b: str, min_samples: int = 10,
    ) -> Optional[int]:
        """Create a new A/B experiment if none exists for this variable+project."""
        existing = self.db.get_running_experiments(project)
        for exp in existing:
            if exp["variable"] == variable:
                logger.debug(
                    f"Experiment already running for {variable} in {project}"
                )
                return None

        exp_id = self.db.create_experiment(
            project, name, variable, variant_a, variant_b, min_samples,
        )
        logger.info(
            f"Created A/B experiment: {name} ({variant_a} vs {variant_b})"
        )
        return exp_id

    def get_variant(
        self, project: str, variable: str,
    ) -> Tuple[Optional[int], Optional[str], Optional[str]]:
        """Get the variant to use for this action.

        Returns (experiment_id, variant_label, variant_value).
        Returns (None, None, None) if no experiment is running.
        """
        experiments = self.db.get_running_experiments(project)
        for exp in experiments:
            if exp["variable"] == variable:
                # Balanced assignment: alternate based on count parity
                results = self.db.get_experiment_results(exp["id"])
                count_a = results.get("a", {}).get("count", 0)
                count_b = results.get("b", {}).get("count", 0)

                if count_a <= count_b:
                    return (exp["id"], "a", exp["variant_a"])
                else:
                    return (exp["id"], "b", exp["variant_b"])

        return (None, None, None)

    def record_result(
        self, experiment_id: int, action_id: int, variant: str,
        engagement: float = 0.0, upvotes: int = 0,
        replies: int = 0, was_removed: bool = False,
    ):
        """Record the outcome of an action in an A/B experiment."""
        self.db.log_ab_result(
            experiment_id, action_id, variant,
            engagement, upvotes, replies, was_removed,
        )

    def evaluate_experiments(self):
        """Evaluate all running experiments. Called during learning cycle."""
        experiments = self.db.get_running_experiments()
        for exp in experiments:
            self._evaluate_one(exp)

    def _evaluate_one(self, exp: Dict):
        """Evaluate a single experiment."""
        results = self.db.get_experiment_results(exp["id"])
        a_data = results.get("a", {})
        b_data = results.get("b", {})

        count_a = a_data.get("count", 0)
        count_b = b_data.get("count", 0)
        min_samples = exp.get("min_samples", 10)

        # Check if both variants have enough samples
        if count_a >= min_samples and count_b >= min_samples:
            avg_a = a_data.get("avg_eng", 0)
            avg_b = b_data.get("avg_eng", 0)

            if avg_a == 0 and avg_b == 0:
                winner = "tie"
            elif avg_a == 0:
                winner = "b"
            elif avg_b == 0:
                winner = "a"
            else:
                diff = abs(avg_a - avg_b) / max(avg_a, avg_b)
                if diff > SIGNIFICANCE_THRESHOLD:
                    winner = "a" if avg_a > avg_b else "b"
                else:
                    winner = "tie"

            self.db.conclude_experiment(exp["id"], winner)
            winning_value = exp["variant_a"] if winner == "a" else exp["variant_b"]
            logger.info(
                f"A/B experiment '{exp['experiment_name']}' concluded: "
                f"winner={winner} ({winning_value}), "
                f"A={avg_a:.2f} (n={count_a}), B={avg_b:.2f} (n={count_b})"
            )

            # Apply winner to learned weights
            if winner != "tie":
                try:
                    self.db.update_learned_weight(
                        category=exp["variable"],
                        key=winning_value,
                        project=exp["project"],
                        weight=max(avg_a, avg_b),
                        sample_count=count_a + count_b,
                        avg_engagement=max(avg_a, avg_b),
                    )
                except Exception as e:
                    logger.debug(f"Failed to update weight from A/B: {e}")

        else:
            # Check if experiment is too old
            from datetime import datetime
            created = datetime.fromisoformat(exp["created_at"])
            age_days = (datetime.utcnow() - created).days
            if age_days > MAX_EXPERIMENT_DAYS:
                self.db.conclude_experiment(exp["id"], "cancelled")
                logger.info(
                    f"A/B experiment '{exp['experiment_name']}' cancelled "
                    f"(insufficient data after {age_days} days)"
                )

    def auto_create_experiments(self, project: Dict):
        """Smart experiment creation based on learned weights.

        Tests 4 variables in priority order:
        1. Tone: if top 2 tones are within 20%
        2. Post type: if top 2 post types are within 25%
        3. Content length: short vs long if within 30%
        4. Promo ratio: if current ratio is in uncertain zone

        Max 2 concurrent experiments per project.
        """
        proj_name = project.get("project", {}).get("name", "unknown")

        running = self.db.get_running_experiments(proj_name)
        if len(running) >= 2:
            return

        running_vars = {exp["variable"] for exp in running}

        # Priority order: tone > post_type > content_length > promo_ratio
        if "tone" not in running_vars:
            if self._try_create_tone_experiment(proj_name):
                return

        if "post_type" not in running_vars:
            if self._try_create_post_type_experiment(proj_name):
                return

        if "content_length" not in running_vars:
            if self._try_create_length_experiment(proj_name):
                return

        if "promo_ratio" not in running_vars:
            if self._try_create_promo_experiment(proj_name):
                return

    def _try_create_tone_experiment(self, proj_name: str) -> bool:
        """Create tone experiment if top 2 are close."""
        tone_weights = self.db.get_learned_weights("tone", proj_name)
        if len(tone_weights) >= 2:
            top, second = tone_weights[0], tone_weights[1]
            if top["sample_count"] >= 3 and second["sample_count"] >= 3:
                diff = abs(top["weight"] - second["weight"])
                max_w = max(top["weight"], second["weight"], 0.01)
                if diff / max_w < 0.2:
                    self.create_experiment(
                        project=proj_name,
                        name=f"tone_{top['key']}_vs_{second['key']}",
                        variable="tone",
                        variant_a=top["key"],
                        variant_b=second["key"],
                    )
                    return True
        return False

    def _try_create_post_type_experiment(self, proj_name: str) -> bool:
        """Create post_type experiment if top 2 types are within 25%."""
        pt_weights = self.db.get_learned_weights("post_type", proj_name)
        if len(pt_weights) >= 2:
            top, second = pt_weights[0], pt_weights[1]
            if top["sample_count"] >= 5 and second["sample_count"] >= 5:
                diff = abs(top["weight"] - second["weight"])
                max_w = max(top["weight"], second["weight"], 0.01)
                if diff / max_w < 0.25:
                    self.create_experiment(
                        project=proj_name,
                        name=f"posttype_{top['key']}_vs_{second['key']}",
                        variable="post_type",
                        variant_a=top["key"],
                        variant_b=second["key"],
                        min_samples=8,
                    )
                    return True
        return False

    def _try_create_length_experiment(self, proj_name: str) -> bool:
        """Test short vs long content if both exist with similar performance."""
        try:
            rows = self.db.conn.execute(
                """SELECT
                    CASE WHEN content_length < 200 THEN 'short'
                         ELSE 'long' END as length_cat,
                    COUNT(*) as count, AVG(engagement_score) as avg_eng
                   FROM performance
                   WHERE project = ? AND content_length > 0
                   GROUP BY length_cat HAVING count >= 5""",
                (proj_name,),
            ).fetchall()

            if len(rows) >= 2:
                scores = {r["length_cat"]: r["avg_eng"] for r in rows}
                if "short" in scores and "long" in scores:
                    diff = abs(scores["short"] - scores["long"])
                    max_s = max(scores.values(), default=0.01)
                    if diff / max_s < 0.3:
                        self.create_experiment(
                            project=proj_name,
                            name="length_short_vs_long",
                            variable="content_length",
                            variant_a="short",
                            variant_b="long",
                            min_samples=10,
                        )
                        return True
        except Exception:
            pass
        return False

    def _try_create_promo_experiment(self, proj_name: str) -> bool:
        """Test promo ratios if current is in uncertain zone [10-35%]."""
        weights = self.db.get_learned_weights("content_type", proj_name)
        promo_w = organic_w = 0.0
        for w in weights:
            if w["key"] == "promotional":
                promo_w = w["weight"]
            elif w["key"] == "organic":
                organic_w = w["weight"]

        total = promo_w + organic_w
        if total > 0:
            current_ratio = promo_w / total
            if 0.10 <= current_ratio <= 0.35:
                low = f"{max(0.05, current_ratio - 0.10):.2f}"
                high = f"{min(0.40, current_ratio + 0.10):.2f}"
                self.create_experiment(
                    project=proj_name,
                    name=f"promo_{low}_vs_{high}",
                    variable="promo_ratio",
                    variant_a=low,
                    variant_b=high,
                    min_samples=15,
                )
                return True
        return False

    def get_active_experiments(self, project: str = "") -> List[Dict]:
        """Get all active experiments for reporting."""
        return self.db.get_running_experiments(project)
