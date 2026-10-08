

import os
import time
import json
import asyncio
import logging
import pickle
import csv
import io
import uuid
import hashlib
from xml.etree import ElementTree
import requests
import imageio.v3 as imageio
import numpy as np
import geopandas as gpd
from datetime import datetime, timedelta
from typing import List, Literal
from concurrent.futures import ThreadPoolExecutor, as_completed
from shapely.geometry import LineString, box
from pyproj import Transformer
from rasterio import features as rio_features, transform as rio_transform
from detection_quality import (
    PROFILES, valid_data_mask, road_buffer_m, select_observations,
    observation_quality, build_trends,
)
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator
import uvicorn

import osmnx as ox
from sentinelhub import (
    SHConfig, SentinelHubRequest, DataCollection,
    BBox, CRS, MimeType, SentinelHubCatalog,
)
from dotenv import load_dotenv

# ─────────────────────────────────────────────────────────────────────────────
# Setup
# ─────────────────────────────────────────────────────────────────────────────

# Only project configuration may override saved UI credentials. The default
# dotenv search walks up to the home directory and can load unrelated old keys.
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ARGUS")

SECONDS_OFFSET_B02_B04 = 1.01  # Sentinel-2 temporal sensing offset between B02 and B04

# Dynamic Storage Root (Expansion Drive or Local Fallback)
DATA_DIR = os.getenv("DRISHX_DATA_DIR", os.path.join(os.getcwd(), "drishx_data"))
DETECTION_DIR = os.path.join(DATA_DIR, "sentinel_data/detections")
os.makedirs(DETECTION_DIR, exist_ok=True)

OVERPASS_MIRRORS = [
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass-api.de/api/interpreter",
]

# Network session with retries
_session = requests.Session()
_retry = Retry(
    total=2, backoff_factor=1.0,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET", "POST"],
)
_adapter = HTTPAdapter(max_retries=_retry)
_session.mount("http://", _adapter)
_session.mount("https://", _adapter)

ox.settings.requests_session = _session
ox.settings.requests_timeout = 30
ox.settings.overpass_rate_limit = False
ox.settings.max_query_area_size = 1_000_000_000_000
ox.settings.log_console = False
# OSMnx Cache Redirection
ox.settings.use_cache = True
ox.settings.cache_folder = os.path.join(DATA_DIR, "osm_cache")

# Copernicus Data Space config
CONFIG = SHConfig()
# SHConfig() restores credentials previously saved via the UI (CONFIG.save()).
# Environment variables take precedence, but only when both are actually set —
# blindly assigning os.getenv() results would overwrite restored credentials
# with None on every restart.
_env_id = os.getenv("COPERNICUS_CLIENT_ID")
_env_secret = os.getenv("COPERNICUS_CLIENT_SECRET")
if _env_id and _env_secret:
    CONFIG.sh_client_id = _env_id
    CONFIG.sh_client_secret = _env_secret
CONFIG.sh_base_url = "https://sh.dataspace.copernicus.eu"
CONFIG.sh_token_url = "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"

# How the active credentials were provided and when they last passed
# verification. Surfaced by GET /api/auth/status.
AUTH_STATE = {
    "source": "env" if (_env_id and _env_secret)
    else ("ui" if (CONFIG.sh_client_id and CONFIG.sh_client_secret) else None),
    "last_verified": None,
    "verification_error": None,
}

# SentinelHub Cache Redirection
CONFIG.cache_dir = os.path.join(DATA_DIR, "sh_cache")
os.makedirs(CONFIG.cache_dir, exist_ok=True)

# Note: We do NOT call CONFIG.save() here to avoid TOML serialization errors with NoneTypes
logger.info(f"DrishX Storage Link: {DATA_DIR}")
logger.info("Copernicus Data Space Authentication: CONFIGURED FOR CDSE")

FEATURED_SITES = [
    {"id": "v1", "name": "Braunschweig A7 (Research-Grade)", "bbox": [52.25, 10.45, 52.32, 10.55], "country": "Germany", "type": "high_volume"},
    {"id": "v2", "name": "Frankfurt A3 (High-Density)", "bbox": [50.05, 8.55, 50.12, 8.65], "country": "Germany", "type": "high_volume"},
    {"id": "v3", "name": "Karlsruhe A5 (Research-Standard)", "bbox": [48.95, 8.35, 49.05, 8.45], "country": "Germany", "type": "standard"},
]


# ─────────────────────────────────────────────────────────────────────────────
# Helper math — mirrors S2TD.array_utils.math
# ─────────────────────────────────────────────────────────────────────────────

def normalized_ratio(a, b):
    """(a - b) / (a + b), safe division."""
    denom = a + b
    with np.errstate(divide="ignore", invalid="ignore"):
        result = np.where(denom != 0, (a - b) / denom, 0.0)
    return result.astype(np.float32)


def rescale_s2(bands):
    """Rescale Sentinel-2 L2A reflectance values (typically 0–10000 int) to 0–1 float."""
    bands = bands.astype(np.float32)
    if np.nanmax(bands) > 10:  # likely DN scale
        bands /= 10000.0
    return bands


# ─────────────────────────────────────────────────────────────────────────────
# Array subset — exact replica of S2TD.pick_arr_subset
# ─────────────────────────────────────────────────────────────────────────────

def pick_arr_subset(arr, y, x, size):
    """Pick a size×size window centred on (y, x) from a 2D or 3D array."""
    size_low = size // 2
    size_up = size // 2
    if size_low + size_up < size:
        size_up += 1
    ymin = max(0, y - size_low)
    ymax = max(0, y + size_up)
    xmin = max(0, x - size_low)
    xmax = max(0, x + size_up)
    if arr.ndim == 2:
        return arr[ymin:ymax, xmin:xmax]
    elif arr.ndim == 3:
        return arr[:, ymin:ymax, xmin:xmax]
    return arr


# ─────────────────────────────────────────────────────────────────────────────
# Feature stack — 7 features used by the supplied S2TD model
# ─────────────────────────────────────────────────────────────────────────────

def build_feature_stack(data, road_mask=None):
    """
    Build the 7-feature stack from Sentinel-2 bands.

    Input channels: B04(R), B03(G), B02(B), B08(NIR), CLM[, SCL, dataMask].

    Feature order (Table 1, Fisser et al. 2022):
        0: variance of (B04, B03, B02)
        1: normalized_ratio(B04, B02)  — red / blue
        2: normalized_ratio(B03, B02)  — green / blue
        3: B04 - mean(B04)
        4: B03 - mean(B03)
        5: B02 - mean(B02)
        6: B08 - mean(B08)
    """
    R = data[:, :, 0].astype(np.float32)    # B04
    G = data[:, :, 1].astype(np.float32)    # B03
    B = data[:, :, 2].astype(np.float32)    # B02
    NIR = data[:, :, 3].astype(np.float32)  # B08

    # Rescale if needed
    bands = np.stack([R, G, B, NIR], axis=0)
    bands = rescale_s2(bands)
    R, G, B, NIR = bands[0], bands[1], bands[2], bands[3]

    # Cloud mask → NaN
    cloud = ~valid_data_mask(data)
    if road_mask is not None:
        cloud |= ~road_mask.astype(bool)
    R[cloud] = np.nan
    G[cloud] = np.nan
    B[cloud] = np.nan
    NIR[cloud] = np.nan

    H, W = R.shape
    fs = np.zeros((7, H, W), dtype=np.float32)

    # Check for any valid data to avoid "Mean of empty slice" warnings
    if np.any(~cloud):
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", category=RuntimeWarning)
            # Feature 0: variance of visible bands
            # The supplied S2TD model uses division by N+1 (ddof=-1).
            fs[0] = np.nanvar(np.stack([R, G, B], axis=0), axis=0, ddof=-1)

            # Features 1–2: normalized ratios
            fs[1] = normalized_ratio(R, B)
            fs[2] = normalized_ratio(G, B)

            # Features 3–6: mean-centered bands
            fs[3] = R - np.nanmean(R)
            fs[4] = G - np.nanmean(G)
            fs[5] = B - np.nanmean(B)
            fs[6] = NIR - np.nanmean(NIR)
    else:
        # All pixels are cloud-masked
        fs.fill(np.nan)

    # Ensure NaN consistency
    nan_mask = np.isnan(fs[3])
    fs[:, nan_mask] = np.nan

    return {
        "feature_stack": fs,
        "bands": {"R": R, "G": G, "B": B, "NIR": NIR},
        "cloud_mask": cloud,
    }


# ─────────────────────────────────────────────────────────────────────────────
# RF Model loading
# ─────────────────────────────────────────────────────────────────────────────

# Path to the trained Random Forest model from S2TruckDetect
RF_MODEL_PATH = os.getenv("RF_MODEL_PATH", os.path.join(os.path.dirname(__file__), "rf_model.pickle"))
_rf_model = None


def load_rf_model(path=None):
    """Load the trained RF model from pickle. Returns None if not found."""
    global _rf_model
    p = path or RF_MODEL_PATH
    if _rf_model is not None:
        return _rf_model
    if os.path.isfile(p):
        try:
            with open(p, "rb") as model_file:
                model = pickle.load(model_file)
            if set(model.classes_) != {1, 2, 3, 4} or model.n_features_in_ != 7:
                raise ValueError("Expected S2TD classes 1=background, 2=blue, 3=green, 4=red and 7 features")
            _rf_model = model
            logger.info(f"Loaded trained RF model from {p}")
            return _rf_model
        except Exception as e:
            logger.error(f"Failed to load RF model from {p}: {e}")
    else:
        logger.warning(f"RF model not found at {p} — analysis is unavailable")
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Classification — trained RF with explicit class labels
# ─────────────────────────────────────────────────────────────────────────────

def rf_classify(feature_stack, road_mask, rf_model, profile="balanced"):
    """
    Classify pixels using the trained Random Forest model.
    Map model classes explicitly and require a strong blue seed.

    :param feature_stack: (7, H, W) feature array
    :param road_mask: (H, W) binary road mask
    :param rf_model: trained sklearn RandomForestClassifier
    :return: (probabilities (4, H, W), prediction (H, W) int8)
    """
    H, W = feature_stack.shape[1], feature_stack.shape[2]
    if set(rf_model.classes_) != {1, 2, 3, 4}:
        raise ValueError("Unsupported model classes; expected numeric S2TD labels 1–4")

    # Reshape to (n_pixels, 7) for sklearn
    vars_reshaped = []
    for band_idx in range(feature_stack.shape[0]):
        vars_reshaped.append(feature_stack[band_idx].flatten())
    vars_reshaped = np.array(vars_reshaped).swapaxes(0, 1)  # (n_pixels, 7)

    # Build NaN mask — exclude NaN and Inf pixels
    nan_mask_flat = np.zeros_like(vars_reshaped)
    for var_idx in range(vars_reshaped.shape[1]):
        nan_mask_flat[:, var_idx] = ~np.isnan(vars_reshaped[:, var_idx])
    not_nan = (np.nanmin(nan_mask_flat, axis=1).astype(bool)
               & np.min(np.isfinite(vars_reshaped), axis=1).astype(bool))
    not_nan &= road_mask.astype(bool).ravel()

    # Run RF predict_proba on valid pixels only
    if not np.any(not_nan):
        # Graceful return if no valid pixels found (e.g., all cloud masked)
        probabilities_shaped = np.zeros((4, H, W), dtype=np.float32)
        classification = np.zeros((H, W), dtype=np.int8)
        return probabilities_shaped, classification

    predictions_flat = rf_model.predict_proba(vars_reshaped[not_nan])

    # Map probabilities back to spatial grid
    n_classes = 4
    probabilities_shaped = np.zeros((n_classes, H * W), dtype=np.float32)
    for idx, label in enumerate(rf_model.classes_):
        probabilities_shaped[int(label) - 1, not_nan] = predictions_flat[:, idx]

    probabilities_shaped = probabilities_shaped.reshape((n_classes, H, W))

    # Zero out NaN positions
    nan_2d = np.isnan(feature_stack[0])
    probabilities_shaped[:, nan_2d] = 0

    # Index 1 is BLUE, not background. Retain raw probabilities for scoring.
    eligible = probabilities_shaped.copy()
    eligible[1][eligible[1] < PROFILES[profile]["blue_min"]] = 0

    classification = np.nanargmax(eligible, axis=0).astype(np.int8) + 1
    classification[np.max(eligible, axis=0) == 0] = 0
    classification[nan_2d] = 0

    # Apply road mask
    rm = road_mask.astype(bool)
    classification[~rm] = 0

    return probabilities_shaped, classification


def classify(feature_stack, road_mask, rf_model=None, profile="balanced"):
    """
    Unified classifier entry point.
    Require the trained RF. Heuristic outputs must not masquerade as detections.
    """
    if rf_model is not None:
        logger.debug("Using trained RF model for classification")
        return rf_classify(feature_stack, road_mask, rf_model, profile)
    else:
        raise ValueError("Trained RF model unavailable. Install rf_model.pickle with Git LFS and scikit-learn 1.3.2.")


# ─────────────────────────────────────────────────────────────────────────────
# Object extraction — S2TD clustering with support and edge checks
# ─────────────────────────────────────────────────────────────────────────────

class ObjectExtractor:
    """
    Extracts truck objects from the RF prediction raster using recursive
    neighbourhood clustering, matching the S2TD reference implementation.
    """

    def __init__(self, probabilities, lat_arr=None, lon_arr=None, *, profile="balanced", transform=None, crs=None):
        """
        :param probabilities: (4, H, W) class probabilities
        :param lat_arr: 1-D array of latitude per row
        :param lon_arr: 1-D array of longitude per column
        """
        self.probabilities = probabilities
        self.lat = lat_arr
        self.lon = lon_arr
        self.profile = PROFILES[profile]
        self.transform = transform
        self.to_wgs84 = Transformer.from_crs(crs, "EPSG:4326", always_xy=True) if crs else None

    def extract(self, predictions_arr):
        """Main extraction loop over all blue (class 2) seed pixels."""
        preds = predictions_arr.copy()
        probs = self.probabilities.copy()

        preds[preds == 1] = 0  # zero out background
        blue_ys, blue_xs = np.where(preds == 2)
        detections = []
        sub_size = 9

        for i in range(len(blue_ys)):
            y_blue, x_blue = int(blue_ys[i]), int(blue_xs[i])
            if preds[y_blue, x_blue] == 0:
                continue

            subset_9 = pick_arr_subset(preds, y_blue, x_blue, sub_size).copy()
            subset_3 = pick_arr_subset(preds, y_blue, x_blue, 3).copy()
            subset_9_probs = pick_arr_subset(probs, y_blue, x_blue, sub_size).copy()

            half_idx_y = y_blue - max(0, y_blue - sub_size // 2)
            half_idx_x = x_blue - max(0, x_blue - sub_size // 2)
            current_value = subset_9[half_idx_y, half_idx_x]

            new_value = 100
            if not all(v in subset_9 for v in [2, 3, 4]):
                continue

            cluster, seen_idx, seen_vals, _ = self._cluster_array(
                arr=subset_9, probs=subset_9_probs,
                point=[half_idx_y, half_idx_x],
                new_value=new_value, current_value=current_value,
                yet_seen_indices=[], yet_seen_values=[],
                skipped_one=False,
            )

            if np.count_nonzero(cluster == new_value) < 3:
                continue

            det = self._postprocess_cluster(
                cluster, preds, probs, subset_3,
                y_blue, x_blue,
                half_idx_y, half_idx_x,
                new_value,
            )
            if det is not None:
                preds = det["updated_preds"]
                detections.append(det["detection"])

        return detections

    def _cluster_array(self, arr, probs, point, new_value, current_value,
                       yet_seen_indices, yet_seen_values, skipped_one):
        """Recursive neighbourhood clustering — matches S2TD._cluster_array."""
        if len(yet_seen_indices) == 0:
            yet_seen_indices.append(point)
            yet_seen_values.append(current_value)

        arr_mod = arr.copy()
        arr_mod[point[0], point[1]] = 0

        window_3x3 = pick_arr_subset(arr_mod, point[0], point[1], 3).copy()
        if window_3x3.shape[0] >= 2 and window_3x3.shape[1] >= 2:
            cy = min(1, window_3x3.shape[0] - 1)
            cx = min(1, window_3x3.shape[1] - 1)
            if window_3x3[cy, cx] == 2:
                window_3x3[window_3x3 == 4] = 1  # eliminate reds near blue

        y, x = point[0], point[1]
        window_3x3_probs = pick_arr_subset(probs, y, x, 3)

        windows = [window_3x3]
        windows_probs = [window_3x3_probs]
        if current_value == 4 or skipped_one:
            windows = windows[0:1]

        ys, xs = np.array([], dtype=int), np.array([], dtype=int)
        window_idx = 0
        offset_y, offset_x = 0, 0

        while len(ys) == 0 and window_idx < len(windows):
            window = windows[window_idx]
            window_p = windows_probs[window_idx]
            offset_y = min(point[0], 1)
            offset_x = min(point[1], 1)

            go_next = (current_value + 1) in window or current_value == 2
            target_value = current_value + 1 if go_next else current_value
            match = window == target_value
            if np.count_nonzero(match) == 0:
                target_value = current_value
                match = window == target_value

            ys_found, xs_found = np.where(match)

            # Probability-based tie-breaking
            if len(ys_found) > 1 and window_p.ndim == 3 and window_p.shape[0] > (target_value - 1):
                wp_target = window_p[target_value - 1] * match
                max_prob_mask = match & (wp_target == np.max(wp_target[match]))
                ys_found, xs_found = np.where(max_prob_mask)

            ys, xs = ys_found, xs_found
            window_idx += 1

        ymin_w = max(0, point[0] - offset_y)
        xmin_w = max(0, point[1] - offset_x)

        for y_local, x_local in zip(ys, xs):
            ny, nx = ymin_w + int(y_local), xmin_w + int(x_local)
            if [ny, nx] in yet_seen_indices:
                continue
            if ny < 0 or ny >= arr.shape[0] or nx < 0 or nx >= arr.shape[1]:
                continue
            try:
                cv = arr[ny, nx]
            except IndexError:
                continue

            # Red already seen but this is green or blue → skip
            if 4 in yet_seen_values and cv <= 3:
                continue

            arr_mod[ny, nx] = new_value
            yet_seen_indices.append([ny, nx])
            yet_seen_values.append(cv)

            # Guard: avoid picking many more reds than blues and greens
            n_blue = sum(1 for v in yet_seen_values if v == 2)
            n_green = sum(1 for v in yet_seen_values if v == 3)
            n_red = sum(1 for v in yet_seen_values if v == 4)
            if n_red > n_blue and n_red > n_green:
                break

            arr_mod, yet_seen_indices, yet_seen_values, skipped_one = self._cluster_array(
                arr_mod, probs, [ny, nx], new_value, cv,
                yet_seen_indices, yet_seen_values, skipped_one,
            )

        arr_mod[point[0], point[1]] = new_value
        return arr_mod, yet_seen_indices, yet_seen_values, skipped_one

    def _postprocess_cluster(self, cluster, preds_copy, probs, subset_3,
                             y_blue, x_blue, half_idx_y, half_idx_x,
                             new_value):
        """Validate cluster evidence and produce a candidate detection."""
        # Add neighbouring blues from the 3×3 window
        ys_ba, xs_ba = np.where(subset_3 == 2)
        ys_ba = ys_ba + half_idx_y - min(y_blue, 1)
        xs_ba = xs_ba + half_idx_x - min(x_blue, 1)
        for yb, xb in zip(ys_ba, xs_ba):
            yb_c = int(np.clip(yb, 0, cluster.shape[0] - 1))
            xb_c = int(np.clip(xb, 0, cluster.shape[1] - 1))
            cluster[yb_c, xb_c] = new_value

        cluster[cluster != new_value] = 0
        cys, cxs = np.where(cluster == new_value)
        if len(cys) == 0:
            return None

        # Map subset coords back to full array
        ymin_sub = int(np.clip(y_blue - half_idx_y, 0, np.inf))
        xmin_sub = int(np.clip(x_blue - half_idx_x, 0, np.inf))
        cys_full = cys + ymin_sub
        cxs_full = cxs + xmin_sub

        ymin = int(np.min(cys_full))
        xmin = int(np.min(cxs_full))
        ymax = int(np.max(cys_full)) + 1  # +1: box extends to upper bound of pixel
        xmax = int(np.max(cxs_full)) + 1

        H, W = preds_copy.shape
        ymin, ymax = max(0, ymin), min(H, ymax)
        xmin, xmax = max(0, xmin), min(W, xmax)

        box_preds = preds_copy[ymin:ymax, xmin:xmax].copy()
        box_probs = probs[1:, ymin:ymax, xmin:xmax].copy()  # classes 2,3,4 → indices 0,1,2
        # Only pixels reached by this cluster may validate or score it. A nearby
        # unrelated red/green pixel inside its rectangular box is not evidence.
        support = np.zeros_like(box_preds, dtype=bool)
        support[cys_full - ymin, cxs_full - xmin] = True
        box_preds[~support] = 0

        # Spectral evidence from the extracted cluster only
        max_probs = []
        for cls_offset, cls_val in enumerate([2, 3, 4]):
            mask = (box_preds == cls_val)
            vals = box_probs[cls_offset] * mask
            mp = float(np.nanmax(vals)) if np.any(mask) else 0.0
            max_probs.append(mp)

        mean_max_spectral_probability = float(np.nanmean(max_probs))
        mean_spectral_probability = float(np.mean([
            box_probs[int(label) - 2, y, x]
            for y, x in zip(*np.where(support))
            if (label := box_preds[y, x]) in (2, 3, 4)
        ]))

        # Validation checks
        all_given = all(v in box_preds for v in [2, 3, 4])
        large_enough = box_preds.shape[0] > 2 or box_preds.shape[1] > 2
        too_large = box_preds.shape[0] > 5 or box_preds.shape[1] > 5

        if too_large or not all_given or not large_enough:
            return None

        # Score: TWO terms — matches reference
        score = mean_max_spectral_probability + mean_spectral_probability
        if score <= self.profile["score_min"] or min(max_probs) < self.profile["class_min"]:
            return None

        # Direction (blue → red vector)
        by, bx = np.where(box_preds == 2)
        ry, rx = np.where(box_preds == 4)
        blue_idx = np.array([by[0], bx[0]], dtype=np.int8)
        red_idx = np.array([ry[0], rx[0]], dtype=np.int8)
        vector = (blue_idx - red_idx) * np.array([1, -1], dtype=np.int8)
        heading = float(np.degrees(np.arctan2(vector[1], vector[0])) % 360)

        # Speed
        diameter = max(box_preds.shape) * 10 - 10
        speed_kmh = float(np.sqrt(diameter * 20) / SECONDS_OFFSET_B02_B04 * 3.6)

        # Geo-coordinates (centre of detection box)
        row, col = (ymin + ymax - 1) / 2, (xmin + xmax - 1) / 2
        if self.transform is not None:
            east, north = rio_transform.xy(self.transform, row, col)
            lon_centre, lat_centre = self.to_wgs84.transform(east, north)
        else:
            lat_centre = float((self.lat[ymin] + self.lat[ymax - 1]) / 2)
            lon_centre = float((self.lon[xmin] + self.lon[xmax - 1]) / 2)

        # Zero out detected pixels to prevent re-detection
        preds_copy[ymin:ymax, xmin:xmax] *= np.zeros_like(box_preds)
        # Also zero 3×3 around blue pixels
        blue_in_box = np.where(box_preds == 2)
        for yb, xb in zip(blue_in_box[0], blue_in_box[1]):
            y0, y1 = max(0, ymin + yb - 1), min(H, ymin + yb + 2)
            x0, x1 = max(0, xmin + xb - 1), min(W, xmin + xb + 2)
            preds_copy[y0:y1, x0:x1] *= (preds_copy[y0:y1, x0:x1] != 2).astype(np.int8)

        crop_id = f"truck_{uuid.uuid4().hex}.png"

        return {
            "updated_preds": preds_copy,
            "detection": {
                "lat": lat_centre,
                "lon": lon_centre,
                "confidence": float(min(score / 2.4, 1.0)),
                "score_calibrated": False,
                "s_score": round(score, 3),
                "speed_kmh": round(speed_kmh, 1),
                "heading": round(heading, 1),
                "heading_desc": self._direction_to_compass(heading),
                "id": crop_id,
                "image_url": f"/detections/{crop_id}",
                "box_shape": list(box_preds.shape),
                "pixel_row": row,
                "pixel_col": col,
                "max_probs": {"blue": max_probs[0], "green": max_probs[1], "red": max_probs[2]},
            },
        }

    @staticmethod
    def _direction_to_compass(deg):
        labels = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
        return labels[int((deg + 22.5) // 45) % 8]


# ─────────────────────────────────────────────────────────────────────────────
# ARGUS Engine
# ─────────────────────────────────────────────────────────────────────────────

class ARGUSEngine:
    def __init__(self):
        self.history = []
        self.rf_model = load_rf_model()

    def fetch_roads(self, bbox_coords, progress_cb=None):
        """Fetch only the supported road ways, with bounded requests and caching.

        Avoid the full driving-graph downloader: its automatic Overpass retries
        can wait indefinitely, even though detection only needs line geometry.
        """
        cache_dir = os.path.join(DATA_DIR, "road_cache")
        os.makedirs(cache_dir, exist_ok=True)
        cache_key = hashlib.sha256(json.dumps(bbox_coords).encode()).hexdigest()[:24]
        cache_path = os.path.join(cache_dir, cache_key + ".geojson")
        if os.path.isfile(cache_path):
            try:
                with open(cache_path) as saved:
                    return gpd.GeoDataFrame.from_features(json.load(saved)["features"], crs="EPSG:4326")
            except (ValueError, KeyError):
                logger.warning("Ignoring unreadable road cache")
        south, west, north, east = bbox_coords
        query = f'[out:json][timeout:15];way["highway"~"^(motorway|trunk|primary)$"]({south},{west},{north},{east});out geom;'
        for mirror in OVERPASS_MIRRORS:
            logger.info("Fetching road ways from %s", mirror)
            if progress_cb:
                progress_cb("Fetching major-road geometry...", 10)
            try:
                # Requests directly, rather than the retrying session, bounds
                # each mirror attempt even when it responds with 429 or 504.
                response = requests.post(mirror, data={"data": query}, timeout=(5, 20))
                response.raise_for_status()
                payload = response.json()
                if payload.get("remark"):
                    raise ValueError("Overpass returned incomplete results")
                ways = []
                for element in payload.get("elements", []):
                    highway = element.get("tags", {}).get("highway")
                    if element.get("type") != "way" or not road_buffer_m(highway):
                        continue
                    coords = [(point["lon"], point["lat"]) for point in element.get("geometry", [])]
                    if len(coords) >= 2:
                        ways.append({"geometry": LineString(coords), "highway": highway, "osmid": element["id"]})
                if not ways:
                    return gpd.GeoDataFrame()
                roads = gpd.GeoDataFrame(ways, crs="EPSG:4326")
                temp_path = cache_path + "." + uuid.uuid4().hex + ".tmp"
                try:
                    with open(temp_path, "w") as saved:
                        saved.write(roads.to_json())
                    os.replace(temp_path, cache_path)
                except OSError:
                    logger.warning("Roads fetched, but could not save their cache")
                logger.info("Fetched %s major-road ways", len(roads))
                return roads
            except (requests.RequestException, ValueError, KeyError) as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                logger.warning("Road mirror failed (%s, HTTP %s)", type(exc).__name__, status)
        # A small, cached map extract can keep a local test working when the
        # Overpass services are unavailable. Never use the editing API for
        # large areas or bulk downloads.
        if (north - south) * (east - west) <= 0.0025:
            logger.info("Trying a small OpenStreetMap map extract")
            try:
                response = requests.get(
                    "https://api.openstreetmap.org/api/0.6/map",
                    params={"bbox": f"{west},{south},{east},{north}"},
                    headers={"User-Agent": "DrishX/1.0 (local road-traffic analysis)"},
                    timeout=(5, 20),
                )
                response.raise_for_status()
                root = ElementTree.fromstring(response.content)
                nodes = {n.attrib["id"]: (float(n.attrib["lon"]), float(n.attrib["lat"])) for n in root.findall("node")}
                ways = []
                for way in root.findall("way"):
                    tags = {t.attrib["k"]: t.attrib["v"] for t in way.findall("tag")}
                    refs = [nd.attrib["ref"] for nd in way.findall("nd")]
                    if road_buffer_m(tags.get("highway")) and len(refs) >= 2 and all(ref in nodes for ref in refs):
                        ways.append({"geometry": LineString([nodes[ref] for ref in refs]), "highway": tags["highway"], "osmid": int(way.attrib["id"])})
                if not ways:
                    return gpd.GeoDataFrame()
                roads = gpd.GeoDataFrame(ways, crs="EPSG:4326")
                try:
                    temp_path = cache_path + "." + uuid.uuid4().hex + ".tmp"
                    with open(temp_path, "w") as saved:
                        saved.write(roads.to_json())
                    os.replace(temp_path, cache_path)
                except OSError:
                    logger.warning("Could not cache the small road extract")
                return roads
            except (requests.RequestException, ValueError, KeyError, ElementTree.ParseError) as exc:
                logger.warning("Small road extract failed (%s)", type(exc).__name__)
        raise RuntimeError("Road data services are temporarily unavailable. Try a smaller area or reuse a cached area.")

    def detect_trucks(self, data, bbox_coords, timestamp, road_mask, *, profile="balanced", transform=None, crs=None):
        """
        Detect trucks using corrected Fisser et al. methodology.

        :param data: (H, W, 5) array — [B04, B03, B02, B08, CLM]
        :param bbox_coords: [min_lat, min_lon, max_lat, max_lon]
        :param timestamp: str ISO timestamp
        :param road_mask: (H, W) binary mask of road pixels
        :return: list of detection dicts
        """
        min_lat, min_lon, max_lat, max_lon = bbox_coords
        H, W = data.shape[:2]

        # 1. Build feature stack (corrected order)
        feat = build_feature_stack(data, road_mask)
        feature_stack = feat["feature_stack"]

        # 2. Classify with the trained RF and selected filter
        probs, prediction = classify(feature_stack, road_mask, self.rf_model, profile)

        # 3. Lat/lon arrays for geo-referencing
        lat_arr = max_lat - (np.arange(H) + 0.5) * (max_lat - min_lat) / H
        lon_arr = min_lon + (np.arange(W) + 0.5) * (max_lon - min_lon) / W

        # 4. Object extraction (corrected)
        extractor = ObjectExtractor(probs, lat_arr, lon_arr, profile=profile, transform=transform, crs=crs)
        detections = extractor.extract(prediction)

        # 5. Add timestamp and save crops
        for det in detections:
            det["timestamp"] = timestamp
            det["profile"] = profile
            try:
                self._save_crop(data, det, H, W, min_lat, min_lon, max_lat, max_lon)
            except Exception as e:
                logger.warning(f"Could not save crop for {det['id']}: {e}")

        return detections

    def _save_crop(self, data, det, H, W, min_lat, min_lon, max_lat, max_lon):
        """Save a 20×20 RGB crop centred on the detection."""
        cy, cx = int(round(det["pixel_row"])), int(round(det["pixel_col"]))
        cy, cx = int(np.clip(cy, 0, H - 1)), int(np.clip(cx, 0, W - 1))

        y0, y1 = max(0, cy - 10), min(H, cy + 10)
        x0, x1 = max(0, cx - 10), min(W, cx + 10)

        rgb = data[y0:y1, x0:x1, :3].astype(np.float32)
        rgb = rescale_s2(rgb)
        rgb = (np.clip(rgb, 0, 0.3) / 0.3 * 255).astype(np.uint8)

        path = os.path.join(DETECTION_DIR, det["id"])
        imageio.imwrite(path, rgb)


# ─────────────────────────────────────────────────────────────────────────────
# FastAPI Application
# ─────────────────────────────────────────────────────────────────────────────

app = FastAPI(title="ARGUS Corridor Intelligence")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

engine = ARGUSEngine()


class AnalyzeRequest(BaseModel):
    bbox: List[float] = Field(min_length=4, max_length=4)
    label: str = "New Mission"
    months: int = Field(default=4, ge=1, le=48)
    max_frames: int = Field(default=5, ge=1, le=1000)
    profile: Literal["balanced", "precision"] = "balanced"

    @field_validator("bbox")
    @classmethod
    def valid_bbox(cls, value):
        south, west, north, east = value
        if not all(np.isfinite(value)) or not (-80 <= south < north <= 84 and -180 <= west < east <= 180):
            raise ValueError("Use an ordered, finite bbox within UTM coverage (80°S–84°N)")
        return value


def analysis_grid(bbox_coords):
    """A fixed 10 m projected grid shared by imagery, roads and coordinates."""
    south, west, north, east = bbox_coords
    crs = CRS.get_utm_from_wgs84((west + east) / 2, (south + north) / 2)
    projected = BBox([west, south, east, north], CRS.WGS84).transform_bounds(crs)
    left, bottom = np.floor(np.array(projected.lower_left) / 10) * 10
    right, top = np.ceil(np.array(projected.upper_right) / 10) * 10
    width, height = int(round((right - left) / 10)), int(round((top - bottom) / 10))
    if max(width, height) > 2500:
        raise ValueError("AOI exceeds the 10 m imagery request limit. Select an area smaller than about 25 km per side.")
    return BBox([left, bottom, right, top], crs), (width, height), rio_transform.from_origin(left, top, 10, 10)


@app.post("/api/analyze")
async def analyze(req: AnalyzeRequest):
    def event_generator():
        started = time.perf_counter()
        try:
            def progress(msg, pct):
                return json.dumps({"type": "progress", "message": msg, "percent": pct}) + "\n"

            if engine.rf_model is None:
                raise ValueError("Trained RF model unavailable. Install rf_model.pickle with Git LFS and scikit-learn 1.3.2; restart the server.")
            sh_bbox, (width, height), transform = analysis_grid(req.bbox)
            yield progress(f"Starting {req.profile} analysis for: {req.label}", 0)
            yield progress("Discovering motorway, trunk and primary roads...", 10)
            roads = engine.fetch_roads(req.bbox)
            if roads.empty:
                raise ValueError("No major roads found in AOI.")

            south, west, north, east = req.bbox
            projected = roads.to_crs(sh_bbox.crs.pyproj_crs())
            buffers = projected.geometry.buffer(projected["highway"].map(road_buffer_m))
            aoi = gpd.GeoSeries([box(west, south, east, north)], crs="EPSG:4326").to_crs(projected.crs).iloc[0]
            buffers = buffers.intersection(aoi)
            shapes = [(g, 1) for g in buffers if not g.is_empty]
            if not shapes:
                raise ValueError("No major roads intersect the AOI.")
            road_mask = rio_features.rasterize(shapes, out_shape=(height, width), transform=transform, fill=0, all_touched=False)
            if not road_mask.any():
                raise ValueError("No road pixels at 10 m resolution. Select a larger road corridor.")
            yield progress(f"Found {len(roads)} road segments on a 10 m grid.", 25)

            catalog = SentinelHubCatalog(config=CONFIG)
            end_date = datetime.now()
            start_date = end_date - timedelta(days=req.months * 30)
            cdse_collection = DataCollection.SENTINEL2_L2A.define_from("s2l2a", service_url=CONFIG.sh_base_url)
            yield progress("Searching Copernicus catalog...", 30)
            date_range = f"{start_date.strftime('%Y-%m-%dT00:00:00Z')}/{end_date.strftime('%Y-%m-%dT23:59:59Z')}"
            catalog_key = hashlib.sha256(json.dumps([req.bbox, date_range]).encode()).hexdigest()
            catalog_dir = os.path.join(DATA_DIR, "catalog_cache")
            os.makedirs(catalog_dir, exist_ok=True)
            catalog_path = os.path.join(catalog_dir, catalog_key + ".json")
            results = None
            if os.path.isfile(catalog_path) and time.time() - os.path.getmtime(catalog_path) < 900:
                try:
                    with open(catalog_path) as saved:
                        results = json.load(saved)
                except (OSError, ValueError):
                    pass
            if results is None:
                results = list(catalog.search(
                    cdse_collection, bbox=sh_bbox, time=(start_date.date().isoformat(), end_date.date().isoformat()),
                    filter="eo:cloud_cover < 60",
                    fields={"include": ["properties.datetime", "id"], "exclude": []},
                ))
                temp_catalog = catalog_path + "." + uuid.uuid4().hex + ".tmp"
                try:
                    with open(temp_catalog, "w") as saved:
                        json.dump(results, saved)
                    os.replace(temp_catalog, catalog_path)
                except OSError:
                    logger.warning("Could not cache the catalog search")
            final_obs = select_observations(results, req.max_frames)
            if not final_obs:
                raise ValueError(f"No candidate imagery found in the last {req.months} months.")
            yield progress(f"Sampling {len(final_obs)} dates across the requested period.", 40)

            evalscript = """//VERSION=3
function setup() {
  return {
    input: ["B02", "B03", "B04", "B08", "CLM", "SCL", "dataMask"],
    output: { id: "default", bands: 7, sampleType: "FLOAT32" }
  };
}
function evaluatePixel(s) {
  return [s.B04, s.B03, s.B02, s.B08, s.CLM, s.SCL, s.dataMask];
}"""

            def fetch_frame(obs):
                timestamp = obs["properties"]["datetime"]
                request = SentinelHubRequest(
                    evalscript=evalscript,
                    input_data=[SentinelHubRequest.input_data(
                        data_collection=cdse_collection, time_interval=(timestamp, timestamp),
                        other_args={"processing": {"upsampling": "NEAREST", "downsampling": "NEAREST"}},
                    )],
                    responses=[SentinelHubRequest.output_response("default", MimeType.TIFF)],
                    bbox=sh_bbox, size=(width, height), config=CONFIG,
                    data_folder=os.path.join(DATA_DIR, "imagery_cache"),
                )
                frames = request.get_data(save_data=True)
                if not frames or frames[0].shape != (height, width, 7):
                    raise ValueError("Missing imagery or an unexpected image grid")
                return frames[0]

            detections, observations = [], []
            # Keep at most five images in flight, even for long archives.
            with ThreadPoolExecutor(max_workers=5) as executor:
                iterator = iter(final_obs)
                pending = {executor.submit(fetch_frame, obs): obs for obs in [next(iterator) for _ in range(min(5, len(final_obs)))]}
                while pending:
                    future = next(as_completed(pending))
                    obs = pending.pop(future)
                    timestamp = obs["properties"]["datetime"]
                    try:
                        data = future.result()
                        quality = observation_quality(data, road_mask, timestamp)
                        quality["scene_id"] = obs["id"]
                        quality["profile"] = req.profile
                        if quality["status"] == "usable":
                            found = engine.detect_trucks(data, req.bbox, timestamp, road_mask,
                                                         profile=req.profile, transform=transform,
                                                         crs=sh_bbox.crs.pyproj_crs())
                            quality["detection_count"] = len(found)
                            detections.extend(found)
                        observations.append(quality)
                        del data
                    except Exception as exc:
                        logger.warning("Frame %s failed: %s", timestamp, exc)
                        observations.append({"timestamp": timestamp, "scene_id": obs["id"],
                                             "profile": req.profile, "status": "failed",
                                             "detection_count": None, "clear_road_fraction": None})
                    next_obs = next(iterator, None)
                    if next_obs is not None:
                        pending[executor.submit(fetch_frame, next_obs)] = next_obs
                    yield progress(f"Processed {len(observations)}/{len(final_obs)} observations", 40 + int(55 * len(observations) / len(final_obs)))

            observations.sort(key=lambda o: o["timestamp"])
            detections.sort(key=lambda d: (d["timestamp"], d["id"]))
            mission_id = uuid.uuid4().hex
            usable = sum(o["status"] == "usable" for o in observations)
            elapsed = round(time.perf_counter() - started, 1)
            engine.history.append({
                "mission_id": mission_id, "label": req.label, "bbox": req.bbox,
                "road_count": len(roads), "detections": detections,
                "observations": observations, "profile": req.profile,
                "timestamp": datetime.now().isoformat(),
            })
            yield json.dumps({
                "type": "result", "mission_id": mission_id, "road_count": len(roads),
                "detection_count": len(detections), "usable_observations": usable,
                "excluded_observations": len(observations) - usable,
                "elapsed_seconds": elapsed,
                "message": f"{len(detections)} candidate signatures across {usable}/{len(observations)} usable observations in {elapsed}s. Excluded dates are gaps, not zero traffic.",
            }) + "\n"
        except Exception as exc:
            logger.error("Stream error: %s", exc, exc_info=True)
            yield json.dumps({"type": "error", "message": str(exc)}) + "\n"

    return StreamingResponse(event_generator(), media_type="application/x-ndjson")


@app.get("/api/roads")
async def get_roads(min_lat: float, min_lon: float, max_lat: float, max_lon: float):
    roads = engine.fetch_roads([min_lat, min_lon, max_lat, max_lon])
    if roads.empty:
        return {"type": "FeatureCollection", "features": []}
    return json.loads(roads.to_json())


@app.get("/api/sites")
async def get_sites():
    sites = [
        {
            "id": s["id"], "name": s["name"],
            "lat": (s["bbox"][0] + s["bbox"][2]) / 2,
            "lng": (s["bbox"][1] + s["bbox"][3]) / 2,
            "bbox": s["bbox"], "country": s["country"], "type": s["type"],
        }
        for s in FEATURED_SITES
    ]
    history_sites = [
        {
            "id": h["mission_id"], "name": h["label"],
            "lat": (h["bbox"][0] + h["bbox"][2]) / 2,
            "lng": (h["bbox"][1] + h["bbox"][3]) / 2,
            "bbox": h["bbox"], "country": "Analysis ROI", "type": "history",
        }
        for h in engine.history
    ]
    return sites + history_sites


@app.get("/api/feed")
async def get_feed():
    feed = []
    for h in engine.history:
        for d in h["detections"][:5]:
            feed.append({
                "alert_id": f"alert_{d['id']}",
                "site": {"id": h["mission_id"], "name": h["label"], "country": "ROI"},
                "status": "WARNING",
                "timestamp": d["timestamp"],
                "change_classification": {"change_type": "truck_movement", "confidence": d["confidence"]},
                "detection": {
                    "anomaly_score": round(d["confidence"] * 100, 1),
                    "date_before": "Baseline",
                    "date_after": d["timestamp"],
                },
            })
    return sorted(feed, key=lambda x: x["timestamp"], reverse=True)


@app.get("/api/analytics/trends")
async def get_trends(from_date: str = None, to_date: str = None, site_ids: str = None):
    return build_trends(engine.history, from_date, to_date, site_ids)


@app.get("/api/detections/{mission_id}")
async def get_detections(mission_id: str):
    mission = next((h for h in engine.history if h["mission_id"] == mission_id), None)
    if not mission:
        raise HTTPException(status_code=404, detail="Mission not found")
    return mission["detections"]


@app.get("/api/missions/{mission_id}/export")
async def export_mission(mission_id: str, kind: Literal["observations", "detections"] = "observations"):
    mission = next((m for m in engine.history if m["mission_id"] == mission_id), None)
    if mission is None:
        raise HTTPException(status_code=404, detail="Mission not found")
    fields = (["timestamp", "scene_id", "profile", "status", "detection_count", "clear_road_fraction", "clear_road_pixels", "road_pixels"]
              if kind == "observations" else
              ["id", "timestamp", "lat", "lon", "profile", "s_score", "score_calibrated", "speed_kmh", "heading", "image_url"])
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(mission[kind])
    return Response(output.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="drishx-{mission_id}-{kind}.csv"'})


class AuthRequest(BaseModel):
    client_id: str
    client_secret: str


AUTH_VERIFY_TIMEOUT_S = 15


def _verify_credentials():
    """Request a fresh CDSE token with the credentials currently in CONFIG."""
    from sentinelhub import SentinelHubSession
    session = SentinelHubSession(config=CONFIG)
    _ = session.token


def _classify_auth_error(exc: Exception):
    """Map a verification failure to a machine-readable code + user-facing message."""
    text = str(exc).lower()
    if isinstance(exc, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)) \
            or "connection" in text or "timed out" in text or "name resolution" in text:
        return ("cdse_unreachable",
                "Copernicus is not responding — your keys may be fine. Try again in a few minutes.")
    if "invalid_client" in text or "invalid client" in text or "unauthorized" in text or "401" in text:
        return ("invalid_credentials",
                "Copernicus rejected these keys. Re-copy them, or create a new OAuth client.")
    return ("cdse_error", f"Copernicus returned an unexpected error: {exc}")


@app.post("/api/auth")
async def authenticate(req: AuthRequest):
    """Validate and store Copernicus credentials at runtime."""
    prev_id, prev_secret = CONFIG.sh_client_id, CONFIG.sh_client_secret
    CONFIG.sh_client_id = req.client_id.strip()
    CONFIG.sh_client_secret = req.client_secret.strip()

    try:
        await asyncio.wait_for(asyncio.to_thread(_verify_credentials), timeout=AUTH_VERIFY_TIMEOUT_S)
    except asyncio.TimeoutError:
        CONFIG.sh_client_id, CONFIG.sh_client_secret = prev_id, prev_secret
        logger.error(f"Auth verification timed out after {AUTH_VERIFY_TIMEOUT_S}s")
        return {"status": "error", "code": "cdse_unreachable",
                "message": f"Copernicus did not answer within {AUTH_VERIFY_TIMEOUT_S} seconds — "
                           "your keys may be fine. Try again in a few minutes."}
    except Exception as e:
        # Restore whatever was in place before this attempt. Never fall back to
        # env vars here: they may be unset, which would wipe working credentials.
        CONFIG.sh_client_id, CONFIG.sh_client_secret = prev_id, prev_secret
        code, message = _classify_auth_error(e)
        logger.error(f"Auth failed ({code}): {e}")
        return {"status": "error", "code": code, "message": message}

    try:
        CONFIG.save()
    except Exception as e:
        # A persistence failure shouldn't break the live link; it only means
        # credentials won't survive a server restart.
        logger.warning(f"Credentials verified but could not be persisted: {e}")

    AUTH_STATE["source"] = "ui"
    AUTH_STATE["last_verified"] = datetime.utcnow().isoformat() + "Z"
    AUTH_STATE["verification_error"] = None
    logger.info("Copernicus credentials updated and verified via UI.")
    return {"status": "success", "code": "linked", "message": "Copernicus link established."}


@app.get("/api/auth/status")
async def auth_status():
    """Report whether credentials are configured, their source, and last verification time."""
    linked = bool(CONFIG.sh_client_id and CONFIG.sh_client_secret)
    return {
        "linked": linked,
        "source": AUTH_STATE["source"] if linked else None,
        "last_verified": AUTH_STATE["last_verified"] if linked else None,
        "verification_error": AUTH_STATE["verification_error"] if linked else None,
    }


@app.delete("/api/auth")
async def disconnect():
    """Forget stored Copernicus credentials (runtime and persisted config)."""
    CONFIG.sh_client_id = ""
    CONFIG.sh_client_secret = ""
    try:
        CONFIG.save()
    except Exception as e:
        logger.warning(f"Could not clear persisted credentials: {e}")
    AUTH_STATE["source"] = None
    AUTH_STATE["last_verified"] = None
    AUTH_STATE["verification_error"] = None
    message = "Copernicus credentials removed."
    if os.getenv("COPERNICUS_CLIENT_ID") and os.getenv("COPERNICUS_CLIENT_SECRET"):
        message += " Environment credentials will re-link on the next server restart."
    return {"status": "success", "code": "disconnected", "message": message}


# Serve static detections
app.mount("/detections", StaticFiles(directory=DETECTION_DIR), name="detections")

# Serve frontend
app.mount("/", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "frontend"), html=True), name="frontend")


if __name__ == "__main__":
    try:
        _verify_credentials()
        AUTH_STATE["last_verified"] = datetime.utcnow().isoformat() + "Z"
        logger.info("Copernicus Data Space Authentication: SUCCESS")
    except Exception as e:
        code, message = _classify_auth_error(e)
        AUTH_STATE["verification_error"] = {"code": code, "message": message}
        logger.error("Copernicus Data Space Authentication: FAILED (%s)", code)
        logger.warning("System will start, but satellite monitoring may be degraded.")

    uvicorn.run(app, host=os.getenv("DRISHX_HOST", "127.0.0.1"), port=8000)
