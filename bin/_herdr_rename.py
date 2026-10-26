"""Mecânica de arquivos do `herdr-rename-space`: o que muda no disco quando um projeto troca de
pasta/nome. Nada aqui fala com o Herdr; tudo recebe `home` para poder ser testado numa HOME falsa.

Ideia central: o histórico do Claude e do Codex é indexado pelo CAMINHO da pasta. Claude guarda as
sessões em `~/.claude/projects/<caminho com tudo que não é [A-Za-z0-9] trocado por '-'>/` e o Codex
em `threads.cwd` (sqlite) + `cwd` dentro dos rollouts. Medido em 2026-10-14: `claude --resume <id>`
e `codex resume <id>` acham a sessão pela id mesmo depois do `mv`; reescrever o histórico serve para
ELAS APARECEREM no seletor da pasta nova (Claude lista por pasta de projeto, Codex filtra por cwd).

Regras que valem para tudo:
- só se troca PREFIXO de caminho inteiro (`/a/b` casa `/a/b` e `/a/b/c`, nunca `/a/bc`);
- nenhuma escrita sem backup antes; escrita sempre atômica (tmp + replace);
- o mtime dos arquivos de histórico é preservado (a ordem do seletor não muda);
- conflito (destino já existe) nunca sobrescreve: vira item em `conflitos` e o original fica.
"""
import datetime
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess

CODIFICA_RE = re.compile(r"[^A-Za-z0-9]")


def codifica(caminho):
    """Nome da pasta de projeto do Claude para um cwd."""
    return CODIFICA_RE.sub("-", caminho)


class Mapa:
    """Pares (prefixo antigo -> prefixo novo); o prefixo mais longo vence."""

    def __init__(self, pares):
        pares = [(a.rstrip("/"), b.rstrip("/")) for a, b in pares if a.rstrip("/") != b.rstrip("/")]
        self.ordem = pares                       # ordem dada: o 1o par e' o principal (ganha conflitos)
        self.pares = sorted(pares, key=lambda p: -len(p[0]))

    def __bool__(self):
        return bool(self.pares)

    def aplica(self, caminho):
        """Caminho novo, ou None se nenhum prefixo casa."""
        for velho, novo in self.pares:
            if caminho == velho:
                return novo
            if caminho.startswith(velho + "/"):
                return novo + caminho[len(velho):]
        return None

    def prioridade(self, caminho):
        """Posicao, na ordem dada, do par que casa `caminho` (menor = mais principal)."""
        for velho, _ in self.pares:
            if caminho == velho or caminho.startswith(velho + "/"):
                return self.ordem.index(next(p for p in self.ordem if p[0] == velho))
        return len(self.ordem)

    def regex(self):
        alt = "|".join(re.escape(v) for v, _ in self.pares)
        return re.compile(r'(?P<ant>"(?:cwd|project)"\s*:\s*")(?P<cam>' + alt + r')(?=[/"])')

    def reescreve_texto(self, texto):
        """Troca nos campos "cwd"/"project" de um trecho JSON; devolve (texto, n_trocas)."""
        if not self.pares:
            return texto, 0
        n = [0]

        def sub(m):
            n[0] += 1
            return m.group("ant") + self.aplica(m.group("cam"))
        return self.regex().sub(sub, texto), n[0]


# ------------------------------------------------------------------ utilidades de escrita
def agora():
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


def backup(_home, origem, pasta_backup):
    """Copia `origem` (arquivo) para a pasta de backup; devolve o destino."""
    os.makedirs(pasta_backup, exist_ok=True)
    destino = os.path.join(pasta_backup, os.path.basename(origem))
    n = 1
    while os.path.exists(destino):        # dois arquivos com o mesmo nome (categories.json de repos distintos)
        n += 1
        destino = os.path.join(pasta_backup, f"{os.path.basename(origem)}.{n}")
    shutil.copy2(origem, destino)
    return destino


def gravar_atomico(caminho, conteudo, manter_mtime=False):
    st = os.stat(caminho) if os.path.exists(caminho) else None
    tmp = f"{caminho}.rename-tmp-{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(conteudo)
    if st is not None:
        os.chmod(tmp, st.st_mode & 0o7777)
    os.replace(tmp, caminho)
    if manter_mtime and st is not None:
        os.utime(caminho, ns=(st.st_atime_ns, st.st_mtime_ns))


def reescreve_arquivo_cwd(caminho, mapa, aplicar):
    """Reescreve "cwd"/"project" num .jsonl linha a linha (streaming). Devolve n_trocas."""
    total = 0
    if not aplicar:
        with open(caminho, encoding="utf-8", errors="surrogateescape") as f:
            for linha in f:
                total += mapa.reescreve_texto(linha)[1]
        return total
    st = os.stat(caminho)
    tmp = f"{caminho}.rename-tmp-{os.getpid()}"
    with open(caminho, encoding="utf-8", errors="surrogateescape") as f, \
            open(tmp, "w", encoding="utf-8", errors="surrogateescape") as g:
        for linha in f:
            nova, n = mapa.reescreve_texto(linha)
            total += n
            g.write(nova)
    if total == 0:
        os.unlink(tmp)
        return 0
    os.chmod(tmp, st.st_mode & 0o7777)
    os.replace(tmp, caminho)
    os.utime(caminho, ns=(st.st_atime_ns, st.st_mtime_ns))
    return total


# ------------------------------------------------------------------ Claude
def _cwd_dominante(pasta):
    """cwd mais frequente nas primeiras linhas dos .jsonl de uma pasta de projeto do Claude."""
    cont = {}
    try:
        arquivos = [a for a in os.listdir(pasta) if a.endswith(".jsonl")]
    except OSError:
        return None
    for a in arquivos[:40]:
        try:
            with open(os.path.join(pasta, a), encoding="utf-8", errors="replace") as f:
                for i, linha in enumerate(f):
                    if i > 60:
                        break
                    m = re.search(r'"cwd"\s*:\s*"([^"]+)"', linha)
                    if m:
                        cont[m.group(1)] = cont.get(m.group(1), 0) + 1
        except OSError:
            continue
    # o "cwd de origem" da pasta e' o que, codificado, casa com o nome dela
    nome = os.path.basename(pasta)
    for c, _ in sorted(cont.items(), key=lambda kv: -kv[1]):
        if codifica(c) == nome:
            return c
    return None


def pastas_claude(home, mapa):
    """[(pasta_atual, cwd_de_origem, pasta_destino)] das pastas de projeto que o mapa move."""
    base = os.path.join(home, ".claude", "projects")
    if not os.path.isdir(base):
        return []
    prefixos = [codifica(v) for v, _ in mapa.pares]
    saida = []
    for nome in sorted(os.listdir(base)):
        pasta = os.path.join(base, nome)
        if not os.path.isdir(pasta):
            continue
        if not any(nome == p or nome.startswith(p + "-") for p in prefixos):
            continue
        cwd = _cwd_dominante(pasta)
        if cwd is None:
            # sem .jsonl legivel: so' vale se o NOME e' exatamente o de um prefixo antigo
            cwd = next((v for v, _ in mapa.pares if codifica(v) == nome), None)
        if cwd is None:
            continue
        novo = mapa.aplica(cwd)
        if novo is None:
            continue
        saida.append((pasta, cwd, os.path.join(base, codifica(novo))))
    saida.sort(key=lambda t: (mapa.prioridade(t[1]), t[0]))   # o principal entra primeiro e ganha conflitos
    return saida


def mescla_arvore(origem, destino, conflitos, aplicar):
    """Move `origem` para dentro de `destino` sem sobrescrever nada. Devolve n arquivos movidos."""
    movidos = 0
    if not os.path.exists(destino):
        if aplicar:
            os.makedirs(os.path.dirname(destino), exist_ok=True)
            os.rename(origem, destino)
        return sum(len(fs) for _, _, fs in os.walk(origem)) or 1
    for nome in sorted(os.listdir(origem)):
        o, d = os.path.join(origem, nome), os.path.join(destino, nome)
        if os.path.isdir(o) and not os.path.islink(o) and os.path.isdir(d):
            movidos += mescla_arvore(o, d, conflitos, aplicar)
        elif os.path.lexists(d):
            conflitos.append(f"{o} -> {d} (destino ja existe; origem mantida)")
        else:
            if aplicar:
                os.rename(o, d)
            movidos += 1
    if aplicar:
        try:
            os.rmdir(origem)
        except OSError:
            pass
    return movidos


def migra_historico_claude(home, mapa, pasta_backup, aplicar):
    """Move/mescla as pastas de projeto e reescreve o cwd dentro dos .jsonl."""
    rel = {"pastas": [], "cwd_trocados": 0, "conflitos": []}
    for pasta, cwd, destino in pastas_claude(home, mapa):
        itens = {"de": pasta, "para": destino, "cwd": cwd}
        itens["arquivos"] = mescla_arvore(pasta, destino, rel["conflitos"], aplicar)
        rel["pastas"].append(itens)
        alvo = destino if aplicar else pasta
        for raiz, _, arquivos in os.walk(alvo):
            for a in arquivos:
                if a.endswith(".jsonl"):
                    rel["cwd_trocados"] += reescreve_arquivo_cwd(os.path.join(raiz, a), mapa, aplicar)
    hist = os.path.join(home, ".claude", "history.jsonl")
    rel["history_trocas"] = 0
    if os.path.exists(hist):
        if aplicar:
            backup(home, hist, pasta_backup)
        rel["history_trocas"] = reescreve_arquivo_cwd(hist, mapa, aplicar)
    return rel


def migra_claude_json(home, mapa, apagar_antigas, pasta_backup, aplicar):
    """Chaves de `projects` em ~/.claude.json (confianca da pasta, MCPs). `apagar_antigas`: caminhos
    cuja chave pode sumir (a pasta antiga deixou de existir); as demais ficam."""
    caminho = os.path.join(home, ".claude.json")
    rel = {"chaves": [], "conflitos": []}
    if not os.path.exists(caminho):
        return rel
    bruto = open(caminho, encoding="utf-8").read()
    dados = json.loads(bruto)
    proj = dados.get("projects")
    if not isinstance(proj, dict):
        return rel
    # a pasta principal (1o par do mapa) vem primeiro e ganha as chaves em conflito; so' depois as absorvidas
    for chave in sorted(proj, key=lambda k: (mapa.prioridade(k), k)):
        novo = mapa.aplica(chave)
        if novo is None:
            continue
        rel["chaves"].append((chave, novo))
        if novo in proj:
            proj[novo] = {**proj[chave], **proj[novo]}   # o que ja existe no destino vence
        else:
            proj[novo] = proj[chave] if chave in apagar_antigas else json.loads(json.dumps(proj[chave]))
        if chave in apagar_antigas:
            del proj[chave]
    if aplicar and rel["chaves"]:
        backup(home, caminho, pasta_backup)
        indent = 2 if bruto.startswith('{\n  "') else None
        saida = json.dumps(dados, indent=indent, ensure_ascii=False) + ("\n" if bruto.endswith("\n") else "")
        json.loads(saida)  # valida antes de gravar
        if hashlib.sha256(open(caminho, "rb").read()).hexdigest() != hashlib.sha256(bruto.encode()).hexdigest():
            raise RuntimeError("~/.claude.json mudou durante a leitura (outro Claude escreveu); rode de novo")
        gravar_atomico(caminho, saida)
    return rel


# ------------------------------------------------------------------ Codex
def migra_codex(home, mapa, pasta_backup, aplicar, renomear_config=None):
    rel = {"threads": 0, "rollouts_trocas": 0, "roots": 0, "config": [], "conflitos": []}
    db_path = os.path.join(home, ".codex", "state_5.sqlite")
    rollouts = []
    if os.path.exists(db_path):
        db = sqlite3.connect(db_path, timeout=30)
        try:
            alvos = []
            for velho, novo in mapa.pares:
                n = len(velho)
                for tid, cwd, rp in db.execute(
                        "select id, cwd, rollout_path from threads where substr(cwd,1,?)=? "
                        "and (length(cwd)=? or substr(cwd,?,1)='/')", (n, velho, n, n + 1)):
                    alvos.append((tid, cwd, rp))
            vistos = {}
            for tid, cwd, rp in alvos:
                vistos.setdefault(tid, (cwd, rp))
            rel["threads"] = len(vistos)
            rollouts = [rp for _, rp in vistos.values() if rp]
            if aplicar and vistos:
                bkp = os.path.join(pasta_backup, "state_5.sqlite")
                os.makedirs(pasta_backup, exist_ok=True)
                dst = sqlite3.connect(bkp)
                db.backup(dst)
                dst.close()
                db.execute("begin immediate")
                for tid, (cwd, _) in vistos.items():
                    db.execute("update threads set cwd=? where id=?", (mapa.aplica(cwd), tid))
                for velho, novo in mapa.pares:
                    n = len(velho)
                    rel["roots"] += db.execute(
                        "update project_roots set path=? || substr(path,?) where substr(path,1,?)=? "
                        "and (length(path)=? or substr(path,?,1)='/')", (novo, n + 1, n, velho, n, n + 1)).rowcount
                db.commit()
        finally:
            db.close()
    for rp in rollouts:
        if os.path.exists(rp):
            rel["rollouts_trocas"] += reescreve_arquivo_cwd(rp, mapa, aplicar)
    cfg = os.path.join(home, ".codex", "config.toml")
    if os.path.exists(cfg):
        texto = open(cfg, encoding="utf-8").read()
        novo_texto, trocas = reescreve_config_toml(texto, mapa, renomear_config)
        rel["config"] = trocas
        if aplicar and trocas:
            backup(home, cfg, pasta_backup)
            gravar_atomico(cfg, novo_texto)
    return rel


CABECALHO_TOML = re.compile(r'^\[projects\."(?P<cam>(?:[^"\\]|\\.)*)"\]\s*$')


def reescreve_config_toml(texto, mapa, renomear=None):
    """Troca os cabecalhos `[projects."<caminho>"]` dos caminhos em `renomear` (default: todos os do
    mapa); se o destino ja tem bloco, descarta o antigo. Caminhos que continuam existindo (a pasta de
    runtime de que so' o historico foi absorvido) ficam como estao."""
    linhas = texto.splitlines(keepends=True)
    existentes = {m.group("cam") for l in linhas if (m := CABECALHO_TOML.match(l.rstrip("\n")))}
    saida, trocas, pular = [], [], False
    for l in linhas:
        m = CABECALHO_TOML.match(l.rstrip("\n"))
        if m:
            pular = False
            novo = mapa.aplica(m.group("cam"))
            if novo is not None and (renomear is None or m.group("cam") in renomear):
                if novo in existentes:
                    trocas.append((m.group("cam"), novo, "descartado (destino ja tem bloco)"))
                    pular = True
                    continue
                existentes.add(novo)
                trocas.append((m.group("cam"), novo, "renomeado"))
                saida.append(f'[projects."{novo}"]\n')
                continue
        elif l.startswith("["):
            pular = False
        if not pular:
            saida.append(l)
    return "".join(saida), trocas


# ------------------------------------------------------------------ texto simples (units, inventario, ...)
def substitui_caminho(texto, velho, novo):
    """Troca `velho` por `novo` so' quando e' caminho inteiro (fim ou separador depois)."""
    padrao = re.compile(re.escape(velho) + r"(?![A-Za-z0-9_.\-])")
    return padrao.subn(lambda _: novo, texto)


def edita_texto(caminho, funcao, pasta_backup, aplicar):
    """Aplica `funcao(texto) -> (texto, n)` ao arquivo; devolve n. Faz backup antes de gravar."""
    try:
        texto = open(caminho, encoding="utf-8").read()
    except (OSError, UnicodeDecodeError):
        return 0
    novo, n = funcao(texto)
    if n and aplicar:
        backup(None, caminho, pasta_backup)
        gravar_atomico(caminho, novo)
    return n


UNIT_SUFIXOS = (".service", ".timer", ".path", ".socket", ".container", ".volume", ".network", ".pod", ".kube")


def _pastas_de_units(home):
    """systemd --user e quadlets do podman (os .container viram units depois do daemon-reload)."""
    return [os.path.join(home, ".config", "systemd", "user"), os.path.join(home, ".config", "containers", "systemd")]


def units_afetadas(home, velho, novo, pasta_backup, aplicar):
    """Units/quadlets que citam o caminho antigo. Devolve [(arquivo, n_trocas)]."""
    saida = []
    for base in _pastas_de_units(home):
        if not os.path.isdir(base):
            continue
        for nome in sorted(os.listdir(base)):
            if not nome.endswith(UNIT_SUFIXOS):
                continue
            caminho = os.path.join(base, nome)
            if not os.path.isfile(caminho) or os.path.islink(caminho):
                continue
            n = edita_texto(caminho, lambda t: substitui_caminho(t, velho, novo), pasta_backup, aplicar)
            if n:
                saida.append((nome, n))
    return saida


def substitui_nome(texto, velho, novo):
    """Troca um NOME de agent so' como token inteiro (`dre-exec` nao casa `xdre-exec` nem `dre-exec2`)."""
    padrao = re.compile(r"(?<![A-Za-z0-9_-])" + re.escape(velho) + r"(?![A-Za-z0-9_-])")
    return padrao.subn(lambda _: novo, texto)


def units_com_nomes(home, nomes, pasta_backup, aplicar):
    """Units que citam nomes de agent (ex.: `--agents a-exec,b-exec` do narrador do Guardian).
    `nomes` = [(velho, novo)]. Devolve [(arquivo, n_trocas)]."""
    saida = []
    base = os.path.join(home, ".config", "systemd", "user")
    if not os.path.isdir(base):
        return saida
    for nome in sorted(os.listdir(base)):
        caminho = os.path.join(base, nome)
        if not nome.endswith((".service", ".timer", ".path", ".socket")) or not os.path.isfile(caminho) \
                or os.path.islink(caminho):
            continue

        def fn(t):
            total = 0
            for v, n in nomes:
                if v != n:
                    t, k = substitui_nome(t, v, n)
                    total += k
            return t, total
        n = edita_texto(caminho, fn, pasta_backup, aplicar)
        if n:
            saida.append((nome, n))
    return saida


def symlinks_para(home, velho, novo, aplicar, raizes=None):
    """Symlinks (em pastas de config/bin conhecidas) cujo alvo cai dentro de `velho`."""
    raizes = raizes or [".local/bin", ".agents/skills", ".claude/skills", ".claude/commands",
                        ".claude/agents", ".codex/skills", ".config/herdr/plugins"]
    mapa = Mapa([(velho, novo)])
    achados = []
    for r in raizes:
        base = os.path.join(home, r)
        if not os.path.isdir(base):
            continue
        for raiz, dirs, arquivos in os.walk(base):
            profundidade = os.path.relpath(raiz, base).count(os.sep)
            if profundidade >= 2:
                dirs[:] = []
            for nome in list(dirs) + arquivos:
                p = os.path.join(raiz, nome)
                if not os.path.islink(p):
                    continue
                alvo = os.readlink(p)
                if os.path.isabs(alvo) and (m := mapa.aplica(alvo)):
                    achados.append((p, alvo, m))
                    if aplicar:
                        tmp = p + f".rename-tmp-{os.getpid()}"
                        os.symlink(m, tmp)
                        os.replace(tmp, p)
    return achados


def worktrees_do_git(raiz_git):
    """Caminhos dos worktrees ligados (lidos de .git/worktrees/*/gitdir), para o `worktree repair`."""
    base = os.path.join(raiz_git, ".git", "worktrees")
    saida = []
    if not os.path.isdir(base):
        return saida
    for nome in sorted(os.listdir(base)):
        try:
            gitdir = open(os.path.join(base, nome, "gitdir"), encoding="utf-8").read().strip()
        except OSError:
            continue
        wt = os.path.dirname(gitdir)
        if os.path.isdir(wt):
            saida.append(wt)
    return saida


def git_worktree_repair(raiz_git, worktrees):
    """`git worktree repair` na raiz nova, passando os worktrees ligados. Devolve (ok, saida)."""
    r = subprocess.run(["git", "-C", raiz_git, "worktree", "repair", *worktrees],
                       capture_output=True, text=True, timeout=60)
    return r.returncode == 0, (r.stdout + r.stderr).strip()


# ------------------------------------------------------------------ referencias que sobram (so' relatorio)
def referencias_restantes(velhos, raizes, excluir=("node_modules", ".git", "__pycache__", ".venv", "review")):
    """grep -rIl dos caminhos/nomes antigos em `raizes` (relatorio; nunca edita)."""
    achados = {}
    for v in velhos:
        for raiz in raizes:
            if not os.path.exists(raiz):
                continue
            cmd = ["grep", "-rIlF"] + [f"--exclude-dir={e}" for e in excluir] + ["--", v, raiz]
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            except (OSError, subprocess.TimeoutExpired):
                continue
            for linha in r.stdout.splitlines():
                achados.setdefault(linha, set()).add(v)
    return {k: sorted(v) for k, v in sorted(achados.items())}
