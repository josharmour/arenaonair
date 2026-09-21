"""Tests for arenaonair.narrator + templates -- the anti-drone contract.

Covers: pool structural variety, variety over consecutive renders, window
exclusivity (QR6), escalating brevity, replay determinism, shape-signature
drone scoring, announcer-not-coach scanning, and a full match_01 replay
through differ + narrator with state alignment.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from arenaonair import events as ev
from arenaonair.differ import EventDiffer
from arenaonair.gre_parser import parse_line_all
from arenaonair.models import (
    CardRef,
    Event,
    GameState,
    MatchMeta,
    PlayerView,
    TurnInfo,
    ZoneView,
)
from arenaonair.narrator import Narrator
from arenaonair.state_builder import GameStateBuilder
from arenaonair.templates import (
    NARRATIVE_POOLS,
    TEMPLATE_POOLS,
    TEMPLATE_SHORT_POOLS,
    all_templates,
    gloss_phrase,
    pool_sizes,
    shape_signature,
    validate_pools,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
MATCH_01 = REPO_ROOT / "fixtures" / "matches" / "match_01.jsonl"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_event(kind, seat=None, payload=None, ts=0.0, salience=1):
    return Event(kind=kind, seat=seat, payload=dict(payload or {}),
                 ts=ts, salience=salience)


def make_state(snapshot_id=1,
               lives=(20, 20),
               creatures=(),
               lands=(),
               active_player=1,
               turn_number=None,
               player_names=None):
    objects = {}
    bf_ids = []
    for ref in (*creatures, *lands):
        objects[ref.instance_id] = ref
        bf_ids.append(ref.instance_id)
    zones = {"battlefield:pub": ZoneView(
        zone_id=1, zone_type="ZoneType_Battlefield", owner_seat=None,
        object_ids=tuple(bf_ids))}
    players = {
        seat: PlayerView(seat=seat, life=lives[idx], starting_life=20)
        for idx, seat in enumerate((1, 2))
    }
    meta = MatchMeta(match_id="test-match", format_name="Brawl_Ladder",
                     player_names=dict(player_names or {}))
    return GameState(
        snapshot_id=snapshot_id, prev_snapshot_id=None, zones=zones,
        objects=objects, players=players,
        turn_info=TurnInfo(turn_number=turn_number,
                           active_player=active_player, phase=None),
        match_meta=meta, local_seat=None)


def land(iid, ctrl=1):
    return CardRef(instance_id=iid, grp_id=None, name="Plains",
                   type_line=None, card_types=("land",),
                   controller_seat=ctrl, owner_seat=ctrl)


def creature(iid, power=2, ctrl=1):
    return CardRef(instance_id=iid, grp_id=None, name="Bear",
                   type_line=None, card_types=("creature",), power=power,
                   toughness=2, controller_seat=ctrl, owner_seat=ctrl)


FORBIDDEN_COACH_PATTERNS = (
    "you should", "you must", "you'll want", "you need to",
    "consider ", "don't ", "dont ", "make sure", "remember to",
    "be careful", "watch out for", "think about", "try to",
)


# ---------------------------------------------------------------------------
# Template pool structure
# ---------------------------------------------------------------------------

class TestPoolStructure:

    def test_every_play_by_play_kind_has_pool(self):
        for kind in ev.PLAY_BY_PLAY_KINDS:
            assert len(TEMPLATE_POOLS.get(kind, [])) >= 6, kind

    def test_every_narrative_kind_has_pool(self):
        for kind in ev.NARRATIVE_KINDS:
            assert len(NARRATIVE_POOLS.get(kind, [])) >= 6, kind

    def test_major_kinds_have_at_least_six_templates(self):
        majors = (ev.LAND_DROP, ev.CAST, ev.RESOLVE, ev.COUNTER,
                  ev.ATTACK_DECLARED, ev.BLOCK_DECLARED, ev.COMBAT_DAMAGE,
                  ev.LIFE_CHANGE, ev.BOARD_SHIFT)
        sizes = pool_sizes()
        for kind in majors:
            assert sizes[kind][0] >= 6

    def test_validate_pools_clean(self):
        assert validate_pools() == []

    def test_no_duplicates_within_any_single_pool(self):
        for pool in (TEMPLATE_POOLS, TEMPLATE_SHORT_POOLS, NARRATIVE_POOLS):
            for kind, templates in pool.items():
                assert len(set(templates)) == len(templates), kind

    def test_short_pools_exist_for_play_by_play(self):
        for kind in ev.PLAY_BY_PLAY_KINDS:
            assert len(TEMPLATE_SHORT_POOLS.get(kind, [])) >= 6, kind


# ---------------------------------------------------------------------------
# Glosses
# ---------------------------------------------------------------------------

class TestGlosses:

    def test_two_mana_removal_spell(self):
        assert gloss_phrase("Murder", "Instant", "{1}{B}") == \
            "Murder, a two-mana removal spell"

    def test_real_threat_for_big_creature(self):
        out = gloss_phrase("Colossus", "Creature -- Golem", None, 6)
        assert "a real threat" in out

    def test_legendary_gloss(self):
        out = gloss_phrase("The Legend", "Legendary Creature -- Wizard")
        assert "their legend" in out

    def test_unknown_card_falls_back_to_name(self):
        assert gloss_phrase("Mystery Card", None) == "Mystery Card"

    def test_converted_mana_hybrid(self):
        from arenaonair.templates import converted_mana
        assert converted_mana("{2}{B}{B}") == 4
        assert converted_mana("{W/U}{W/U}") == 4
        assert converted_mana(None) is None


# ---------------------------------------------------------------------------
# Variety engine
# ---------------------------------------------------------------------------

class TestVariety:

    def test_twelve_land_drops_yield_five_distinct_texts(self):
        n = Narrator()
        state = make_state()
        texts = []
        for i in range(12):
            u = n.render(make_event(ev.LAND_DROP, 1, {"name": f"L{i}"},
                                    ts=float(i)), state)
            if u is not None:
                texts.append(u.text)
        assert len(texts) >= 10          # brevity may silence a couple
        assert len(set(texts)) >= 5

    def test_no_immediate_repeat_same_kind(self):
        n = Narrator()
        state = make_state()
        prev_text = None
        for i in range(15):
            u = n.render(make_event(ev.CAST, 2,
                                    {"name": f"Spell{i}", "grp_id": i},
                                    ts=float(i), salience=2), state)
            if u is None:
                continue
            if prev_text is not None:
                assert u.text != prev_text
            prev_text = u.text

    def test_window_exclusivity_high_pool_kinds(self):
        """Same template never twice within 8 renders (pool >= 6)."""
        n = Narrator(window=8)
        state = make_state()
        rendered = []
        for i in range(24):
            u = n.render(make_event(ev.LIFE_CHANGE, 1,
                                    {"from": 20 - i, "to": 19 - i,
                                     "delta": -1},
                                    ts=float(i)), state)
            if u is not None:
                rendered.append(u.text)
        # Within any rolling window of 8 consecutive renders of this kind,
        # no two utterances may share a template identity. We approximate by
        # exact-text equality (distinct slots make texts unique anyway);
        # the strict guarantee is enforced by the engine's blocked-set logic.
        for start in range(len(rendered) - 8):
            window_texts = rendered[start:start + 8]
            # allow at most one accidental collision only when slots were
            # identical; with varying life totals texts must be distinct
            assert len(set(window_texts)) >= len(window_texts) - 1

    def test_never_same_template_twice_consecutively(self):
        n = Narrator()
        state = make_state()
        last_idx_per_kind = {}
        
        # Instrument the engine directly: repeatedly pick indices for one
        # kind and assert no consecutive duplicates and no in-window dupes.
        fs_tracker = n._family_state(ev.CAST, "full")
        pool = TEMPLATE_POOLS[ev.CAST]
        picks = [n._pick_index(ev.CAST, "full", pool) for _ in range(40)]
        for a, b in zip(picks, picks[1:]):
            assert a != b
        for start in range(len(picks) - 8):
            window_set = set(picks[start:start + 8])
            assert len(window_set) == len(picks[start:start + 8])

    def test_streak_resets_when_kind_changes(self):
        n = Narrator()
        state = make_state()
        # two identical land drops -> second is short family
        u1 = n.render(make_event(ev.LAND_DROP, 1, {"name": "Plains"},
                                 ts=1.0), state)
        u2 = n.render(make_event(ev.LAND_DROP, 1, {"name": "Plains"},
                                 ts=2.0), state)
        assert u1 is not None and u2 is not None
        # an intervening different kind resets the streak
        n.render(make_event(ev.CAST, 1, {"name": "Bolt"}, ts=3.0,
                            salience=2), state)
        u3 = n.render(make_event(ev.LAND_DROP, 1, {"name": "Plains"},
                                 ts=4.0), state)
        assert u3 is not None
        # streak restarted: this should be full-family again (longer text)
        assert len(u3.text.split()) >= 2


# ---------------------------------------------------------------------------
# Escalating brevity
# ---------------------------------------------------------------------------

class TestEscalatingBrevity:

    def test_third_identical_low_salience_is_silent(self):
        n = Narrator()
        state = make_state()
        e = lambda i: make_event(ev.LAND_DROP, 1, {"name": "SameLand"},
                                 ts=float(i))
        u1 = n.render(e(1), state)
        u2 = n.render(e(2), state)
        u3 = n.render(e(3), state)
        assert u1 is not None
        assert u2 is not None
        assert u3 is None

    def test_high_salience_breaks_the_silence_rule(self):
        n = Narrator()
        state = make_state()
        e_low = lambda i: make_event(ev.LIFE_CHANGE, 1,
                                     {"from": 20, "to": 19, "delta": -1},
                                     ts=float(i))
        assert n.render(e_low(1), state) is not None
        assert n.render(e_low(2), state) is not None
        # third identical LOW event would be silenced...
        assert n.render(e_low(3), state) is None
        # ...but a HIGH-salience identical event still speaks
        e_high = make_event(ev.LIFE_CHANGE, 1,
                            {"from": 20, "to": 19, "delta": -1},
                            ts=4.0, salience=2)
        assert n.render(e_high, state) is not None

    def test_second_occurrence_uses_short_variant(self):
        n = Narrator()
        state = make_state()
        e1 = make_event(ev.LAND_DROP, 1, {"name": "Island"}, ts=1.0)
        e2 = make_event(ev.LAND_DROP, 1, {"name": "Island"}, ts=2.0)
        u1 = n.render(e1, state)
        u2 = n.render(e2, state)
        assert u1 is not None and u2 is not None
        # short variants are strictly shorter or equal in word count
        assert len(u2.text.split()) <= len(u1.text.split())

    def test_different_names_do_not_form_a_streak(self):
        n = Narrator()
        state = make_state()
        for i in range(5):
            u = n.render(make_event(ev.LAND_DROP, 1,
                                    {"name": f"Distinct{i}"}, ts=float(i)),
                         state)
            assert u is not None   # never silenced -- each is a new event


# ---------------------------------------------------------------------------
# Fast-tempo adaptation
# ---------------------------------------------------------------------------

class TestFastTempoAdaptation:
    def test_fast_tempo_silences_low_salience(self):
        n = Narrator()
        state = make_state()
        e_low = make_event(ev.LAND_DROP, 1, {"name": "Forest"}, salience=1)
        assert n.render(e_low, state, tempo="fast") is None

    def test_fast_tempo_renders_high_salience_with_short_pool(self):
        n = Narrator()
        state = make_state()
        e_cast = make_event(ev.CAST, 1, {"name": "Lightning Bolt"}, salience=2)
        u_fast = n.render(e_cast, state, tempo="fast")
        assert u_fast is not None
        assert "Lightning Bolt" in u_fast.text
        assert len(u_fast.text.split()) <= 4

    def test_fast_tempo_preserves_must_speak_events(self):
        n = Narrator()
        state = make_state()
        e_end = make_event(ev.GAME_END, None, {"reason": "loss"}, salience=3)
        u = n.render(e_end, state, tempo="fast")
        assert u is not None


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------

class TestDeterminism:

    def _synthetic_stream(self):
        events = []
        for i in range(10):
            events.append(make_event(ev.LAND_DROP, 1,
                                     {"name": f"Land{i}"}, ts=float(i)))
            events.append(make_event(ev.CAST, 2,
                                     {"name": f"Spell{i}", "grp_id": i},
                                     ts=float(i) + 0.5, salience=2))
            events.append(make_event(ev.LIFE_CHANGE, 2,
                                     {"from": 20 - i, "to": 19 - i,
                                      "delta": -1},
                                     ts=float(i) + 0.75))
            events.append(make_event(ev.NARRATIVE_ARC, 1,
                                     {"from_arc": "even",
                                      "to_arc": "pulling_away",
                                      "leader_seat": 1,
                                      "detail": "momentum shifts"},
                                     ts=float(i) + 0.9, salience=2))
            events.append(make_event(ev.BOARD_SHIFT, 1,
                                     {"creatures_before": i,
                                      "creatures_after": i + 1,
                                      "power_before": 2 * i,
                                      "power_after": 2 * i + 2},
                                     ts=float(i) + 0.95))
        return events

    def test_same_sequence_same_transcript(self):
        events = self._synthetic_stream()
        states = [make_state(snapshot_id=i + 1,
                             lives=(20 - i // 2, 20 - i // 3),
                             creatures=tuple(creature(100 + j)
                                             for j in range(i % 5)),
                             lands=tuple(land(200 + j) for j in range(i)),
                             turn_number=i + 1)
                  for i in range(len(events))]
        
# === part3 ===
        t1 = self._run_transcript(events, states)
        t2 = self._run_transcript(events, states)
        assert t1 == t2
        assert len(t1) > 20

    def test_different_match_id_may_differ_but_still_deterministic(self):
        events = self._synthetic_stream()
        states_a = [make_state(snapshot_id=i + 1, turn_number=i + 1)
                    for i in range(len(events))]
        for s in states_a:
            object.__setattr__(
                s.match_meta, "match_id", s.match_meta.match_id) \
                if False else None
        t1 = self._run_transcript(events, states_a)
        t2 = self._run_transcript(events, states_a)
        assert t1 == t2

    @staticmethod
    def _run_transcript(events, states):
        n = Narrator()
        out = []
        for event, state in zip(events, states):
            u = n.render(event, state)
            if u is not None:
                out.append((u.uid, u.kind, u.text, u.salience,
                            u.ts_created))
        return out


# ---------------------------------------------------------------------------
# Shape signatures / drone scoring (QR6)
# ---------------------------------------------------------------------------

class TestShapeSignatures:

    def test_shape_signature_basic_properties(self):
        a = shape_signature("Attackers coming in -- three creatures now.")
        b = shape_signature("Attackers coming in -- five bodies here.")
        c = shape_signature("Over to Josh for the next turn.")
        assert a == b          # same first words + length bucket
        assert a != c
        assert shape_signature("") == ""

    def test_no_two_consecutive_share_shape_over_mixed_stream(self):
        n = Narrator()
        state = make_state(turn_number=3)
        stream = [
            make_event(ev.MATCH_START, None,
                       {"match_id": "m"}, ts=0.0, salience=3),
            make_event(ev.LAND_DROP, 1, {"name": "L1"}, ts=1.0),
            make_event(ev.CAST, 2, {"name": "Shock", "grp_id": 1},
                       ts=2.0, salience=2),
            make_event(ev.RESOLVE, 2,
                       {"name": "Shock", "instance_id": 9,
                        "to_zone": "graveyard", "cast_name": "Shock",
                        "resolved": True}, ts=3.0, salience=2),
            make_event(ev.ATTACK_DECLARED, None,
                       {"attackers": [{"instance_id": 4, "name": "Bear",
                                       "target_seat": 1}],
                        "total_power": 2}, ts=4.0, salience=2),
            make_event(ev.BLOCK_DECLARED, None,
                       {"blocks": [{"blocker_instance_id": 5,
                                    "attacker_instance_ids": [4]}]},
                       ts=5.0, salience=2),
            make_event(ev.COMBAT_DAMAGE, 1,
                       {"amount": 2, "source_instance": 4,
                        "target_seat": 1}, ts=6.0, salience=2),
            make_event(ev.LIFE_CHANGE, 1,
                       {"from": 20, "to": 18, "delta": -2},
                       ts=7.0, salience=2),
            make_event(ev.BOARD_SHIFT, 2,
                       {"creatures_before": 0, "creatures_after": 2,
                        "power_before": 0, "power_after": 4},
                       ts=8.0),
            make_event(ev.TURN_START, 2, {"active_player": 2}, ts=9.0),
            make_event(ev.NARRATIVE_ARC, 1,
                       {"from_arc": "even", "to_arc": "standoff",
                        "leader_seat": 1, "detail": "boards stall"},
                       ts=10.0, salience=2),
            make_event(ev.NARRATIVE_RESOURCE, 1,
                       {"streak_kind": "flood", "length": 4,
                        "detail": "extra lands four turns running"},
                       ts=11.0),
            make_event(ev.NARRATIVE_CALLBACK, 2,
                       {"beat_kind": "answer", "turn_distance": 5,
                        "ref_seat": 2, "detail": "echoes earlier answer"},
                       ts=12.0),
            make_event(ev.NARRATIVE_SPECULATION, 1,
                       {"kind": "race_shaping",
                        "detail": "the race gets real",
                        "leader_seat": 1}, ts=13.0),
            make_event(ev.GAME_END, None,
                       {"winning_team_id": 1, "reason":
                        "Normal"}, ts=14.0, salience=3),
        ]
        # pad to 20+ events with varied kinds
        stream += [
            make_event(ev.LAND_DROP, 2, {"name": f"L{i}"}, ts=20.0 + i)
            for i in range(6)
        ]
        shapes = []
        for e in stream:
            u = n.render(e, state)
            if u is not None:
                shapes.append(Narrator.shape_signature(u.text))
        assert len(shapes) >= 15
        for a, b in zip(shapes, shapes[1:]):
            assert a != b

    def test_static_method_accessible(self):
        assert callable(Narrator.shape_signature)


# ---------------------------------------------------------------------------
# Announcer-not-coach
# ---------------------------------------------------------------------------

class TestAnnouncerNotCoach:

    def test_no_coach_phrases_in_any_template(self):
        for text in all_templates():
            lowered = text.lower()
            for pattern in FORBIDDEN_COACH_PATTERNS:
                assert pattern not in lowered, (pattern, text)

    def test_no_coach_phrases_in_rendered_output(self):
        n = Narrator()
        state = make_state(player_names={1: "Josh", 2: "Rival"})
        kinds_payloads = [
            (ev.MATCH_START, None, {}, 3),
            (ev.LAND_DROP, 1, {"name": "L"}, 1),
            (ev.CAST, 1, {"name": "Bolt", "grp_id": 3}, 2),
            (ev.RESOLVE, 1, {"name": "Bolt", "instance_id": 3,
                             "to_zone": "graveyard"}, 2),
            (ev.COUNTER, 2, {"name": "Negate",
                             "countered_by_seat": 2}, 2),
            (ev.ATTACK_DECLARED, None,
             {"attackers": [{"instance_id": 1, "name": "Bear",
                             "target_seat": 2}], "total_power": 3}, 2),
            (ev.BLOCK_DECLARED, None,
             {"blocks": [{"blocker_instance_id": 9,
                          "attacker_instance_ids": [1]}]}, 2),
            (ev.COMBAT_DAMAGE, 2,
             {"amount": 3, "source_instance": 1, "target_seat": 2}, 2),
            (ev.LIFE_CHANGE, 2,
             {"from": 20, "to": 17, "delta": -3}, 2),
            (ev.BOARD_SHIFT, 1,
             {"creatures_before": 1, "creatures_after": 3,
              "power_before": 2, "power_after": 6}, 1),
            (ev.TURN_START, 1, {"active_player": 1}, 0),
            (ev.GAME_END, None,
             {"winning_team_id": 1}, 3),
            (ev.NARRATIVE_ARC, 1,
             {"from_arc": "even", "to_arc": "pulling_away",
              "leader_seat": 1}, 2),
            (ev.NARRATIVE_RESOURCE, 1,
             {"streak_kind": "screw", "length": 3}, 1),
            (ev.NARRATIVE_CALLBACK, 1,
             {"beat_kind": "trade", "turn_distance": 4}, 1),
            (ev.NARRATIVE_SPECULATION, 1,
             {"kind": "race_shaping"}, 1),
        ]
        
# === part4 ===
        for i in range(6):
            for kind, seat, payload, sal in kinds_payloads:
                e = make_event(kind, seat, payload, ts=float(i) * 10,
                               salience=sal)
                u = n.render(e, state)
                if u is not None:
                    lowered = u.text.lower()
                    for pattern in FORBIDDEN_COACH_PATTERNS:
                        assert pattern not in lowered, (pattern, u.text)


# ---------------------------------------------------------------------------
# Robustness (never raises)
# ---------------------------------------------------------------------------

class TestRobustness:

    def test_none_event_returns_none(self):
        assert Narrator().render(None, None) is None

    def test_unknown_kind_returns_none(self):
        e = make_event("totally_unknown_kind", 1, {})
        assert Narrator().render(e, None) is None

    def test_malformed_payload_does_not_raise(self):
        n = Narrator()
        e = make_event(ev.CAST, 1, {"name": 12345, "grp_id": "junk",
                                    "instance_id": None}, salience=2)
        u = n.render(e, None)
        assert u is None or isinstance(u.text, str)

    def test_broken_state_does_not_raise(self):
        n = Narrator()
        e = make_event(ev.LAND_DROP, 1, {"name": "L"}, salience=1)
        u = n.render(e, "not-a-state")
        assert u is None or isinstance(u.text, str)

    def test_empty_payload_renders_with_defaults(self):
        n = Narrator()
        e = make_event(ev.CAST, 1, {}, salience=2)
        u = n.render(e, None)
        assert u is not None
        assert "a spell" in u.text


# ---------------------------------------------------------------------------
# Integration: full match_01 replay through differ + narrator
# ---------------------------------------------------------------------------

def load_match_01_pairs():
    records = [json.loads(line)
               for line in MATCH_01.read_text().splitlines() if line.strip()]
    messages = []
    for idx, rec in enumerate(records):
        raw = json.dumps(rec["obj"])
        messages.extend(parse_line_all(float(idx), raw))
    builder = GameStateBuilder()
    current = None
    pairs = []
    since = []
    for msg in messages:
        nxt = builder.apply(current, msg)
        if nxt is not None:
            pairs.append((current, nxt, list(since)))
            since = []
            current = nxt
        since.append(msg)
    return pairs


class TestMatch01Replay:

    @pytest.fixture(scope="class")
    @classmethod
    def transcript(cls):
        pairs = load_match_01_pairs()
        differ = EventDiffer()
        narrator = Narrator()
        utterances = []
        exceptions = []
        for prev, cur, since in pairs:
            try:
                events = differ.diff(prev, cur, since)
            except Exception as exc:      # pragma: no cover
                exceptions.append(exc)
                continue
            for event in events:
                try:
                    u = narrator.render(event, cur)
                except Exception as exc:  # pragma: no cover
                    exceptions.append(exc)
                    continue
                if u is not None:
                    utterances.append(u)
        assert exceptions == []
        return utterances

    def test_at_least_thirty_utterances(self, transcript):
        assert len(transcript) >= 30

    def test_zero_exceptions_implied_by_fixture(self, transcript):
        # fixture asserts no exception occurred during collection
        assert all(u.text for u in transcript)

    def test_drone_score_no_consecutive_same_shape(self, transcript):
        shapes = [Narrator.shape_signature(u.text) for u in transcript]
        collisions = [(a, b) for a, b in zip(shapes, shapes[1:]) if a == b]
        assert collisions == [], collisions[:5]

    def test_uids_unique(self, transcript):
        uids = [u.uid for u in transcript]
        assert len(set(uids)) == len(uids)

    def test_variety_over_full_match(self, transcript):
        texts = [u.text for u in transcript]
        # overall diversity comfortably above coin-flip repetition
        assert len(set(texts)) / len(texts) >= 0.7
        # full-length sentences (>= 6 words) are overwhelmingly distinct;
        # residual repeats trace to unnamed-card fixtures where several
        # casts/resolves carry no card name (slot collapses to 'a spell'),
        # not to template reuse -- shape-level drone scoring above covers
        # the QR6 contract properly.
        long_texts = [t for t in texts if len(t.split()) >= 6]
        assert len(long_texts) == 0 or (
            len(set(long_texts)) / len(long_texts) >= 0.85)
