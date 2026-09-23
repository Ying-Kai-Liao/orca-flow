"""Attention rules for the board: which agent panes need a human, and why.

Why a module of its own: the board, `worktrees.py inventory` and the separate `jev-handoff`
project all need the same answer to "does this pane need someone?". Keeping it in one pure
function (a plain dict in, plain values out, no Orca, gh, git or clock calls) means there is
one rule set, and `jev-handoff` can import `classify` and swap in a scored model with the
same signature. The row schema is documented in references/board.md.

`make_row` is here too because it is just as pure: it turns one `orca worktree ps --json`
worktree plus one of its agents into the row `classify` reads, so every caller builds rows
the same way.
"""
import re

ATTENTION = ("needs_human", "blocked", "unhanded_pr", "stale", "handoff",
             "unknown", "working", "done", "idle")
# Sort order for display: what needs a person first, quiet rows last.
PRIORITY = {a: i for i, a in enumerate(ATTENTION)}

STALE_MIN = 30
TAIL_CHARS = 2000
WORKING_STATES = {"working"}
# `permission` is a tool-approval prompt: the agent is stopped until someone answers it.
WAITING_STATES = {"waiting", "permission"}
DONE_STATES = {"done"}
IDLE_STATES = {"idle", None, ""}

# Phrases that ask the user to decide, matched only in the last paragraph and only as
# requests. Deliberately narrow: the merge queue and workers end with status reports, and a
# report that ends with a period must stay done/working, not needs_human. A bare "confirm"
# is not here because "I confirmed ..." and "confirm the timer didn't start" (an instruction
# to check something) are both common in reports.
DECISION_PHRASES = (
    "拍板", "請確認", "需要你決定", "你決定",
    "should i ", "which do you want", "which one do you want", "do you want me to",
    "please confirm", "can you confirm", "could you confirm", "your call", "let me know which",
)
# Trailing markup that can sit after the real last character: bold, code, quotes, brackets.
_TRAILING = re.compile(r"[\s*_`'\"」』）)\]]+$")


def _last_paragraph(text):
    parts = [p for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]
    return parts[-1] if parts else ""


def asks_user(message):
    """(True, why) when the last assistant message puts a question or a decision to the user."""
    text = (message or "").strip()
    if not text:
        return False, ""
    tail = _TRAILING.sub("", text)
    if tail.endswith(("?", "？")):
        return True, "last message ends with a question"
    last = _last_paragraph(text).lower()
    for phrase in DECISION_PHRASES:
        if phrase in last:
            return True, f"last message asks for a decision ({phrase.strip()!r})"
    return False, ""


def classify(row, stale_min=STALE_MIN):
    """(attention, reason) for one agent pane row. Exactly one attention value from ATTENTION.

    Order matters, first match wins:
    1. Orca says the agent is waiting on the user.
    2. The card says BLOCKED, then HANDOFF: the worker has already said what it needs.
    3. The agent stopped and its last message asks something (prose questions look like done).
    4. An open, non-draft PR nobody handed to the queue, with no agent still working on it.
    5. Working, but no update for longer than stale_min.
    6. Plain working / done / idle; anything else is unknown.
    """
    state = row.get("state")
    comment = (row.get("comment") or "").strip().upper()
    if state in WAITING_STATES:
        return "needs_human", f"Orca state is {state}"
    if comment.startswith("BLOCKED"):
        return "blocked", "card comment starts with BLOCKED"
    if comment.startswith("HANDOFF"):
        return "handoff", "card comment starts with HANDOFF"
    if state not in WORKING_STATES:
        asks, why = asks_user(row.get("last_message_tail") or row.get("last_message"))
        if asks:
            return "needs_human", why
    pr = row.get("pr") or {}
    if (pr.get("number") and (pr.get("state") or "").upper() == "OPEN" and pr.get("isDraft") is not True
            and not row.get("handover") and state not in WORKING_STATES):
        return "unhanded_pr", f"PR #{pr['number']} is open with no handover file"
    if state in WORKING_STATES:
        mins = row.get("minutes_since_update")
        if mins is not None and mins > stale_min:
            return "stale", f"working but no update for {int(mins)} min (> {stale_min})"
        return "working", "Orca state is working"
    if state in DONE_STATES:
        return "done", "Orca state is done"
    if state in IDLE_STATES:
        return "idle", "no agent pane" if not row.get("pane") else "Orca state is idle"
    return "unknown", f"unrecognised Orca state {state!r}"


def _minutes(now_ms, then_ms):
    return round((now_ms - then_ms) / 60000, 1) if then_ms else None


def make_row(wt, agent, now_ms, pr=None, handover=None):
    """One board row from a `worktree ps` worktree and one of its agents (None for a worktree
    with no agent pane). pr and handover come from the caller, which can call gh and read
    the queue directory; this function can't."""
    agent = agent or {}
    msg = (agent.get("lastAssistantMessage") or "").strip()
    return {
        "host": wt.get("hostId"),
        "repo": wt.get("repo"),
        "repo_id": wt.get("repoId"),
        "worktree": (wt.get("path") or "").rstrip("/").split("/")[-1] or wt.get("displayName"),
        "worktree_id": wt.get("worktreeId"),
        "path": wt.get("path"),
        "branch": (wt.get("branch") or "").removeprefix("refs/heads/") or None,
        "is_main": bool(wt.get("isMainWorktree")),
        "column": wt.get("workspaceStatus"),
        "comment": wt.get("comment") or "",
        "unread": bool(wt.get("unread")),
        "pr": pr,
        "handover": handover,
        "pane": agent.get("paneKey"),
        "agent_type": agent.get("agentType"),
        "state": agent.get("state"),
        "working_mode": agent.get("workingMode"),
        "tool": agent.get("toolName"),
        "prompt": (agent.get("prompt") or "")[:200],
        "minutes_in_state": _minutes(now_ms, agent.get("stateStartedAt")),
        "minutes_since_update": _minutes(now_ms, agent.get("updatedAt")),
        "last_message": msg[:200],
        # The question is usually at the end of a long message, so the rules read the tail.
        # It stays in the JSON so classify gives the same answer when re-run on board.json.
        "last_message_tail": msg[-TAIL_CHARS:],
    }
