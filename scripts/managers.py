"""Who manages which worker: the manager records worktrees.py inventory and board.py show.

Why a module of its own: a dispatcher starts managers, managers start workers, and both
scripts have to give the same answer to "whose worker is this, and is that manager still
running?". Everything here is a plain file read, no Orca or git calls, so the tests can run
it on fixture folders. Whether a terminal still exists does need Orca; the callers ask it
(`orca terminal list`) and pass terminal_index() of the answer in.

The files (written by other scripts, only read here):
- <git-common-dir>/orca-flow/managers/<slug>/manager.json, from spawn_manager.py:
  {slug, source, source_id, terminal, session, status, ...}; status is
  starting | running | handed-over | done.
- <git-common-dir>/orca-flow/briefs/<task>/manager.json, from spawn_worker.py --manager:
  {session, terminal, cwd, at}; terminal is the manager's $ORCA_TERMINAL_HANDLE.

A worker belongs to a managers/ record when its terminal equals the record's terminal, or
failing that its session equals the record's session. A manager seen only in workers' files
(an interactive manager nobody started with spawn_manager.py) is still listed, named by its
session.

A missing folder or file means "no record". An unreadable one is skipped and a note is added
to the caller's notes list: a broken file must not take the board or the inventory down.
"""
import json
import os

DONE = "done"


def _read_json(path, notes):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        notes.append(f"skipped {path}: unreadable ({e.__class__.__name__})")
        return None
    if not isinstance(data, dict):
        notes.append(f"skipped {path}: not a JSON object")
        return None
    return data


def load_managers(common_dir, notes=None):
    """managers/ records sorted by slug. Each is its manager.json, with `slug` falling back to
    the folder name so a record is always addressable."""
    notes = [] if notes is None else notes
    if not common_dir:
        return []
    root = os.path.join(common_dir, "orca-flow", "managers")
    try:
        names = sorted(os.listdir(root))
    except FileNotFoundError:
        return []
    except OSError as e:
        notes.append(f"skipped {root}: unreadable ({e.__class__.__name__})")
        return []
    out = []
    for name in names:
        d = os.path.join(root, name)
        if not os.path.isdir(d):
            continue
        f = os.path.join(d, "manager.json")
        if not os.path.exists(f):
            notes.append(f"skipped {d}: no manager.json")
            continue
        rec = _read_json(f, notes)
        if rec is not None:
            out.append({**rec, "slug": rec.get("slug") or name})
    return out


def worker_owner(common_dir, task, notes=None):
    """The worker's briefs/<task>/manager.json ({session, terminal, ...}), or None."""
    notes = [] if notes is None else notes
    if not common_dir or not task:
        return None
    return _read_json(os.path.join(common_dir, "orca-flow", "briefs", task, "manager.json"), notes)


def match(owner, managers):
    """The entry of `managers` this owner record points at: terminal first, then session.
    Terminal first because a session name can be reused by a later manager; a terminal
    handle is unique to one pane."""
    if not owner:
        return None
    term, session = owner.get("terminal"), owner.get("session")
    if term:
        for m in managers:
            if m.get("terminal") == term:
                return m
    if session:
        for m in managers:
            if m.get("session") == session:
                return m
    return None


def name_of(m):
    """How a manager is shown: its slug, else its session, else its terminal handle."""
    return m.get("slug") or m.get("session") or m.get("terminal") or "?"


def terminal_index(terminals):
    """{handle: {"orphaned", "pane"}} from `orca terminal list --json`'s terminals. pane is
    "<tabId>:<leafId>", which is what `orca worktree ps` calls an agent's paneKey: that is how
    a manager's terminal handle is tied to its row on the board."""
    out = {}
    for t in terminals or []:
        if t.get("handle"):
            tab, leaf = t.get("tabId"), t.get("leafId")
            out[t["handle"]] = {"orphaned": bool(t.get("orphaned")), "pane": f"{tab}:{leaf}" if tab and leaf else None}
    return out


def terminal_state(handle, terminals):
    """live / hidden / gone, or None when Orca's terminal list isn't known (terminals None).
    hidden: Orca still runs the terminal but it is orphaned, with no pane in the UI, so
    nobody can see or answer it."""
    if terminals is None or not handle:
        return None
    t = terminals.get(handle)
    if t is None:
        return "gone"
    return "hidden" if t["orphaned"] else "live"


def is_live(m, terminals):
    """True/False, or None when Orca's terminal list isn't known. A record marked done is
    not live even if its terminal is still open: the manager has said it finished. A hidden
    terminal is live (the session runs), just not visible."""
    state = terminal_state(m.get("terminal"), terminals)
    if state is None:
        return None
    return m.get("status") != DONE and state != "gone"


def is_dead(m):
    """A managers/ record that hasn't said done but whose terminal is gone: it died, and
    whoever dispatched it needs to know."""
    return bool(m.get("recorded")) and m.get("status") != DONE and m.get("terminal_state") == "gone"


def assign(common_dir, tasks, terminals=None, notes=None):
    """(managers, owner_of) for one repo.

    managers: every managers/ record (with or without workers), then every manager known
    only from workers' files, each as {name, slug, session, status, source, source_id,
    terminal, recorded, live, terminal_state, workers}. terminals is terminal_index()'s
    result, or None when Orca couldn't be asked (live and terminal_state are then None). owner_of: {task: manager name or None} for `tasks`.
    """
    notes = [] if notes is None else notes
    managers = []
    for rec in load_managers(common_dir, notes):
        managers.append({"name": name_of(rec), "slug": rec["slug"], "session": rec.get("session"),
                         "status": rec.get("status"), "source": rec.get("source"),
                         "source_id": rec.get("source_id"), "terminal": rec.get("terminal"),
                         "recorded": True, "workers": []})
    recorded = list(managers)
    extra = []
    owner_of = {}
    for task in tasks:
        owner = worker_owner(common_dir, task, notes)
        if not owner or not (owner.get("terminal") or owner.get("session")):
            owner_of[task] = None
            continue
        m = match(owner, recorded) or match(owner, extra)
        if m is None:
            m = {"name": owner.get("session") or owner.get("terminal"), "slug": None,
                 "session": owner.get("session"), "status": None, "source": None, "source_id": None,
                 "terminal": owner.get("terminal"), "recorded": False, "workers": []}
            extra.append(m)
        m["workers"].append(task)
        owner_of[task] = m["name"]
    managers += sorted(extra, key=lambda m: m["name"])
    for m in managers:
        m["live"] = is_live(m, terminals)
        m["terminal_state"] = terminal_state(m.get("terminal"), terminals)
    return managers, owner_of
