"""Per-user anomaly detection shared by payment and sign-in scoring.

There are no labelled fraud cases to train on, so instead of a classifier we fit an
Isolation Forest on the user's own history and ask how unusual the new event is
compared with it. The model is small (a few hundred rows at most) and is fitted on
demand, so there is nothing to train, store or version separately.
"""
import logging
import math

logger = logging.getLogger(__name__)

MIN_HISTORY = 15          # below this the user's history says too little to compare against
MAX_HISTORY = 400


def hour_features(dt):
    """Encode time of day on a circle so 23:00 and 01:00 come out close together."""
    h = dt.hour + dt.minute / 60
    return [math.sin(2 * math.pi * h / 24), math.cos(2 * math.pi * h / 24)]


def unusualness(history, sample, random_state=0):
    """How unusual `sample` is against `history` (lists of equal-length feature vectors).

    Returns the share of the user's own past events that look *more* unusual than the
    new one: 0.0 means stranger than anything seen before, 0.5 is typical. Returns None
    when there isn't enough history or scikit-learn isn't installed.
    """
    if len(history) < MIN_HISTORY:
        return None
    try:
        import numpy as np
        from sklearn.ensemble import IsolationForest
    except ImportError:  # pragma: no cover - dependency is in requirements.txt
        logger.warning('scikit-learn not installed; skipping anomaly scoring')
        return None

    X = np.asarray(history[-MAX_HISTORY:], dtype=float)
    model = IsolationForest(n_estimators=64, random_state=random_state).fit(X)
    train_scores = model.score_samples(X)          # higher = more normal
    new_score = model.score_samples(np.asarray([sample], dtype=float))[0]
    return float((train_scores < new_score).mean())
