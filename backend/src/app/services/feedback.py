"""Threshold-tuning feedback report (SPEC §5).

Replays logged clustering decisions against candidate τ values and combines them
with user corrections (labeled override pairs) to suggest better thresholds.
Human confirms before anything is applied — no online learning.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models import ClusterDecision, OverridePair

CANDIDATE_TAUS = [0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90]


async def threshold_report(session: AsyncSession) -> dict[str, object]:
    decisions = (
        await session.scalars(
            select(ClusterDecision).where(ClusterDecision.similarity.isnot(None))
        )
    ).all()
    pairs = (await session.scalars(select(OverridePair))).all()

    # Labeled similarities: 'same' pairs want attach (sim above τ),
    # 'different' pairs want separation. Join overrides to the decision row of
    # the same article to get its similarity (approximation — good enough for v1).
    sim_by_article = {d.article_id: d.similarity for d in decisions}
    labeled: list[tuple[float, bool]] = []  # (similarity, should_be_same)
    for p in pairs:
        sim = sim_by_article.get(p.article_id)
        if sim is not None:
            labeled.append((sim, p.label == "same"))

    evaluated: list[dict[str, float]] = []
    for tau in CANDIDATE_TAUS:
        tp = fp = tn = fn = 0
        for sim, should_same in labeled:
            predicted_same = sim >= tau
            if predicted_same and should_same:
                tp += 1
            elif predicted_same and not should_same:
                fp += 1
            elif not predicted_same and should_same:
                fn += 1
            else:
                tn += 1
        precision = tp / (tp + fp) if tp + fp else 1.0
        recall = tp / (tp + fn) if tp + fn else 1.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        evaluated.append(
            {"tau": tau, "precision": round(precision, 3), "recall": round(recall, 3),
             "f1": round(f1, 3)}
        )

    best = max(evaluated, key=lambda e: e["f1"]) if labeled else None
    return {
        "current": {"tau_attach": settings.tau_attach, "tau_gray": settings.tau_gray},
        "labeled_pairs": len(labeled),
        "decisions_logged": len(decisions),
        "candidates": evaluated,
        "suggested_tau_attach": best["tau"] if best and best["f1"] > 0 else None,
        "note": "Suggestions apply only on manual confirmation (admin settings).",
    }
