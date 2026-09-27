"""grpId -> card name / type / cost resolution backed by a local SQLite cache.

MTGA GRE messages identify cards only by numeric ``grpId`` (with names as
numeric localization ids), which is useless for narration until mapped to real
card data.  Scryfall publishes bulk data whose ``arena_id`` field equals the
MTGA grpId on most cards; :mod:`arenaonair.build_carddb` builds a local
SQLite cache from that data and this module reads it.

The cache lives outside the repo (default ``~/.cache/arenaonair/cards.sqlite``)
and is entirely optional: every lookup degrades gracefully to ``None`` when the
database is missing or the id is unknown, logging at DEBUG level at most once
per missing id.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable, Optional, Tuple

logger = logging.getLogger(__name__)

__all__ = [
    "CardDb",
    "CardInfo",
    "calculate_cmc",
    "is_sweeper",
    "is_tutor",
    "is_bomb",
    "detect_archetype",
    "is_cantrip_or_filter",
    "is_counterspell",
    "is_combat_trick",
]

DEFAULT_DB_PATH = Path.home() / ".cache" / "arenaonair" / "cards.sqlite"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
    arena_id   INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    type_line  TEXT NOT NULL,
    mana_cost  TEXT NOT NULL,
    card_types TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class CardInfo:
    """Resolved card identity for a single grpId."""

    name: str
    type_line: str
    mana_cost: str
    card_types: Tuple[str, ...]
    oracle_text: str = ""


def _csv_to_tuple(csv: str) -> Tuple[str, ...]:
    if not csv:
        return ()
    return tuple(t for t in csv.split(",") if t)


def calculate_cmc(mana_cost: str) -> int:
    """Calculate converted mana cost / mana value from a mana cost string."""
    if not isinstance(mana_cost, str) or not mana_cost:
        return 0
    symbols = re.findall(r"\{([^}]+)\}", mana_cost)
    if not symbols:
        return 0
    cmc = 0
    for s in symbols:
        if s.isdigit():
            cmc += int(s)
        elif s.upper() in ("X", "Y", "Z"):
            pass
        elif "/" in s:
            parts = s.split("/")
            val = 1
            for p in parts:
                if p.isdigit():
                    val = max(val, int(p))
            cmc += val
        else:
            cmc += 1
    return cmc


SWEEPER_NAMES = frozenset({
    "sunfall", "farewell", "depopulate", "wrath of god", "blasphemous act",
    "toxic deluge", "damnation", "supreme verdict", "day of judgment",
    "vanquish the horde", "the meathook massacre", "temporary lockdown",
    "brotherhood's end", "gix's command", "phyrexian scriptures", "crux of fate",
    "languish", "hour of devastation", "star of extinction", "ondu inversion",
    "crippling fear", "path of peril", "doomskar", "culling ritual",
    "cyclonic rift", "extinction event", "realm-cloaked giant", "kaya's wrath",
    "cleansing nova", "time wipe", "planar cleansing", "deadly cover-up",
    "burn down the house", "by invitation only", "carnival of carnage"
})


def is_sweeper(name: str) -> bool:
    """Check if a card name corresponds to a known mass removal / sweeper."""
    if not isinstance(name, str) or not name:
        return False
    low = name.lower().strip()
    if low in SWEEPER_NAMES:
        return True
    if any(k in low for k in ("wrath", "cleansing", "lockdown", "sweeper")):
        return True
    return False


TUTOR_NAMES = frozenset({
    "demonic tutor", "vampiric tutor", "diabolic intent", "beseech the mirror",
    "grim tutor", "wishclaw talisman", "chord of calling", "green sun's zenith",
    "finale of devastation", "invasion of ikoria", "fauna shaman",
    "eldritch evolution", "natural order", "summoner's pact", "stoneforge mystic",
    "open the armory", "steelshaper's gift", "enlightened tutor", "idyllic tutor",
    "mystical tutor", "spellseeker", "solve the equation", "merchant scroll",
    "personal tutor", "karn, the great creator", "fae of wishes", "sylvan scrying",
    "crop rotation", "primeval titan", "scapeshift", "traverse the ulvenwald",
    "search for glory", "imperial recruiter", "recruiter of the guard",
    "whir of invention", "fabricate", "trinket mage", "tribute mage", "trophy mage",
    "scheming symmetry", "mastermind's acquisition", "demonic consultation",
    "tainted pact", "insatiable avarice"
})


def is_tutor(name: str) -> bool:
    """Check if a card searches library for specific targets."""
    if not isinstance(name, str) or not name:
        return False
    low = name.lower().strip()
    if low in TUTOR_NAMES:
        return True
    return "tutor" in low


def is_bomb(info: CardInfo) -> bool:
    """Check if a card is a high-salience threat (planeswalker, high CMC or massive stats)."""
    if not isinstance(info, CardInfo):
        return False
    types = [t.lower() for t in info.card_types]
    if "planeswalker" in types:
        return True
    cmc = calculate_cmc(info.mana_cost)
    if cmc >= 5:
        return True
    return False


ARCHETYPE_SIGNATURES: list[tuple[str, frozenset[str], int]] = [
    # (Archetype Name, Card Signatures, Minimum matches required)
    ("Izzet Phoenix", frozenset({"arclight phoenix", "sleight of hand", "consider", "lightning axe", "picklock prankster"}), 2),
    ("Mono-Red Aggro", frozenset({"kumano faces kakkazan", "monastery swiftspear", "slickshot show-off", "play with fire", "heartfire hero"}), 2),
    ("Boros Convoke", frozenset({"knight-errant of eos", "gleeful demolition", "resolute reinforcements", "novice inspector", "venerated loxodon"}), 2),
    ("Azorius Control", frozenset({"no more lies", "the wandering emperor", "dovin's veto", "supreme verdict", "sunfall", "teferi, hero of dominaria"}), 2),
    ("Rakdos Vampires", frozenset({"sorin, imperious bloodlord", "vein ripper", "bloodtithe harvester"}), 2),
    ("Rakdos Midrange", frozenset({"bloodtithe harvester", "fable of the mirror-breaker", "thoughtseize", "dauthi voidwalker"}), 2),
    ("Domain Ramp", frozenset({"leyline binding", "atraxa, grand unifier", "up the beanstalk", "herd migration"}), 2),
    ("Amalia Combo", frozenset({"amalia benavides aguirre", "wildgrowth walker", "lunarch veteran"}), 2),
    ("Dimir Midrange", frozenset({"psychic frog", "deep-cavern bat", "gix, yawgmoth praetor"}), 2),
    ("Mono-Green Devotion", frozenset({"karn, the great creator", "nykthos, shrine of nyx", "old-growth troll", "cavalier of thorns"}), 2),
]


def detect_archetype(cards: Iterable[str]) -> str | None:
    """Fingerprint competitive archetype from played/revealed cards."""
    low_cards = {c.lower().strip() for c in cards if isinstance(c, str)}
    for name, signatures, min_count in ARCHETYPE_SIGNATURES:
        matches = len(low_cards & signatures)
        if matches >= min_count:
            return name
    return None


CANTRIP_AND_FILTER_NAMES = frozenset({
    "opt", "consider", "sleight of hand", "preordain", "brainstorm", "ponder",
    "faithless looting", "expressive iteration", "serum visions", "gitaxian probe",
    "thought scour", "chart a course", "thrill of possibility", "curate",
    "impulse", "frantic search", "demand answers", "highway robbery", "peek"
})


def is_cantrip_or_filter(name: str) -> bool:
    """Check if a card is a cheap draw / filtering spell used for sculpting."""
    if not isinstance(name, str) or not name:
        return False
    return name.lower().strip() in CANTRIP_AND_FILTER_NAMES


COUNTERSPELL_NAMES = frozenset({
    "counterspell", "no more lies", "dovin's veto", "make disappear",
    "spell pierce", "mystical dispute", "absorb", "saw it coming",
    "essence scatter", "negate", "disdainful stroke", "mana leak",
    "force of will", "force of negation", "pact of negation", "flusterstorm",
    "mindbreak trap", "stern scolding", "an offer you can't refuse", "memory lapse",
    "syncopate", "wash away", "quench", "jawari disruption", "metallic rebuke"
})


def is_counterspell(name: str) -> bool:
    """Check if a card name corresponds to a counterspell."""
    if not isinstance(name, str) or not name:
        return False
    low = name.lower().strip()
    if low in COUNTERSPELL_NAMES:
        return True
    return any(k in low for k in ("counter", "veto", "negate", "disdainful", "quench"))


COMBAT_TRICK_NAMES = frozenset({
    "giant growth", "monstrous rage", "brute force", "mutagenic growth",
    "snakeskin veil", "tamiyo's safekeeping", "tyvar's stand", "loran's escape",
    "shore up", "blossoming defense", "gods willing", "infuriate",
    "titan's strength", "invigorate", "become immense", "embercleave",
    "shelter", "ancestral anger"
})


def is_combat_trick(name: str) -> bool:
    """Check if a card is a recognized combat trick / instant-speed pump."""
    if not isinstance(name, str) or not name:
        return False
    low = name.lower().strip()
    if low in COMBAT_TRICK_NAMES:
        return True
    return any(k in low for k in ("growth", "rage", "strength", "safekeeping", "escape", "stand"))


def installed_arena_databases() -> list[Path]:
    """Known Steam/Epic/direct-install roots; never scan a user's whole disk."""
    home = Path.home()
    roots = [
        home / "Library/Application Support/Steam/steamapps/common/MTGA",
        Path("/Users/Shared/Epic Games/MagicTheGathering"),
        home / ".steam/steam/steamapps/common/MTGA",
        home / ".local/share/Steam/steamapps/common/MTGA",
        Path(os.environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)")) / "Steam/steamapps/common/MTGA",
        Path(os.environ.get("PROGRAMFILES", "C:/Program Files")) / "Wizards of the Coast/MTGA",
        Path(os.environ.get("PROGRAMFILES", "C:/Program Files")) / "Epic Games/MagicTheGathering",
    ]
    found = []
    for root in roots:
        try:
            found.extend((root / "MTGA_Data/Downloads/Raw").glob("Raw_CardDatabase_*.mtga"))
        except OSError:
            continue
    return sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)


class CardDb:
    """Read-only accessor over the locally built card cache.

    Thread safety: a single shared connection guarded by an RLock.  SQLite
    connections are cheap to share this way for read-only workloads, and the
    lock keeps cursor use serialized across narrator/reader threads.
    """

    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH, *, arena_db_path: str | Path | None = None) -> None:
        self._path = Path(db_path)
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None
        self._missing_logged: set[int] = set()
        self._missing_warned_no_db = False
        self._open()
        self._arena_conn = None
        candidates = [Path(arena_db_path)] if arena_db_path is not None else (
            installed_arena_databases() if self._path == DEFAULT_DB_PATH else [])
        for candidate in candidates:
            conn = None
            try:
                conn = sqlite3.connect(candidate.resolve().as_uri() + "?mode=ro", uri=True, check_same_thread=False)
                conn.execute("SELECT GrpId, TitleId, TypeTextId, SubtypeTextId, OldSchoolManaText FROM Cards LIMIT 0")
                conn.execute("SELECT LocId, Loc, Formatted FROM Localizations_enUS LIMIT 0")
                self._arena_conn = conn
                logger.info("CardDb: installed Arena database enabled for cache misses")
                break
            except (sqlite3.Error, OSError):
                if conn is not None:
                    conn.close()

    # -- lifecycle ---------------------------------------------------------

    def _open(self) -> None:
        if not self._path.is_file():
            logger.debug("CardDb: cache file %s does not exist yet", self._path)
            self._warn_missing_db_once()
            return
        try:
            conn = sqlite3.connect(str(self._path), check_same_thread=False)
            conn.execute("SELECT COUNT(*) FROM cards")
        except sqlite3.Error as exc:
            logger.warning("CardDb: cannot open cache %s: %s", self._path, exc)
            self._warn_missing_db_once()
            return
        self._conn = conn
        self._has_oracle = "oracle_text" in {r[1] for r in conn.execute("PRAGMA table_info(cards)")}

    def _warn_missing_db_once(self) -> None:
        if not self._missing_warned_no_db:
            self._missing_warned_no_db = True
            logger.debug(
                "CardDb: no usable external cache at %s; trying installed Arena data "
                "(build one with: arenaonair build-carddb)",
                self._path,
            )

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
            if self._arena_conn is not None:
                self._arena_conn.close()
                self._arena_conn = None

    def _arena_lookup(self, grp_id):
        conn = self._arena_conn
        if conn is None:
            return None
        try:
            row = conn.execute("SELECT TitleId, TypeTextId, SubtypeTextId, OldSchoolManaText FROM Cards WHERE GrpId=?", (grp_id,)).fetchone()
            if row is None:
                return None
            def localized(loc_id):
                result = conn.execute("SELECT Loc FROM Localizations_enUS WHERE LocId=? ORDER BY Formatted LIMIT 1", (loc_id,)).fetchone()
                return re.sub(r"<[^>]+>", "", result[0]) if result else ""
            name, head, subtypes = (localized(loc_id) for loc_id in row[:3])
            if not name:
                return None
            raw_cost = row[3] or ""
            symbols = re.findall(r"o([0-9]+|[WUBRGCXYZS])", raw_cost)
            cost = "".join("{" + symbol + "}" for symbol in symbols) if "".join("o" + symbol for symbol in symbols) == raw_cost else ""
            types = tuple(t for t in head.split() if t.lower() in {"artifact", "battle", "creature", "enchantment", "instant", "land", "planeswalker", "sorcery", "kindred", "tribal"})
            return CardInfo(name, head + (" — " + subtypes if subtypes else ""), cost, types)
        except sqlite3.Error:
            return None

    # -- queries -----------------------------------------------------------

    def lookup(self, grp_id: int) -> Optional[CardInfo]:
        """Resolve a MTGA grpId to card info, or ``None`` if unknown."""
        if not isinstance(grp_id, int) or isinstance(grp_id, bool):
            return None
        with self._lock:
            row = None
            if self._conn is not None:
                try:
                    row = self._conn.execute(
                        "SELECT name, type_line, mana_cost, card_types, " + ("oracle_text" if self._has_oracle else "''") + " FROM cards WHERE arena_id = ?",
                        (grp_id,),
                    ).fetchone()
                except sqlite3.Error as exc:
                    logger.warning("CardDb: lookup failed for %s: %s", grp_id, exc)
            if row is None:
                native = self._arena_lookup(grp_id)
                if native is not None:
                    if self._conn is not None and self._has_oracle:
                        # Exact card name only: never infer rules from a similar
                        # printing name or an adjacent Arena ID.
                        rules = self._conn.execute("SELECT oracle_text FROM cards WHERE name=? AND oracle_text != '' LIMIT 1", (native.name,)).fetchone()
                        if rules:
                            native = replace(native, oracle_text=rules[0])
                    return native
                self._log_missing(grp_id)
                return None
        name, type_line, mana_cost, card_types_csv, oracle_text = row
        return CardInfo(
            name=name,
            type_line=type_line,
            mana_cost=mana_cost,
            card_types=_csv_to_tuple(card_types_csv),
            oracle_text=oracle_text or "",
        )

    def counts(self) -> Tuple[int, int]:
        """Diagnostics: ``(total_rows, resolved_sample)``.

        ``resolved_sample`` is how many of a small probe of well-known arena
        ids currently resolve; useful for detecting a truncated/partial cache.
        """
        with self._lock:
            if self._conn is None:
                return (0, 0)
            total = int(self._conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0])
        probe = (75553, 81286, 69493, 9135, 103511)
        resolved = sum(1 for gid in probe if self.lookup(gid) is not None)
        return (total, resolved)

    def as_resolver(self):
        """Return a memoized (grp_id -> card_name) resolver for GameStateBuilder."""
        cache: dict[int, Optional[str]] = {}

        def resolve(grp_id: int) -> Optional[str]:
            if not isinstance(grp_id, int) or isinstance(grp_id, bool):
                return None
            if grp_id in cache:
                return cache[grp_id]
            try:
                info = self.lookup(grp_id)
            except Exception:
                info = None
            name = getattr(info, "name", None)
            cache[grp_id] = name if isinstance(name, str) and name else None
            return cache[grp_id]

        return resolve

    def as_info_resolver(self):
        """Return a memoized (grp_id -> CardInfo) resolver."""
        cache: dict[int, Optional[CardInfo]] = {}

        def resolve(grp_id: int) -> Optional[CardInfo]:
            if not isinstance(grp_id, int) or isinstance(grp_id, bool):
                return None
            if grp_id in cache:
                return cache[grp_id]
            try:
                info = self.lookup(grp_id)
            except Exception:
                info = None
            cache[grp_id] = info
            return info

        return resolve

    # -- internals ----------------------------------------------------------

    def _log_missing(self, grp_id: int) -> None:
        if grp_id in self._missing_logged:
            return
        self._missing_logged.add(grp_id)
        logger.debug("CardDb: grpId %s not found in cache", grp_id)
