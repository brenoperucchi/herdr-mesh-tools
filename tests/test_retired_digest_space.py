"""The digest repository belongs to painel; its retired space must not return."""
import ast
from pathlib import Path


def entries():
    path=Path(__file__).resolve().parents[1]/'bin/herdr-bootstrap'
    tree=ast.parse(path.read_text())
    node=next(n.value for n in tree.body if isinstance(n,ast.Assign)
              and any(isinstance(t,ast.Name) and t.id=='SPACES' for t in n.targets))
    return {e.elts[0].value:e for e in node.elts}


def test_retired_digest_space_is_absent_but_repo_remains_in_painel():
    spaces=entries()
    assert 'digest' not in spaces
    assert 'digest-exec' not in ast.dump(spaces['painel'])
    assert 'painel-exec' in ast.dump(spaces['painel'])
    assert '/Devs/digest' in ast.dump(spaces['painel'])
    assert {'painel','strata-opencode','lastro-rust','herdr-mesh-tools'} <= spaces.keys()
