"""Rodada herdr-mesh-tools-186 (18/11): um revisor importou o modulo congelado, o Python criou
_snapshot/.../__pycache__/*.pyc e a rodada virou `falha: material congelado mudou` com os dois
pareceres validos. Bytecode cache nao e' material; qualquer outro arquivo novo continua sendo."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin"))
import _herdr_review_complement as complement  # noqa: E402


def _round(tmp_path):
    (tmp_path / "_snapshot" / "pkg").mkdir(parents=True)
    (tmp_path / "_snapshot" / "pkg" / "a.py").write_text("x=1\n")
    (tmp_path / "r-rev-1").mkdir()
    (tmp_path / "r-rev-1" / "request.md").write_text("req\n")
    return tmp_path


def test_pycache_created_by_a_reviewer_does_not_change_the_manifest(tmp_path):
    root = _round(tmp_path)
    frozen = complement.manifest(root)
    (root / "_snapshot" / "pkg" / "__pycache__").mkdir()
    (root / "_snapshot" / "pkg" / "__pycache__" / "a.cpython-314.pyc").write_bytes(b"\0\1")
    assert complement.manifest(root) == frozen


def test_any_other_new_or_changed_file_still_changes_the_manifest(tmp_path):
    root = _round(tmp_path)
    frozen = complement.manifest(root)
    (root / "_snapshot" / "pkg" / "extra.py").write_text("y=2\n")
    assert complement.manifest(root) != frozen
    (root / "_snapshot" / "pkg" / "extra.py").unlink()
    (root / "_snapshot" / "pkg" / "a.py").write_text("x=2\n")
    assert complement.manifest(root) != frozen
    (root / "_snapshot" / "pkg" / "a.py").write_text("x=1\n")
    (root / "_snapshot" / "pkg" / "notes.pyc").write_bytes(b"1")  # .pyc fora de __pycache__ conta
    assert complement.manifest(root) != frozen
