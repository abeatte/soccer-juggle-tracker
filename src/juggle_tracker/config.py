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
    roi: list[float] = None  # type: ignore[assignment]

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
            cfg = Config(raw=data)
            _resolve_paths(cfg, root)
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
    mdl = cfg.raw.setdefault("models", {})
    for k in ("detector", "pose"):
        if mdl.get(k):
            mdl[k] = abs_(mdl[k])
    # Re-wrap so the _Dotted views see the mutations.
    cfg.__post_init__()
