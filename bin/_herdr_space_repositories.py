"""Pure SPACES repository/launch previews. Never writes CLI trust configuration."""
import os
from pathlib import Path
import _herdr_trust as trust


def repositories(entry):
    opts=entry[5] if len(entry)>5 else {}
    if not isinstance(opts,dict):
        raise RuntimeError('opções de SPACES devem ser um dict')
    additional=opts.get('additional_repositories',[])
    if not isinstance(additional,list) or not all(isinstance(p,str) for p in additional):
        raise RuntimeError('additional_repositories deve ser lista de caminhos absolutos')
    result=[]
    for path in [entry[1],*additional]:
        if not isinstance(path,str) or not os.path.isabs(path):
            raise RuntimeError('repositório deve ter caminho absoluto')
        try:
            resolved=str(trust.validate_path_policy(path,Path.home()))
        except trust.TrustError as exc:
            raise RuntimeError(str(exc)) from exc
        if resolved not in result:result.append(resolved)
    return result


def launch_args(entry,name,kind,extra,*,review_repo=None):
    args=list(extra)
    opts=entry[5] if len(entry)>5 else {}
    if not isinstance(opts,dict):raise RuntimeError('opções de SPACES devem ser um dict')
    if not review_repo and not opts.get('additional_repositories'):
        return args  # Preserve legacy no-Git and custom startup profiles.
    roots=repositories(entry)
    selected=None
    if review_repo is not None:
        if not os.path.isabs(review_repo):raise RuntimeError('alvo de revisão deve ser absoluto')
        selected=os.path.realpath(review_repo)
        if selected not in roots:raise RuntimeError('alvo de revisão fora de SPACES')
    if kind!='claude':return args
    exec_name=entry[2][0] if entry[2] else None
    slug=entry[4] if len(entry)>4 else entry[0].lower()
    dirs=roots[1:] if name==exec_name else (
        [selected] if selected and selected!=roots[0] and name in
        {f'{slug}-rev',f'{slug}-rev-1',f'{slug}-rev-2'} else [])
    if not dirs:return args
    if any(a=='--add-dir' or a.startswith('--add-dir=') for a in args):
        raise RuntimeError('perfil já declara --add-dir; reconciliar com o cadastro antes da subida')
    if any(not Path(p).is_dir() for p in dirs):
        raise RuntimeError('diretório adicional cadastrado não existe')
    return [*args,'--add-dir',*dirs]
