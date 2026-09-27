"""Bounded real-model booth evaluation, with a recording speaker and NO audio.

Usage: python -m tools.replay_llm --out /tmp/booth-eval.json
Add --log-path fixtures/matches/match_01.jsonl for a bounded recorded-log replay.
The output is local and mode 0600: transcripts can contain private game facts.
"""
from dataclasses import replace
import argparse
import json
import os
from pathlib import Path
import sqlite3
import time

from arenaonair.carddb import CardDb, DEFAULT_DB_PATH
from arenaonair.card_knowledge import CardKnowledge
from arenaonair.config import Config, resolve_booth
from arenaonair.differ import EventDiffer
from arenaonair.gre_parser import parse_line_all
from arenaonair.llm_booth import GenerativeBooth, JsonClient
from arenaonair.models import CardRef, DeliveryResult, Event, GameState, MatchMeta, PlayerView, TurnInfo, ZoneView
from arenaonair.speech import SpeechPump, SpeechQueue
from arenaonair.state_builder import GameStateBuilder, with_knowledge
from arenaonair.story import StoryModel


class RecordingSpeaker:
    def __init__(self):
        self.transcript = []
    def speak(self, u):
        self.transcript.append({'role': u.role, 'voice': u.voice, 'text': u.text})
        return DeliveryResult(u.uid, True)
    def cancel(self):
        pass


def scenarios(carddb):
    # Synthetic observations isolate causal/factual cases. Card rules are read
    # from the actual local card cache; no invented card rules in the fixture.
    with sqlite3.connect(DEFAULT_DB_PATH) as conn:
        gids = {name: conn.execute('SELECT arena_id FROM cards WHERE name=? LIMIT 1', (name,)).fetchone()[0]
                for name in ('Grim Tutor', 'Demonic Tutor', 'Llanowar Elves', 'Opt')}
    snap = GameState(1, None, {}, {}, {1: PlayerView(1, 20), 2: PlayerView(2, 20)},
        TurnInfo(0, 1, None), MatchMeta('evaluation', None, {1: 'Alice', 2: 'Bob'}),
        game_id=1, game_stage='GameStage_Start', chain_valid=True)
    yield 'mulligan-stage', snap, [Event('game_start', None, {}, 0, 3)]
    yield 'mulligan-observed-synthetic', snap, [Event('mulligan', 1, {'count': 1}, 0, 2)]
    snap = replace(snap, turn_info=TurnInfo(3, 1, 'main'), game_stage='GameStage_Play')
    def play(name, iid, kind='cast'):
        return Event(kind, 1, {'instance_id': iid, 'grp_id': gids[name], 'name': name}, 0, 2)
    for label, name, iid in [('tutor-with-life-rule', 'Grim Tutor', 100),
                              ('tutor-without-life-rule', 'Demonic Tutor', 101),
                              ('first-elves', 'Llanowar Elves', 102),
                              ('second-elves', 'Llanowar Elves', 103)]:
        ref = CardRef(iid, gids[name], name, carddb.lookup(gids[name]).type_line, (), controller_seat=1)
        snap = replace(snap, objects={**snap.objects, iid: ref}, zones={'stack:pub': ZoneView(1, 'stack', None, (iid,))})
        yield label, snap, [play(name, iid)]
        if iid == 100:
            snap = replace(snap, zones={'graveyard:1': ZoneView(2, 'graveyard', 1, (iid,))})
            yield 'stack-departure', snap, [play(name, iid, 'resolve')]
            snap = replace(snap, players={1: PlayerView(1, 17), 2: PlayerView(2, 20)})
            yield 'life-change-no-cause-evidence', snap, [Event('life_change', 1, {'from': 20, 'to': 17, 'delta': -3}, 0, 2)]
    snap = replace(snap, turn_info=TurnInfo(5, 1, 'combat'),
        zones={'battlefield:pub': ZoneView(3, 'battlefield', None, (102, 103))})
    yield 'combat', snap, [Event('attack_declared', 1, {'attackers': [
        {'instance_id': 102, 'name': 'Llanowar Elves'}, {'instance_id': 103, 'name': 'Llanowar Elves'}]}, 0, 2)]
    snap = replace(snap, players={1: PlayerView(1, 17), 2: PlayerView(2, 19)})
    yield 'ordinary-one-damage', snap, [Event('combat_damage', 1, {'amount': 1, 'source_instance': 102, 'target_seat': 2}, 0, 2),
        Event('life_change', 2, {'from': 20, 'to': 19, 'delta': -1}, 0, 2)]
    yield 'same-public-event-no-new-fact', snap, [Event('life_change', 2, {'from': 20, 'to': 19, 'delta': -1}, 0, 2)]


def recorded(path, carddb):
    """Recorded batches as (label, snap, events, game read); turn starts open game-read moments."""
    builder = GameStateBuilder(name_resolver=carddb.as_resolver())
    differ = EventDiffer(card_lookup=carddb.lookup)
    story = StoryModel(card_lookup=carddb.lookup)
    previous, backlog = None, []
    with path.open(errors='replace') as log:
        for raw in log:
            if path.suffix == '.jsonl':
                raw = json.dumps(json.loads(raw)['obj'])
            for msg in parse_line_all(time.time(), raw):
                backlog.append(msg)
                snap = builder.apply(previous, msg)
                if snap is None:
                    continue
                snap = with_knowledge(snap, [snap.local_seat] if snap.local_seat else [], time.monotonic())
                events = [e for e in differ.diff(previous, snap, backlog) if e.kind in {
                    'cast', 'resolve', 'combat_damage', 'attack_declared', 'life_change', 'game_start', 'game_end',
                    'land_drop', 'turn_start'}]
                story.update(snap)
                previous, backlog = snap, []
                if events:
                    yield 'recorded-' + '-'.join(e.kind for e in events), snap, events, story.read(snap)


def card_scenarios(names, carddb):
    """Named synthetic casts using actual locally resolved Arena identities."""
    with sqlite3.connect(DEFAULT_DB_PATH) as conn:
        for iid, name in enumerate(names, 200):
            row = conn.execute('SELECT arena_id FROM cards WHERE name=? LIMIT 1', (name,)).fetchone()
            if row is None:
                raise ValueError('Unknown local card: ' + name)
            gid = row[0]
            info = carddb.lookup(gid)
            snap = GameState(iid, None, {'stack:pub': ZoneView(1, 'stack', None, (iid,))},
                {iid: CardRef(iid, gid, name, info.type_line, (), controller_seat=1)}, {1: PlayerView(1, 20), 2: PlayerView(2, 20)},
                TurnInfo(5, 1, 'main'), MatchMeta('card-evaluation-' + str(iid), None, {1: 'Alice', 2: 'Bob'}),
                game_id=1, game_stage='GameStage_Play', chain_valid=True)
            yield 'synthetic-card-' + name, snap, [Event('cast', 1, dict(name=name, grp_id=gid, instance_id=iid), 0, 2)]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--log-path', type=Path)
    parser.add_argument('--card', action='append', help='Evaluate a named synthetic cast; repeat for more cards')
    parser.add_argument('--max-requests', type=int, default=12)
    parser.add_argument('--max-age', type=float, default=24.0, help='Short values exercise stale-output rejection')
    parser.add_argument('--from-turn', type=int, default=0,
                        help='Recorded replays: earlier turns build context but send no requests')
    parser.add_argument('--focus', choices=('calls', 'balanced', 'analysis'), default='balanced',
                        help='Commentary focus for recorded replays (game-read moments follow it)')
    parser.add_argument('--base-url', default=os.environ.get('ARENAONAIR_BASE_URL', 'http://127.0.0.1:8444/v1'))
    parser.add_argument('--key-file', default=str(Path.home()/'.config/arenaonair/llm.key'))
    args = parser.parse_args()
    cfg = Config(broadcast_mode='dual', llm_base_url=args.base_url, llm_key_file=args.key_file, llm_coalesce=0,
                 llm_max_age=args.max_age, commentary_focus=args.focus)
    carddb = CardDb()
    queue = SpeechQueue()
    client = JsonClient(cfg)
    candidates = []
    original_generate = client.generate
    def capture(context):
        output = original_generate(context)
        candidates.append(output)
        return output
    client.generate = capture
    audits = []
    original_verify = client.verify
    def audit(context, turns):
        ok = original_verify(context, turns)
        audits.append({'passed': ok, 'lines': [t['text'] for t in turns],
                       'unsupported': list(getattr(client, 'validation_issues', []))})
        return ok
    client.verify = audit
    booth = GenerativeBooth(cfg, queue, resolve_booth(cfg), carddb=carddb, knowledge=CardKnowledge(), client=client)
    speaker = RecordingSpeaker()
    pump = SpeechPump(queue, speaker, on_delivery=booth.delivered, validate=booth.valid_for_delivery)
    samples = []
    requests = 0
    try:
        cases = card_scenarios(args.card, carddb) if args.card else recorded(args.log_path, carddb) if args.log_path else scenarios(carddb)
        for label, snap, events, *read in cases:
            if requests >= min(max(args.max_requests, 1), 30):
                break
            # This is a paced quality replay: let backoff finish before the next
            # recorded batch. The live app instead drops actions during outages.
            delay = booth.backoff_until - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            # Replay age is receipt time, not historical wall-clock timestamps.
            queue.set_active_match(snap.match_meta.match_id)
            if read:
                booth.update_read(read[0])
            for event in events:
                if event.kind == 'turn_start':
                    booth.turn_started(snap)
                else:
                    booth.submit(event, snap)
            if snap.turn_info.turn_number is not None and snap.turn_info.turn_number < args.from_turn:
                booth.pending.clear()
                booth.batch_since = None
                continue
            work = booth.take_work()
            before = len(speaker.transcript)
            candidates.clear()
            audits.clear()
            if work:
                requests += 1
                booth.process(work)
                while pump.run_once():
                    pass
            samples.append({'scenario': label, 'status': booth.status(), 'candidate': list(candidates), 'audits': list(audits),
                'knowledge': {k: v for k, v in work.context['facts'].items() if k.startswith('knowledge:')} if work else {},
                'generated': [dict(role=r['role'], text=r['text'], status=r['status']) for r in booth.records if work and r['created'] == work.started],
                'delivered': speaker.transcript[before:]})
            print(label, booth.state, booth.latency, 'delivered', len(speaker.transcript)-before, flush=True)
        if not requests:
            raise RuntimeError('No parsed game events; use a supported Player.log or fixtures/matches/*.jsonl')
        result = {'mode': 'recorded' if args.log_path else 'synthetic', 'model': cfg.llm_model,
                  'requests': requests, 'samples': samples, 'status': booth.status(),
                  'transcript': speaker.transcript,
                  'limitations': 'Recording speaker confirms pipeline delivery, not audible synthesis. Replay is paced per batch, not real-time playback.'}
        fd = os.open(args.out, os.O_WRONLY|os.O_CREAT|os.O_TRUNC, 0o600)
        os.chmod(args.out, 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump(result, f, indent=2)
        print('Saved restricted local evaluation:', args.out)
    finally:
        booth.stop()
        carddb.close()


if __name__ == '__main__':
    main()
