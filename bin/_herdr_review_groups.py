"""Opt-in review groups anchored to a live coordinator, independent of SPACES.

Lock order: group -> existing submission locks. Recovery subprocesses finish
before the dispatcher acquires its group transaction; never pass a lock to a
bootstrap child. Locks coordinate mesh clients, not manual daemon operations.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
import re
import time


@dataclass(frozen=True)
class ReviewGroup:
    slug: str
    coordinator_name: str
    project_root: str
    coordinator_kind: str = "claude"
    rev1_kind: str = "codex"
    rev2_kind: str = "claude"
    scout_kind: str | None = None

    def roles(self):
        roles = {"rev-1": self.rev1_kind, "rev-2": self.rev2_kind}
        if self.scout_kind:
            roles["scout"] = self.scout_kind
        return {f"{self.slug}-{role}": (role, kind) for role, kind in roles.items()}


GROUPS = {
    "guardian2": ReviewGroup(
        "guardian2", "guardian2-exec", "/home/brenoperucchi/Devs/guardian2"
    ),
}


def validate_registry(legacy_spaces=()):
    used = set()
    legacy = set()
    legacy_slugs = set()
    for entry in legacy_spaces:
        slug = entry[4] if len(entry) > 4 else entry[0].lower()
        legacy_slugs.add(slug)
        legacy.update(f"{slug}-{r}" for r in ("exec", "rev", "rev-1", "rev-2", "scout"))
        if entry[2]:
            legacy.add(entry[2][0])
    for key, group in GROUPS.items():
        if key != group.slug or key in legacy_slugs:
            raise RuntimeError(f"grupo inválido ou colisão com SPACES: {key}")
        root = group.project_root
        if not os.path.isabs(root) or os.path.realpath(root) != root:
            raise RuntimeError(f"raiz do grupo deve ser absoluta e canônica: {root}")
        names = [group.coordinator_name, *group.roles()]
        if len(set(names)) != len(names):
            raise RuntimeError(f"coordenador não pode ser papel descartável: {key}")
        for name in names:
            if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", name) or name in used or name in legacy:
                raise RuntimeError(f"nome inválido ou duplicado no registro de grupos: {name}")
            used.add(name)
        if (group.coordinator_kind not in ("codex", "claude")
                or any(kind not in ("codex", "claude") for _, kind in group.roles().values())):
            raise RuntimeError(f"família de revisor não suportada: {key}")


def get_group(slug):
    validate_registry()
    return GROUPS.get(slug)


def group_for_agent(name):
    validate_registry()
    return next((g for g in GROUPS.values()
                 if name == g.coordinator_name or name in g.roles()), None)


def coordinator_name(slug):
    group = get_group(slug)
    return group.coordinator_name if group else f"{slug}-exec"


def identity(info):
    return {k: info.get(k) for k in (
        "name", "agent", "pane_id", "tab_id", "workspace_id", "terminal_id",
        "agent_session", "cwd", "foreground_cwd",
    )}


def _check_location(info, root, core):
    if info.get("interactive_ready") is False:
        raise RuntimeError(f"agent não está interactive-ready: {info.get('name')}")
    for field in ("cwd", "foreground_cwd"):
        if not info.get(field) or core.project_root(info[field]) != root:
            raise RuntimeError(f"vínculo de grupo recusado: {info.get('name')} {field} fora de {root}")
    for field in ("pane_id", "tab_id", "workspace_id"):
        if not info.get(field):
            raise RuntimeError(f"vínculo de grupo sem {field}")


def check_project(info, root, core):
    _check_location(info, root, core)
    if not isinstance(info.get("agent_session"), dict) or not info["agent_session"].get("value"):
        raise RuntimeError(f"vínculo de grupo sem sessão comprovada: {info.get('name')}")


def resolve_binding(group, core, invoking_root=None):
    root = core.project_root(group.project_root)
    if root != group.project_root or (invoking_root is not None and core.project_root(invoking_root) != root):
        raise RuntimeError(f"grupo {group.slug}: raiz da invocação diverge de {group.project_root}")
    info = core.get_agent_info(group.coordinator_name)
    check_project(info, root, core)
    if info.get("name") != group.coordinator_name or info.get("agent") != group.coordinator_kind:
        raise RuntimeError(f"coordenador do grupo {group.slug} não coincide com o registro")
    return {"slug": group.slug, "root": root, "coordinator": identity(info)}


def revalidate(binding, core):
    current = resolve_binding(get_group(binding["slug"]), core, binding["root"])
    if current != binding:
        raise RuntimeError(f"coordenador mudou durante operação do grupo {binding['slug']}")
    return current


def validate_member(binding, name, info, core):
    group = get_group(binding["slug"])
    if name not in group.roles() or info.get("name") != name:
        raise RuntimeError(f"alvo não pertence ao grupo: {name}")
    check_project(info, binding["root"], core)
    return _check_member_topology(binding, name, info)


def _check_member_topology(binding, name, info):
    group = get_group(binding["slug"])
    owner = binding["coordinator"]
    if (info.get("agent") != group.roles()[name][1]
            or info.get("pane_id") == owner["pane_id"]
            or any(info.get(f) != owner[f] for f in ("tab_id", "workspace_id"))):
        raise RuntimeError(f"revisor {name} fora da tab/workspace/família do grupo")
    return info


def existing_members(binding, core):
    group = get_group(binding["slug"])
    members = {}
    # A declarative group never imports or rewrites legacy migration state.
    for name in [f"{group.slug}-rev", *group.roles()]:
        try:
            info = core.get_agent_info(name)
        except RuntimeError as exc:
            if core._is_agent_not_found(exc):
                continue
            raise
        if name == f"{group.slug}-rev":
            raise RuntimeError(f"grupo {group.slug}: nome legacy conflitante {name}")
        members[name] = validate_member(binding, name, info, core)
    return members


@contextmanager
def group_lock(group, core):
    state = os.environ.get("HERDR_MESH_STATE_DIR") or os.path.expanduser("~/.local/state/herdr-mesh")
    lockdir = os.path.join(state, "locks")
    os.makedirs(lockdir, mode=0o700, exist_ok=True)
    key = json.dumps([core.herdr_argv(), group.slug, group.project_root], sort_keys=True)
    digest = hashlib.sha256(key.encode()).hexdigest()
    fd = os.open(os.path.join(lockdir, f"group-{digest}.lock"), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        deadline = time.monotonic() + core.DISPATCH_LOCK_WAIT_S
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"grupo {group.slug}: rodada ou recuperação em andamento")
                time.sleep(0.1)
        yield
    finally:
        os.close(fd)


@contextmanager
def dispatch_reservation(prompts, core, binding=None):
    groups = {g.slug: g for name in prompts if (g := group_for_agent(name)) is not None}
    if not groups:
        yield
        return
    if len(groups) != 1:
        raise RuntimeError("uma rodada não pode misturar grupos")
    group = next(iter(groups.values()))
    if any(name not in group.roles() for name in prompts):
        raise RuntimeError("rodada contém alvo fora dos papéis descartáveis do grupo")
    with group_lock(group, core):
        binding = binding or resolve_binding(group, core)
        revalidate(binding, core)
        existing_members(binding, core)
        for name in prompts:
            validate_member(binding, name, core.get_agent_info(name), core)
        yield


def protected_candidate(info, agents, core):
    """Never let legacy anonymous recovery adopt a group pane or its tab."""
    for group in GROUPS.values():
        names = {group.coordinator_name, *group.roles()}
        anchors = [a for a in agents if a.get("name") in names]
        if any((info.get("workspace_id"), info.get("tab_id")) ==
               (a.get("workspace_id"), a.get("tab_id")) for a in anchors):
            return True
        if not anchors and info.get("cwd") and core.project_root(info["cwd"]) == group.project_root:
            return True
    return False


def validate_initial_codex_member(binding, name, info, core):
    """Only the first canonical hydration may lack a native session."""
    group = get_group(binding['slug'])
    if (name not in group.roles() or info.get('name') != name
            or group.roles()[name][1] != 'codex' or info.get('agent_session') is not None
            or info.get('interactive_ready') is not True
            or info.get('agent_status') not in ('idle', 'done')
            or not info.get('terminal_id')):
        raise RuntimeError('hidratação inicial exige papel Codex sem sessão, explicitamente pronto')
    _check_location(info, binding['root'], core)
    return _check_member_topology(binding, name, info)
