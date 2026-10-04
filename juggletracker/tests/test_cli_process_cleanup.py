from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import juggletracker.cli as cli  # noqa: E402
import juggletracker.pipeline as pipeline_module  # noqa: E402


def test_process_command_closes_pipeline_when_processing_fails(monkeypatch):
    class FakePipeline:
        closed = False

        def __init__(self, _cfg):
            pass

        def process(self, *_args, **_kwargs):
            raise RuntimeError("processing failed")

        def close(self):
            self.closed = True

    instances = []

    def create_pipeline(cfg):
        instance = FakePipeline(cfg)
        instances.append(instance)
        return instance

    monkeypatch.setattr(cli, "load_config", lambda _path: object())
    monkeypatch.setattr(pipeline_module, "Pipeline", create_pipeline)
    args = SimpleNamespace(
        config="config.yaml",
        clip="clip.mp4",
        debug_video=None,
        move=False,
    )

    with pytest.raises(RuntimeError, match="processing failed"):
        cli._process(args)

    assert instances[0].closed