#!/usr/bin/env python3
"""Compatibility entry point for the reusable Type1 training tool."""
try:
    from .tools.train_type1 import edit_distance, evaluate, main, read_dataset, split_rows
except ImportError:  # direct execution from second_bot/
    from tools.train_type1 import edit_distance, evaluate, main, read_dataset, split_rows

if __name__ == "__main__":
    raise SystemExit(main())
