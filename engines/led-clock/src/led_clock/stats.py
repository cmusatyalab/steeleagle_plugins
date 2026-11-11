"""Summary statistics of latencies, shared by the engine's log lines and the
summarize command."""

import numpy as np

PERCENTILES = (50, 95, 99)


def latency_stats(samples):
    """{count, mean, std, min, p50, p95, p99, max} of `samples`, in their
    units, or None if there are none."""
    if len(samples) == 0:
        return None
    values = np.asarray(samples, dtype=float)
    stats = {
        "count": len(values),
        "mean": float(values.mean()),
        "std": float(values.std()),
        "min": float(values.min()),
    }
    for p, v in zip(PERCENTILES, np.percentile(values, PERCENTILES)):
        stats[f"p{p}"] = float(v)
    stats["max"] = float(values.max())
    return stats


def format_stats(name, stats):
    """One line of `stats`, in milliseconds, labelled `name`."""
    if stats is None:
        return f"{name}: none"
    return f"{name} ms: " + " ".join(
        f"{key} {value:.1f}" for key, value in stats.items() if key != "count"
    )
