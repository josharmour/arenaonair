"""Regressions from the September 21 live dual-booth broadcast."""
from dataclasses import replace

import pytest

from arenaonair.app import ArenaOnAirApp
from arenaonair.config import Config
from arenaonair.differ import EventDiffer
from arenaonair.models import CardRef, DeliveryResult, Event, GameState, GreMessage, MatchMeta, TurnInfo, Utterance, ZoneView
from arenaonair.narrator import Narrator
from arenaonair.speech import SpeechPump, SpeechQueue
from arenaonair.state_builder import GameStateBuilder


def state(gid):
    return GameState(gid, None, {}, {}, {}, TurnInfo(2, 1, "main"),
                     MatchMeta("match", None), game_id=1, gre_state_id=gid)


def test_public_analyst_reply_survives_ticks_and_new_cast_during_anchor(tmp_path, monkeypatch):
    from test_llm_booth import Writer
    monkeypatch.setattr("arenaonair.llm_booth.JsonClient", lambda _: Writer())
    app = ArenaOnAirApp(Config(broadcast_mode="dual", narration_mode="llm", llm_coalesce=0, carddb_path=str(tmp_path / "none")), dry_run=True)
    app._match_id = "match"
    app.queue.set_active_match("match")
    first = Event("cast", 1, {"name": "Opt"}, 1., 2)
    second = Event("cast", 2, {"name": "Consider"}, 2., 2)
    s1, s2, s3 = state(1), state(2), state(3)
    app._invalidate_analysis(s1)
    heard = []
    class Speaker:
        def speak(self, utt):
            heard.append(utt)
            if len(heard) == 1:
                # GRE often advances several times while one sentence speaks.
                app._invalidate_analysis(s2)
                app._invalidate_analysis(s3)
                app._events_for = lambda *args: [second]
                app._consume_snapshot(s2, s3, [])
            return DeliveryResult(utt.uid, True)
    app.pump = SpeechPump(app.queue, Speaker(), on_delivery=app.llm.delivered, validate=app.llm.valid_for_delivery)
    try:
        app._handle_event(first, s1)
        app.llm.process(app.llm.take_work())
        app.pump.run_once()
        app.pump.run_once()
        assert [(u.role, u.voice) for u in heard] == [
            ("play_by_play", "am_adam"), ("color_analyst", "am_onyx")]
        assert heard[1].anchor_uid == heard[0].uid
        assert app.llm.pending  # new play remains available for the next request
    finally:
        app.stop()


def test_only_current_fact_replies_expire_on_state_change(tmp_path):
    app = ArenaOnAirApp(Config(carddb_path=str(tmp_path / "none")), dry_run=True)
    app._invalidate_analysis(state(1))
    public = Utterance("public", "match", "cast", "Public reaction", 2, 1., anchor_uid="anchor")
    private = replace(public, uid="private", requires_fresh_state=True)
    app.queue.enqueue(public)
    app.queue.enqueue(private)
    try:
        app._invalidate_analysis(state(2))
        assert app.queue.pending() == [public]
        app._invalidate_analysis(replace(state(3), game_id=2))
        assert not app.queue.pending()
    finally:
        app.stop()


def test_adjacent_reply_yields_to_urgent_boundary():
    q = SpeechQueue()
    q.set_active_match("m")
    q.note_anchor_result(DeliveryResult("a", True))
    reply = Utterance("r", "m", "cast", "Reaction", 2, 2., anchor_uid="a")
    ordinary = Utterance("o", "m", "cast", "Next play", 2, 1.)
    urgent = Utterance("end", "m", "game_end", "Game over", 3, 3.)
    for u in (ordinary, reply, urgent):
        q.enqueue(u)
    assert q.pop_best() == urgent
    assert q.pop_best() == reply
    assert q.pop_best() == ordinary


def test_pruning_keeps_delivered_reply_but_not_orphan_or_expired():
    q = SpeechQueue()
    q.set_active_match("m")
    q.now_fn = lambda: 100.
    q.note_anchor_result(DeliveryResult("delivered", True))
    valid = Utterance("v", "m", "cast", "Reaction", 2, 1., anchor_uid="delivered", expires_ts=110.)
    orphan = replace(valid, uid="o", anchor_uid="unspoken")
    expired = replace(valid, uid="x", expires_ts=99.)
    private = replace(valid, uid="p", requires_fresh_state=True)
    for u in (valid, orphan, expired, private):
        q.enqueue(u)
    assert q.prune_plays("m", preserve_public_replies=True) == 3
    assert q.pending() == [valid]


@pytest.mark.parametrize("back_type", ["GameObjectType_Card", "GameObjectType_MDFCBack"])
def test_linked_back_face_has_its_own_name(back_type):
    b = GameStateBuilder(lambda gid: "Malakir Rebirth // Malakir Mire" if gid == 73308 else None)
    raw = {"grpId": 73309, "othersideGrpId": 73308, "type": back_type,
           "cardTypes": ["CardType_Land"]}
    assert b._build_card_ref(20, raw).name == "Malakir Mire"
    assert b._build_card_ref(21, {**raw, "grpId": 73308, "othersideGrpId": 73309,
                                 "type": "GameObjectType_Card"}).name == "Malakir Rebirth"
    # Unknown standalone identity must never borrow a guessed adjacent ID.
    assert b._build_card_ref(22, {"grpId": 73309}).name is None
    assert b._build_card_ref(23, {"grpId": 73308}).name == "Malakir Rebirth // Malakir Mire"


def test_ability_stack_exit_is_not_announced_as_a_spell():
    b = GameStateBuilder()
    ref = b._build_card_ref(573, {"grpId": 148623, "type": "GameObjectType_Ability", "controllerSeatId": 2})
    prev = replace(state(1), zones={"stack:pub": ZoneView(1, "stack", None, (573,))}, objects={573: ref})
    cur = replace(state(2), zones={"pending:pub": ZoneView(2, "pending", None, (573,))}, objects={573: ref})
    assert not EventDiffer().detect_resolve_and_counter(prev, cur)
    # An unknown-name real spell is still an observable spell resolution.
    spell = replace(ref, object_type="GameObjectType_Card", card_types=("instant",))
    assert EventDiffer().detect_resolve_and_counter(replace(prev, objects={573: spell}), replace(cur, objects={573: spell}))


@pytest.mark.parametrize("kind", ["cast", "resolve"])
def test_fast_calls_keep_known_card_name(kind):
    n = Narrator()
    for i in range(20):
        utt = n.render(Event(kind, 1, {"name": "Consider"}, float(i), 2), state(i), tempo="fast")
        assert utt and "consider" in utt.text.lower()


def test_unknown_land_is_never_called_a_spell():
    n = Narrator()
    for i in range(3):
        utt = n.render(Event("land_drop", 1, {"name": None}, float(i), 1), state(i))
        if utt:
            assert "spell" not in utt.text.lower()


@pytest.mark.parametrize('stage,turn', [('GameStage_Start', None), ('GameStage_Start', 0),
    ('GameStage_Start', 1), ('GameStage_Play', 0), ('GameStage_Play', None),
    ('GameStage_GameOver', 5), (None, 5)])
def test_empty_hand_is_not_topdeck_without_live_play(stage, turn):
    from arenaonair.story import StoryModel
    snap = replace(state(1), game_stage=stage, turn_info=TurnInfo(turn, 1, None),
                   zones={'hand:1': ZoneView(1, 'hand', 1, ())})
    assert not [e for e in StoryModel().update(snap) if e.kind == 'topdeck_mode']


def test_start_to_play_preserves_stage_and_allows_real_topdeck():
    from arenaonair.story import StoryModel
    builder = GameStateBuilder()
    story = StoryModel()
    def feed(gid, stage=None, hand=(), turn=None, known=True, game=1):
        info = {'gameNumber': game}
        if stage is not None:
            info['stage'] = stage
        zone = {'zoneId': 1, 'type': 'ZoneType_Hand', 'ownerSeatId': 1}
        if known:
            zone['objectInstanceIds'] = list(hand)
        msg = GreMessage('gre.GameStateMessage', {
            'type': 'GameStateType_Full' if gid == 1 else 'GameStateType_Diff',
            'gameStateId': gid, 'prevGameStateId': gid - 1,
            'gameInfo': info, 'zones': [zone],
            'turnInfo': {'turnNumber': turn, 'activePlayer': 1}}, 0.)
        snap = builder.apply(None, msg)
        return snap, [e for e in story.update(snap) if e.kind == 'topdeck_mode']
    assert not feed(1, 'GameStage_Start', turn=0)[1]
    assert not feed(2, hand=(10, 11, 12), turn=0)[1]
    assert not feed(3, hand=(), turn=0)[1]  # mulligan returns the hand
    assert not feed(4, 'GameStage_Play', hand=(20, 21), turn=1)[1]
    snap, events = feed(5, hand=(), turn=2)  # diffs usually omit gameInfo.stage
    assert snap.game_stage == 'GameStage_Play'
    assert len(events) == 1
    assert not feed(6, hand=(), turn=2)[1]
    assert not feed(1, 'GameStage_Start', turn=0, game=2)[1]


def test_unreported_hand_membership_is_not_known_zero():
    from arenaonair.story import StoryModel
    snap = replace(state(1), game_stage='GameStage_Play',
                   zones={'hand:1': ZoneView(1, 'hand', 1, (), membership_known=False)})
    assert not [e for e in StoryModel().update(snap) if e.kind == 'topdeck_mode']
