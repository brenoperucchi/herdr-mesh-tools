"""Isola o log de bloqueios de composicao: os testes usam panes falsos e nunca devem
escrever no log real (~/.local/state/herdr-mesh/compose-guard.log)."""
import tempfile

import pytest


@pytest.fixture(autouse=True)
def _isola_log_de_composicao(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setenv("HERDR_MESH_STATE_DIR", d)
        yield
