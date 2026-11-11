#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["tree-sitter>=0.23", "tree-sitter-typescript>=0.23", "jedi>=0.19"]
# ///
"""Use the canonical discovery engine from the sibling code-review-sweep skill.

The metadata above repeats the engine's, because `uv run --script` reads only this file."""

import importlib.util
from pathlib import Path
import runpy
import sys


ENGINE = Path(__file__).resolve().parents[2] / "code-review-sweep" / "sweep.py"

if __name__ == "__main__":
    runpy.run_path(str(ENGINE), run_name="__main__")
else:
    spec = importlib.util.spec_from_file_location(__name__, ENGINE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[__name__] = module
    spec.loader.exec_module(module)
