"""Local, conservative team sizing. Predictions are estimates, not guarantees."""
import json
import math
import time
import uuid


class TeamOptimizer:
    def __init__(self, cache=None, project="", model=""):
        self.cache, self.project, self.model = cache, project, model
        self.plan = None
        self.assessment = {}
        if cache is not None:
            with cache.connect() as db:
                db.execute("""CREATE TABLE IF NOT EXISTS team_experience (
                    id TEXT PRIMARY KEY, project TEXT, model TEXT, created REAL,
                    payload TEXT NOT NULL)""")

    def history(self):
        if self.cache is None:
            return []
        with self.cache.connect() as db:
            rows = db.execute("SELECT payload FROM team_experience WHERE project=? AND model=? "
                              "ORDER BY created DESC LIMIT 100", (self.project, self.model)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def recommend(self, work_units=3, parallel_fraction=0.6, risk=0.5,
                  quality_weight=0.5, time_weight=0.25, cost_weight=0.25):
        values = [work_units, parallel_fraction, risk, quality_weight, time_weight, cost_weight]
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in values):
            raise ValueError("Параметры должны быть конечными числами.")
        if not 1 <= work_units <= 10000 or not 0 <= parallel_fraction <= 1 or not 0 <= risk <= 1:
            raise ValueError("work_units: 1–10000; parallel_fraction и risk: 0–1.")
        weights = [quality_weight, time_weight, cost_weight]
        if min(weights) < 0 or sum(weights) <= 0:
            raise ValueError("Веса неотрицательные, их сумма должна быть больше нуля.")
        weights = [v / sum(weights) for v in weights]
        history = [h for h in self.history() if h.get("outcome") in {"complete", "failed"}
                   and h.get("plan") and h["plan"]["work_units"] / work_units >= 0.5
                   and h["plan"]["work_units"] / work_units <= 2]
        # Amdahl time + pairwise coordination; saturating review benefit.
        candidates = []
        for workers in range(1, math.ceil(work_units) + 1):
            overhead = 0.04 * workers * (workers - 1) / work_units
            duration = (1 - parallel_fraction) + parallel_fraction / workers + overhead
            cost = 1 + 0.15 * workers + overhead
            defect = min(1, risk / (1 + 0.6 * (workers - 1)) + overhead * 0.15)
            samples = [h for h in history if len(h["agents"]) == workers]
            # Three prior observations avoid overfitting a single lucky run.
            defect = (3 * defect + sum(h["outcome"] == "failed" for h in samples)) / (3 + len(samples))
            measured = [h for h in samples if h.get("tokens") is not None]
            all_measured = [h for h in history if h.get("tokens") is not None]
            if measured and all_measured:
                baseline = sum(h["tokens"] / h["plan"]["work_units"] for h in all_measured) / len(all_measured)
                ratio = sum(h["tokens"] / h["plan"]["work_units"] for h in measured) / len(measured) / max(1, baseline)
                cost = (3 * cost + len(measured) * ratio) / (3 + len(measured))
            if samples and history:
                baseline = sum(h["seconds"] / h["plan"]["work_units"] for h in history) / len(history)
                ratio = sum(h["seconds"] / h["plan"]["work_units"] for h in samples) / len(samples) / max(0.001, baseline)
                duration = (3 * duration + len(samples) * ratio) / (3 + len(samples))
            score = weights[0] * defect + weights[1] * duration + weights[2] * cost
            candidates.append(dict(workers=workers, score=score, relative_time=duration,
                                   relative_tokens=cost, failure_proxy=defect, samples=len(samples)))
        best = min(candidates, key=lambda c: (c["score"], c["workers"]))
        self.plan = dict(work_units=work_units, parallel_fraction=parallel_fraction, risk=risk,
                         weights=weights, **best)
        return {"recommendation": self.plan, "total_with_supervisor": best["workers"] + 1,
                "alternatives": sorted(candidates, key=lambda c: c["score"])[:5],
                "history_samples": len(history), "advisory": True,
                "note": "Число исполнителей включает постановку, тестирование и ревью. Это рекомендация, не лимит. "
                        "Токены — приближение стоимости для одной модели, не цена API. "
                        "failure_proxy отражает сбои выполнения, а не доказанное качество кода."}

    def record(self, *, goal, agents, seconds, usage, outcome):
        record = dict(goal=goal, agents=agents, seconds=seconds, usage=usage, outcome=outcome,
                      tokens=usage.get("total_tokens"), plan=self.plan, assessment=self.assessment)
        if self.cache is not None:
            with self.cache.connect() as db:
                db.execute("INSERT INTO team_experience VALUES (?,?,?,?,?)",
                           (uuid.uuid4().hex, self.project, self.model, time.time(),
                            json.dumps(record, ensure_ascii=False)))
        return record
