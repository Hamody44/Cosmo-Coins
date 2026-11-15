#!/usr/bin/env python3
"""Compatibility facade for Type1 RGB evaluation helpers and CLI."""
try:
    from .tools.evaluate_type1_rgb import *  # noqa: F401,F403
    from .tools.evaluate_type1_rgb import main
except ImportError:  # direct execution with PYTHONPATH=second_bot
    from tools.evaluate_type1_rgb import *  # noqa: F401,F403
    from tools.evaluate_type1_rgb import main

if __name__ == "__main__":
    raise SystemExit(main())
