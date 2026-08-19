#!/usr/bin/env python3
"""
generate_fillers.py — Render the filler-clip bank (assets/fillers/).

Thin wrapper kept for backward compatibility. The generic renderer now lives in
scripts/generate_cached_audio.py, which produces all cached-speech banks (fillers,
acks, script lines, bank questions). This entry point renders only the fillers.

    python generate_fillers.py            # render fillers via Cartesia
    python generate_fillers.py --dry-run  # silent PCM, no TTS spend
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_HERE, "scripts"))

from generate_cached_audio import render_grouped, _load_filler_groups, ASSETS  # noqa: E402


def main():
    dry_run = "--dry-run" in sys.argv
    render_grouped(os.path.join(ASSETS, "fillers"), "phase", _load_filler_groups(), dry_run=dry_run)


if __name__ == "__main__":
    main()
