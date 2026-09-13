"""Kind e args de um papel tem que combinar no herdr-bootstrap.

Regressao real (2026-09-12): cinco execs foram migrados de Claude pra Codex ao
vivo com herdr-swap, e SPACES ficou pra tras declarando ("x-exec", "claude",
EXEC_ARGS). Rodar o bootstrap teria recriado os cinco em Claude/Sonnet e
desfeito a migracao em silencio -- sem erro, sem aviso, so o mesh voltando
sozinho pra conta errada.

O teste nao sabe qual kind cada exec DEVE ter (isso e' decisao do usuario); ele
so garante que kind e args nao se contradigam, que e' a forma que o erro tomou.
"""

import importlib.util
import os
import unittest
from importlib.machinery import SourceFileLoader

BIN_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")


def _load(name):
    path = os.path.join(BIN_DIR, name)
    loader = SourceFileLoader(name.replace("-", "_"), path)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


boot = _load("herdr-bootstrap")

# Flags que so existem numa CLI. `--effort` e' do Claude Code; `-c chave=valor`
# e' do Codex. Passar um pro outro nao da erro visivel: a CLI ignora ou trata
# como prompt, e o agent sobe com o modelo/effort default -- exatamente o tipo
# de silencio que este teste existe pra quebrar.
SO_CLAUDE = {"--effort"}
SO_CODEX = {"-c"}


KINDS = {"claude", "codex"}


def _papeis():
    """Todos os (nome, kind, args) declarados em SPACES.

    As tuplas de SPACES nao tem aridade fixa -- llm-gateway carrega um 5o campo
    (override de slug, uma str). Filtrar por FORMA em vez de por posicao: so
    tupla de 3 cujo [1] e' um kind conhecido. Iterar cru por space[2:] fazia a
    str virar tres caracteres e o teste falhava com 'l nao pina --model'.
    """
    for space in boot.SPACES:
        for entrada in space[2:]:
            if not isinstance(entrada, tuple) or len(entrada) != 3:
                continue
            nome, kind, args = entrada
            if kind not in KINDS:
                continue
            yield nome, kind, list(args or [])


class BootstrapKindArgsTests(unittest.TestCase):
    def test_nenhum_papel_mistura_flags_de_cli(self):
        for nome, kind, args in _papeis():
            with self.subTest(papel=nome, kind=kind):
                usadas = {a for a in args if a.startswith("-")}
                if kind == "claude":
                    self.assertFalse(usadas & SO_CODEX,
                                     f"{nome} e' claude mas usa flag do Codex: {usadas & SO_CODEX}")
                elif kind == "codex":
                    self.assertFalse(usadas & SO_CLAUDE,
                                     f"{nome} e' codex mas usa flag do Claude: {usadas & SO_CLAUDE}")

    def test_todo_papel_pina_model_e_effort(self):
        """A regra de 2026-09-09: model E effort explicitos em TODO papel.

        Sem isso o papel herda ~/.claude/settings.json ou ~/.codex/config.toml,
        que o /model interativo reescreve -- foi assim que dois revisores
        acordaram em Fable 5.1, o modelo mais caro do catalogo.
        """
        for nome, kind, args in _papeis():
            with self.subTest(papel=nome):
                self.assertIn("--model", args, f"{nome} nao pina --model")
                tem_effort = "--effort" in args or any(
                    "reasoning_effort" in a for a in args)
                self.assertTrue(tem_effort, f"{nome} nao pina effort")


if __name__ == "__main__":
    unittest.main()
