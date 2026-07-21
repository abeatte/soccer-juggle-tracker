"""Configuration loading and validation.

Loads ``config.yaml`` (falling back to ``config.example.yaml``) into a nested
dict wrapped in dot-accessible objects, and applies a couple of derived
defaults (e.g. CPU thread count).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import yaml


# Machine-written calibration overrides, deep-merged over config.yaml at load.
# Keeps the hand-authored, commented config.yaml pristine; deleting this file
# reverts calibration to config.yaml defaults.
CALIBRATION_OVERRIDES_FILENAME = "calibration_overrides.yaml"

# Scalar calibration params exposed as editable HA number entities. Each maps a
# dotted config path to UI bounds. ROI + keypoint lists are intentionally NOT
# here (they need the Phase-2 visual editor, not a slider).
TUNABLE_PARAMS = [
    {"path": ("models", "ball_conf"), "slug": "ball_conf",
     "min": 0.0, "max": 1.0, "step": 0.01, "int": False,
     "name": "Ball Confidence", "icon": "mdi:soccer"},
    {"path": ("models", "person_conf"), "slug": "person_conf",
     "min": 0.0, "max": 1.0, "step": 0.01, "int": False,
     "name": "Person Confidence", "icon": "mdi:human"},
    {"path": ("models", "face_conf"), "slug": "face_conf",
     "min": 0.0, "max": 1.0, "step": 0.01, "int": False,
     "name": "Face Confidence", "icon": "mdi:face-recognition"},
    {"path": ("processing", "infer_long_edge"), "slug": "infer_long_edge",
     "min": 320, "max": 1280, "step": 32, "int": True,
     "name": "Inference Resolution", "icon": "mdi:image-size-select-large"},
    {"path": ("processing", "person_stride"), "slug": "person_stride",
     "min": 1, "max": 10, "step": 1, "int": True,
     "name": "Person/Pose Stride", "icon": "mdi:skip-forward"},
    {"path": ("juggle", "contact_radius_px"), "slug": "contact_radius_px",
     "min": 20, "max": 300, "step": 5, "int": False,
     "name": "Contact Radius (px)", "icon": "mdi:radius"},
    {"path": ("juggle", "min_arc_px"), "slug": "min_arc_px",
     "min": 2, "max": 80, "step": 1, "int": False,
     "name": "Min Arc Height (px)", "icon": "mdi:arc"},
    {"path": ("juggle", "ground_y_frac"), "slug": "ground_y_frac",
     "min": 0.5, "max": 1.0, "step": 0.01, "int": False,
     "name": "Ground Line (frac)", "icon": "mdi:arrow-collapse-down"},
    {"path": ("juggle", "smooth_window"), "slug": "smooth_window",
     "min": 1, "max": 15, "step": 1, "int": True,
     "name": "Smoothing Window", "icon": "mdi:chart-bell-curve"},
    {"path": ("juggle", "lost_frames_reset"), "slug": "lost_frames_reset",
     "min": 5, "max": 120, "step": 1, "int": True,
     "name": "Lost-Ball Frames", "icon": "mdi:eye-off-outline"},
    {"path": ("juggle", "max_bridge_frames"), "slug": "max_bridge_frames",
     "min": 0, "max": 30, "step": 1, "int": True,
     "name": "Ball Bridge Frames", "icon": "mdi:vector-polyline"},
    {"path": ("identity", "match_threshold"), "slug": "match_threshold",
     "min": 0.2, "max": 0.8, "step": 0.01, "int": False,
     "name": "Face Match Threshold", "icon": "mdi:account-check"},
    {"path": ("identity", "vote_min_frames"), "slug": "vote_min_frames",
     "min": 1, "max": 10, "step": 1, "int": True,
     "name": "Identity Vote Frames", "icon": "mdi:vote"},
    {"path": ("ball_fallback", "hough_param2"), "slug": "fallback_sensitivity",
     "min": 5, "max": 60, "step": 1, "int": False,
     "name": "Ball CV Sensitivity", "icon": "mdi:tune-variant"},
    {"path": ("ball_fallback", "max_radius"), "slug": "fallback_max_radius",
     "min": 5, "max": 120, "step": 1, "int": True,
     "name": "Ball CV Max Radius", "icon": "mdi:circle-outline"},
    {"path": ("ball_fallback", "search_radius"), "slug": "fallback_search_radius",
     "min": 20, "max": 400, "step": 10, "int": True,
     "name": "Ball CV Search Radius", "icon": "mdi:image-filter-center-focus"},
    {"path": ("ball_fallback", "min_radius"), "slug": "fallback_min_radius",
     "min": 1, "max": 60, "step": 1, "int": True,
     "name": "Ball CV Min Radius", "icon": "mdi:circle-small"},
    {"path": ("ball_fallback", "hough_param1"), "slug": "fallback_hough_param1",
     "min": 20, "max": 300, "step": 5, "int": False,
     "name": "Ball CV Edge Threshold", "icon": "mdi:image-filter-black-white"},
    {"path": ("ball_fallback", "dp"), "slug": "fallback_dp",
     "min": 1.0, "max": 3.0, "step": 0.1, "int": False,
     "name": "Ball CV Accumulator (dp)", "icon": "mdi:grid"},
    {"path": ("identity", "max_people"), "slug": "max_people",
     "min": 1, "max": 10, "step": 1, "int": True,
     "name": "Max People", "icon": "mdi:account-group"},
    {"path": ("processing", "torch_threads"), "slug": "torch_threads",
     "min": 0, "max": 16, "step": 1, "int": True,
     "name": "Torch Threads", "icon": "mdi:cpu-64-bit"},
    {"path": ("thermal", "max_temp_c"), "slug": "thermal_max_temp_c",
     "min": 60, "max": 100, "step": 1, "int": True,
     "name": "Thermal Max Temp (C)", "icon": "mdi:thermometer-alert"},
    {"path": ("thermal", "resume_temp_c"), "slug": "thermal_resume_temp_c",
     "min": 50, "max": 95, "step": 1, "int": True,
     "name": "Thermal Resume Temp (C)", "icon": "mdi:thermometer-low"},
]

# Slug for the ball-fallback ON/OFF *switch* (a boolean, so it is NOT part of
# TUNABLE_PARAMS which only produces numeric `number` entities). Handled as a
# special case in the calibration command router. Toggling it writes
# ball_fallback.enabled to the overrides file; it activates on Apply & Restart,
# exactly like the numeric calibration entities.
BALL_FALLBACK_ENABLED_SLUG = "ball_fallback_enabled"
BALL_FALLBACK_ENABLED_PATH = ("ball_fallback", "enabled")

_PARAM_BY_SLUG = {p["slug"]: p for p in TUNABLE_PARAMS}


def param_for_slug(slug: str) -> dict | None:
    return _PARAM_BY_SLUG.get(slug)


def get_by_path(raw: dict, path: tuple, default: Any = None) -> Any:
    d = raw
    for k in path[:-1]:
        d = d.get(k, {}) if isinstance(d, dict) else {}
    return d.get(path[-1], default) if isinstance(d, dict) else default


def clamp_param(spec: dict, value: float) -> float | int:
    """Clamp a raw value to the param's bounds and cast to its type."""
    v = max(float(spec["min"]), min(float(spec["max"]), float(value)))
    return int(round(v)) if spec.get("int") else round(v, 4)


def _deep_merge(base: dict, over: dict) -> dict:
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def load_overrides(overrides_path: str) -> dict:
    if overrides_path and os.path.exists(overrides_path):
        try:
            with open(overrides_path, "r", encoding="utf-8") as fh:
                return yaml.safe_load(fh) or {}
        except Exception:
            return {}
    return {}


def set_override(overrides_path: str, path: tuple, value: Any) -> None:
    """Persist a single overridden value into the overrides file (nested)."""
    data = load_overrides(overrides_path)
    d = data
    for k in path[:-1]:
        d = d.setdefault(k, {})
    d[path[-1]] = value
    with open(overrides_path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(data, fh, default_flow_style=False, sort_keys=False)


def clear_overrides(overrides_path: str) -> bool:
    """Remove the overrides file (revert to config.yaml defaults)."""
    try:
        if overrides_path and os.path.exists(overrides_path):
            os.remove(overrides_path)
            return True
    except OSError:
        pass
    return False


class _Dotted:
    """Thin dot-access wrapper over a dict, recursive."""

    def __init__(self, data: dict[str, Any]):
        self._data = data

    def __getattr__(self, item: str) -> Any:
        try:
            val = self._data[item]
        except KeyError as exc:  # pragma: no cover - defensive
            raise AttributeError(item) from exc
        if isinstance(val, dict):
            return _Dotted(val)
        return val

    def get(self, item: str, default: Any = None) -> Any:
        return self._data.get(item, default)

    def as_dict(self) -> dict[str, Any]:
        return self._data


@dataclass
class Config:
    raw: dict[str, Any]

    camera: _Dotted = None  # type: ignore[assignment]
    capture: _Dotted = None  # type: ignore[assignment]
    processing: _Dotted = None  # type: ignore[assignment]
    models: _Dotted = None  # type: ignore[assignment]
    identity: _Dotted = None  # type: ignore[assignment]
    juggle: _Dotted = None  # type: ignore[assignment]
    home_assistant: _Dotted = None  # type: ignore[assignment]
    database: _Dotted = None  # type: ignore[assignment]
    calibration: _Dotted = None  # type: ignore[assignment]
    ball_fallback: _Dotted = None  # type: ignore[assignment]
    roi: list[float] = None  # type: ignore[assignment]
    source_path: str = None  # type: ignore[assignment]
    overrides_path: str = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        for key in (
            "camera",
            "capture",
            "processing",
            "models",
            "identity",
            "juggle",
            "home_assistant",
            "database",
            "calibration",
            "ball_fallback",
        ):
            setattr(self, key, _Dotted(self.raw.get(key, {})))
        self.roi = self.raw.get("roi", [0.0, 0.0, 1.0, 1.0])


def _project_root() -> str:
    # src/juggle_tracker/config.py -> repo root is three levels up.
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def load_config(path: str | None = None) -> Config:
    """Load configuration. Explicit ``path`` wins; else config.yaml; else example."""
    root = _project_root()
    candidates = []
    if path:
        candidates.append(path)
    candidates += [
        os.path.join(root, "config.yaml"),
        os.path.join(root, "config.example.yaml"),
    ]
    for candidate in candidates:
        if candidate and os.path.exists(candidate):
            with open(candidate, "r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
            # Deep-merge machine-written calibration overrides over the base.
            overrides_path = os.path.join(
                os.path.dirname(candidate), CALIBRATION_OVERRIDES_FILENAME
            )
            overrides = load_overrides(overrides_path)
            if overrides:
                _deep_merge(data, overrides)
            cfg = Config(raw=data)
            _resolve_paths(cfg, root)
            cfg.source_path = candidate
            cfg.overrides_path = overrides_path
            return cfg
    raise FileNotFoundError(
        "No config found. Copy config.example.yaml to config.yaml."
    )


def _resolve_paths(cfg: Config, root: str) -> None:
    """Make relative paths absolute against the project root."""

    def abs_(p: str) -> str:
        return p if os.path.isabs(p) else os.path.join(root, p)

    cfg.raw.setdefault("database", {})
    cfg.raw["database"]["path"] = abs_(cfg.database.get("path", "data/juggle.db"))
    cap = cfg.raw.setdefault("capture", {})
    cap["inbox_dir"] = abs_(cap.get("inbox_dir", "inbox"))
    cap["processed_dir"] = abs_(cap.get("processed_dir", "processed"))
    cap["highscore_dir"] = abs_(cap.get("highscore_dir", "highscores"))
    mdl = cfg.raw.setdefault("models", {})
    for k in ("detector", "pose"):
        if mdl.get(k):
            mdl[k] = abs_(mdl[k])
    # Re-wrap so the _Dotted views see the mutations.
    cfg.__post_init__()
