#!/usr/bin/env python3
"""ArenaOnAir replay harness -- fixture replay + QR6 drone scoring + determinism.

Replays a recorded match fixture (JSONL of ``{"kind": ..., "obj": {...}}``
records) through the FULL narration pipeline:

    gre_parser.parse_line_all(json.dumps(record['obj']))
        -> GameStateBuilder(name_resolver).replay   (snapshot stream)
        -> EventDiffer.diff(prev, cur, msgs_since_prev)
           + StoryModel.update(cur)                  (narratable events)
        -> Narrator.render(event, state)             (utterances)

then SCORES the resulting transcript against the DESIGN section 6 contracts:

* ``drone_score``          -- consecutive utterance pairs sharing a sentence
  shape (templates.shape_signature). Contract 7 / QR6: PASS at 0.
* ``window_repeat_score``  -- max repeat count (occurrences beyond the
  first) of any single text inside a rolling 8-utterance window.
  QR6: PASS at <= 1.
* ``coverage_by_kind``     -- per event kind how many events rendered vs were
  suppressed by the narrator.
* throughput               -- events/sec processed + total utterances (latency
  proxy per PRD section 8).

``--determinism`` replays the fixture a second time from scratch and asserts
the utterance list is identical (contract 6).

Exit code is 0 iff every checked threshold passes.

Usage:
    python tools/replay_harness.py [--fixture PATH] [--verbose]
                                   [--json OUT] [--determinism]

Stdlib only; Python >= 3.10.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from arenaonair.carddb import DEFAULT_DB_PATH, CardDb          # noqa: E402
from arenaonair.differ import EventDiffer                      # noqa: E402
from arenaonair.gre_parser import parse_line_all               # noqa: E402
from arenaonair.narrator import Narrator                       # noqa: E402
from arenaonair.state_builder import GameStateBuilder          # noqa: E402
from arenaonair.story import StoryModel                        # noqa: E402
from arenaonair.templates import shape_signature               # noqa: E402

__all__ = [
    "DRONE_PASS_THRESHOLD", "WINDOW_REPEAT_PASS_THRESHOLD", "WINDOW_SIZE",
    "load_records", "make_name_resolver", "replay_fixture",
    "drone_score", "window_repeat_score", "coverage_by_kind",
    "score_report", "run_replay", "main",
]

#: QR6 / DESIGN contract 7: zero consecutive same-shape pairs allowed.
DRONE_PASS_THRESHOLD = 0

#: QR6: any single text may repeat at most once inside the rolling window.
WINDOW_REPEAT_PASS_THRESHOLD = 1

#: Rolling-window size for the repeat check (narrator's variety window).
WINDOW_SIZE = 8


def load_records(fixture_path):
    """Read a JSONL fixture into a list of record dicts. Malformed lines skip."""
    records = []
    with open(fixture_path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict) and isinstance(rec.get("obj"), dict):
                records.append(rec)
    return records


def make_name_resolver():
    """grpId -> card name resolver backed by the carddb cache when present.

    Returns ``None`` when the cache does not exist (the builder degrades to
    unnamed cards gracefully). Lookups are memoized; failures degrade to None.
    """
    if not DEFAULT_DB_PATH.is_file():
        return None
    try:
        db = CardDb()
    except Exception:
        return None

    cache = {}

    def resolve(grp_id):
        if not isinstance(grp_id, int) or isinstance(grp_id, bool):
            return None
        if grp_id in cache:
            return cache[grp_id]
        try:
            info = db.lookup(grp_id)
        except Exception:
            info = None
        name = getattr(info, "name", None)
        cache[grp_id] = name if isinstance(name, str) and name else None
        return cache[grp_id]

    return resolve


def replay_fixture(records, name_resolver=None):
    """Replay parsed fixture records through parser->builder->differ->story->narrator.

    Returns a dict with keys: utterances (list), event_kinds (list),
    snapshots (int), messages (int), elapsed_s (float), events_per_sec (float).
    """
    builder = GameStateBuilder(name_resolver=name_resolver)
    differ = EventDiffer()
    story = StoryModel()
    narrator = Narrator()

    utterances = []
    event_kinds = []
    snapshots = 0
    messages = 0

    prev_state = None
    cur_state = None       # authoritative current snapshot handed to apply()
    window_msgs = []       # GRE messages accumulated since the last snapshot

    t0 = time.perf_counter()
    for rec in records:
        obj = rec.get("obj")
        if not isinstance(obj, dict):
            continue
        try:
            raw = json.dumps(obj)
        except (TypeError, ValueError):
            continue
        for msg in parse_line_all(0.0, raw):
            messages += 1
            window_msgs.append(msg)
            nxt = builder.apply(cur_state, msg)
            if nxt is None:
                continue

            snapshots += 1
            events = list(differ.diff(prev_state, nxt, window_msgs))
            events.extend(story.update(nxt))
            for event in events:
                event_kinds.append(event.kind)
                utt = narrator.render(event, nxt)
                if utt is not None:
                    utterances.append(utt)

            # The new snapshot becomes both the diff baseline and the state
            # handed back into apply(); the message window resets.
            prev_state = nxt
            cur_state = nxt
            window_msgs = []

    elapsed_s = time.perf_counter() - t0

    return {
        "utterances": utterances,
        "event_kinds": event_kinds,
        "snapshots": snapshots,
        "messages": messages,
        "elapsed_s": elapsed_s,
        "events_per_sec": (len(event_kinds) / elapsed_s) if elapsed_s > 0 else 0.0,
    }


def drone_score(utterances):
    """Count consecutive utterance pairs sharing a non-empty shape signature."""
    sigs = [shape_signature(getattr(u, "text", "")) for u in utterances]
    return sum(1 for a, b in zip(sigs, sigs[1:]) if a == b and a != "")


def window_repeat_score(utterances, window=WINDOW_SIZE):
    """Repeat count of the worst text inside any rolling ``window`` slice.

    A text's repeat count inside a slice is ``occurrences - 1`` (each extra
    appearance beyond the first is one repeat); the score is the maximum over
    every rolling slice. QR6 PASS threshold is ``<= 1``: at most one repeat of
    any single line within eight utterances.
    """
    texts = [getattr(u, "text", "") for u in utterances]
    best = 0
    for start in range(len(texts)):
        counts = {}
        for text in texts[start:start + window]:
            counts[text] = counts.get(text, 0) + 1
        if counts:
            local_max = max(counts.values()) - 1
            if local_max > best:
                best = local_max
    return best


def coverage_by_kind(event_kinds, utterances):
    """{kind: {'rendered': n_rendered, 'suppressed': n_suppressed}}."""
    seen_events = {}
    for kind in event_kinds:
        seen_events[kind] = seen_events.get(kind, 0) + 1
    rendered_counts = {}
    for utt in utterances:
        kind = getattr(utt, "kind", "?")
        rendered_counts[kind] = rendered_counts.get(kind, 0) + 1

    out = {}
    for kind in sorted(set(seen_events) | set(rendered_counts)):
        rendered_n = rendered_counts.get(kind, 0)
        out[kind] = {
            "rendered": rendered_n,
            "suppressed": max(seen_events.get(kind, 0) - rendered_n, 0),
        }
    return out


def score_report(result):
    """Score one replay_fixture() result into a plain-dict report."""
    utterances = result["utterances"]
    drone_n = drone_score(utterances)
    repeat_n = window_repeat_score(utterances)
    return {
        "drone_score": drone_n,
        "drone_pass": drone_n <= DRONE_PASS_THRESHOLD,
        "window_repeat_score": repeat_n,
        "window_repeat_pass": repeat_n <= WINDOW_REPEAT_PASS_THRESHOLD,
        "coverage_by_kind": coverage_by_kind(
            result["event_kinds"], utterances),
        "total_events": len(result["event_kinds"]),
        "total_utterances": len(utterances),
        "snapshots": result["snapshots"],
        "messages": result["messages"],
        "events_per_sec": round(result["events_per_sec"], 2),
        "elapsed_s": round(result["elapsed_s"], 4),
    }


def _utterance_tuples(utterances):
    """Hashable projection of an utterance list for determinism comparison."""
    return [(getattr(u, "uid", ""), getattr(u, "kind", ""),
             getattr(u, "text", ""), getattr(u, "salience", -1),
             round(float(getattr(u, "ts_created", 0.0)), 6))
            for u in utterances]


def run_replay(fixture_path, determinism=False):
    """Full harness run over one fixture.

    Returns ``(report_dict, passed_bool)``. With ``determinism=True`` the
    fixture is replayed twice from scratch and the utterance lists compared;
    ``report['deterministic']`` reflects the outcome.
    """
    records = load_records(fixture_path)
    resolver = make_name_resolver()

    first = replay_fixture(records, name_resolver=resolver)
    report = score_report(first)

    deterministic = True
    if determinism:
        second = replay_fixture(records, name_resolver=resolver)
        deterministic = (_utterance_tuples(first["utterances"])
                         == _utterance_tuples(second["utterances"]))
        report["deterministic"] = deterministic

    report["fixture"] = str(fixture_path)
    report["utterances"] = len(first["utterances"])

    passed_flags = [report["drone_pass"], report["window_repeat_pass"]]
    if determinism:
        passed_flags.append(report["deterministic"])
    return report, all(passed_flags)


# ---------------------------------------------------------------------------
# Human table + CLI
# ---------------------------------------------------------------------------

def _print_table(report):
    print("=" * 62)
    print("ArenaOnAir replay harness -- %s" % report.get("fixture", "?"))
    print("=" * 62)
    print("messages parsed      : %s" % report["messages"])
    print("snapshots folded     : %s" % report["snapshots"])
    print("events emitted       : %s" % report["total_events"])
    print("utterances rendered  : %s" % report["total_utterances"])
    print("throughput           : %s events/sec" % report["events_per_sec"])
    print("-" * 62)
    print("drone_score          : %s (threshold <= %s) [%s]"
          % (report["drone_score"], DRONE_PASS_THRESHOLD,
             "PASS" if report["drone_pass"] else "FAIL"))
    print("window_repeat_score  : %s (threshold <= %s, window=%s) [%s]"
          % (report["window_repeat_score"], WINDOW_REPEAT_PASS_THRESHOLD,
             WINDOW_SIZE,
             "PASS" if report["window_repeat_pass"] else "FAIL"))
    if "deterministic" in report:
        det_flag = "PASS" if report["deterministic"] else "FAIL"
        print("deterministic replay : %s" % det_flag)
    print("-" * 62)
    print("%-24s%10s%12s" % ("kind", "rendered", "suppressed"))
    for kind, counts in report["coverage_by_kind"].items():
        print("%-24s%10s%12s"
              % (kind, counts["rendered"], counts["suppressed"]))
    print("=" * 62)


def main(argv=None):
    """CLI entry point. Exit code 0 iff all checked thresholds pass."""
    arg_parser = argparse.ArgumentParser(
        prog="replay_harness",
        description="Replay an ArenaOnAir fixture through the full pipeline "
                    "and score the transcript (QR6 drone metrics).")
    arg_parser.add_argument(
        "--fixture", metavar="PATH",
        default=str(_REPO_ROOT / "fixtures" / "matches" / "match_01.jsonl"),
        help="Fixture JSONL to replay (default: match_01.jsonl)")
    arg_parser.add_argument(
        "--verbose", action="store_true",
        help="Print each rendered utterance after the summary table")
    arg_parser.add_argument(
        "--json", metavar="OUT", default=None,
        help="Also write the machine-readable report to OUT as JSON")
    arg_parser.add_argument(
        "--determinism", action="store_true",
        help="Replay twice and assert identical utterance lists")

    args = arg_parser.parse_args(argv)

    try:
        report, passed = run_replay(args.fixture, determinism=args.determinism)
    except FileNotFoundError:
        print("ERROR: fixture not found: %s" % args.fixture, file=sys.stderr)
        return 2

    _print_table(report)

    if args.verbose:
        records = load_records(args.fixture)
        resolver = make_name_resolver()
        result = replay_fixture(records, name_resolver=resolver)
        print()
        for utt in result["utterances"]:
            print("[%-20s] %s" % (utt.kind, utt.text))

    if args.json:
        payload = dict(report)
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write("\n")
        print("JSON report written to %s" % args.json)

    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
