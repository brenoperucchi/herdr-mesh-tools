"""Explicit completion of a partial level-two round, on its frozen material."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import uuid


def digest(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"arquivo regular esperado: {path}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def manifest(round_dir):
    root = Path(round_dir)
    paths = []
    for name in ('diff.patch', 'index.patch', 'worktree.patch'):
        if (root / name).exists():
            paths.append(root / name)
    snapshot = root / '_snapshot'
    if snapshot.exists():
        paths.extend(p for p in snapshot.rglob('*') if not p.is_dir())
    paths.extend(root.glob('*/request.md'))
    if not paths:
        raise RuntimeError('rodada sem material congelado')
    for path in paths:
        if any(parent.is_symlink() for parent in path.parents if parent != root.parent):
            raise RuntimeError('material congelado atravessa diretório symlink')
    return {str(p.relative_to(root)): digest(p) for p in sorted(paths)}


def success(entry):
    return (entry.get('status') in ('idle', 'done')
            and type(entry.get('verdict_bytes')) is int and entry['verdict_bytes'] > 0)


@contextmanager
def reservation(root):
    fd = os.open(root / '.complement.lock', os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError('complemento já em andamento') from exc
        yield
    finally:
        os.close(fd)


def run(runtime, args, cwd):
    """runtime is the dispatcher module; all delivery uses its existing core."""
    core = runtime.core
    root = Path(args.complementar).resolve()
    if root.parent != Path(cwd).resolve() / '.herdr' / 'review' or not root.is_dir():
        raise RuntimeError('complemento deve apontar para rodada do próprio projeto')
    with reservation(root):
        path = root / 'metrics.json'
        try:
            metrics = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            raise RuntimeError(f'métricas inválidas: {exc}') from exc
        if not isinstance(metrics, dict) or not isinstance(metrics.get('reviewers'), dict):
            raise RuntimeError('métricas inválidas')
        if any(not isinstance(entry, dict) for entry in metrics['reviewers'].values()):
            raise RuntimeError('métricas de revisores inválidas')
        attempts = metrics.get('complements', [])
        if not isinstance(attempts, list) or any(not isinstance(a, dict) for a in attempts):
            raise RuntimeError('histórico de complementos inválido')
        if any(a.get('status') == 'in_progress' for a in attempts):
            raise RuntimeError('complemento anterior sem resultado confirmado (unknown); não reenviar')
        names = [f'{args.slug}-rev-1', f'{args.slug}-rev-2']
        if (metrics.get('slug') != args.slug or metrics.get('nivel') != 2
                or metrics.get('review_status') != 'parcial'
                or metrics.get('coverage_scope') != 'unit'
                or set(metrics.get('reviewers', {})) not in (set(names), {f'{args.slug}-rev', names[1]})):
            raise RuntimeError('complemento exige rodada nível 2 parcial de unidade do mesmo slug')
        names = list(metrics['reviewers'])
        frozen = metrics.get('material_manifest')
        if not frozen or manifest(root) != frozen:
            raise RuntimeError('material congelado mudou ou não possui hashes; não complementar')
        missing = [n for n in names if not success(metrics['reviewers'][n])]
        if len(missing) != 1 or set(missing) != set(metrics.get('missing_reviewers', [])):
            raise RuntimeError('métricas de faltantes inconsistentes')
        for name in names:
            if name not in missing:
                entry = metrics['reviewers'][name]
                if not entry.get('verdict_sha256') or digest(root / name / 'verdict.md') != entry['verdict_sha256']:
                    raise RuntimeError('parecer anterior mudou; não completar a cobertura')
        previous_hashes = {n: metrics['reviewers'][n]['verdict_sha256'] for n in names if n not in missing}
        selected = missing
        if args.revisor:
            name = next(n for n in names if n.endswith(('-rev', '-rev-1'))) if args.revisor == 'rev-1' else f'{args.slug}-rev-2'
            if name not in missing:
                raise RuntimeError('--revisor deve ser um dos faltantes desta rodada')
            selected = [name]
        group = runtime.review_groups.get_group(args.slug)
        binding = runtime.review_groups.resolve_binding(group, core, cwd) if group else None
        # A changed coordinator is a global authority failure, not a recipient
        # failure that partial dispatch could mask.
        if binding != metrics.get('group_binding'):
            raise RuntimeError('vínculo do coordenador mudou desde a rodada parcial')
        expected, refused, restores = {}, {}, {}
        for name in selected:
            try:
                restores[name] = core.ensure_required_agents(args.slug, cwd, [name], selected_only=True)
                info = runtime.review_candidate(name, cwd)
                if group:
                    runtime.review_groups.validate_member(binding, name, info, core)
                if core.effective_status(info) not in ('idle', 'done'):
                    raise RuntimeError('revisor não está idle/done')
                expected[name] = info
            except RuntimeError as exc:
                refused[name] = str(exc)
        attempt = {'id': str(uuid.uuid4()), 'started_at': core.now_iso(), 'selected': selected,
                   'restore': restores, 'refused': refused, 'status': 'in_progress'}
        metrics.setdefault('complements', []).append(attempt)
        try:
            if manifest(root) != frozen:
                raise RuntimeError('material mudou antes do complemento; envio recusado')
        except (OSError, RuntimeError) as exc:
            attempt.update(status='refused_before_send', error=str(exc), finished_at=core.now_iso())
            core.write_json_atomic(path, metrics)
            raise RuntimeError(str(exc)) from exc
        core.write_json_atomic(path, metrics)
        prompts = {n: 'Complemento explícito da rodada parcial; revise o mesmo material congelado. '
                   + runtime.prompt_text(str(root / n)) for n in expected}
        artifacts = {n: str(root / n / 'verdict.md') for n in expected}
        if prompts:
            try:
                result, detail, settled = runtime.dispatch_and_wait_all(
                    prompts, args.timeout, required_artifacts=artifacts,
                    expected_agents=expected, check_composition=False,
                    group_binding=binding, partial_preflight=True)
            except core.DispatchPreflightError as exc:
                result = {n: 'preflight_aborted' for n in expected}
                detail = {n: exc.as_dict() for n in expected}
                settled = {}
        else:
            result, detail, settled = {}, {}, {}
        for name in selected:
            previous = metrics['reviewers'][name]
            attempt.setdefault('previous', {})[name] = previous
            if name in refused:
                entry = {'dispatched': False, 'status': 'unavailable', 'reason': refused[name]}
            else:
                verdict = root / name / 'verdict.md'
                size = verdict.stat().st_size if verdict.is_file() and not verdict.is_symlink() else 0
                entry = {'dispatched': True, 'status': result[name], 'detail': detail.get(name),
                         'settle_ts': settled.get(name), 'verdict_bytes': size,
                         'dispatch_ts': attempt['started_at'],
                         'agent_session': expected[name]['agent_session']['value'],
                         'kind': expected[name].get('agent')}
                published = detail.get(name, {}).get('required_artifact') if isinstance(detail.get(name), dict) else None
                if success(entry) and published:
                    current = core._artifact_snapshot(str(verdict))
                    if not current or any(current.get(k) != published.get(k) for k in ('bytes', 'mtime_ns', 'inode')):
                        entry.update(status='artifact_changed', reason='parecer mudou após assentamento')
                if success(entry):
                    entry['verdict_sha256'] = digest(verdict)
                    entry['solution_contract'] = core.solution_contract_status(str(verdict), 'review')
                else:
                    entry['reason'] = f"{result[name]}: parecer novo não confirmado"
            metrics['reviewers'][name] = entry
        for name, expected_hash in previous_hashes.items():
            try:
                unchanged = digest(root / name / 'verdict.md') == expected_hash
            except RuntimeError:
                unchanged = False
            if not unchanged:
                metrics['reviewers'][name] = {'dispatched': False, 'status': 'artifact_changed',
                                              'reason': 'parecer anterior mudou durante complemento'}
        covered = [n for n in names if success(metrics['reviewers'][n])]
        status, exit_code = runtime.review_policy.outcome(names, covered)
        # Do not bless changed material even if both artifacts appeared.
        material_error = None
        try:
            if manifest(root) != frozen:
                material_error = 'material congelado mudou durante o complemento'
        except (OSError, RuntimeError) as exc:
            material_error = f'material não verificável após complemento: {exc}'
        if material_error:
            attempt['error'] = material_error
            metrics.update(material_changed=True, material_error=material_error)
            for entry in metrics['reviewers'].values():
                entry.update(coverage_valid=False, reason=material_error)
            covered = []
            status, exit_code = 'falha', 1
        metrics.update(review_status=status, approval=False, coverage_only=True,
                       missing_reviewers=[n for n in names if n not in covered],
                       requires_followup=status != 'completa')
        attempt.update(status=status, finished_at=core.now_iso())
        core.write_json_atomic(path, metrics)
        print(f'complemento: {status}; round dir: {root}; cobertura não é aprovação')
        return exit_code
