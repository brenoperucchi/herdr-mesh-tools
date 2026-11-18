import contextlib
import importlib.machinery
import importlib.util
import os
import sys
from unittest import mock

import pytest

BIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")
sys.path.insert(0, BIN)
import _herdr_restart_guard as g  # noqa: E402
import _herdr_review_handshake as hs  # noqa: E402


class Core:
    def __init__(self, caller):
        self.caller = caller

    def get_agent_info(self, pane):
        if self.caller is None:
            raise RuntimeError("agent_not_found")
        return self.caller

    def project_root(self, p):
        return p


TARGET = {"cwd": "/p/x", "foreground_cwd": "/p/x"}


@pytest.mark.parametrize("caller,ok", [
    ({"name": "x-exec", "cwd": "/p/x"}, True),
    ({"name": "x-scout", "cwd": "/p/outro"}, False),      # prefixo sem o mesmo projeto
    ({"name": "y-exec", "cwd": "/p/y"}, False),            # outro space
    ({"name": "herdr-exec", "cwd": "/p/h"}, True),
    ({"name": "coordenador", "cwd": "/p/c"}, True),
    (None, False),                                          # leitura falhou
])
def test_authorize(caller, ok):
    assert (g.authorize("x-rev-1", TARGET, "pane", Core(caller), hs) is None) is ok


def test_unproven_caller_is_refused_even_with_privileged_hint():
    assert g.authorize("x-rev-1", TARGET, None, Core({"name": "coordenador", "cwd": "/"}), hs)


class Pipe:
    def __init__(self, line):
        self._line = line

    def readline(self):
        return self._line


class Proc:
    def __init__(self, line):
        self.stdout = Pipe(line)


def test_wait_ready_only_accepts_the_ready_line():
    sel = lambda r, w, x, t: (r, [], [])
    assert g.wait_ready(Proc(b"READY\n"), 1, sel) == (True, "READY")
    assert g.wait_ready(Proc(b""), 1, sel) == (False, "")           # helper saiu sem dizer nada
    assert g.wait_ready(Proc(b"RECUSADO: outro space\n"), 1, sel) == (False, "RECUSADO: outro space")
    assert g.wait_ready(Proc(b"READY\n"), 1, lambda r, w, x, t: ([], [], [])) == (False, "")   # prazo


def test_signal_ready_writes_and_silences_the_terminal():
    import subprocess
    code = ("import sys; sys.path.insert(0, %r); import _herdr_restart_guard as g; g.signal_ready(); "
            "print('depois'); sys.stderr.write('erro')" % BIN)
    r = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, timeout=20)
    assert r.stdout == b"READY\n" and r.stderr == b""


def _load():
    loader = importlib.machinery.SourceFileLoader("restart_guard_cli", os.path.join(BIN, "herdr-agent-restart"))
    spec = importlib.util.spec_from_loader("restart_guard_cli", loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


def _wire(m, monkeypatch, caller_pane, caller_info, agent_pane="w:p1"):
    info = {"name": "x-rev-1", "pane_id": agent_pane, "agent": "codex", "agent_status": "idle", "cwd": "/p/x",
            "foreground_cwd": "/p/x", "agent_session": {"value": "S"}}

    @contextlib.contextmanager
    def ctx(environ, runtime_factory=None):
        yield caller_pane
    monkeypatch.setattr(m.review_wake, "proven_caller_context", ctx)
    monkeypatch.setattr(m, "agentes", lambda: [info])
    monkeypatch.setattr(m, "processos", lambda: {agent_pane: (10, "codex", ["codex"])})
    monkeypatch.setattr(m.probe_cache, "ler", lambda: {})
    monkeypatch.setattr(m.core, "get_agent_info", lambda pane: caller_info)
    monkeypatch.setattr(m.core, "project_root", lambda p: p)
    monkeypatch.setattr(m, "validar_codex", lambda *a, **k: None)
    monkeypatch.setattr(m, "checar_seguranca", lambda i: [])
    monkeypatch.delenv("HERDR_PANE_ID", raising=False)               # sem a pista de ambiente
    return info


def test_own_pane_is_recognized_by_the_proof_not_by_the_env_var(monkeypatch, capsys):
    m = _load()
    _wire(m, monkeypatch, "w:p1", {"name": "x-rev-1", "cwd": "/p/x"})
    monkeypatch.setattr(sys, "argv", ["herdr-agent-restart", "x-rev-1", "--session", "S"])
    helper = mock.Mock()
    with mock.patch.object(m.subprocess, "Popen", return_value=helper) as popen, \
         mock.patch.object(m.restart_guard, "wait_ready", return_value=(True, "READY")), \
         mock.patch.object(m, "reiniciar") as rest:
        assert m.main() == 0
    rest.assert_not_called()                                         # nada sincrono no proprio pane
    assert popen.call_args[0][0][-3:-1] == ["--_separado", "--session"] or "--_separado" in popen.call_args[0][0]
    assert "proprio pane" in capsys.readouterr().out


def test_own_pane_helper_that_does_not_confirm_fails_without_restarting(monkeypatch, capsys):
    m = _load()
    _wire(m, monkeypatch, "w:p1", {"name": "x-rev-1", "cwd": "/p/x"})
    monkeypatch.setattr(sys, "argv", ["herdr-agent-restart", "x-rev-1", "--session", "S"])
    helper = mock.Mock()
    with mock.patch.object(m.subprocess, "Popen", return_value=helper), \
         mock.patch.object(m.restart_guard, "wait_ready", return_value=(False, "RECUSADO: outro space")), \
         mock.patch.object(m, "reiniciar") as rest:
        assert m.main() == 1
    assert "RECUSADO: outro space" in capsys.readouterr().err      # o motivo do helper chega a quem rodou
    helper.kill.assert_called_once()
    rest.assert_not_called()


def test_other_authorized_pane_keeps_the_normal_synchronous_path(monkeypatch):
    m = _load()
    _wire(m, monkeypatch, "w:p9", {"name": "herdr-exec", "cwd": "/p/h"})
    monkeypatch.setattr(sys, "argv", ["herdr-agent-restart", "x-rev-1", "--session", "S"])
    with mock.patch.object(m.subprocess, "Popen") as popen, mock.patch.object(m, "reiniciar", return_value=(False, "x")) as rest:
        m.main()
    popen.assert_not_called()
    rest.assert_called_once()


def test_separado_helper_refuses_a_foreign_caller_and_never_signals_or_restarts(monkeypatch, capsys):
    m = _load()
    _wire(m, monkeypatch, "w:p9", {"name": "y-exec", "cwd": "/p/y"})
    monkeypatch.setattr(sys, "argv", ["herdr-agent-restart", "x-rev-1", "--force", "--_separado", "--session", "S"])
    with mock.patch.object(m.restart_guard, "signal_ready") as ready, mock.patch.object(m, "reiniciar") as rest:
        assert m.main() == 1
    ready.assert_not_called()
    rest.assert_not_called()


def test_separado_helper_authorized_signals_waits_then_restarts_with_fresh_reads(monkeypatch):
    m = _load()
    _wire(m, monkeypatch, "w:p1", {"name": "x-rev-1", "cwd": "/p/x"})
    monkeypatch.setattr(sys, "argv", ["herdr-agent-restart", "x-rev-1", "--force", "--_separado", "--session", "S"])
    order = []
    with mock.patch.object(m.restart_guard, "signal_ready", side_effect=lambda: order.append("ready")), \
         mock.patch.object(m.time, "sleep", side_effect=lambda s: order.append("sleep")), \
         mock.patch.object(m, "reiniciar", side_effect=lambda *a, **k: order.append("restart") or (False, "x")):
        m.main()
    assert order[:3] == ["ready", "sleep", "restart"]


def test_cli_refuses_before_any_kill_and_dry_run_is_not_guarded(monkeypatch, capsys):
    m = _load()
    _wire(m, monkeypatch, None, None)                                # sem prova
    monkeypatch.setattr(sys, "argv", ["herdr-agent-restart", "x-rev-1", "--session", "S"])
    with mock.patch.object(m, "reiniciar") as rest, mock.patch.object(m, "encerrar") as kill:
        assert m.main() == 1
    rest.assert_not_called()
    kill.assert_not_called()
    assert "RECUSADO" in capsys.readouterr().err
    monkeypatch.setattr(sys, "argv", ["herdr-agent-restart", "x-rev-1", "--session", "S", "--dry-run"])
    assert m.main() == 0


def test_occupant_replaced_during_the_delay_is_refused_before_any_restart(monkeypatch, capsys):
    # r196 rev-1: o novo ocupante do pane nao herda a autorizacao do chamador original.
    m = _load()
    info = _wire(m, monkeypatch, "w:p1", {"name": "x-rev-1", "cwd": "/p/x"})
    monkeypatch.setattr(sys, "argv", ["herdr-agent-restart", "x-rev-1", "--force", "--_separado", "--session", "S"])
    current = {"v": dict(info)}
    monkeypatch.setattr(m, "agentes", lambda: [current["v"]])
    state = {"caller": {"name": "x-rev-1", "cwd": "/p/x"}}
    monkeypatch.setattr(m.core, "get_agent_info", lambda pane: state["caller"])

    def swap(_s):
        current["v"] = dict(info, name="y-exec", cwd="/p/y", foreground_cwd="/p/y", agent_session={"value": "new-y"})
        state["caller"] = {"name": "y-exec", "cwd": "/p/y"}
    with mock.patch.object(m.restart_guard, "signal_ready"), mock.patch.object(m.time, "sleep", side_effect=swap), \
         mock.patch.object(m, "achar_alvo", side_effect=lambda a, ag: ag[0]), \
         mock.patch.object(m, "reiniciar") as rest, mock.patch.object(m, "encerrar") as kill:
        assert m.main() == 1
    rest.assert_not_called()
    kill.assert_not_called()
    assert "mudou durante o atraso" in capsys.readouterr().err


@pytest.mark.parametrize("field,value", [("terminal_id", "OUTRO"), ("agent", "claude"), ("cwd", "/p/x2"),
                                          ("agent_session", {"value": "outra"})])
def test_any_identity_change_during_the_delay_is_refused(monkeypatch, field, value, capsys):
    m = _load()
    info = _wire(m, monkeypatch, "w:p1", {"name": "x-rev-1", "cwd": "/p/x"})
    info["terminal_id"] = "T"
    monkeypatch.setattr(sys, "argv", ["herdr-agent-restart", "x-rev-1", "--force", "--_separado", "--session", "S"])
    cur = {"v": info}
    monkeypatch.setattr(m, "agentes", lambda: [cur["v"]])
    with mock.patch.object(m.restart_guard, "signal_ready"), \
         mock.patch.object(m.time, "sleep", side_effect=lambda s: cur.update(v=dict(info, **{field: value}))), \
         mock.patch.object(m, "reiniciar") as rest:
        assert m.main() == 1
    rest.assert_not_called()
    assert "mudou durante o atraso" in capsys.readouterr().err


def test_vanished_caller_during_the_delay_is_refused(monkeypatch):
    m = _load()
    _wire(m, monkeypatch, "w:p9", {"name": "herdr-exec", "cwd": "/p/h"})
    monkeypatch.setattr(sys, "argv", ["herdr-agent-restart", "x-rev-1", "--force", "--_separado", "--session", "S"])
    state = {"c": {"name": "herdr-exec", "cwd": "/p/h"}}
    monkeypatch.setattr(m.core, "get_agent_info", lambda pane: state["c"])
    with mock.patch.object(m.restart_guard, "signal_ready"), \
         mock.patch.object(m.time, "sleep", side_effect=lambda s: state.update(c={"name": "outro", "cwd": "/p/z"})), \
         mock.patch.object(m, "reiniciar") as rest:
        assert m.main() == 1
    rest.assert_not_called()



def test_restart_calls_the_handshake_only_for_codex_without_session():
    src = open(os.path.join(BIN, "herdr-agent-restart")).read()
    assert 'kind == "codex" and sid_novo is None' in src and "codex_handshake.after_restart" in src



def test_restart_block_binds_to_the_pre_restart_info_not_the_post_restart_read():
    src = open(os.path.join(BIN, "herdr-agent-restart")).read()
    block = src[src.index('if kind == "codex" and sid_novo is None'):]
    block = block[:block.index('print(f"handshake:')]
    assert 'info.get("terminal_id")' in block and 'novo.get("terminal_id")' not in block
    assert "expected_cwd=restart_identity.pane_cwd(info)" in block
