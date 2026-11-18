import importlib.util
import os
import sys
from importlib.machinery import SourceFileLoader
from unittest import mock

BIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")
sys.path.insert(0, BIN)
loader = SourceFileLoader("hcs", os.path.join(BIN, "herdr-codex-handshake"))
spec = importlib.util.spec_from_loader(loader.name, loader)
cli = importlib.util.module_from_spec(spec)
loader.exec_module(cli)


import contextlib

import pytest

_REAL_PROVEN = cli.review_wake.proven_caller_context  # antes do fixture autouse trocar


def _ctx(pane):
    @contextlib.contextmanager
    def ctx(environ, runtime_factory=None):
        yield pane
    return ctx


@pytest.fixture(autouse=True)
def allowed_caller(monkeypatch):
    # Os testes abaixo exercitam o fluxo; a autorizacao tem testes proprios mais adiante.
    monkeypatch.setattr(cli.review_wake, "proven_caller_context", _ctx("pane-x"))
    monkeypatch.setattr(cli, "caller_identity", lambda pane: ("x-exec", "/r"))


def test_split_name():
    assert cli.split_name("lastro-rust-rev-1") == ("lastro-rust", "rev-1")
    assert cli.split_name("llm-gateway-exec") == ("llm-gateway", "exec")
    assert cli.split_name("whatever") == (None, None)


def test_agent_with_session_is_a_noop_and_sends_nothing(capsys):
    info = {"agent": "codex", "agent_status": "idle", "interactive_ready": True,
            "agent_session": {"value": "s"}, "cwd": "/r"}
    with mock.patch.object(cli.core, "get_agent_info", return_value=info), \
         mock.patch.object(cli.core, "project_root", return_value="/r"), \
         mock.patch.object(cli.hs, "ensure_session") as ensure:
        assert cli.main(["x-exec"]) == 0
    ensure.assert_not_called()
    assert "nada a fazer" in capsys.readouterr().out


def test_sessionless_codex_goes_through_ensure_session(capsys):
    info = {"agent": "codex", "agent_status": "idle", "interactive_ready": True,
            "agent_session": None, "cwd": "/r", "terminal_id": "T"}
    done = {"agent_session": {"value": "new"}}
    with mock.patch.object(cli.core, "get_agent_info", return_value=info), \
         mock.patch.object(cli.core, "project_root", return_value="/r"), \
         mock.patch.object(cli.hs, "ensure_session", return_value=done) as ensure:
        assert cli.main(["x-rev-1"]) == 0
    assert ensure.call_args[0][:3] == ("x-rev-1", "/r", "x")
    assert "new" in capsys.readouterr().out


def test_root_comes_from_the_invoker_not_from_the_target(capsys, monkeypatch):
    # achado r187/rev-2 P2: raiz derivada do proprio alvo tornava a validacao de projeto uma tautologia.
    info = {"agent": "codex", "agent_status": "idle", "interactive_ready": True,
            "agent_session": None, "cwd": "/outro", "terminal_id": "T"}
    seen = {}
    monkeypatch.setattr(cli.os, "getcwd", lambda: "/invocador")
    monkeypatch.setattr(cli, "caller_identity", lambda pane: ("x-exec", "/invocador"))
    with mock.patch.object(cli.core, "get_agent_info", return_value=info), \
         mock.patch.object(cli.core, "project_root", side_effect=lambda p: p), \
         mock.patch.object(cli.hs, "ensure_session", side_effect=lambda n, r, *a, **k: seen.update(root=r) or {"agent_session": {"value": "v"}}):
        cli.main(["x-rev-1"])
    assert seen["root"] == "/invocador"


def test_refusal_is_exit_1_and_bad_args_exit_2(capsys):
    info = {"agent": "codex", "agent_status": "idle", "interactive_ready": True,
            "agent_session": None, "cwd": "/r", "terminal_id": "T"}
    with mock.patch.object(cli.core, "get_agent_info", return_value=info), \
         mock.patch.object(cli.core, "project_root", return_value="/r"), \
         mock.patch.object(cli.hs, "ensure_session", side_effect=RuntimeError("não reenviar")):
        assert cli.main(["x-rev-1"]) == 1
    assert cli.main([]) == 2 and cli.main(["--x"]) == 2 and cli.main(["foo"]) == 2


# --- decisao do coordenador 18/11 12:20: quem pode rodar -------------------------------------
@pytest.mark.parametrize("caller,target,ok", [
    ("x-exec", "x-rev-1", True),               # proprio space
    ("x-rev-2", "x-rev-1", True),
    ("herdr-exec", "other-rev-1", True),       # dono da reciclagem
    ("coordenador", "other-exec", True),
    ("y-exec", "x-rev-1", False),              # outro space
    ("cerebro-vigia", "x-rev-1", False),
    (None, "x-rev-1", False),                  # nao comprovado
    ("", "x-rev-1", False),
])
def test_authorize_caller(caller, target, ok):
    assert (cli.hs.authorize_caller(target, caller) is None) is ok


def test_cli_refuses_other_space_before_touching_the_target(monkeypatch, capsys):
    monkeypatch.setattr(cli, "caller_identity", lambda pane: ("y-exec", "/r"))
    with mock.patch.object(cli.core, "get_agent_info") as get, mock.patch.object(cli.hs, "ensure_session") as ensure:
        assert cli.main(["x-rev-1"]) == 1
    get.assert_not_called()
    ensure.assert_not_called()
    assert "recusado" in capsys.readouterr().err


def test_cli_refuses_unproven_caller(monkeypatch):
    monkeypatch.setattr(cli.review_wake, "proven_caller_context", _ctx(None))
    with mock.patch.object(cli.hs, "ensure_session") as ensure:
        assert cli.main(["x-rev-1"]) == 1
    ensure.assert_not_called()


# --- handshake dentro do herdr-agent-restart (decisao do coordenador, item 2) ------------------
class FakeCore:
    def __init__(self, info):
        self._info = info
        self.waited = []

    def get_agent_info(self, n):
        return self._info

    def project_root(self, p):
        return "/r"

    def wait_quiescent(self, n, **k):
        self.waited.append(n)


def test_after_restart_noop_when_session_is_present():
    c = FakeCore({"agent_session": {"value": "s"}, "pane_id": "p", "terminal_id": "T"})
    assert cli.hs.after_restart("x-rev-1", "p", "T", c, None) == ("has_session", None)
    assert c.waited == []


def test_after_restart_runs_handshake_for_sessionless_codex_and_never_raises():
    c = FakeCore({"agent_session": None, "cwd": "/r", "pane_id": "p", "terminal_id": "T"})
    with mock.patch.object(cli.hs, "ensure_session", return_value={"agent_session": {"value": "n"}, "pane_id": "p", "terminal_id": "T"}) as ensure:
        assert cli.hs.after_restart("x-rev-1", "p", "T", c, None) == ("done", "n")
    assert ensure.call_args[1]["expected_pane"] == "p" and ensure.call_args[1]["expected_terminal"] == "T"
    assert c.waited == ["x-rev-1"] and ensure.call_args[0][:3] == ("x-rev-1", "/r", "x")
    with mock.patch.object(cli.hs, "ensure_session", side_effect=RuntimeError("não reenviar")):
        status, detail = cli.hs.after_restart("x-rev-1", "p", "T", c, None)
    assert status == "failed" and "não reenviar" in detail




# --- achados r191 ----------------------------------------------------------------------------
def test_after_restart_never_hydrates_a_name_that_resolves_elsewhere():
    for other in ({"pane_id": "w9:p9", "terminal_id": "T"}, {"pane_id": "p", "terminal_id": "OTHER"}):
        c = FakeCore({"agent_session": None, "cwd": "/other", **other})
        with mock.patch.object(cli.hs, "ensure_session") as ensure:
            status, detail = cli.hs.after_restart("x-rev-1", "p", "T", c, None)
        assert status == "failed" and "outro pane" in detail
        ensure.assert_not_called()
    with mock.patch.object(cli.hs, "ensure_session") as ensure:
        assert cli.hs.after_restart("x-rev-1", None, "T", FakeCore({}), None)[0] == "skipped"
        assert cli.hs.after_restart("x-rev-1", "p", None, FakeCore({}), None)[0] == "skipped"
    ensure.assert_not_called()


def test_ensure_session_expected_binding_refuses_other_pane_without_sending(tmp_path):
    import _herdr_review_handshake as hs
    info = {"name": "x-rev-1", "agent": "codex", "pane_id": "w9:p9", "terminal_id": "T", "agent_session": None,
            "interactive_ready": True, "agent_status": "idle"}
    import contextlib as cl

    class C:
        def validate_agent_project(self, n, r):
            return info

        @cl.contextmanager
        def _dispatch_submission_locks(self, a):
            yield

        def _dispatch_differences(self, a, b, include_lifecycle=True):
            return []
    rg = type("RG", (), {"group_for_agent": staticmethod(lambda n: None)})
    with pytest.raises(RuntimeError, match="outro pane"):
        hs.ensure_session("x-rev-1", str(tmp_path), "x", [], C(), rg, expected_pane="w1:p1")


def test_hint_env_var_must_match_the_native_proof_and_proof_failure_refuses():
    class w:  # noqa: N801
        proven_caller_context = staticmethod(_REAL_PROVEN)
    rt = lambda: type("R", (), {"caller": lambda self: {"pane_id": "w:p7"}, "env": {}})()
    with w.proven_caller_context({"HERDR_PANE_ID": "w:p7"}, rt) as pane:
        assert pane == "w:p7"
    with w.proven_caller_context({}, rt) as pane:
        assert pane == "w:p7"
    with w.proven_caller_context({"HERDR_PANE_ID": "w:COORD"}, rt) as pane:   # pista forjada
        assert pane is None
    def boom():
        raise RuntimeError("identity_unproven")
    with w.proven_caller_context({"HERDR_PANE_ID": "w:p7"}, boom) as pane:    # sem prova, mesmo com a pista
        assert pane is None


def test_route_of_the_proof_stays_for_the_whole_block_and_is_restored(monkeypatch):
    class w:  # noqa: N801
        proven_caller_context = staticmethod(_REAL_PROVEN)
    for k in ("HERDR_SOCKET_PATH", "HERDR_CONFIG_PATH", "HERDR_SESSION"):
        monkeypatch.delenv(k, raising=False)
    rt = lambda: type("R", (), {"caller": lambda self: {"pane_id": "p"}, "env": {"HERDR_SOCKET_PATH": "/A.sock"}})()
    with w.proven_caller_context({}, rt):
        assert os.environ["HERDR_SOCKET_PATH"] == "/A.sock"
    assert "HERDR_SOCKET_PATH" not in os.environ


def test_same_space_prefix_without_the_same_project_is_refused(monkeypatch, capsys):
    # achado r191 rev-1: o prefixo do nome nao prova associacao ao space.
    monkeypatch.setattr(cli, "caller_identity", lambda pane: ("x-scout", "/outro-projeto"))
    info = {"agent": "codex", "agent_status": "idle", "interactive_ready": True,
            "agent_session": None, "cwd": "/r", "terminal_id": "T"}
    with mock.patch.object(cli.core, "get_agent_info", return_value=info), \
         mock.patch.object(cli.core, "project_root", side_effect=lambda p: p or "/r"), \
         mock.patch.object(cli.os, "getcwd", return_value="/r"), \
         mock.patch.object(cli.hs, "ensure_session") as ensure:
        assert cli.main(["x-rev-1"]) == 1
    ensure.assert_not_called()
    assert "projeto" in capsys.readouterr().err


# --- achados r192 ----------------------------------------------------------------------------
def test_expected_binding_is_checked_before_the_early_return_when_a_session_already_exists(tmp_path):
    import _herdr_review_handshake as hs
    other = {"name": "x-rev-1", "agent": "codex", "pane_id": "other:p9", "terminal_id": "T9",
             "agent_session": {"value": "native-B"}, "interactive_ready": True, "agent_status": "idle"}

    class C:
        def validate_agent_project(self, n, r):
            return other
    rg = type("RG", (), {"group_for_agent": staticmethod(lambda n: None)})
    with pytest.raises(RuntimeError, match="outro pane"):
        hs.ensure_session("x-rev-1", str(tmp_path), "x", [], C(), rg, expected_pane="w1:p1", expected_terminal="T1")


def test_after_restart_does_not_attribute_another_identitys_session_to_the_target():
    base = {"agent_session": None, "cwd": "/r", "pane_id": "p", "terminal_id": "T"}
    c = FakeCore(base)
    bad = {"agent_session": {"value": "native-B"}, "pane_id": "other:p9", "terminal_id": "T9"}
    with mock.patch.object(cli.hs, "ensure_session", return_value=bad):
        status, detail = cli.hs.after_restart("x-rev-1", "p", "T", c, None)
    assert status == "failed" and "não corresponde" in detail


def test_after_restart_refuses_when_the_project_changed_after_the_restart():
    c = FakeCore({"agent_session": None, "cwd": "/replacement", "pane_id": "p", "terminal_id": "T"})
    c.project_root = lambda p: p
    with mock.patch.object(cli.hs, "ensure_session") as ensure:
        status, detail = cli.hs.after_restart("x-rev-1", "p", "T", c, None, expected_cwd="/original")
    assert status == "failed" and "projeto" in detail
    ensure.assert_not_called()
