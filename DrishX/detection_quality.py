"""Quality controls shared by detection, observation reporting and exports.

Thresholds are engineering defaults, not calibrated accuracy guarantees.
"""

import numpy as np
from scipy.ndimage import binary_dilation


PROFILES = {
    "balanced": {"blue_min": 0.75, "score_min": 1.2, "class_min": 0.0},
    "precision": {"blue_min": 0.85, "score_min": 1.5, "class_min": 0.60},
}
MIN_CLEAR_ROAD_FRACTION = 0.80
ROAD_BUFFERS_M = {"motorway": 20, "trunk": 15, "primary": 10}


def valid_data_mask(data):
    """Accept finite reflectance, excluding cloud/shadow/snow and no-data.

    Channels: R, G, B, NIR, CLM, SCL, dataMask. The five-channel legacy
    form remains readable, but cannot provide SCL or explicit no-data checks.
    A one-pixel margin excludes mixed pixels bordering invalid observations.
    """
    if data.ndim != 3 or data.shape[2] not in (5, 7):
        raise ValueError("Expected R, G, B, NIR, CLM[, SCL, dataMask] channels")
    invalid = ~np.isfinite(data).all(axis=2)
    invalid |= np.any(data[:, :, :4] <= 0, axis=2)
    invalid |= data[:, :, 4] != 0
    if data.shape[2] == 7:
        invalid |= data[:, :, 6] != 1
        invalid |= np.isin(data[:, :, 5], [0, 1, 3, 8, 9, 10, 11])
    return ~binary_dilation(invalid, structure=np.ones((3, 3)), border_value=0)


def road_buffer_m(highway):
    """OSMnx may combine several highway tags on a simplified edge."""
    values = highway if isinstance(highway, list) else [highway]
    return max((ROAD_BUFFERS_M.get(v, 0) for v in values), default=0)


def select_observations(results, limit):
    """One acquisition per day, sampled across the entire requested period."""
    unique = {}
    for result in sorted(results, key=lambda r: (r["properties"]["datetime"], r["id"])):
        unique.setdefault(result["properties"]["datetime"][:10], result)
    ordered = [unique[d] for d in sorted(unique)]
    if len(ordered) <= limit:
        return ordered
    if limit == 1:
        return ordered[-1:]
    return [ordered[i] for i in np.linspace(0, len(ordered) - 1, limit, dtype=int)]


def observation_quality(data, road_mask, timestamp):
    road_pixels = int(np.count_nonzero(road_mask))
    clear_pixels = int(np.count_nonzero(valid_data_mask(data) & road_mask.astype(bool)))
    fraction = clear_pixels / road_pixels if road_pixels else 0.0
    return {
        "timestamp": timestamp,
        "status": "usable" if fraction >= MIN_CLEAR_ROAD_FRACTION else "insufficient_coverage",
        "road_pixels": road_pixels,
        "clear_road_pixels": clear_pixels,
        "clear_road_fraction": round(fraction, 4),
        "detection_count": None,
    }


def build_trends(history, from_date=None, to_date=None, site_ids=None):
    """Only usable observations contribute counts; absence is a gap, not zero."""
    requested = set(site_ids.split(",")) if site_ids else set()
    dates, missions = set(), []
    usable, excluded = 0, 0
    for mission in history:
        if requested and mission["mission_id"] not in requested:
            continue
        counts, quality = {}, {}
        observations = mission.get("observations", [])
        for obs in observations:
            day = obs["timestamp"][:10]
            if (from_date and day < from_date) or (to_date and day > to_date):
                continue
            dates.add(day)
            quality[day] = obs
            if obs["status"] == "usable":
                counts[day] = obs["detection_count"]
                usable += 1
            else:
                excluded += 1
        missions.append((mission, counts, quality))
    labels = sorted(dates)
    colors = ["#3b82f6", "#f59e0b", "#10b981", "#ef4444", "#a855f7", "#ec4899"]
    datasets = []
    for i, (mission, counts, quality) in enumerate(missions):
        color = colors[i % len(colors)]
        datasets.append({
            "mission_id": mission["mission_id"],
            "label": mission["label"],
            "data": [counts.get(d) for d in labels],
            "observations": [quality.get(d) for d in labels],
            "borderColor": color,
            "backgroundColor": color + "22",
            "spanGaps": False,
        })
    return {
        "labels": labels, "datasets": datasets,
        "summary": {
            "total_detections": sum(v for ds in datasets for v in ds["data"] if v is not None),
            "missions_count": len(datasets), "usable_observations": usable,
            "excluded_observations": excluded,
        },
    }
