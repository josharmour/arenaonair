"""arenaonair.narrator -- events -> utterances via the variety engine.

Implements ``arenaonair.interfaces.Narrator``. See DESIGN section 3.5 and
the PRD anti-repetition contract:

* History-weighted template selection: a rolling window (default 8) of
  recently used template indices per (kind, brevity-family) biases selection
  away from recent picks; the same template is never picked twice in a row
  and never recurs inside the window while the pool permits.
* Determinism: the RNG stream is seeded from a stable hash of match_id plus
  a per-render sequence number, so identical event sequences produce an
  identical transcript on every replay.
* Context-aware slots: actor/opponent names from match_meta.player_names,
  life totals, turn number, board counts -- pulled defensively from state.
* Escalating brevity: the Nth consecutive identical low-salience event
  (same kind + seat + name signature) renders full -> short -> silent.
  Streaks reset when a different kind intervenes or salience rises.
* Announcer-not-coach: templates never instruct the player.
* Never raises: any malformed event or state degrades to None.
"""

from __future__ import annotations

import random
import zlib
from typing import Any

from . import events as ev
from . import templates as tpl
from .models import Event, GameState, Utterance


DEFAULT_WINDOW = 8


def _stable_hash(text):
    """Deterministic, process-independent seed component (zlib.crc32)."""
    try:
        return zlib.crc32(str(text).encode("utf-8")) & 0xFFFFFFFF
    except Exception:
        return 0


class _FamilyState:
    """Rolling window + streak bookkeeping for one (kind, family) pair."""

    __slots__ = ("window",)

    def __init__(self):
        self.window = []            # recently used template indices


class Narrator:
    """Variety-engine template renderer (interfaces.Narrator)."""

    def __init__(self, window: int = DEFAULT_WINDOW,
                 local_name: str | None = None):
        self.window = max(2, int(window))
        self.local_name = local_name
        self._families: dict[tuple[str, str], _FamilyState] = {}
        self._streak_sig: dict[str, tuple] = {}      # kind -> signature
        self._streak_count: dict[str, int] = {}      # kind -> count
        self._seq = 0                                # render-attempt counter
        self._last_shape: str | None = None          # QR6 consecutive guard
        self._current_match_id: str | None = None
        self._land_seen: dict[int, int] = {}         # seat -> drops rendered
        self._last_kind: str | None = None           # streak-reset sentinel
        self._pending_kind: str | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def render(self, event: Event, state: GameState | None, tempo: str = "normal"):
        """Event (+optional state) -> Utterance | None. Never raises."""
        try:
            return self._render_inner(event, state, tempo=tempo)
        except Exception:
            return None

    @staticmethod
    def shape_signature(text: str) -> str:
        """Sentence-shape fingerprint (delegates to templates module)."""
        return tpl.shape_signature(text)

    # ------------------------------------------------------------------
    # Core pipeline
    # ------------------------------------------------------------------

    def _render_inner(self, event, state, tempo: str = "normal"):
        if event is None:
            return None
        kind = getattr(event, "kind", None)
        if not isinstance(kind, str) or kind not in ev.ALL_KINDS:
            return None

        # Fast-tempo discipline:
        # 1. Silence low-salience events (< SALIENCE_HIGH) during fast tempo,
        #    preserving critical events (game_end, match_start, etc.).
        salience = int(getattr(event, "salience", 0) or 0)
        if tempo == "fast" and salience < ev.SALIENCE_HIGH:
            return None

        pool = tpl.TEMPLATE_POOLS.get(kind) or tpl.NARRATIVE_POOLS.get(kind)
        short_pool = tpl.TEMPLATE_SHORT_POOLS.get(kind)
        if not pool:
            return None

        self._current_match_id = self._match_id(event, state)
        sig = self._signature_base(event)

        # A different kind intervening resets this kind's streak.
        if self._last_kind is not None and self._last_kind != kind:
            self._streak_sig.pop(kind, None)
            self._streak_count[kind] = 0
        self._pending_kind = kind

        # --- escalating brevity / tempo adaptation -------------------------
        if self._streak_sig.get(kind) == sig:
            self._streak_count[kind] = self._streak_count.get(kind, 0) + 1
        else:
            self._streak_sig[kind] = sig
            self._streak_count[kind] = 1

        family = "full"
        if (tempo == "fast" or self._streak_count[kind] >= 2) and short_pool:
            family = "short"
        if (self._streak_count[kind] >= 3
                and getattr(event, "salience", 0) < ev.SALIENCE_HIGH):
            return None               # silent acknowledgment

        active_pool = short_pool if family == "short" else pool

        # --- history-weighted selection --------------------------------------
        idx = self._pick_index(kind, family, active_pool)
        template = active_pool[idx]

        # --- slot filling -----------------------------------------------------
        slots = self._build_slots(event, state)
        text = self._safe_format(template, slots)
        if not text or not text.strip():
            return None

        # QR6 belt-and-braces: if this text's shape equals the previous
        # utterance's shape and an alternate exists, rotate once.
        shape = tpl.shape_signature(text)
        if shape == self._last_shape and len(active_pool) > 1:
            tried = {idx}
            for _attempt in range(3):
                if len(tried) >= len(active_pool):
                    break
                alt_idx = self._pick_index(kind, family, active_pool,
                                           exclude=set(tried))
                tried.add(alt_idx)
                alt_text = self._safe_format(active_pool[alt_idx], slots)
                if not alt_text or not alt_text.strip():
                    continue
                alt_shape = tpl.shape_signature(alt_text)
                if alt_shape != shape:
                    idx, text, shape = alt_idx, alt_text, alt_shape
                    break
        self._last_shape = shape

        text = self._polish(text)
        self._last_kind = self._pending_kind

        # --- utterance ----------------------------------------------------------
        self._seq += 1
        match_id = self._current_match_id
        return Utterance(
            uid=f"{match_id or 'unknown'}-{kind}-{self._seq}",
            match_id=match_id,
            kind=kind,
            text=text.strip(),
            salience=int(getattr(event, "salience", ev.SALIENCE_LOW) or 0),
            ts_created=float(getattr(event, "ts", 0.0) or 0.0),
        )

    # ------------------------------------------------------------------
    # Selection machinery
    # ------------------------------------------------------------------

    def _family_state(self, kind, family):
        key = (kind, family)
        fs = self._families.get(key)
        if fs is None:
            fs = _FamilyState()
            self._families[key] = fs
        return fs

    def _pick_index(self, kind, family, pool, exclude=None):
        """Weighted pick avoiding consecutive repeats + in-window repeats."""
        n = len(pool)
        if n == 0:
            raise ValueError("empty pool")
        if n == 1:
            return 0

        fs = self._family_state(kind, family)
        rng_seed = ((_stable_hash(self._current_match_id or "") * 31
                     + _stable_hash(kind) * 7
                     + _stable_hash(family) * 3 + self._seq)
                    & 0xFFFFFFFFFFFF)
        rng = random.Random(rng_seed)

        recent = list(fs.window)[-self.window:]
        blocked = set(recent)
        if exclude:
            blocked |= set(exclude)

        candidates = [i for i in range(n) if i not in blocked]
        if not candidates:
            # Window saturated (pool smaller than window): prefer rotating
            # through every template by excluding the last n-1 picks.
            spread_block = set(recent[-(n - 1):]) if n > 1 else set()
            candidates = [i for i in range(n) if i not in spread_block]
            if not candidates:
                candidates = [i for i in range(n)
                              if i not in set(recent[-1:])]
            if exclude:
                filtered = [i for i in candidates if i not in exclude]
                candidates = filtered or list(range(n))

        weights = []
        for i in candidates:
            penalty = 1.0
            for distance, recent_idx in enumerate(reversed(recent)):
                if recent_idx == i:
                    penalty *= 0.25 * (0.8 ** distance)
            weights.append(max(penalty, 0.01))

        chosen = rng.choices(candidates, weights=weights, k=1)[0]
        fs.window.append(chosen)
        del fs.window[: max(0, len(fs.window) - self.window)]
        return chosen

    # ------------------------------------------------------------------
    # Signatures / streaks
    # ------------------------------------------------------------------

    @staticmethod
    def _signature_base(event):
        """Identity of an event for streak counting (kind+seat+names)."""
        payload = getattr(event, "payload", {}) or {}
        names = payload.get("names")
        name_part = tuple(names) if isinstance(names, (list, tuple)) \
            else payload.get("name") or payload.get("cast_name") or ""
        try:
            return (getattr(event, "kind", ""),
                    getattr(event, "seat", None), str(name_part))
        except Exception:
            return (str(getattr(event, "kind", "")),)

    # ------------------------------------------------------------------
    # Slot extraction (every accessor individually guarded)
    # ------------------------------------------------------------------

    def _build_slots(self, event, state):
        slots: dict[str, Any] = {}
        payload = getattr(event, "payload", {}) or {}
        kind = getattr(event, "kind", "")

        seat = getattr(event, "seat", None)
        actor_slot = self._actor_name(seat, state)
        opp_seat = self._other_seat(seat, state)
        opp_slot = self._actor_name(opp_seat, state)

        slots["actor"] = actor_slot
        slots["opp"] = opp_slot

        name_val = payload.get("name") or payload.get("cast_name")
        slots["card"] = str(name_val) if name_val else "a spell"

        names_list = payload.get("names")
        
# === slot fillers appended below ===
        if isinstance(names_list, (list, tuple)) and names_list:
            pretty = ", ".join(str(n) for n in names_list if n)
            slots["card"] = pretty or slots["card"]

        # --- numeric / contextual slots ---------------------------------
        slots["count"] = self._num_or_empty(payload.get("count"))
        slots["count_word"] = tpl.num_word(slots["count"])
        slots["amount"] = self._num_or_empty(payload.get("amount"))
        slots["amount_word"] = tpl.num_word(slots["amount"])

        from_life = self._num_or_empty(payload.get("from"))
        to_life = self._num_or_empty(payload.get("to"))
        delta = self._num_or_empty(payload.get("delta"))
        if delta == "" and from_life != "" and to_life != "":
            try:
                delta = int(to_life) - int(from_life)
            except (TypeError, ValueError):
                delta = ""
        slots["from_life"] = from_life
        slots["to_life"] = to_life
        slots["delta"] = delta
        slots["delta_word"] = tpl.num_word(abs(delta)) if delta != "" else ""

        # life-change danger coloring
        danger = False
        try:
            danger = (delta < 0 and isinstance(to_life, int)
                      and to_life <= 5)
        except Exception:
            danger = False
        if kind == ev.LIFE_CHANGE:
            if isinstance(delta, int) and delta > 0:
                slots["direction_prep"] = "up"
                slots["against_or_for"] = "for"
                slots["claws_or_drops"] = "claws back to"
                slots["down_or_up"] = "Up"
            elif isinstance(delta, int) and delta < 0:
                slots["direction_prep"] = "down"
                slots["against_or_for"] = "against"
                slots["claws_or_drops"] = "drops to"
                slots["down_or_up"] = "Down"
            else:
                slots["direction_prep"] = "to"
                slots["against_or_for"] = "for"
                slots["claws_or_drops"] = "sitting at"
                slots["down_or_up"] = "Over"

            if danger:
                slots["danger_color"] = (
                    f"Danger zone -- {actor_slot} is hanging by a thread "
                    f"at {to_life}.")
                slots["danger_clause"] = " -- dangerously low now"
            else:
                direction = "down" if (isinstance(delta, int) and delta < 0) \
                    else "up"
                slots["danger_color"] = (
                    f"Life shifting {direction} for {actor_slot} -- "
                    f"{to_life} on the dial.")
                slots["danger_clause"] = ""
        else:
            slots["danger_color"] = ""
            slots["danger_clause"] = ""
            slots["direction_prep"] = ""
            slots["against_or_for"] = ""
            slots["claws_or_drops"] = ""
            slots["down_or_up"] = ""

        # --- land-drop context ---------------------------------------------
        land_count = self._land_count(state, seat)
        if land_count == "" or land_count == 0:
            # State lacks usable land data -> rely on our own tally so the
            # copy never reads 'up to 0 lands'.
            if not self._state_has_lands(state):
                land_count = self._land_seen.get(seat, 0) + 1
                self._land_seen[seat] = land_count
        slots["land_count"] = land_count
        ordinal = payload.get("count") if payload.get("count") else None
        if ordinal in (None, ""):
            ordinal = land_count
        slots["land_num"] = self._num_or_empty(ordinal) or "?"
        try:
            n_int = int(ordinal) if ordinal not in (None, "") else None
        except (TypeError, ValueError):
            n_int = None
        suffix = ""
        if n_int is not None:
            suffix = {1: "st", 2: "nd", 3: "rd"}.get(
                n_int % 10 if n_int % 100 not in (11, 12, 13) else -1,
                "th")
            suffix = {1: "st", 2: "nd", 3: "rd"}.get(n_int, "th") \
                if n_int <= 3 else suffix
        slots["ordinal_clause"] = f" -- number {n_int}" \
            if n_int is not None else ""

        # --- attack / block / combat -----------------------------------------
        attackers = payload.get("attackers")
        atk_count = len(attackers) if isinstance(attackers, (list, tuple)) \
            else ""
        slots["atk_count"] = atk_count
        if atk_count == "":
            atk_count = slots["count"]
        slots["count_word"] = tpl.num_word(atk_count) \
            if atk_count != "" else slots["count_word"]
        total_power = payload.get("total_power")
        try:
            total_power_int = int(total_power)
        except (TypeError, ValueError):
            total_power_int = None
        slots["power_word"] = (tpl.num_word(total_power_int)
                               if total_power_int is not None else "")
        lead_name = ""
        if isinstance(attackers, (list, tuple)) and attackers:
            first = attackers[0]
            if isinstance(first, dict):
                lead_name = str(first.get("name") or "")
        slots["lead_card"] = lead_name or slots["card"]

        blocks = payload.get("blocks")
        blk_count = len(blocks) if isinstance(blocks, (list, tuple)) else ""
        slots["blk_count"] = blk_count
        slots["block_count_word"] = tpl.num_word(blk_count) \
            if blk_count != "" else ""

        source_iid = payload.get("source_instance")
        source_ref = self._object_ref(source_iid, state)
        source_name = getattr(source_ref, "name", None) \
            if source_ref is not None else None
        slots["source_card"] = str(source_name) if source_name \
            else slots["card"]

        target_seat = payload.get("target_seat", seat)
        slots["target_actor"] = self._actor_name(target_seat, state)

        life_after = self._life_of(target_seat, state)
        slots["life_after_clause"] = (
            f" -- down to {life_after}" if life_after != "" else "")

        # --- board-shift context ----------------------------------------------
        cb = self._num_or_empty(payload.get("creatures_before"))
        ca = self._num_or_empty(payload.get("creatures_after"))
        pb = self._num_or_empty(payload.get("power_before"))
        pa = self._num_or_empty(payload.get("power_after"))
        slots["creatures_before"] = cb
        slots["creatures_after"] = ca
        slots["power_before"] = pb
        slots["power_after"] = pa
        try:
            pa_total = int(pa) if pa != "" else None
            pb_total = int(pb) if pb != "" else None
            power_after_total = pa_total if pa_total is not None \
                else pb_total
        except (TypeError, ValueError):
            power_after_total = None
        slots["power_after_total"] = (
            tpl.num_word(power_after_total)
            if power_after_total is not None else "?")
        try:
            parity_cb, parity_ca = int(cb), int(ca)
            opp_creatures = self._opp_creature_count(state, seat)
            parity_clause = ""
            if opp_creatures != "" and parity_ca == int(opp_creatures):
                parity_clause = " -- back to even"
            elif power_after_total is not None and power_after_total >= 10:
                parity_clause = " -- and it's a lot of muscle"
            slots["parity_clause"] = parity_clause
        except (TypeError, ValueError):
            slots["parity_clause"] = ""

        # --- misc context --------------------------------------------------------
        turn_number = self._turn_number(state)
        slots["turn_number"] = turn_number
        slots["turn_word"] = turn_number if turn_number != "" else "next"

        format_name = getattr(getattr(state, "match_meta", None),
                              "format_name", None)
        slots["format_name"] = str(format_name).replace("_", " ").title() \
            if format_name else "the match"

        reason_val = payload.get("reason")
        reason_txt = str(reason_val).replace("_", " ") \
            if reason_val else ""
        winner_seat_id = payload.get("winning_team_id")
        winner_actor_txt = ""
        
# === part2 continues ===
        if winner_seat_id is not None and state is not None:
            try:
                for p_seat, p_view in (getattr(state, "players", {}) or {}).items():
                    team = getattr(p_view, "team_id", None)
                    if team is not None and int(team) == int(winner_seat_id):
                        winner_actor_txt = self._actor_name(p_seat, state)
                        break
            except Exception:
                winner_actor_txt = ""
        slots["winner"] = winner_actor_txt or actor_slot
        slots["reason_clause"] = f" -- {reason_txt}" if reason_txt else ""
        slots["reason"] = reason_txt

        # --- resolve/counter specifics -----------------------------------------
        to_zone = payload.get("to_zone")
        zone_pretty = str(to_zone).replace("_", " ") if to_zone else ""
        dest_map = {"battlefield": "onto the battlefield",
                    "graveyard": "into the graveyard",
                    "exile": "straight to exile"}
        slots["dest_clause"] = dest_map.get(zone_pretty,
                                            f" -- to {zone_pretty}") \
            if zone_pretty else ""
        slots["for_actor_clause"] = f" for {actor_slot}"

        counter_seat = payload.get("countered_by_seat")
        counter_actor = self._actor_name(counter_seat, state) \
            if counter_seat is not None else ""
        slots["counter_actor"] = counter_actor or opp_slot

        # --- mana-cost slot (converted cost from payload when present) -------------
        mana_cost_val = payload.get("mana_cost")
        cmc_val = tpl.converted_mana(mana_cost_val)
        if cmc_val is None:
            cmc_val = payload.get("cmc") if isinstance(
                payload.get("cmc"), int) else None
        slots["mana_words"] = tpl.num_word(cmc_val) \
            if cmc_val is not None else "some"

        # --- gloss slot ------------------------------------------------------------
        grp_id = payload.get("grp_id")
        ref = self._object_ref(payload.get("instance_id"), state)
        gloss_source_ref = ref
        if gloss_source_ref is None and grp_id is not None and state:
            gloss_source_ref = None   # no reverse grp lookup without carddb
        type_line = getattr(gloss_source_ref, "type_line", None) \
            if gloss_source_ref is not None else None
        power_val = getattr(gloss_source_ref, "power", None) \
            if gloss_source_ref is not None else None
        slots["gloss"] = tpl.gloss_phrase(
            name=name_val, type_line=type_line, mana_cost=None,
            power=power_val) or slots["card"]

        # --- narrative-kind slots ---------------------------------------------------
        slots.update(self._narrative_slots(kind, payload, state))

        return slots

    def _narrative_slots(self, kind, payload, state):
        out: dict[str, Any] = {}
        detail = str(payload.get("detail") or "")
        out["arc_detail"] = detail
        out["detail"] = detail

        if kind == ev.NARRATIVE_ARC:
            from_arc = payload.get("from_arc")
            to_arc = payload.get("to_arc")
            out["from_arc_pretty"] = tpl.arc_phrase(from_arc) \
                or str(from_arc or "").replace("_", " ")
            out["to_arc_pretty"] = tpl.arc_phrase(to_arc) \
                or str(to_arc or "").replace("_", " ")
            trans = tpl.arc_transition_phrase(from_arc, to_arc)
            out["arc_transition_clause"] = (trans[:1].upper() + trans[1:]) \
                if trans else "The story shifts"
            leader_seat = payload.get("leader_seat")
            out["leader_actor"] = self._actor_name(leader_seat, state)
            out["arc_tail"] = ""
            out["arc_color"] = ("The momentum needle just jumped -- "
                                f"{out['to_arc_pretty']}.")
            out["arc_consequence_clause"] = (
                f"Expect {out['to_arc_pretty']} from here.")

        elif kind == ev.NARRATIVE_RESOURCE:
            streak_kind = payload.get("streak_kind")
            length_val = payload.get("length")
            length_word = tpl.num_word(length_val) \
                if length_val != "" else ""
            out["streak_kind"] = str(streak_kind or "")
            out["length"] = self._num_or_empty(length_val)
            out["length_word"] = length_word
            clause = tpl.streak_phrase(streak_kind, length_val)
            out["streak_clause"] = (clause[:1].upper() + clause[1:] + ".") \
                if clause else (detail[:1].upper() + detail[1:] + "."
                                if detail else "Resources tell a story.")
            actor_here = self._actor_name(getattr(payload, "get", lambda *_: None)("seat") if False else payload.get("seat"), state)
            
# === part3 continues ===
            out["streak_tail"] = (
                f"'s {out['streak_kind']} streak hits {length_word}"
                if length_word else " rides a resource streak")
            out["streak_aside"] = detail or "The resources tell a story."
            out["streak_color"] = out["streak_clause"].rstrip(".")
            out["streak_consequence_clause"] = (
                f"That's {length_word} straight"
                if length_word else "That's a stretch")
            out["streak_noun_clause"] = ""
            out["actor"] = actor_here or out.get("actor", "")

        elif kind == ev.NARRATIVE_CALLBACK:
            distance_val = payload.get("turn_distance")
            distance_word = tpl.num_word(distance_val) \
                if distance_val not in (None, "") else ""
            out["beat_kind"] = str(payload.get("beat_kind") or "")
            out["turn_distance"] = self._num_or_empty(distance_val)
            out["distance_word"] = distance_word
            ref_seat = payload.get("ref_seat")
            out["ref_actor"] = self._actor_name(ref_seat, state)
            base = detail or "an earlier beat still echoes"
            clause = base if base.endswith(".") else base + "."
            out["callback_clause"] = clause[:1].upper() + clause[1:]
            out["callback_tail"] = "."
            out["callback_aside"] = clause
            out["callback_color"] = (
                f"Sound familiar? {clause}")
            out["callback_consequence_clause"] = (
                f"History rhymes: {clause}")
            out["callback_tail2"] = "."

        elif kind == ev.NARRATIVE_SPECULATION:
            spec_kind = str(payload.get("kind") or "")
            out["spec_kind"] = spec_kind
            leader_seat = payload.get("leader_seat")
            leader_actor = self._actor_name(leader_seat, state)
            out["leader_actor"] = leader_actor
            base = detail or "the next move could change everything"
            clause = base if base.endswith(".") else base + "."
            out["speculation_clause"] = clause[:1].upper() + clause[1:]
            cond = f"If that resolves, {base}" \
                if spec_kind == "race_shaping" else base
            out["spec_conditional_clause"] = (
                cond[:1].upper() + cond[1:] + ".")
            out["speculation_aside"] = clause
            out["speculation_color"] = f"Buckle up -- {base}."
            out["spec_tail"] = "."
            out["spec_stat_clause"] = clause

        return out

    # ------------------------------------------------------------------
    # State accessors (all defensive)
    # ------------------------------------------------------------------

    def _match_id(self, event, state):
        try:
            mid = getattr(getattr(state, "match_meta", None), "match_id",
                          None)
            if mid:
                return str(mid)
        except Exception:
            pass
        try:
            mid = (getattr(event, "payload", {}) or {}).get("match_id")
            if mid:
                return str(mid)
        except Exception:
            pass
        return None

    def _actor_name(self, seat, state):
        try:
            names = getattr(getattr(state, "match_meta", None),
                            "player_names", None) or {}
            name = names.get(seat)
            if name:
                return str(name)
        except Exception:
            pass
        try:
            local_seat = getattr(state, "local_seat", None)
            if (seat is not None and local_seat is not None
                    and int(seat) == int(local_seat) and self.local_name):
                return self.local_name
        except Exception:
            pass
        return f"seat {seat}" if seat is not None else "our player"

    def _other_seat(self, seat, state):
        try:
            seats = sorted((getattr(state, "players", {}) or {}).keys())
            int_seats = [s for s in seats if isinstance(s, int)]
            if seat is None:
                return int_seats[0] if int_seats else None
            others = [s for s in int_seats if s != int(seat)]
            return others[0] if others else None
        except Exception:
            return None

    def _life_of(self, seat, state):
        try:
            view = (getattr(state, "players", {}) or {}).get(seat)
            life = getattr(view, "life", None)
            return int(life) if life is not None else ""
        except Exception:
            return ""

    def _land_count(self, state, seat):
        try:
            bf_ids = []
            for zone in (getattr(state, "zones", {}) or {}).values():
                zt = str(getattr(zone, "zone_type", "")).lower()
                if "battlefield" in zt:
                    bf_ids.extend(getattr(zone, "object_ids", ()) or ())
                    break
            objs = getattr(state, "objects", {}) or {}
            count = 0
            for iid in bf_ids:
                ref = objs.get(iid)
                if ref is None:
                    continue
                ctypes = tuple(getattr(ref, "card_types", ()) or ())
                ctrl = getattr(ref, "controller_seat", None)
                if "land" in ctypes and (seat is None or ctrl == seat):
                    count += 1
            return count
        except Exception:
            return ""

    def _state_has_lands(self, state):
        try:
            for zone in (getattr(state, "zones", {}) or {}).values():
                zt = str(getattr(zone, "zone_type", "")).lower()
                if "battlefield" not in zt:
                    continue
                objs = getattr(state, "objects", {}) or {}
                for iid in getattr(zone, "object_ids", ()) or ():
                    ref = objs.get(iid)
                    if ref is not None and "land" in tuple(
                            getattr(ref, "card_types", ()) or ()):
                        return True
        except Exception:
            pass
        return False

    def _opp_creature_count(self, state, seat):
        try:
            opp_seat = self._other_seat(seat, state)
            bf_ids = []
            for zone in (getattr(state, "zones", {}) or {}).values():
                zt = str(getattr(zone, "zone_type", "")).lower()
                if "battlefield" in zt:
                    bf_ids.extend(getattr(zone, "object_ids", ()) or ())
                    break
            objs = getattr(state, "objects", {}) or {}
            count = 0
            for iid in bf_ids:
                ref = objs.get(iid)
                if ref is None:
                    continue
                ctypes = tuple(getattr(ref, "card_types", ()) or ())
                ctrl = getattr(ref, "controller_seat", None)
                if ("creature" in ctypes and opp_seat is not None
                        and ctrl == opp_seat):
                    count += 1
            return count
        except Exception:
            return ""

    def _object_ref(self, instance_id, state):
        try:
            if instance_id is None:
                return None
            return (getattr(state, "objects", {}) or {}).get(instance_id)
        except Exception:
            return None

    def _turn_number(self, state):
        try:
            tn = getattr(getattr(state, "turn_info", None),
                         "turn_number", None)
            return int(tn) if tn is not None else ""
        except Exception:
            return ""

    @staticmethod
    def _num_or_empty(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return ""

    @staticmethod
    def _polish(text):
        """Cosmetic cleanup: spacing + dangling separators."""
        try:
            cleaned = " ".join(str(text).split())
            cleaned = cleaned.replace("-- .", "--.").replace(" .", ".")
            cleaned = cleaned.replace("spends some mana on", "fires off")
            cleaned = cleaned.replace("with land number ?",
                                      "with another land")
            cleaned = cleaned.replace("? lands now", "lands stacking up")
            cleaned = cleaned.replace("turn ? begins", "gets moving")
            cleaned = cleaned.replace(": ? in the hot seat",
                                      " in the hot seat")
            import re as _re
            cleaned = _re.sub(
                r"\b(one) (creatures|blockers|attackers)",
                lambda m: f"{m.group(1)} {m.group(2)[:-1]}", cleaned)
            return cleaned
        except Exception:
            return text

    @staticmethod
    def _safe_format(template, slots):
        try:
            class _SafeDict(dict):
                def __missing__(self, key):
                    return ""
            return str(template).format_map(_SafeDict(slots))
        except Exception:
            try:
                return str(template)
            except Exception:
                return ""
