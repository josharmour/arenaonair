"""Personas, hole-card gating, cross-match memory, recaps, overlay, doctor, pipelining."""
from dataclasses import replace
import json
import os
import stat
import sys
import urllib.request

import pytest

from arenaonair import config as config_mod
from arenaonair.config import Config, hole_cards_enabled, load, resolve_booth
from arenaonair.history import GameRecord, HistoryStore, deck_key
from arenaonair.llm_booth import GenerativeBooth, ModelError
from arenaonair.matchlog import MatchRecorder, latest_match_file
from arenaonair.models import (CardRef, DeliveryResult, Event, GameState, MatchMeta, PlayerView,
                               SeatKnowledge, TurnInfo, ZoneView)
from arenaonair.personas import PERSONAS, cap_excitement
from arenaonair.recap import build_script, highlights, llm_script, write_recap
from arenaonair.speech import SpeechQueue


def snap(**changes):
    base = GameState(1, None, {}, {}, {1: PlayerView(1, 20), 2: PlayerView(2, 20)},
                     TurnInfo(2, 1, 'main'), MatchMeta('m1', 'Standard', {1: 'Alice', 2: 'Bob'}),
                     local_seat=1, game_id=1, gre_state_id=1, chain_valid=True, player_deck=(5, 6, 7))
    return replace(base, **changes)


# -- personas & config -----------------------------------------------------------

def test_persona_selects_voice_preset_and_style(tmp_path):
    booth = resolve_booth(load(tmp_path / 'none.toml', persona='esports', broadcast_mode='dual'))
    assert booth['persona'] == 'esports'
    assert (booth['pbp_voice'], booth['analyst_voice']) == config_mod.BOOTH_PRESETS['esports_arena'][:2]
    # An explicit preset still wins over the persona's default voices.
    booth = resolve_booth(Config(persona='esports', booth_preset='mixed_duo', broadcast_mode='dual'))
    assert booth['pbp_voice'] == config_mod.BOOTH_PRESETS['mixed_duo'][0]
    assert load(tmp_path / 'none.toml', persona='nonsense').persona is None


def test_persona_caps_template_excitement():
    assert cap_excitement(PERSONAS['test_match'], 'electric') == 'tense'
    assert cap_excitement(PERSONAS['esports'], 'electric') == 'electric'
    assert cap_excitement(None, 'electric') == 'electric'


@pytest.mark.parametrize('cfg, expected', [
    (Config(), True),                                                   # playing, not streaming
    (Config(stream_enabled=True), False),                               # stream without delay
    (Config(stream_enabled=True, stream_delay_s=45), True),
    (Config(hole_cards='off'), False),
    (Config(stream_enabled=True, hole_cards='on'), True),
    (Config(log_player1='a.log', hole_cards='on'), False),              # shared logs: a player could hear
    (Config(relay_bind='0.0.0.0:8765'), False),                         # the opponent's hand
    (Config(log_player1='a.log', spectator=True), True),
    (Config(relay_bind='0.0.0.0:8765', spectator=True, stream_enabled=True), False),
])
def test_hole_card_gating(cfg, expected):
    assert hole_cards_enabled(cfg) is expected


def test_stream_section_in_toml(tmp_path):
    path = tmp_path / 'c.toml'
    path.write_text('[stream]\nenabled = true\ndelay_s = 60\noverlay_port = 8787\n')
    cfg = load(path)
    assert (cfg.stream_enabled, cfg.stream_delay_s, cfg.overlay_port) == (True, 60.0, 8787)


# -- history --------------------------------------------------------------------

def test_history_record_streak_and_intro(tmp_path):
    h = HistoryStore(tmp_path / 'h.sqlite')
    for i, result in enumerate(['loss', 'win', 'win', 'win']):
        h.record_game(GameRecord(f'old{i}', '1', 'Standard', 'Alice', 'Bob' if i < 2 else 'Carol',
                                 result, 8, deck_key([7, 6, 5]), ('Voja',), ended_at=100 + i))
    assert h.vs_opponent('Bob').games == 2 and h.vs_opponent('Bob').wins == 1
    assert h.streak() == ('win', 3)
    assert h.opponent_cards('Bob') == ['Voja']
    line = h.intro_line(match_id='new', local_name='Alice', opp_name='Bob', deck=(5, 6, 7))
    assert 'Bob' in line and 'two previous games' in line and 'Voja' in line
    line = h.intro_line(match_id='new', local_name='Alice', opp_name='Zed', deck=())
    assert line == 'Alice arrives on a three-game winning streak.'
    facts = h.facts(match_id='old0', local_name='Alice', opp_name='Bob', deck=(5, 6, 7))
    assert facts['history:opponent']['previous_games'] == 1      # current match excluded
    assert (facts['history:deck']['deck_wins'], facts['history:deck']['deck_losses']) == (3, 0)
    h.close()


def test_recorder_keeps_public_events_and_updates_history(tmp_path):
    h = HistoryStore(tmp_path / 'h.sqlite')
    saved = []
    rec = MatchRecorder(tmp_path, history=h, on_game_saved=lambda m, g: saved.append((m, g)))
    s = snap()
    rec.observe(Event('cast', 2, {'name': 'Voja', 'grp_id': 9, 'instance_id': 50}, 0, 2), s)
    rec.observe(Event('trap_armed', 1, {'name': 'Counterspell'}, 0, 2), s)
    s = replace(s, players={1: PlayerView(1, 20), 2: PlayerView(2, 12)}, turn_info=TurnInfo(5, 1, 'combat'))
    rec.observe(Event('life_change', 2, {'from': 20, 'to': 12, 'delta': -8}, 0, 2), s)
    rec.observe(Event('game_end', None, {'winning_team_id': 1}, 0, 3), s)
    match = saved[0][0]
    kinds = [e['kind'] for e in match['games'][0]['events']]
    assert kinds == ['cast', 'life_change', 'game_end']          # private detector dropped
    assert match['games'][0]['result'] == 'win' and match['games'][0]['opp_cards'] == ['Voja']
    assert h.vs_opponent('Bob').wins == 1
    path = latest_match_file(tmp_path)
    assert json.loads(path.read_text())['match_id'] == 'm1'
    if os.name != 'nt':
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    # Intro speaks once per match and only once the pairing is known.
    assert rec.history_intro(snap(match_meta=MatchMeta('m2', None, {1: 'Alice'}))) is None
    assert 'Bob' in rec.history_intro(snap(match_meta=MatchMeta('m2', None, {1: 'Alice', 2: 'Bob'})))
    assert rec.history_intro(snap(match_meta=MatchMeta('m2', None, {1: 'Alice', 2: 'Bob'}))) is None
    h.close()


# -- recaps -----------------------------------------------------------------------

MATCH = {'match_id': 'm1', 'local_seat': 1, 'names': {'1': 'Alice', '2': 'Bob'}, 'games': [{
    'game_id': '1', 'winner_seat': 1, 'result': 'win', 'turns': 7, 'final_life': {'1': 14, '2': 0},
    'events': [
        {'kind': 'life_change', 'seat': 2, 'turn': 3, 'data': {'from': 20, 'to': 18, 'delta': -2}},
        {'kind': 'counter', 'seat': 2, 'turn': 4, 'data': {'name': 'Voja', 'countered_by_name': 'Negate'}},
        {'kind': 'life_change', 'seat': 2, 'turn': 6, 'data': {'from': 18, 'to': 9, 'delta': -9}},
        {'kind': 'cast', 'seat': 1, 'turn': 7, 'data': {'name': 'Lightning Bolt'}},
        {'kind': 'game_end', 'seat': None, 'turn': 7, 'data': {'winning_team_id': 1}}]}]}


def test_deterministic_recap_uses_only_recorded_facts():
    g = highlights(MATCH)[0]
    assert g['swing'] == {'turn': 6, 'player': 'Bob', 'lost': 9, 'from': 18, 'to': 9}
    assert g['finisher']['card'] == 'Lightning Bolt'
    text = ' '.join(l['text'] for l in build_script(MATCH))
    for fact in ('Alice in 7 turns', 'turn 6', '18 down to 9', 'Negate', 'Lightning Bolt', 'Bob on 0'):
        assert fact in text
    assert {l['role'] for l in build_script(MATCH, dual=False)} == {'play_by_play'}


class RecapClient:
    def __init__(self, turns, accept=True):
        self.turns, self.accept = turns, accept
    def _request(self, system, context):
        return {'turns': self.turns}
    def verify(self, context, turns):
        return self.accept


GOOD = [{'role': 'play_by_play', 'text': 'Alice closes it out in 7 turns.', 'refs': ['game:1:result']},
        {'role': 'color_analyst', 'text': 'Turn 6 was the swing: Bob fell from 18 to 9.', 'refs': ['game:1:swing']},
        {'role': 'play_by_play', 'text': 'Lightning Bolt was the last word.', 'refs': ['game:1:finisher']}]


def test_llm_recap_is_validated():
    assert [l['text'] for l in llm_script(MATCH, RecapClient(GOOD))][0] == GOOD[0]['text']
    invented = [dict(GOOD[0], text='Alice wins in 5 turns.')] + GOOD[1:]
    with pytest.raises(ModelError, match='unsupported_number'):
        llm_script(MATCH, RecapClient(invented))
    with pytest.raises(ModelError, match='role_or_fields'):
        llm_script(MATCH, RecapClient([dict(GOOD[0], refs=['made:up'])] + GOOD[1:]))
    with pytest.raises(ModelError, match='factual_rejection'):
        llm_script(MATCH, RecapClient(GOOD, accept=False))


def test_write_recap_falls_back_to_deterministic(tmp_path):
    result = write_recap(MATCH, tmp_path, client=RecapClient([], accept=True))
    assert result['source'] == 'deterministic'
    assert 'Lightning Bolt' in open(result['markdown'], encoding='utf-8').read()


# -- overlay ----------------------------------------------------------------------

def test_overlay_serves_page_and_hides_hand_when_off_air():
    from arenaonair.overlay import OverlayServer
    server = OverlayServer(0)
    server.start()
    try:
        page = urllib.request.urlopen(server.url, timeout=5).read().decode()
        assert 'EventSource' in page
        hand = ZoneView(9, 'ZoneType_Hand', 1, (70,))
        s = snap(zones={'hand:1': hand}, objects={70: CardRef(70, 1, 'Negate', 'Instant', ('instant',))},
                 seat_knowledge={1: SeatKnowledge(1, hand_visible=True, hand_fresh_asof=1.0)})
        server.update(s, hole_cards=False)
        state = json.loads(urllib.request.urlopen(server.url + 'state', timeout=5).read())
        assert state['hand'] == [] and state['players'][0] == {'seat': 1, 'name': 'Alice', 'life': 20}
        server.update(s, hole_cards=True)
        assert server.state()['hand'] == ['Negate']
    finally:
        server.close()


# -- doctor / setup ---------------------------------------------------------------

def test_detailed_logs_marker(tmp_path):
    from arenaonair.doctor import detailed_logs_status
    log = tmp_path / 'Player.log'
    log.write_bytes(b'boot\nDETAILED LOGS: DISABLED\n')
    assert detailed_logs_status(log) is False
    log.write_bytes(b'DETAILED LOGS: DISABLED\nrestart\nDETAILED LOGS: ENABLED\n')
    assert detailed_logs_status(log) is True
    log.write_bytes(b'nothing yet\n')
    assert detailed_logs_status(log) is None
    assert detailed_logs_status(tmp_path / 'missing.log') is None


def test_doctor_reports_missing_log_with_a_fix(tmp_path):
    from arenaonair.doctor import FAIL, run_checks
    checks = {c.name: c for c in run_checks(Config(log_path=str(tmp_path / 'nope.log')))}
    assert checks['Arena log'].status == FAIL and 'Arena' in checks['Arena log'].fix


def test_setup_writes_config_and_private_key(tmp_path, monkeypatch, capsys):
    from arenaonair import doctor
    from arenaonair.doctor import setup_main
    monkeypatch.setattr(doctor, 'resolve_log_path', lambda cfg: tmp_path / 'Player.log')
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('USERPROFILE', str(tmp_path))
    answers = iter(['esports', 'y', 'custom', 'https://llm.example/v1', 'some-model', 'y', '8787', '0'])
    target = tmp_path / 'cfg' / 'config.toml'
    setup_main(['--config', str(target)], input_fn=lambda _: next(answers), getpass_fn=lambda _: 'sekret')
    cfg = load(target)
    assert (cfg.persona, cfg.broadcast_mode, cfg.llm_base_url, cfg.overlay_port) == (
        'esports', 'dual', 'https://llm.example/v1', 8787)
    assert 'sekret' not in target.read_text()
    key = tmp_path / '.config' / 'arenaonair' / 'llm.key'
    assert key.read_text().strip() == 'sekret'
    if os.name != 'nt':
        assert stat.S_IMODE(key.stat().st_mode) == 0o600
    assert hole_cards_enabled(cfg) is False          # streaming with no delay
    assert 'Hole cards stay off air' in capsys.readouterr().out


def test_subcommand_dispatch(monkeypatch):
    from arenaonair import app, doctor
    monkeypatch.setattr(doctor, 'doctor_main', lambda argv: 7)
    assert app.main(['doctor']) == 7


# -- generative booth: airtime, hole cards, history, pipelining ------------------

class Writer:
    def __init__(self, turns):
        self.turns = turns
        self.contexts = []
    def generate(self, context):
        self.contexts.append(context)
        e = context['new_events'][0]
        return {'turns': [dict(t, refs=[e]) for t in self.turns]}
    def verify(self, context, turns):
        return True


PBP_Q = {'role': 'play_by_play', 'text': 'Opt is on the stack.'}
ANALYST = {'role': 'color_analyst', 'text': 'A cast, outcome still unknown.'}


def booth(turns, **cfg):
    config = Config(broadcast_mode='dual', llm_coalesce=0, **cfg)
    q = SpeechQueue()
    q.set_active_match('m1')
    q.now_fn = lambda: 100.0
    b = GenerativeBooth(config, q, resolve_booth(config), client=Writer(turns), now=lambda: 100.0)
    return b, q


def cast(iid, sal=2):
    return Event('cast', 2, {'name': 'Opt', 'grp_id': 1, 'instance_id': iid}, 0, sal)


def step(b, event, s=None):
    b.submit(event, s or snap())
    work = b.take_work()
    b.process(work)
    return work


def clear(b, q):
    q.cancel_uids({u.uid for u in q.pending()}, 'test_reset')
    b.tracked.clear()


def test_analyst_airtime_budget_per_turn():
    b, q = booth([PBP_Q, ANALYST], commentary_focus='calls')  # one analyst line per turn
    step(b, cast(1))
    assert [u.role for u in q.pending()] == ['play_by_play', 'color_analyst']
    clear(b, q)
    step(b, cast(2))                                     # same turn: analyst over budget
    assert [u.role for u in q.pending()] == ['play_by_play']
    assert b.counts['airtime_budget'] == 1
    clear(b, q)
    step(b, cast(3, sal=3))                              # urgent plays are exempt
    assert [u.role for u in q.pending()] == ['play_by_play', 'color_analyst']
    clear(b, q)
    step(b, cast(4), snap(turn_info=TurnInfo(3, 2, 'main')))  # new turn, fresh budget
    assert [u.role for u in q.pending()] == ['play_by_play', 'color_analyst']


def test_hole_cards_off_removes_private_facts_and_history_is_merged():
    hand = ZoneView(9, 'ZoneType_Hand', 1, (70,))
    s = snap(zones={'hand:1': hand}, objects={70: CardRef(70, 1, 'Negate', 'Instant', ('instant',))},
             seat_knowledge={1: SeatKnowledge(1, hand_visible=True, hand_fresh_asof=100.0)})
    b, _ = booth([PBP_Q])
    b.history_facts = lambda snap: {'history:opponent': {'opponent': 'Bob', 'previous_games': 2}}
    b.observe(s)
    assert 'private:hand:1' in b.facts and 'history:opponent' in b.facts
    assert b.facts['state:local_seat']['seat'] == 1
    b.hole_cards = False
    b.observe(s)
    assert 'private:hand:1' not in b.facts and 'history:opponent' in b.facts


def test_pipelined_exchange_rejected_when_inflight_line_fails():
    b, q = booth([PBP_Q])
    step(b, cast(1))
    first = q.pending()[0]
    b.submit(cast(2), snap())
    work = b.take_work()
    assert work.inflight == (first.uid,) and work.context['in_flight'][0]['text'] == PBP_Q['text']
    b.delivered(DeliveryResult(first.uid, False, 'failed'), first)   # audience never heard it
    b.process(work)
    assert b.counts['inflight_not_heard'] == 1
    assert [u.uid for u in q.pending()] == [first.uid]


# -- match opener names -----------------------------------------------------------

def test_match_opener_never_voices_placeholder_names():
    from arenaonair.narrator import Narrator
    n = Narrator()
    unknown = GameState(1, None, {}, {}, {}, TurnInfo(None, None, None), MatchMeta('m', 'Brawl_Ladder', {}))
    for i in range(30):
        for kind in ('match_start', 'game_start'):
            utt = n.render(Event(kind, None, {}, i, 3), unknown)
            assert utt is None or 'our player' not in utt.text
    known = replace(unknown, match_meta=MatchMeta('m2', 'Brawl_Ladder', {1: 'Creole', 2: 'armour'}), local_seat=2)
    texts = [n.render(Event('match_start', None, {}, i, 3), known).text for i in range(12)]
    named = [t for t in texts if 'Creole' in t]
    assert named and all(t.index('armour') < t.index('Creole') for t in named)   # local player first


# -- template analyst -------------------------------------------------------------

def test_speakable_rules_reads_symbols_and_drops_reminders():
    from arenaonair.analyst import speakable_rules
    assert speakable_rules('{T}: Add {G}.') == 'Tap it: Add green.'
    assert speakable_rules('Flying (This creature can’t be blocked except by fliers.)\nWhen it enters, draw a card.') == \
        'Flying. When it enters, draw a card.'
    assert speakable_rules('Pay {2}{U/B}: scry 1.') == 'Pay two blue or black: scry 1.'
    assert speakable_rules(' '.join(['word'] * 50)) is None
    assert speakable_rules('') is None


def test_template_analyst_explains_opponent_cards_once_per_turn():
    from arenaonair.analyst import TemplateAnalyst
    from arenaonair.carddb import CardInfo
    from arenaonair.models import Utterance
    cards = {1: CardInfo('Opt', 'Instant', '{U}', ('instant',), oracle_text='Scry 1. Draw a card.'),
             2: CardInfo('Grizzly Bears', 'Creature — Bear', '{1}{G}', ('creature',))}
    a = TemplateAnalyst(card_lookup=cards.get, clock=lambda: 0.0)
    anchor = Utterance('u1', 'm1', 'cast', 'Bob casts Opt.', 2, 0.0, voice='am_adam')
    opp_cast = Event('cast', 2, {'name': 'Opt', 'grp_id': 1}, 0, 2)
    line = a.companion(opp_cast, snap(), anchor, voice='am_onyx')
    assert 'Scry one' not in line.text and 'Scry 1. Draw a card.' in line.text
    assert (line.role, line.voice, line.anchor_uid) == ('color_analyst', 'am_onyx', 'u1')
    # Budget: one analyst line per turn; a new turn allows another card.
    bears = Event('cast', 2, {'name': 'Grizzly Bears', 'grp_id': 2}, 0, 2)
    assert a.companion(bears, snap(), anchor) is None
    later = snap(turn_info=TurnInfo(3, 2, 'main'))
    assert 'two-mana creature' in a.companion(bears, later, anchor).text
    # Never the local player's own card, never the same card twice a game.
    turn4 = snap(turn_info=TurnInfo(4, 1, 'main'))
    assert a.companion(Event('cast', 1, {'name': 'Opt', 'grp_id': 1}, 0, 2), turn4, anchor) is None
    assert a.companion(opp_cast, snap(turn_info=TurnInfo(5, 2, 'main')), anchor) is None


def test_template_analyst_calls_big_life_swings_only():
    from arenaonair.analyst import TemplateAnalyst
    from arenaonair.models import Utterance
    a = TemplateAnalyst()
    anchor = Utterance('u1', 'm1', 'life_change', 'Bob drops to 12.', 2, 0.0)
    s = snap(players={1: PlayerView(1, 18), 2: PlayerView(2, 12)})
    assert a.companion(Event('life_change', 2, {'delta': -2, 'to': 12}, 0, 2), s, anchor) is None
    line = a.companion(Event('life_change', 2, {'delta': -8, 'to': 12}, 0, 2), s, anchor)
    assert line.text == "That's eight life gone for Bob in one hit, and Bob now trails 12 to 18."


def test_dual_is_the_default_booth():
    assert Config().broadcast_mode == 'dual'


def test_own_hand_changes_do_not_silence_unrelated_exchanges():
    hand = lambda *ids: ZoneView(9, 'ZoneType_Hand', 1, ids)
    objs = {70: CardRef(70, 1, 'Negate', 'Instant', ('instant',)), 71: CardRef(71, 2, 'Shock', 'Instant', ('instant',))}
    k = {1: SeatKnowledge(1, hand_visible=True, hand_fresh_asof=100.0)}
    before = snap(zones={'hand:1': hand(70, 71)}, objects=objs, seat_knowledge=k)
    after = snap(zones={'hand:1': hand(70)}, objects=objs, seat_knowledge=k)   # Shock left the hand
    for text, spoken in (('Bob casts Opt.', True), ('Alice is still holding Shock.', False)):
        b, q = booth([{'role': 'play_by_play', 'text': text}])
        b.submit(cast(1), before)
        work = b.take_work()
        b.observe(after)                      # hand changes while the model writes
        b.process(work)
        assert bool(q.pending()) is spoken, text


class AuditingWriter(Writer):
    """Writer whose auditor passes only the first ``good`` lines."""
    def __init__(self, turns, good):
        super().__init__(turns)
        self.good = good
    def verify(self, context, turns):
        self.line_results = [i < self.good for i in range(len(turns))]
        return all(self.line_results)


def test_supported_opening_lines_survive_a_rejected_later_line():
    two = [PBP_Q, ANALYST]
    b, q = booth(two)
    b.client = AuditingWriter(two, good=1)
    step(b, cast(1))
    assert [u.text for u in q.pending()] == [PBP_Q['text']] and b.counts['salvaged_lines'] == 1
    b, q = booth(two)
    b.client = AuditingWriter(two, good=0)                    # first line fails: nothing to keep
    step(b, cast(1))
    assert not q.pending() and b.counts['factual_rejection'] == 1


def test_locally_invalid_second_line_is_dropped_not_the_exchange():
    invented = {'role': 'color_analyst', 'text': 'That makes 7 cards drawn.'}   # number not in evidence
    b, q = booth([PBP_Q, invented])
    step(b, cast(1))
    assert [u.text for u in q.pending()] == [PBP_Q['text']]


def test_explained_cards_are_tracked_from_delivered_analyst_lines():
    b, q = booth([PBP_Q])
    b.submit(cast(1), snap())
    work = b.take_work()
    assert work.context['explained_this_game'] == []
    b.tracked['x'] = {'epoch': b.epoch, 'role': 'color_analyst', 'id': 'heard:x', 'text': 'Opt looks ahead.',
                      'refs': ['card:1'], 'facts': {'card:1': {'name': 'Opt'}}, 'play_ids': [], 'deps': {}, 'created': 100.0}
    b.delivered(DeliveryResult('x', True), None)
    assert b.explained == {'Opt'}


def test_attack_facts_carry_attacker_power_and_player():
    b, _ = booth([PBP_Q])
    objs = {5: CardRef(5, 1, 'Pup', 'Creature', ('creature',), power=1, controller_seat=2),
            6: CardRef(6, 2, 'Warmaster', 'Creature', ('creature',), power=2, controller_seat=2)}
    b.submit(Event('attack_declared', 2, {'attackers': [{'instance_id': 5, 'name': 'Pup'},
                                                        {'instance_id': 6, 'name': 'Warmaster'}]}, 0, 2),
             snap(objects=objs, turn_info=TurnInfo(3, 2, 'combat')))
    record = b.pending[-1]
    assert record['player'] == 'Bob' and record['data']['total_power'] == 3
    assert [r['power'] for r in record['data']['attackers']] == [1, 2]


def test_setup_preserves_explicit_arena_log(tmp_path, monkeypatch):
    from arenaonair import doctor
    path = tmp_path / 'Player.log'
    target = tmp_path / 'config.toml'
    target.write_text('log_path = ' + json.dumps(str(path)) + '\n', encoding='utf-8')
    monkeypatch.setattr(doctor, 'run_checks', lambda cfg: [])
    answers = iter(['classic', 'y', 'later', 'n'])
    assert doctor.setup_main(['--config', str(target)], input_fn=lambda _: next(answers)) == 0
    assert load(target).log_path == str(path)
