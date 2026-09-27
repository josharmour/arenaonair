"""Game-flow commentary: the story model's read, game-read moments and the commentary focus."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from arenaonair import events as ev
from arenaonair.app import ArenaOnAirApp, _build_arg_parser
from arenaonair.config import Config, load, resolve_booth
from arenaonair.llm_booth import GenerativeBooth, ModelError, validate_output
from arenaonair.models import CardRef, Event, GameState, MatchMeta, PlayerView, TurnInfo, ZoneView
from arenaonair.speech import SpeechQueue
from arenaonair.story import StoryModel


# -- the story model's read ----------------------------------------------------

def _card(iid, ctrl, types=("creature",), power=None):
    return CardRef(iid, None, None, None, tuple(types), power, power, controller_seat=ctrl, owner_seat=ctrl)


def _state(sid, lives, active, cards=(), hands=None, stage=None):
    zones = {"bf": ZoneView(1, "ZoneType_Battlefield", None, tuple(c.instance_id for c in cards))}
    for seat, n in (hands or {}).items():
        zones[f"hand:{seat}"] = ZoneView(10 + seat, "ZoneType_Hand", seat, tuple(seat * 1000 + i for i in range(n)))
    return GameState(sid, None, zones, {c.instance_id: c for c in cards},
                     {s: PlayerView(s, life) for s, life in zip((1, 2), lives)}, TurnInfo(None, active, None),
                     MatchMeta("m", None, {1: "Alice", 2: "Bob"}), game_stage=stage)


def test_read_reports_the_game_from_public_state():
    model = StoryModel()
    lands = (_card(1, 1, ("land",)), _card(2, 2, ("land",)), _card(3, 2, ("land",)))
    bear, giant = _card(10, 1, power=3), _card(11, 1, power=4)
    states = [
        _state(1, (20, 20), 1, lands[:1]),
        _state(2, (20, 20), 2, lands[:2]),
        _state(3, (20, 20), 1, lands[:2] + (bear,)),
        _state(4, (20, 17), 2, lands + (bear,)),
        _state(5, (20, 14), 1, lands + (bear,)),
        _state(6, (20, 11), 2, lands + (bear, giant), hands={1: 4, 2: 6}),
    ]
    for state in states:
        model.update(state)
    read = model.read(states[-1])
    assert read["shape"] == "pulling_away" and read["ahead"] == {"seat": 1, "name": "Alice"}
    assert "at least 6" in read["shape_meaning"] and read["recent_turns"] == 4
    alice, bob = read["players"]
    assert (alice["life"], alice["creatures"], alice["creature_power"], alice["cards_in_hand"]) == (20, 2, 7, 4)
    assert alice["turns_without_new_land"] == 2  # Alice's last two turns added no land
    assert (bob["life"], bob["life_change_recent"], bob["turns_without_new_land"]) == (11, -9, 0)
    assert "race" not in read and alice["power_at_least_opponent_life"] is False
    assert model.read(replace(states[-1], game_stage="GameStage_Start")) is None
    assert StoryModel().read(states[0]) is None  # nothing folded in yet


@pytest.mark.parametrize("lives, recent, power, shape, who", [
    ((1, 8), (-22, -13), (14, 15), "race", None),              # both boards match the other's life
    ((12, 13), (-6, -7), (2, 3), "race", None),                # both lost at least 5 lately
    ((23, 8), (-2, -13), (21, 5), "pulling_away", ("ahead", 1)),
    ((9, 18), (-1, -7), (6, 2), "comeback_brewing", ("fighting_back", 1)),
    ((20, 19), (0, -1), (6, 5), "standoff", None),
    ((20, 19), (0, -1), (6, 0), "even", None),
])
def test_read_shapes_come_from_the_numbers_they_report(lives, recent, power, shape, who):
    from arenaonair.story import _read_shape
    players = [{"seat": s, "name": n, "life": life, "life_change_recent": r, "creature_power": p,
                "power_at_least_opponent_life": 0 < p >= other}
               for s, n, life, r, p, other in zip((1, 2), ("A", "B"), lives, recent, power, lives[::-1])]
    out = _read_shape(players)
    assert out["shape"] == shape
    assert (who[0] in out and out[who[0]]["seat"] == who[1]) if who else not ({"ahead", "fighting_back"} & set(out))


# -- the generative booth ------------------------------------------------------

def _read(shape="even", lives=(20, 20), power=(0, 0), stuck=(0, 0), **extra):
    players = [{"seat": s, "name": n, "life": life, "creatures": 1 if p else 0, "creature_power": p,
                "power_at_least_opponent_life": p >= other, "lands": 3, "turns_without_new_land": k}
               for s, n, life, p, other, k in zip((1, 2), ("Alice", "Bob"), lives, power, lives[::-1], stuck)]
    return {"shape": shape, "players": players, "shape_meaning": "meaning", "meaning": "computed", **extra}


def _snap(turn=3):
    return GameState(1, None, {}, {}, {1: PlayerView(1, 20), 2: PlayerView(2, 20)}, TurnInfo(turn, 1, "main"),
                     MatchMeta("m", None, {1: "Alice", 2: "Bob"}), local_seat=1, game_id=1, chain_valid=True)


class Writer:
    def __init__(self, line=None, role="color_analyst"):
        self.contexts, self.line, self.role = [], line, role

    def generate(self, context):
        self.contexts.append(context)
        event = context["new_events"][0]
        return {"turns": [{"role": self.role, "text": self.line or "A quiet start.", "refs": [event]}]}

    def verify(self, context, turns):
        return True


def _booth(focus="balanced", mode="dual", writer=None, **cfg):
    config = Config(broadcast_mode=mode, llm_coalesce=0, commentary_focus=focus, **cfg)
    queue = SpeechQueue()
    queue.set_active_match("m")
    queue.now_fn = lambda: 100.0
    booth = GenerativeBooth(config, queue, resolve_booth(config), client=writer or Writer(), now=lambda: 100.0)
    return booth, queue


def _turns(booth, reads):
    """Start one turn per read; return the turn numbers that opened a game-read moment."""
    moments = []
    for turn, read in enumerate(reads, 1):
        booth.update_read(read)
        before = booth.counts["game_read"]
        booth.turn_started(_snap(turn))
        if booth.counts["game_read"] > before:
            moments.append(turn)
    return moments


def test_game_read_moments_follow_the_focus():
    steady = [_read()] * 6
    assert _turns(_booth("analysis")[0], steady) == [3, 4, 5, 6]
    assert _turns(_booth("balanced")[0], steady) == [3, 5]
    assert _turns(_booth("calls")[0], steady) == []  # an even game with nothing new stays unsaid
    shift = [_read()] * 3 + [_read("pulling_away", (20, 11), ahead={"seat": 1, "name": "Alice"})] * 3
    assert _turns(_booth("balanced")[0], shift) == [3, 4, 6]  # a change can't wait for the round
    assert _turns(_booth("calls")[0], shift) == [4]


def test_game_read_moment_is_a_citable_event_and_live_fact():
    writer = Writer("Alice leads 20 to 11 on life, with 7 power on board to Bob's 0.")
    booth, queue = _booth("analysis", writer=writer)
    read = _read("pulling_away", (20, 11), (7, 0), ahead={"seat": 1, "name": "Alice"})
    _turns(booth, [read] * 3)
    work = booth.take_work()
    context = work.context
    event = context["facts"][context["new_events"][0]]
    assert event["kind"] == "game_read" and event["data"]["shape"] == "pulling_away"
    assert context["facts"]["state:game_read"]["players"][0]["creature_power"] == 7
    assert context["focus"].startswith("Mostly analysis")
    booth.process(work)
    [line] = queue.pending()
    assert line.role == "color_analyst" and line.text.startswith("Alice leads")


def test_solo_booth_reads_the_game_on_play_by_play():
    writer = Writer("Bob is stuck on lands for 2 turns.", role="play_by_play")
    booth, queue = _booth("analysis", mode="solo", writer=writer)
    _turns(booth, [_read(stuck=(0, 2))] * 3)
    booth.process(booth.take_work())
    assert [u.role for u in queue.pending()] == ["play_by_play"]


def test_routine_plays_become_context_under_balanced_focus():
    land = Event(ev.LAND_DROP, 1, {"name": "Island", "grp_id": 5, "instance_id": 20}, 0, ev.SALIENCE_LOW)
    spell = Event(ev.CAST, 2, {"name": "Opt", "grp_id": 1, "instance_id": 21}, 0, ev.SALIENCE_LOW)
    booth, _ = _booth("balanced")
    booth.submit(land, _snap())
    assert not booth.pending and booth.counts["quiet_event"] == 1
    booth.submit(spell, _snap())
    context = booth.take_work().context
    kinds = {context["facts"][ref]["kind"] for ref in context["facts"] if ref.startswith("event:")}
    assert kinds == {"land_drop", "cast"} and len(context["new_events"]) == 1
    calls, _ = _booth("calls")
    calls.submit(land, _snap())
    assert len(calls.pending) == 1


def test_analyst_airtime_follows_the_focus():
    booth, _ = _booth("analysis")
    booth.observe(_snap())
    assert booth._analyst_budget_left() == 3
    booth.config.commentary_focus = "calls"
    assert booth._analyst_budget_left() == 1
    assert _booth("analysis", analyst_lines_per_turn=0)[0]._analyst_budget_left() == 0


@pytest.mark.parametrize("text", [
    "Bob should really attack here.", "Alice needs to find a blocker.", "That block was a mistake.",
    "Holding back is the right play."])
def test_coaching_never_reaches_the_air(text):
    context = {"facts": {"event:1:1": {"kind": "game_read", "data": _read()}}, "new_events": ["event:1:1"],
               "heard": [], "earlier_points": [], "booth": {}}
    with pytest.raises(ModelError, match="coaching"):
        validate_output({"turns": [{"role": "color_analyst", "text": text, "refs": ["event:1:1"]}]}, context)
    ok = "Should those attackers connect, Bob drops low."
    validate_output({"turns": [{"role": "color_analyst", "text": ok, "refs": ["event:1:1"]}]}, context)


@pytest.mark.parametrize("text", [
    "Game read says even, but Cirvin has four power.", "The read has shifted: that's officially pulling away.",
    "Read's refreshed for turn seven.", "Bob is pulling_away on creature_power."])
def test_the_booths_own_notes_stay_off_air(text):
    context = {"facts": {"event:1:1": {"kind": "game_read", "data": _read()}}, "new_events": ["event:1:1"],
               "heard": [], "earlier_points": [], "booth": {}}
    with pytest.raises(ModelError, match="internal_jargon"):
        validate_output({"turns": [{"role": "color_analyst", "text": text, "refs": ["event:1:1"]}]}, context)
    fine = "Alice is pulling away, with nothing across the table."
    validate_output({"turns": [{"role": "color_analyst", "text": fine, "refs": ["event:1:1"]}]}, context)


def test_coaching_is_held_back_not_spoken():
    booth, queue = _booth("analysis", writer=Writer("Bob has to block with everything."))
    _turns(booth, [_read()] * 3)
    booth.process(booth.take_work())
    assert not queue.pending() and booth.state == "coaching"


# -- configuration, app and window --------------------------------------------

def test_focus_setting_loads_and_flag_parses(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[broadcast]\nfocus = "analysis"\n', encoding="utf-8")
    assert load(path).commentary_focus == "analysis"
    path.write_text('[broadcast]\nfocus = "shouting"\n', encoding="utf-8")
    assert load(path).commentary_focus == "balanced"
    assert Config().commentary_focus == "balanced" and Config().analyst_lines_per_turn is None
    assert _build_arg_parser().parse_args(["--focus", "calls"]).focus == "calls"


def test_app_switches_focus_live_and_saves(tmp_path):
    app = ArenaOnAirApp(Config(carddb_path=str(tmp_path / "none")), dry_run=True)
    app.config_path = tmp_path / "config.toml"
    try:
        assert app.status()["focus"] == "balanced"
        assert app.set_commentary_focus("analysis") is True
        assert app.status()["focus"] == "analysis" and load(app.config_path).commentary_focus == "analysis"
        assert app.set_commentary_focus("analysis") is False
    finally:
        app.stop()


def test_scripted_booth_drops_routine_calls_under_analysis(tmp_path):
    app = ArenaOnAirApp(Config(carddb_path=str(tmp_path / "none"), commentary_focus="analysis"), dry_run=True)
    rendered = []
    app.narrator.render = lambda event, snap, **kw: rendered.append(event.kind)
    try:
        for kind, salience in ((ev.LAND_DROP, 1), (ev.RESOLVE, 1), (ev.CAST, 1), (ev.NARRATIVE_ARC, 2)):
            app._handle_event(Event(kind, 1, {"name": "Opt"}, 0, salience), _snap())
        assert rendered == [ev.CAST, ev.NARRATIVE_ARC]
    finally:
        app.stop()


def test_turn_start_reaches_the_booth_below_the_verbosity_gate(tmp_path):
    app = ArenaOnAirApp(Config(carddb_path=str(tmp_path / "none")), dry_run=True)
    seen = []
    app.llm = SimpleNamespace(turn_started=seen.append, submit=lambda *a: seen.append("submitted"),
                              valid_for_delivery=lambda u: True)
    try:
        app._handle_event(Event(ev.TURN_START, 1, {}, 0, ev.SALIENCE_FILLER), _snap())
        assert len(seen) == 1 and seen[0] != "submitted"
    finally:
        app.llm = None
        app.stop()


def test_window_focus_control(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    from arenaonair.diagnostics import BugReportLogs
    from arenaonair.ui import BroadcastWindow
    QApplication.instance() or QApplication([])
    state, calls = {"focus": "balanced"}, []

    def set_focus(focus):
        calls.append(focus)
        state["focus"] = focus
        return True
    app = SimpleNamespace(booth={"pbp_voice": "am_adam", "analyst_voice": "am_onyx"}, queue=SpeechQueue(),
                          speaker=None, set_commentary_focus=set_focus,
                          status=lambda: {"state": "watching", "broadcast_mode": "dual", "queued": 0,
                                          "focus": state["focus"]})
    window = BroadcastWindow(app, BugReportLogs())
    try:
        assert window.focus_buttons["balanced"].isChecked() and window.mode_buttons["dual"].isChecked()
        window.focus_buttons["analysis"].click()
        assert calls == ["analysis"] and window.focus_buttons["analysis"].isChecked()
        assert window.mode_buttons["dual"].isChecked()  # separate choices, not one radio group
        assert window.voice_note.text().startswith("Mostly analysis")
    finally:
        window.timer.stop()
        window.close()
