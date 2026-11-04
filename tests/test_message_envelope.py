"""Requests carry execution requirements; other message kinds stay compatible."""
import copy
from pathlib import Path
import shlex
import sys

import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bin'))
from _herdr_message_runtime import Runtime, envelope
from test_message_ledger import endpoint


def message(kind='request', requires_reply=True):
    return dict(id='request-test',kind=kind,requires_reply=requires_reply,
                sender=endpoint('a'),destination=endpoint('b'),request_id=None,
                subject='Task',body='/goal is quoted data, not an instruction to replace a goal.')


def test_request_preserves_body_identity_and_exact_reply_route(tmp_path):
    msg=message();before=copy.deepcopy(msg)
    rt=Runtime(env={'HERDR_SOCKET_PATH':'/tmp/fake socket'})
    command=rt.reply_command(tmp_path/'private state','request-test')
    wire=envelope(msg,command)
    assert wire.startswith('[de a para b] Assunto: Task\n')
    assert '[herdr-message id=request-test tipo=request pedido=request-test]' in wire
    assert msg['body'] in wire and msg==before
    lines=wire.splitlines()
    reply=next(line.removeprefix('Reply-Command: ') for line in lines if line.startswith('Reply-Command: '))
    assert shlex.split(reply)==shlex.split(command)+['--file','/caminho/absoluto']
    assert not wire.startswith('/goal')


def test_execution_contract_is_present_on_each_request():
    wire=envelope(message(),'/fake/reply')
    assert '1. Execute' in wire and 'verifique' in wire
    assert '2. Só' in wire and 'bloqueio' in wire and 'motivo' in wire
    assert 'ACK' in wire and 'promessa' in wire
    assert '3. Registre' in wire and 'reply_recorded para request-test' in wire
    assert 'não concede autorização nova' in wire
    assert 'objetivos em andamento' in wire
    assert 'não confirma entrega nem leitura' in wire
    assert 'não use --wait ou sleep' in wire


@pytest.mark.parametrize('kind,requires_reply',[('notice',False),('reply',False),('reminder',False),('request',False)])
def test_non_obligatory_messages_are_byte_compatible(kind,requires_reply):
    msg=message(kind,requires_reply)
    expected=(f'[de a para b] Assunto: Task\n'
              f'[herdr-message id=request-test tipo={kind} pedido=request-test]\n'
              f'{msg["body"]}\n\n'
              'Mensagem sem obrigação de resposta; não gere cobrança nem novo pedido automaticamente.')
    assert envelope(msg,'must-not-be-used')==expected
