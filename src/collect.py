#!/usr/bin/env python3
"""
Runs all four DORA metric collection scripts in sequence, writing their
CSVs to ./data/. Equivalent to running trello_mttr.py, trello_cfr.py,
deploy_frequency.py, and lead_time.py individually.

Usage:
    ./fetch_data.sh
"""

import runpy

SCRIPTS = ["trello_mttr", "trello_cfr", "deploy_frequency", "lead_time"]


def main():
    for script in SCRIPTS:
        print(f"--- {script}.py ---")
        try:
            runpy.run_module(script, run_name="__main__")
        except SystemExit as e:
            if e.code not in (None, 0):
                raise
        print()


if __name__ == "__main__":
    main()
