"""Run with: python -m pytest tests  (no card DB or network needed)."""
import os
os.environ.setdefault("DATA_DIR", "/tmp/deckweb-test")
import mtg


def test_quantities():
    e, _, _ = mtg.parse_text("8x Forest\n8 Island\n8 x Swamp\nSol Ring")
    assert [(x["qty"], x["name"]) for x in e] == [(8, "Forest"), (8, "Island"), (8, "Swamp"), (1, "Sol Ring")]


def test_markers():
    e, _, _ = mtg.parse_text("1x Sol Ring (c21) 263 [Ramp,Commander{top}] ^Have,#37d67a^ *F*")
    assert e[0]["name"] == "Sol Ring" and e[0]["set"] == "c21" and e[0]["number"] == "263"
    assert e[0]["tags"] == ["Ramp", "Commander"]


def test_tappedout_blocks_and_inline():
    e, _, w = mtg.parse_text("#ramp\n1x Cultivate\n\n#draw\n1x Cultivate #value\n1x Brainstorm *CMDR*")
    assert len(e) == 2 and e[0]["tags"] == ["ramp", "value", "draw"] and len(w) == 1
    assert "Commander" in e[1]["tags"]


def test_sections():
    e, _, _ = mtg.parse_text("Commander\n1 Atraxa, Praetors' Voice\n\nDeck\n1 Sol Ring")
    assert e[0]["tags"] == ["Commander"] and e[1]["tags"] == []


def test_normalize():
    assert mtg.normalize("Sneak-Attack") == mtg.normalize("sneak attack")
    assert "delver of secrets" in mtg.name_keys("Delver of Secrets // Insectile Aberration")
