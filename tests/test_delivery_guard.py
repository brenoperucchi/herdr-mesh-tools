"""The bootstrap may create a role, but must not hydrate a refused pane."""
import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path
import sys
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
import _herdr_dispatch as core


@pytest.mark.parametrize("code", [1, 2, 3, 4])
def test_delivery_guard_refuses_every_nonzero_code(code):
    with mock.patch.object(core.subprocess, "run", return_value=mock.Mock(
        returncode=code, stdout="guard refused", stderr=""
    )):
        with pytest.raises(RuntimeError, match=f"código {code}"):
            core.require_can_send("test-rev-1")


def test_delivery_guard_uses_same_session_environment():
    with mock.patch.dict(core.os.environ, {"HERDR_SESSION": "isolated-review-test"}), \
         mock.patch.object(core.subprocess, "run", return_value=mock.Mock(returncode=0)) as run:
        core.require_can_send("test-rev-1")
    assert run.call_args.args[0][-2:] == ["test-rev-1", "--json"]
    # No replacement env is passed: the child guard inherits HERDR_SESSION,
    # which its imported core translates into --session for every read.
    assert "env" not in run.call_args.kwargs


def test_bootstrap_cannot_submit_after_guard_refusal():
    path = Path(core.__file__).parent / "herdr-bootstrap"
    loader = SourceFileLoader("bootstrap_delivery_test", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    boot = importlib.util.module_from_spec(spec)
    loader.exec_module(boot)
    with mock.patch.object(boot.core, "guard_delivery", side_effect=RuntimeError("refused")), \
         mock.patch.object(boot.subprocess, "run") as run:
        with pytest.raises(RuntimeError, match="refused"):
            boot.api("agent", "prompt", "test-rev-1", "hydrate")
    run.assert_not_called()


@pytest.mark.parametrize('output', [
    '{"codigo":0,"pode_enviar":true,"pane":"other:pane"}',
    '{"codigo":1,"pode_enviar":false,"pane":"w:p1"}',
    '',
])
def test_zero_exit_does_not_override_wrong_pane_or_malformed_verdict(output):
    with mock.patch.object(core.subprocess,'run',return_value=mock.Mock(returncode=0,stdout=output)):
        with pytest.raises(RuntimeError): core.require_can_send('target',expected_pane='w:p1')


def test_guard_timeout_refuses_delivery():
    with mock.patch.object(core.subprocess,'run',side_effect=core.subprocess.TimeoutExpired('guard',30)):
        with pytest.raises(RuntimeError,match='leitura falhou'): core.require_can_send('target')


@pytest.mark.parametrize('args', [
    ('agent','prompt','target','text'),('pane','run','w:p1','/status'),
    ('pane','send-text','w:p1','text'),
])
def test_core_delivery_paths_do_not_submit_after_refusal(args):
    info={'name':'target','pane_id':'w:p1'}
    with mock.patch.object(core,'get_agent_info',return_value=info), \
         mock.patch.object(core,'require_can_send',side_effect=RuntimeError('refused')), \
         mock.patch.object(core.subprocess,'run') as run:
        with pytest.raises(RuntimeError):core.api(*args)
    run.assert_not_called()


def test_refused_native_probe_cannot_discard_a_human_draft():
    with mock.patch.object(core,'require_can_send',side_effect=RuntimeError('draft')), \
         mock.patch.object(core,'_herdr_allow_empty') as native:
        with pytest.raises(RuntimeError):core._discard_headless_composition('w:p1')
    native.assert_not_called()


def test_refused_native_delivery_never_sends_escape_cleanup():
    with mock.patch.object(core,'guard_delivery',side_effect=core.DeliveryRefusedError('human draft')), \
         mock.patch.object(core.subprocess,'run') as run:
        with pytest.raises(core.ContextResetError,match='composição preservada'):
            core._prompt_native_headless('target','w:p1','/status')
    run.assert_not_called()
