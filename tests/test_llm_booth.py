"""Generative booth contracts. Fake writers test lifecycle, not model quality."""
from dataclasses import replace
import json
import threading
import time

import pytest

from arenaonair.config import Config, load, resolve_booth
from arenaonair.llm_booth import GenerativeBooth, JsonClient, ModelError, state_facts, validate_output
from arenaonair.models import CardRef, DeliveryResult, Event, GameState, MatchMeta, PlayerView, SeatKnowledge, TurnInfo, ZoneView
from arenaonair.speech import FakeSpeaker, SpeechPump, SpeechQueue


class Clock:
    value = 100.0
    def __call__(self):
        return self.value


class Writer:
    def __init__(self):
        self.contexts = []
        self.accept = True
        self.output = None
        self.error = None

    def generate(self, context):
        self.contexts.append(context)
        if self.error:
            raise self.error
        if self.output is not None:
            return self.output(context) if callable(self.output) else self.output
        event = context['new_events'][0]
        return {'turns': [
            {'role': 'play_by_play', 'text': 'Opt goes onto the stack.', 'refs': [event]},
            {'role': 'color_analyst', 'text': 'We have a cast, with its outcome still unknown.', 'refs': [event]}]}

    def verify(self, context, turns):
        return self.accept


def snapshot(**changes):
    base = GameState(1, None, {}, {}, {1: PlayerView(1, 20), 2: PlayerView(2, 20)},
                     TurnInfo(2, 1, 'main'), MatchMeta('m', None, {1: 'Alice', 2: 'Bob'}),
                     local_seat=1, game_id=1, gre_state_id=1, chain_valid=True)
    return replace(base, **changes)


def cast(iid=10, kind='cast'):
    return Event(kind, 1, {'name': 'Opt', 'grp_id': 1, 'instance_id': iid}, 0, 2)


def setup_booth(**kw):
    cfg = Config(broadcast_mode='dual', llm_coalesce=0, **kw)
    q, clock, writer = SpeechQueue(), Clock(), Writer()
    q.set_active_match('m')
    q.now_fn = clock
    b = GenerativeBooth(cfg, q, resolve_booth(cfg), client=writer, now=clock)
    pump = SpeechPump(q, FakeSpeaker(), on_delivery=b.delivered, validate=b.valid_for_delivery)
    return b, q, clock, writer, pump


def run(b, event=None, snap=None):
    b.submit(event or cast(), snap or snapshot())
    work = b.take_work()
    assert work is not None
    b.process(work)
    return work


def test_single_source_shared_history_only_after_success():
    b, q, clock, w, pump = setup_booth()
    run(b)
    assert not b.heard
    assert [x['status'] for x in b.records] == ['queued', 'queued']
    first, second = q.pending()
    assert second.anchor_uid == first.uid
    assert (first.voice, second.voice) == ('am_adam', 'am_onyx')
    pump.run_once()
    pump.run_once()
    assert len(b.heard) == 2
    assert [x['status'] for x in b.records] == ['spoken', 'spoken']
    work = run(b, cast(11))
    assert [x['role'] for x in work.context['heard']] == ['play_by_play', 'color_analyst']
    assert 'facts' in work.context['heard'][0]
    assert b.status()['event_to_speech_s'] == 0


def test_analyst_can_initiate_without_equal_airtime():
    b, q, _, w, pump = setup_booth()
    w.output = lambda c: {'turns': [{'role': 'color_analyst', 'text': 'Opt is on the stack.', 'refs': c['new_events']}]}
    run(b)
    assert q.pending()[0].role == 'color_analyst'
    assert q.pending()[0].anchor_uid is None
    pump.run_once()
    assert b.heard[0]['role'] == 'color_analyst'


@pytest.mark.parametrize('output', [{'turns': []}, {'turns': [{'role': 'bad', 'text': 'Oops', 'refs': []}]},
    {'turns': [{'role': 'play_by_play', 'text': '<think>Oops</think>', 'refs': ['unknown']}]},
    {'turns': [{'role': 'play_by_play', 'text': 'Bob is at 999 life.', 'refs': ['event:1:1']}]}])
def test_silence_invalid_roles_reasoning_unknown_refs_and_numbers(output):
    b, q, _, w, _ = setup_booth()
    w.output = output
    run(b)
    assert not q.pending()
    assert not b.heard


def test_semantic_verification_rejects_valid_json_and_valid_references():
    b, q, _, w, _ = setup_booth()
    w.output = lambda c: {'turns': [{'role': 'play_by_play', 'text': 'Opt destroys their creature.', 'refs': c['new_events']}]}
    w.accept = False
    run(b)
    assert b.counts['factual_rejection'] == 1
    assert not q.pending()


def test_timeout_has_backoff_and_no_backlog_or_fallback():
    b, q, clock, w, _ = setup_booth()
    w.error = ModelError('timeout_or_connection')
    run(b)
    for i in range(30):
        b.submit(cast(i), snapshot())
    assert not b.pending and not q.pending()
    assert b.counts['backoff_skipped'] == 30
    clock.value += 3
    w.error = None
    run(b, cast(31), snapshot(snapshot_id=2))
    assert len(q.pending()) == 2


def test_stale_response_rejected_after_network_delay():
    b, q, clock, w, _ = setup_booth(llm_max_age=1)
    b.submit(cast(), snapshot())
    work = b.take_work()
    clock.value += 2
    b.process(work)
    assert b.counts['stale_response'] == 1
    assert not q.pending()


def test_irrelevant_ticks_do_not_expire_exchange_but_relevant_facts_do():
    b, q, _, w, pump = setup_booth()
    w.output = lambda c: {'turns': [{'role': 'play_by_play', 'text': 'Alice is at 20 life.', 'refs': c['new_events'] + ['state:player:1']}]}
    run(b)
    b.observe(snapshot(snapshot_id=2, gre_state_id=2))
    assert len(q.pending()) == 1
    b.observe(snapshot(players={1: PlayerView(1, 19), 2: PlayerView(2, 20)}))
    assert not q.pending()
    assert not b.heard


def private_snapshot(clock, enriched=True):
    refs = {10: CardRef(10, 1, 'Opt', 'Instant', ('instant',)),
            20: CardRef(20, 2, 'Murder', 'Instant', ('instant',))}
    return snapshot(objects=refs, zones={'hand:1': ZoneView(1, 'hand', 1, (10,)),
        'hand:2': ZoneView(2, 'hand', 2, (20,))}, seat_knowledge={
        1: SeatKnowledge(1, True, clock()),
        2: SeatKnowledge(2, enriched, clock() if enriched else None)})


def test_enrichment_and_source_loss_and_private_freshness():
    b, q, clock, w, _ = setup_booth(commentary_focus='calls')  # one analyst line per turn
    run(b, snap=private_snapshot(clock))
    context = w.contexts[0]
    assert 'private:hand:1' in context['facts'] and 'private:hand:2' in context['facts']
    b.observe(private_snapshot(clock, False))
    assert not q.pending()
    run(b, cast(11), private_snapshot(clock, False))
    assert 'private:hand:2' not in w.contexts[-1]['facts']
    clock.value += 6
    b.take_work()  # periodic housekeeping expires private facts without a GRE tick
    assert not any(k.startswith('private:') for k in b.facts)
    # The listener's own hand going stale doesn't void lines that never cited it.
    assert [u.text for u in q.pending()] == ['Opt goes onto the stack.']


def test_hidden_names_never_leak_from_objects_or_unknown_membership():
    _, _, clock, _, _ = setup_booth()
    snap = private_snapshot(clock, False)
    facts = state_facts(snap, None, clock())
    assert 'Murder' not in json.dumps(facts)
    facts = state_facts(replace(snap, chain_valid=False), None, clock())
    assert 'Opt' not in json.dumps(facts)
    badzone = replace(snap.zones['hand:1'], membership_known=False)
    facts = state_facts(replace(snap, zones={'hand:1': badzone}), None, clock())
    assert 'Opt' not in json.dumps(facts)


@pytest.mark.parametrize('change', ['game', 'match', 'close'])
def test_scope_changes_discard_inflight_generation(change):
    b, q, _, _, _ = setup_booth()
    b.submit(cast(), snapshot())
    work = b.take_work()
    if change == 'close':
        b.stop()
    else:
        b.observe(snapshot(game_id=2) if change == 'game' else snapshot(match_meta=MatchMeta('other', None)))
    b.process(work)
    assert not q.pending()
    assert b.counts['stale_response'] == 1


def test_distinct_card_copies_and_recasts_have_distinct_play_ids():
    b, q, _, _, pump = setup_booth()
    b.submit(cast(10), snapshot())
    b.submit(cast(10, 'resolve'), snapshot())
    assert len(b.pending) == 1
    assert b.pending[0]['earlier_stage'] == 'cast'
    assert b.pending[0]['kind'] == 'stack_departure'
    b.submit(cast(11), snapshot())
    assert len({e['play_id'] for e in b.pending}) == 2
    old = b.plays[10]
    b.submit(cast(10), snapshot(snapshot_id=3))
    assert b.plays[10] != old


def test_spoken_cast_suppresses_redundant_departure_not_another_copy():
    b, q, _, _, pump = setup_booth()
    run(b)
    pump.run_once(); pump.run_once()
    b.submit(cast(10, 'resolve'), snapshot())
    assert not b.pending
    b.submit(cast(11), snapshot())
    assert len(b.pending) == 1


@pytest.mark.parametrize('reason', ['failed', 'cancelled', 'expired', 'pruned'])
def test_orphan_chain_never_spoken_or_entered_in_memory(reason):
    b, q, clock, w, pump = setup_booth()
    w.output = lambda c: {'turns': [
        {'role': 'play_by_play', 'text': 'Opt goes on the stack.', 'refs': c['new_events']},
        {'role': 'color_analyst', 'text': 'Its outcome is unknown.', 'refs': c['new_events']},
        {'role': 'play_by_play', 'text': 'We wait for the outcome.', 'refs': c['new_events']}]}
    run(b)
    anchor = q.pending()[0]
    if reason in ('failed', 'cancelled'):
        pump.speaker = FakeSpeaker(fail_uids=[anchor.uid])
        if reason == 'cancelled':
            pump.speaker.speak = lambda u: DeliveryResult(u.uid, False, 'cancelled')
        pump.run_once()
    elif reason == 'expired':
        clock.value += 25
    else:
        q.cancel_uids({anchor.uid}, 'pruned')
    b.take_work()
    assert pump.run_once() is None
    assert not q.pending()
    assert not b.heard
    assert all(r['status'] not in ('queued', 'spoken') for r in b.records)


def test_spoken_points_are_bounded_and_reset_on_game_boundary():
    b, q, _, _, pump = setup_booth()
    for i in range(26):
        # One exchange per game turn keeps the analyst inside its airtime budget.
        run(b, cast(i), snapshot(turn_info=TurnInfo(2 + i, 1, 'main')))
        pump.run_once(); pump.run_once()
    assert len(b.heard) == 16 and len(b.earlier) == 24
    assert all(x['facts'] and x['text'] for x in b.earlier)
    b.observe(snapshot(game_id=2))
    assert not b.heard and not b.earlier


def test_queue_and_pending_work_bounded():
    b, q, _, _, _ = setup_booth(commentary_focus='calls')  # one analyst line per turn
    for i in range(50):
        b.submit(cast(i), snapshot())
    assert len(b.pending) == 16
    work = b.take_work()
    b.process(work)
    b.submit(cast(100), snapshot())
    # Pipelined: the next exchange is written while two lines are still queued,
    # and is anchored behind them.
    work = b.take_work()
    assert work is not None and len(work.inflight) == 2
    assert [t['text'] for t in work.context['in_flight']] == [u.text for u in q.pending()]
    b.process(work)
    assert len(q.pending()) == 3
    assert q.pending()[-1].anchor_uid == work.inflight[-1]
    b.submit(cast(101), snapshot())
    assert b.take_work() is None  # MAX_INFLIGHT_LINES still unspoken


def test_async_ingestion_and_close_prevent_late_reply():
    b, q, _, w, _ = setup_booth()
    entered, release = threading.Event(), threading.Event()
    original = w.generate
    def generate(c):
        entered.set()
        release.wait(2)
        return original(c)
    w.generate = generate
    b.start()
    b.submit(cast(), snapshot())
    assert entered.wait(1)
    start = time.monotonic()
    b.submit(cast(20), snapshot())
    assert time.monotonic() - start < .1
    release.set()
    b.stop()
    assert not q.pending() and not b.thread.is_alive()


def test_config_nested_llm_and_environment_precedence(tmp_path, monkeypatch):
    path = tmp_path / 'config.toml'
    path.write_text('[llm]\nbase_url="https://example.test/v1"\nprofile="generic"\ntimeout=3\n')
    monkeypatch.setenv('ARENAONAIR_MODEL', 'other')
    cfg = load(path, llm_model='explicit')
    assert cfg.llm_base_url == 'https://example.test/v1'
    assert cfg.llm_profile == 'generic' and cfg.llm_timeout == 3
    assert cfg.llm_model == 'explicit'


def test_model_client_never_salvages_reasoning(monkeypatch):
    monkeypatch.setenv('ARENAONAIR_API_KEY', 'test-secret')
    response = {'choices': [{'finish_reason': 'stop', 'message': {
        'content': None, 'reasoning_content': '{"turns":[]}'}}]}
    class Response:
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def read(self, _): return json.dumps(response).encode()
    class Opener:
        def open(self, req, timeout):
            body = json.loads(req.data)
            assert body['chat_template_kwargs'] == {'thinking': True, 'reasoning_effort': 'low'}
            assert req.headers['User-agent'].startswith('ArenaOnAir/')
            return Response()
    monkeypatch.setattr('urllib.request.build_opener', lambda *_: Opener())
    client = JsonClient(Config(llm_base_url='http://127.0.0.1/v1'))
    with pytest.raises(ModelError, match='missing_final_content'):
        client.generate({'facts': {}, 'heard': [], 'earlier_points': [], 'allowed_roles': ['play_by_play']})
    response['choices'][0]['message']['content'] = '{"turns":[]}'
    assert client.generate({'facts': {}, 'heard': [], 'earlier_points': [], 'allowed_roles': ['play_by_play']}) == {'turns': []}


def test_unanswered_exchange_is_not_queued():
    b, q, _, w, _ = setup_booth()
    w.output = lambda c: {'turns': [{'role': 'play_by_play', 'text': 'What does Opt do?', 'refs': c['new_events']}]}
    run(b)
    assert not q.pending()
    assert b.counts['unanswered_question'] == 1


def test_offered_attacker_is_not_observed_attack_and_inferred_counter_is_not_fact():
    b, q, _, w, _ = setup_booth()
    e = Event('attack_declared', 1, {'attackers': [{'instance_id': 10, 'name': 'Opt'}],
                                  'confirmed_instance_ids': []}, 0, 2)
    b.submit(e, snapshot())
    assert not b.pending
    e = Event('counter', 1, {'instance_id': 10, 'name': 'Opt', 'countered_by_name': 'Counterspell',
                           'counter_evidence': 'stack_inference'}, 0, 2)
    b.submit(e, snapshot())
    work = b.take_work()
    fact = work.context['facts'][work.context['new_events'][0]]
    assert fact['kind'] == 'stack_departure'
    assert 'countered_by_name' not in fact['data']


def test_factual_verifier_receives_only_each_lines_cited_evidence(monkeypatch):
    monkeypatch.setenv('ARENAONAIR_API_KEY', 'test-secret')
    client = JsonClient(Config(llm_base_url='http://localhost/v1'))
    seen = []
    def request(system, context):
        seen.append(context)
        return {'checks': [{'index': 0, 'unsupported_claim': '', 'supported': True}]}
    client._request = request
    context = {'facts': {'event:1': {'kind': 'cast'}, 'unused': {'hidden': 'nope'}}, 'heard': [], 'earlier_points': []}
    assert client.verify(context, [{'text': 'A cast.', 'refs': ['event:1']}])
    assert seen[0]['lines'][0]['evidence'] == {'event:1': {'kind': 'cast'}}
    assert 'nope' not in json.dumps(seen)


def test_copied_reports_omit_generated_game_text():
    import logging
    from arenaonair.diagnostics import BugReportLogs
    logs = BugReportLogs()
    record = logging.LogRecord('arenaonair.speech', logging.INFO, '', 0,
        'Speaking color_analyst: PRIVATE CARD IN HAND', (), None)
    record.game_event_kind = 'llm_commentary'
    logs.emit(record)
    assert 'PRIVATE CARD' not in logs.text()
    assert 'omitted' in logs.text()


def test_life_change_cannot_be_explained_as_tutor_payment_even_if_auditor_agrees():
    b, q, _, w, _ = setup_booth()
    w.output = lambda c: {'turns': [{'role': 'color_analyst',
        'text': "That is the tutor's price, paid in life.", 'refs': c['new_events']}]}
    run(b, Event('life_change', 1, {'from': 20, 'to': 17, 'delta': -3}, 0, 2))
    assert not q.pending() and b.counts['unsupported_causality'] == 1


def test_no_claim_of_unblocked_damage_without_block_evidence():
    b, q, _, w, _ = setup_booth()
    w.output = lambda c: {'turns': [{'role': 'play_by_play',
        'text': 'The Elf got through unblocked.', 'refs': c['new_events']}]}
    run(b, Event('combat_damage', 1, {'amount': 1, 'source_instance': 10, 'target_seat': 2}, 0, 2))
    assert not q.pending()


def test_actual_single_log_feeds_shared_llm_booth(tmp_path, monkeypatch):
    from pathlib import Path
    from arenaonair.app import ArenaOnAirApp
    writer = Writer()
    monkeypatch.setattr('arenaonair.llm_booth.JsonClient', lambda _: writer)
    app = ArenaOnAirApp(Config(narration_mode='llm', broadcast_mode='dual', llm_coalesce=0,
                              carddb_path=str(tmp_path/'none')), dry_run=True)
    prev, backlog = None, []
    try:
        with (Path(__file__).parents[1]/'fixtures/matches/match_01.jsonl').open() as f:
            for line in f:
                raw = json.dumps(json.loads(line)['obj'])
                prev, backlog = app._feed_line(prev, backlog, time.time(), raw)
                if any(e['kind'] == 'cast' for e in app.llm.pending):
                    break
        work = app.llm.take_work()
        assert work and any(v.get('kind') == 'cast' for v in work.context['facts'].values())
        app.llm.process(work)
        pump = SpeechPump(app.queue, FakeSpeaker(), on_delivery=app.llm.delivered,
                          validate=app.llm.valid_for_delivery)
        pump.run_once(); pump.run_once()
        assert {h['role'] for h in app.llm.heard} == {'play_by_play', 'color_analyst'}
        assert app.fusion is None
    finally:
        app.stop()


def test_repeated_combat_snapshots_do_not_invent_another_attack():
    b, _, _, _, _ = setup_booth()
    e = Event('attack_declared', 1, {'attackers': [{'instance_id': 10, 'name': 'Elf'}]}, 0, 2)
    combat = snapshot(turn_info=TurnInfo(4, 1, 'combat/declare_attackers'))
    b.submit(e, combat)
    b.submit(e, replace(combat, snapshot_id=2, gre_state_id=2))
    assert len(b.pending) == 1 and b.counts['duplicate_combat'] == 1
    b.observe(replace(combat, turn_info=TurnInfo(4, 1, 'main2')))
    b.submit(e, replace(combat, snapshot_id=3, gre_state_id=3))
    assert len(b.pending) == 2  # a real additional combat is a new observation


def test_one_repair_uses_rejected_claim_feedback_without_marking_it_spoken():
    b, q, _, w, _ = setup_booth()
    calls = []
    def verify(context, turns):
        calls.append(turns)
        w.validation_issues = ['made-up outcome']
        return len(calls) == 2
    w.verify = verify
    run(b)
    assert len(w.contexts) == 2
    assert w.contexts[1]['validation_feedback']['unsupported_claims'] == ['made-up outcome']
    assert not w.contexts[1]['heard'] and not b.heard
    assert len(q.pending()) == 2
    assert b.counts['repair_attempts'] == 1


def test_stack_departure_cannot_be_announced_as_resolution():
    b, q, _, w, _ = setup_booth()
    w.output = lambda c: {'turns': [{'role': 'play_by_play', 'text': 'Opt resolves.', 'refs': c['new_events']}]}
    run(b, cast(kind='resolve'))
    assert not q.pending()
    assert b.counts['unsupported_resolution'] == 1


@pytest.mark.parametrize('text', [
    'The Pup gives future creatures trample.',
    'The Pup heads toward Bob.',
])
def test_uncited_card_rules_and_unobserved_attack_targets_rejected(text):
    b, q, _, w, _ = setup_booth()
    w.output = lambda c: {'turns': [{'role': 'color_analyst', 'text': text, 'refs': c['new_events']}]}
    run(b, Event('attack_declared', 1, {'attackers': [{'instance_id': 10, 'name': 'Pup'}]}, 0, 2))
    assert not q.pending()


def test_unknown_or_truncated_public_zone_never_claims_complete_board():
    _, _, clock, _, _ = setup_booth()
    for zone in (ZoneView(1, 'battlefield', None, (), membership_known=False),
                 ZoneView(1, 'battlefield', None, (999,)),
                 ZoneView(1, 'battlefield', None, tuple(range(40)))):
        facts = state_facts(snapshot(zones={'battlefield:pub': zone}), None, clock())
        assert facts['state:board']['complete'] is False


def test_spelled_numbers_are_checked_against_cited_evidence():
    from arenaonair.llm_booth import numeric_tokens
    assert numeric_tokens('twenty-five, nineteen, and 3') == {'25', '19', '3'}
    context = {'facts': {'event:1': {'kind': 'life_change', 'data': {'from': 20, 'to': 17}}},
               'new_events': ['event:1'], 'heard': [], 'earlier_points': []}
    with pytest.raises(ModelError, match='unsupported_number'):
        validate_output({'turns': [{'role': 'play_by_play', 'text': 'Alice is at eighteen.', 'refs': ['event:1']}]}, context)
    assert validate_output({'turns': [{'role': 'play_by_play', 'text': 'Alice is at seventeen.', 'refs': ['event:1']}]}, context)


def test_one_time_boon_cannot_be_broadened_to_every_creature():
    context = {'facts': {'event:1': {'kind': 'cast'}, 'card:1': {
        'rules': 'You get a one-time boon with When you cast a creature spell, that creature enters with a trample counter.'}},
        'new_events': ['event:1'], 'heard': [], 'earlier_points': []}
    with pytest.raises(ModelError, match='unsupported_rule'):
        validate_output({'turns': [{'role': 'color_analyst', 'text': 'The boon gives creatures trample counters.',
                                   'refs': ['event:1', 'card:1']}]}, context)


def test_the_model_sees_roles_instead_of_unsayable_names():
    b, q, _, writer, _ = setup_booth()
    run(b, cast(), snapshot(match_meta=MatchMeta('m', None, {1: 'Alice', 2: 'Heyster12806'})))
    context = writer.contexts[-1]
    assert context['facts']['state:player:2']['name'] == 'the opponent'
    assert context['facts']['state:player:1']['name'] == 'Alice'
    assert 'Heyster12806' not in json.dumps(context)
