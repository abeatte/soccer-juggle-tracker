#!/usr/bin/env python
"""Standalone wrapper for the auto-calibration tuner.

Lets you run the tuner without installing the package:

    python tools/tune.py CLIPS_DIR [--write] [--space full] ...

The implementation lives in :mod:`juggletracker.tune` (also exposed as the
``juggletracker.cli tune`` subcommand). Run with ``--help`` for all options.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggletracker.tune import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
