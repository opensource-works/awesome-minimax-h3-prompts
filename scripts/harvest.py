#!/usr/bin/env python3
"""Compatibility entry point for the catalog-v2 hydration pipeline.

The old implementation queried an unofficial service, silently dropped
failures, and overwrote human review/rights metadata. Use hydrate.py directly
for new automation; this wrapper remains for contributors with old commands.
"""
from hydrate import main


if __name__ == "__main__":
    print("harvest.py is deprecated; running the official-API hydrate.py workflow")
    main()
