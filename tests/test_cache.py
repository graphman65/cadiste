from __future__ import annotations

import os
from pathlib import Path

from cadiste.cache import TraceCache


def _builder(outputs: list[bytes | None], shapes: bool = False):
    calls: list[int] = []

    def make(shapes_file: Path) -> bytes | None:
        calls.append(1)
        output = outputs[min(len(calls), len(outputs)) - 1]
        if shapes and output is not None:
            shapes_file.write_bytes(b"shapes of " + output)
        return output

    return make, calls


def test_a_trace_is_built_once_until_the_model_source_changes(tmp_path: Path):
    source = tmp_path / "model"
    source.mkdir()
    module = source / "part.py"
    module.write_text("WIDTH = 1\n")
    cache = TraceCache(tmp_path / "cache", [source], salt="")
    make, calls = _builder([b"first", b"second"])

    assert cache.get_or_make("trace", "Bracket", make)[:2] == (b"first", False)
    assert cache.get_or_make("trace", "Bracket", make)[:2] == (b"first", True)
    assert len(calls) == 1

    module.write_text("WIDTH = 22\n")  # an edit: another size
    assert cache.get_or_make("trace", "Bracket", make)[:2] == (b"second", False)
    # The entry made for the old source is gone, not left to pile up.
    assert len(list((tmp_path / "cache").glob("trace-Bracket-*.json"))) == 1


def test_an_edit_that_keeps_the_size_still_invalidates(tmp_path: Path):
    source = tmp_path / "model"
    source.mkdir()
    module = source / "part.py"
    module.write_text("WIDTH = 1\n")
    cache = TraceCache(tmp_path / "cache", [source], salt="")
    make, _ = _builder([b"first", b"second"])
    cache.get_or_make("trace", "Bracket", make)

    module.write_text("WIDTH = 2\n")
    stat = module.stat()
    os.utime(module, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    assert cache.get_or_make("trace", "Bracket", make)[:2] == (b"second", False)


def test_fresh_builds_again_and_a_failed_build_is_not_kept(tmp_path: Path):
    source = tmp_path / "model.py"
    source.write_text("")
    cache = TraceCache(tmp_path / "cache", [source], salt="")
    make, calls = _builder([None, b"built", b"rebuilt"])

    assert cache.get_or_make("trace", "Bracket", make) == (None, False, None)
    assert cache.get_or_make("trace", "Bracket", make)[:2] == (b"built", False)
    assert cache.get_or_make("trace", "Bracket", make, fresh=True)[:2] == (b"rebuilt", False)
    assert cache.get_or_make("trace", "Bracket", make)[:2] == (b"rebuilt", True)
    assert len(calls) == 3


def test_entries_are_separate_per_part_and_per_salt(tmp_path: Path):
    source = tmp_path / "model.py"
    source.write_text("")
    make, _ = _builder([b"a", b"b", b"c"])
    cache = TraceCache(tmp_path / "cache", [source], salt="env=1")

    assert cache.get_or_make("trace", "Bracket", make)[0] == b"a"
    assert cache.get_or_make("trace", "Bracket 2", make)[0] == b"b"
    other_environment = TraceCache(tmp_path / "cache", [source], salt="env=2")
    assert other_environment.get_or_make("trace", "Bracket", make)[:2] == (b"c", False)


def test_a_build_keeps_its_shapes_with_it_and_drops_them_with_it(tmp_path: Path):
    source = tmp_path / "model.py"
    source.write_text("")
    cache = TraceCache(tmp_path / "cache", [source], salt="")
    make, _ = _builder([b"first", b"second"], shapes=True)

    _, _, shapes = cache.get_or_make("trace", "Bracket", make)
    assert shapes is not None and shapes.read_bytes() == b"shapes of first"
    assert cache.get_or_make("trace", "Bracket", make)[1:] == (True, shapes)

    source.write_text("WIDTH = 2\n")
    _, _, rebuilt = cache.get_or_make("trace", "Bracket", make)
    assert rebuilt is not None and rebuilt.read_bytes() == b"shapes of second"
    assert not shapes.exists()  # the old build's shapes went with its JSON
    assert sorted(p.name for p in (tmp_path / "cache").iterdir()) == sorted([rebuilt.name, rebuilt.stem + ".json"])
