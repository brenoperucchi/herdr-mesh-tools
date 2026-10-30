"""Conservative, additive review-level policy; no Herdr side effects."""
import fnmatch
import json
import os
import subprocess


# Project patterns extend these defaults; they cannot remove a global trigger.
DEFAULT_TRIGGERS = {
    "config_seguranca": ["*_herdr_trust*", "*herdr-trust*", "*_herdr_startup*",
                         "*herdr-bootstrap*", "*herdr-add-space*", "*herdr-rename-space*",
                         "*_herdr_rename*", "*/.codex/config.toml", "*/.claude.json"],
    "segredos": [".env*", "*.pem", "*.key", "*secret*", "*credential*", "*secrets*"],
    "privado": ["*guardian*", "*mfc*", "*cerebro*", "*cérebro*"],
    "migracao": ["*migration*", "*migracao*", "*migração*", "*migrate*"],
    "producao": ["*.service", "*.timer", "*systemd*", "*gateway*", "*deploy*"],
    "contrato": ["*protocol*", "*protocolo*", "*schema*", "*contract*", "*contrato*",
                 "*herdr-review*", "*_herdr_review*", "*_herdr_dispatch*", "*_herdr_message*", "*herdr-message*",
                 "*review_policy*", "*review-gatilhos*", "*herdr-ask*"],
}


def _git(cwd, *args):
    try:
        result = subprocess.run(["git", "-C", cwd, *args], capture_output=True, env={**os.environ, "LC_ALL": "C"})
    except OSError as exc:
        raise RuntimeError(f"inventário de arquivos incerto; use nível 2: {exc}") from exc
    return result


def canonical_root(cwd):
    cwd = os.path.realpath(cwd)
    if not os.path.isdir(cwd):
        raise RuntimeError("diretório incerto; use nível 2")
    result = _git(cwd, "rev-parse", "--show-toplevel")
    if result.returncode == 0:
        return os.path.realpath(os.fsdecode(result.stdout).strip())
    # Files mode supports a genuine non-repository, not a broken or unsafe
    # repository whose project policy would otherwise disappear silently.
    probe = cwd
    marker_found = False
    while True:
        marker_found = marker_found or os.path.lexists(os.path.join(probe, ".git"))
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    if (result.stderr.startswith(b"fatal: not a git repository (") and not marker_found
            and not os.environ.get("GIT_DIR") and not os.environ.get("GIT_WORK_TREE")):
        return cwd
    raise RuntimeError("raiz Git incerta; use nível 2: " + result.stderr.decode(errors="replace").strip())


def changed_paths(cwd, base="HEAD", files=None):
    original_dir = os.path.abspath(cwd)
    cwd = canonical_root(cwd)
    if files is not None:
        paths = []
        for path in files:
            joined = os.path.join(original_dir, path)
            for candidate in (os.path.abspath(joined), os.path.realpath(joined)):
                relative = os.path.relpath(candidate, cwd).replace(os.sep, "/")
                if relative not in paths:
                    paths.append(relative)
        return paths
    paths = set()
    for command in (["diff", "--name-only", "--no-renames", "-z", base, "--"],
                    ["diff", "--cached", "--name-only", "--no-renames", "-z", base, "--"],
                    ["diff", "--name-only", "--no-renames", "-z", "--"],
                    ["ls-files", "--others", "--exclude-standard", "-z"]):
        result = _git(cwd, *command)
        if result.returncode:
            raise RuntimeError("inventário de arquivos incerto; use nível 2: "
                               + result.stderr.decode(errors="replace").strip())
        paths.update(os.fsdecode(p) for p in result.stdout.split(b"\0") if p)
    return sorted(paths)


def load_triggers(cwd):
    triggers = {name: list(patterns) for name, patterns in DEFAULT_TRIGGERS.items()}
    path = os.path.join(cwd, ".herdr", "review-gatilhos.json")
    try:
        os.lstat(path)
    except FileNotFoundError:
        parent = os.path.dirname(path)
        try:
            os.stat(parent)
        except FileNotFoundError:
            if os.path.lexists(parent):
                raise RuntimeError(f"{path}: configuração incerta; diretório de política não resolve")
        except OSError as exc:
            raise RuntimeError(f"{path}: configuração incerta; use nível 2: {exc}") from exc
        return triggers
    except OSError as exc:
        raise RuntimeError(f"{path}: configuração incerta; use nível 2: {exc}") from exc
    try:
        with open(path, encoding="utf-8") as stream:
            config = json.load(stream)
        if (not isinstance(config, dict) or set(config) != {"version", "gatilhos"}
                or type(config["version"]) is not int or config["version"] != 1
                or not isinstance(config["gatilhos"], dict)):
            raise ValueError('esperado {"version": 1, "gatilhos": {"nome": ["glob"]}}')
        for name, patterns in config["gatilhos"].items():
            if (not isinstance(name, str) or not name.strip() or not isinstance(patterns, list)
                    or not patterns or any(not isinstance(p, str) or not p.strip() for p in patterns)):
                raise ValueError("cada gatilho exige nome e lista não vazia de padrões")
            triggers.setdefault(name, []).extend(patterns)
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"{path}: configuração incerta; use nível 2: {exc}") from exc
    return triggers


def evaluate(cwd, *, level=2, justification=None, declared=(), base="HEAD", files=None):
    if type(level) is not int or level not in (1, 2):
        raise RuntimeError("nível deve ser 1 ou 2")
    if level == 1 and not (justification or "").strip():
        raise RuntimeError("--nivel 1 exige --justificativa não vazia")
    policy = {"nivel": level, "justificativa": (justification or "").strip(),
              "gatilhos": [], "paths": [], "policy_error": None}
    declared = list(declared)
    if any(not isinstance(n, str) for n in declared):
        raise RuntimeError("--gatilho exige nome textual")
    declared = list(dict.fromkeys(n.strip() for n in declared))
    if any(not n for n in declared):
        raise RuntimeError("--gatilho exige nome não vazio")
    matches = [{"nome": n, "origem": "declarado", "paths": []} for n in declared]
    try:
        original_dir = cwd
        cwd = canonical_root(cwd)
        paths = changed_paths(original_dir, base, files)
        triggers = load_triggers(cwd)
        policy["paths"] = paths
        # Absolute roots intentionally participate in ALL categories. E.g.
        # a gateway project stays production-sensitive even for readme edits;
        # a private project stays private even for an innocuous models.py.
        for name, patterns in triggers.items():
            hits = [p for p in paths if any(
                fnmatch.fnmatchcase(candidate.casefold(), pattern.casefold())
                for pattern in patterns for candidate in (
                    p, os.path.basename(p), os.path.join(cwd, p).replace(os.sep, "/"))) ]
            if hits:
                matches.append({"nome": name, "origem": "caminho", "paths": hits})
    except RuntimeError as exc:
        if level == 1:
            raise
        policy["policy_error"] = str(exc)
        matches.append({"nome": "incerteza", "origem": "policy_error", "paths": []})
    policy["gatilhos"] = matches
    if level == 1 and matches:
        raise RuntimeError("nível 2 obrigatório; gatilhos: " + ", ".join(sorted({m["nome"] for m in matches})))
    return policy


def outcome(expected, successful):
    """A usable artifact is coverage, not semantic approval of the change."""
    expected = set(expected)
    count = len(expected & set(successful))
    if expected and count == len(expected):
        return "completa", 0
    if count:
        return "parcial", 3
    return "falha", 1
