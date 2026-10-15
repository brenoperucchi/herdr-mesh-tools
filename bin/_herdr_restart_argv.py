"""Conservative Codex 0.162 interactive/resume argv normalization.

Unknown arity, wrappers and noninteractive commands are refused before the old
process is stopped. Initial prompts, attachments and old selectors are not
replayed into the resumed conversation.
"""
import os

VALUE_OPTIONS = {
    '-c', '--config', '-m', '--model', '-s', '--sandbox', '-C', '--cd',
    '-p', '--profile', '-a', '--ask-for-approval', '--add-dir', '--enable',
    '--disable', '--local-provider', '--remote', '--remote-auth-token-env',
}
SWITCHES = {
    '--search', '--no-daemon', '--oss', '--strict-config', '--approve-for-me',
    '--dangerously-bypass-approvals-and-sandbox', '--dangerously-bypass-hook-trust',
    '--no-alt-screen',
}
SELECTORS = {'--last', '--all', '--include-non-interactive'}
COMMANDS = {'agents','exec','e','review','login','logout','mcp','plugin','app-server',
            'remote-control','completion','update','doctor','sandbox','debug','apply','a',
            'queue','archive','delete','migrate-rollouts','unarchive','fork','cloud',
            'exec-server','features','help'}


def _option(token):
    if token.startswith('--') and '=' in token:
        return token.split('=',1)
    if len(token)>2 and token[:2] in (VALUE_OPTIONS | {'-i'}):
        return token[:2], token[2:].removeprefix('=')
    return token, None


def codex_options(argv):
    old=list(argv or [])
    if old and os.path.basename(old[0]) in ('codex','codex.exe'):
        old.pop(0)
    elif old and old[0] != 'resume' and not old[0].startswith('-'):
        raise ValueError('launcher Codex desconhecido; não é seguro reconstruir argv')
    retained=[]; i=0; resume=False; positionals=0
    while i<len(old):
        token=old[i]
        if token=='--':
            break
        if token=='resume' and positionals==0 and not resume:
            resume=True; i+=1; continue
        key,attached=_option(token)
        if key=='--worktree':
            raise ValueError('--worktree mudaria a raiz durante restart')
        if key in ('-i','--image'):
            i+=1
            # Attachments belong to the old prompt, never to resume. Everything
            # until the next option can be an attachment or that old prompt.
            while i<len(old) and not old[i].startswith('-'):
                i+=1
            continue
        if key in SELECTORS:
            if attached is not None:
                raise ValueError(f'opção sem valor recebeu valor: {key}')
            i+=1; continue
        if key in SWITCHES:
            if attached is not None:
                raise ValueError(f'opção sem valor recebeu valor: {key}')
            retained.append(token);i+=1;continue
        if key in VALUE_OPTIONS:
            size=1 if attached is not None else 2
            if size==2 and i+1>=len(old):
                raise ValueError(f'falta valor para {key}')
            value=attached if attached is not None else old[i+1]
            if not value:
                raise ValueError(f'valor vazio para {key}')
            model=key in ('-m','--model')
            config=key in ('-c','--config')
            if config and '=' not in value:
                raise ValueError(f'config sem chave=valor: {value}')
            overridden=config and value.split('=',1)[0].strip() in ('model','model_reasoning_effort')
            if not model and not overridden:
                retained.extend(old[i:i+size])
            i+=size;continue
        if token.startswith('-'):
            raise ValueError(f'opção não suportada no restart: {token}')
        if positionals==0 and not resume and token in COMMANDS:
            raise ValueError(f'subcomando não interativo recusado: {token}')
        positionals+=1
        if positionals>(2 if resume else 1):
            raise ValueError('posicionais ambíguos no argv anterior')
        i+=1  # Drop old session selector and/or old prompt, never replay them.
    return retained


def explicit_cwds(options):
    i=0
    while i<len(options):
        key,attached=_option(options[i])
        if key in VALUE_OPTIONS:
            value=attached if attached is not None else options[i+1]
            if key in ('-C','--cd'):
                yield value
            i+=1 if attached is not None else 2
        else:
            i+=1
