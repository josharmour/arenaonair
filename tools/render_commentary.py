"""Render a booth replay transcript as a two-voice WAV, without audio playback.

PYTHONPATH=src:. python -m tools.render_commentary --input replay.json --out commentary.wav
Uses the installed Kokoro engine and each delivered turn's actual voice selection.
Idle match time is condensed; no commentary is generated or rewritten here.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from arenaonair.render import render


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--speed', type=float, default=1.0)
    parser.add_argument('--gap', type=float, default=.4)
    args = parser.parse_args()
    import torch
    torch.set_num_threads(4)
    payload = json.loads(args.input.read_text())
    result = render(payload['transcript'], args.out, speed=args.speed, gap=args.gap)
    cues = args.out.with_suffix('.cues.json')
    with os.fdopen(os.open(cues, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'w') as f:
        json.dump(result, f, indent=2)
    print(f'Wrote {args.out}: {result["duration_seconds"]:.1f} seconds', flush=True)


if __name__ == '__main__':
    main()
