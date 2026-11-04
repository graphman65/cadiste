from __future__ import annotations

from pathlib import Path

from cadiste.references import PREFIX, References, parse


def test_a_pasted_reference_is_found_with_its_label_and_after_a_restart(tmp_path: Path):
    reference = References(tmp_path).create({"label": "Steering Module: #7 CrossPrism", "text": "..."})
    pasted = f"make this thinner {PREFIX}{reference} (Steering Module: #7 CrossPrism), keep the hole"

    found = parse(pasted)
    assert found == reference
    record = References(tmp_path).get(reference)  # another server, the same cache directory
    assert record is not None
    assert (record["id"], record["label"]) == (reference, "Steering Module: #7 CrossPrism")


def test_a_bare_id_is_a_reference_and_an_unknown_one_finds_nothing(tmp_path: Path):
    references = References(tmp_path)
    reference = references.create({})

    assert parse(reference) == reference
    assert references.get("0" * 8 if reference != "0" * 8 else "1" * 8) is None
    assert references.get("../../etc/passwd") is None
