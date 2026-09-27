"""Player names on air: said only when they're regular names, otherwise by role."""
import pytest

from arenaonair.names import LOCAL, OPPONENT, on_air, replacements, scrub, spoken


# Real Arena screen names: said as they are, said more naturally, or not at all.
@pytest.mark.parametrize("name, said", [
    ("armour", "armour"), ("Sparky", "Sparky"), ("Samuel Fisher", "Samuel Fisher"), ("The Grifter", "The Grifter"),
    ("Melchizedeck", "Melchizedeck"), ("vuim", "vuim"), ("O'Brien", "O'Brien"), ("José", "José"),
    ("Schmidt", "Schmidt"), (" Garuck", "Garuck"),
    ("SorinMarkov", "Sorin Markov"), ("EatinBeans", "Eatin Beans"), ("SPARKY", "Sparky"),
    ("MikeUchiha18", None), ("Phoenixblaze420", None), ("2red", None), ("SBK", None), ("QUlRON", None),
    ("bnhjgsyeduligksr", None), ("gchiste", None), ("GalangN", None), ("xX_Slayer_Xx", None),
    ("Zzzap", None), ("田中", None), ("x", None), ("", None), (None, None),
])
def test_only_regular_names_are_said(name, said):
    assert spoken(name) == said


def test_players_without_a_sayable_name_are_called_by_role():
    subs = replacements({1: "SBK", 2: "MikeUchiha18", 3: "Alice"}, local_seat=1)
    assert subs == {"SBK": LOCAL, "MikeUchiha18": OPPONENT}
    text = "MikeUchiha18 casts Kinnan. Llanowar Elves settles in for mikeuchiha18! SBK's turn."
    assert scrub(text, subs) == "The opponent casts Kinnan. Llanowar Elves settles in for the opponent! Our player's turn."
    assert on_air({"name": "MikeUchiha18", "players": [{"name": "SBK"}]}, subs) == {
        "name": OPPONENT, "players": [{"name": LOCAL}]}
    assert scrub("MikeUchiha18x stays", subs) == "MikeUchiha18x stays"  # whole names only
    assert replacements({1: "SBK"}) == {"SBK": OPPONENT}  # local seat not known yet
