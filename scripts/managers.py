"""Who manages which worker: the manager records worktrees.py inventory and board.py show.

Why a module of its own: a dispatcher starts managers, managers start workers, and both
scripts have to give the same answer to "whose worker is this, and is that manager still
running?". Everything here is a plain file read, no Orca or git calls, so the tests can run
it on fixture folders. Whether a terminal still exists does need Orca; the callers ask it
(`orca terminal list`) and pass terminal_index() of the answer in.

The files (written by other scripts, only read here):
- <git-common-dir>/orca-flow/managers/<slug>/manager.json, from spawn_manager.py:
  {slug, source, source_id, terminal, session, status, ...}; status is
  starting | running | handed-over | done | closed. closed (plus terminal_closed_at and
  closed_from) is written by mark_closed() after `worktrees.py cleanup --auto` closed the
  terminal; it is the only write this module makes.
- <git-common-dir>/orca-flow/briefs/<task>/manager.json, from spawn_worker.py --manager:
  {session, terminal, cwd, at}; terminal is the manager's $ORCA_TERMINAL_HANDLE.

A worker belongs to a managers/ record when its terminal equals the record's terminal, or
failing that its session equals the record's session. A manager seen only in workers' files
(an interactive manager nobody started with spawn_manager.py) is still listed, named by its
session.

A missing folder or file means "no record". An unreadable one is skipped and a note is added
to the caller's notes list: a broken file must not take the board or the inventory down.
"""
import datetime
import json
import os
import re

DONE = "done"
HANDED_OVER = "handed-over"
# Set by `worktrees.py cleanup --auto` once it closed the terminal. Finished like done: a gone
# terminal is then expected, not a death, so is_live/is_dead and the board treat both alike.
CLOSED = "closed"
FINISHED = (DONE, CLOSED)
# What the board's classify may say about a manager's pane for cleanup --auto to close it.
# An allowlist rather than excluding needs_human/blocked/unhanded_pr/hidden: anything the rules
# don't recognise (unknown, handoff, stale) keeps the terminal open.
QUIET_ATTENTION = ("done", "idle")
_NOTES_HEADER = "# Manager notes"
# Things a terminal tail may show that nobody should lose track of by closing it: VAR=value
# assignments of key/token/secret/password names, and well-known key formats. Uppercase names
# only for the loose "NAME: value" form, because managers print "tokens: 50k" all the time.
_SECRET = re.compile(
    r"\b[A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD)[A-Z0-9_]*\s*[=:]\s*\S"
    r"|\b(?i:[a-z0-9_]*(?:api[_-]?key|token|secret|password|passwd))\s*=\s*\S"
    r"|\bsk-[A-Za-z0-9_-]{8,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|\bgh[pousr]_[A-Za-z0-9]{20,}"
    r"|\bxox[abpr]-[A-Za-z0-9-]{10,}"
    r"|\bAKIA[0-9A-Z]{16}\b")


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


def brief_tasks(common_dir):
    """Every task whose briefs/<task>/manager.json exists, so a manager's workers are still
    found after their worktrees were removed."""
    if not common_dir:
        return []
    root = os.path.join(common_dir, "orca-flow", "briefs")
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return []
    return [n for n in names if os.path.isfile(os.path.join(root, n, "manager.json"))]


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
    return m.get("status") not in FINISHED and state != "gone"


def is_dead(m):
    """A managers/ record that hasn't said done but whose terminal is gone: it died, and
    whoever dispatched it needs to know. closed means cleanup --auto closed it on purpose."""
    return bool(m.get("recorded")) and m.get("status") not in FINISHED and m.get("terminal_state") == "gone"


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


def notes_written(text):
    """True when notes.md holds more than spawn_manager.py's header line: once the terminal is
    closed its scrollback is gone, and the notes are the only record of what the manager did."""
    if text is None:
        return False
    return any(l.strip() and not l.startswith(_NOTES_HEADER) for l in text.splitlines())


def looks_secret(text):
    return bool(text) and bool(_SECRET.search(text))


def safe_to_close(m, worker_prs, pane, own_terminal, notes_text, tail):
    """(True, why) when cleanup --auto may close this manager's terminal, else (False, why not).
    Pure: the caller gathers the facts.

    m: an assign() entry. worker_prs: {task: gh PR dict or None} for m["workers"], or None when
    gh couldn't be asked. pane: None when Orca shows no agent in the terminal's pane, else
    {"busy", "attention", "reason"} from Orca's state and board_rules.classify. own_terminal:
    the caller's $ORCA_TERMINAL_HANDLE. notes_text: notes.md, or None if missing. tail: the
    terminal's last output, or None if it couldn't be read; it is only matched, never kept.

    Every condition must hold; when unsure, keep the terminal: closing loses its scrollback,
    and leaving one open only costs a line in the next cleanup.
    """
    if not m.get("recorded"):
        return False, "not a managers/ record (interactive session, dispatcher, worker or queue); never auto-closed"
    status = m.get("status")
    if status == HANDED_OVER:
        if worker_prs is None:
            return False, "handed-over, but PR state is unknown"
        if not m.get("workers"):
            return False, "handed-over, but no workers found to check their PRs"
        for task in m["workers"]:
            pr = worker_prs.get(task)
            if not pr:
                return False, f"handed-over, but worker {task} has no PR found"
            if pr.get("state") not in ("MERGED", "CLOSED"):
                return False, f"handed-over, but worker {task}'s PR #{pr.get('number')} is {pr.get('state')}"
    elif status != DONE:
        return False, f"status is {status or 'unset'}, not done or handed-over"
    term = m.get("terminal_state")
    if term is None:
        return False, "Orca's terminal list is unknown"
    if term == "gone":
        return False, "terminal already gone; nothing to close"
    if term == "hidden":
        return False, f"terminal {m.get('terminal')} is orphaned (no pane in Orca); tell the user, never auto-closed"
    if own_terminal and m.get("terminal") == own_terminal:
        return False, "it is this session's own terminal"
    if pane is None:
        return False, "Orca shows no agent in its pane, so it can't tell whether anything runs there"
    if pane.get("busy"):
        return False, "its agent is busy"
    if pane.get("attention") not in QUIET_ATTENTION:
        return False, f"the board says {pane.get('attention')}: {pane.get('reason')}"
    if notes_text is None:
        return False, "no notes.md; its scrollback would be the only record"
    if not notes_written(notes_text):
        return False, "notes.md has only its header; its scrollback would be the only record"
    if tail is None:
        return False, "couldn't read the terminal's output to check it for secrets"
    if looks_secret(tail):
        return False, "may show a secret: tell the user"
    return True, f"{status}, idle, notes.md written"


def mark_closed(common_dir, slug, at=None):
    """Record that cleanup --auto closed this manager's terminal: status closed, when, and the
    status it had, so the board doesn't call it dead. Returns the updated record. Written the
    way spawn_manager.write_record does (tmp + rename), so the board never reads half a file."""
    path = os.path.join(common_dir, "orca-flow", "managers", slug, "manager.json")
    with open(path, encoding="utf-8") as f:
        rec = json.load(f)
    rec["closed_from"] = rec.get("status")
    rec["status"] = CLOSED
    rec["terminal_closed_at"] = at or datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    tmp = f"{path}.tmp-{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False, indent=1)
        f.write("\n")
    os.replace(tmp, path)
    return rec
