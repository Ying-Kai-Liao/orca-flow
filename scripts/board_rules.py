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
# report must stay done/working, not needs_human. So a last paragraph that ends with a period
# is never a request, whatever it mentions ("Per the 2026-09-23 拍板, no per-batch approval."),
# and a phrase right after a negation ("不需要你拍板", "no need to confirm") doesn't count. A bare
# "confirm" is not listed because "I confirmed ..." is common in reports.
DECISION_PHRASES = (
    "拍板", "請確認", "需要你決定", "你決定",
    "should i ", "which do you want", "which one do you want", "do you want me to",
    "please confirm", "can you confirm", "could you confirm", "your call", "let me know which",
)
NEGATIONS = ("不需要", "不用", "不必", "無需", "毋需", "no need", "not need", "don't need", "doesn't need")
NEGATION_WINDOW = 12  # chars before a phrase in which a negation cancels it
# A question longer ago than this with no answer is treated as done: the user saw it or moved
# on, and a board full of day-old questions hides the new ones.
QUESTION_MAX_MIN = 240
# The handover file exists but the queue sent the PR back: it needs handing over again.
NOT_HANDED = {"returned"}

# Trailing markup that can sit after the real last character: bold, code, quotes.
_TRAILING = re.compile(r"[\s*_`'\"」』]+$")
# A trailing parenthetical: "Done. (Tests pass?)" is a report with an aside, not a question.
_TRAILING_BRACKETS = re.compile(r"\s*[(（\[【][^()（）\[\]【】]*[)）\]】]$")
_FENCE = re.compile(r"```.*?(```|$)", re.S)


def _last_paragraph(text):
    parts = [p for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]
    return parts[-1] if parts else ""


def _strip_end(text):
    """Text without trailing markup and trailing bracketed asides, so its real last
    character can be read."""
    prev = None
    while prev != text:
        prev = text
        text = _TRAILING_BRACKETS.sub("", _TRAILING.sub("", text))
    return text


def _negated(text, i):
    before = text[max(0, i - NEGATION_WINDOW):i]
    return any(n in before for n in NEGATIONS)


def asks_user(message):
    """(True, why) when the last assistant message puts a question or a decision to the user."""
    # Code blocks are quoted material (a script, a log line with a "?"), not what the agent says.
    text = _FENCE.sub("", message or "").strip()
    if not text:
        return False, ""
    tail = _strip_end(text)
    if tail.endswith(("?", "？")):
        return True, "last message ends with a question"
    last = _strip_end(_last_paragraph(text))
    if last.endswith((".", "。")):
        return False, ""
    low = last.lower()
    for phrase in DECISION_PHRASES:
        i = low.find(phrase)
        while i >= 0:
            if not _negated(low, i):
                return True, f"last message asks for a decision ({phrase.strip()!r})"
            i = low.find(phrase, i + 1)
    return False, ""


def classify(row, stale_min=STALE_MIN, question_max_min=QUESTION_MAX_MIN):
    """(attention, reason) for one agent pane row. Exactly one attention value from ATTENTION.

    Order matters, first match wins:
    1. Orca says the agent is waiting on the user.
    2. The card says BLOCKED, then HANDOFF: the worker has already said what it needs.
    3. The agent stopped and its last message asks something (prose questions look like done),
       unless it has sat unanswered for longer than question_max_min: then it is done.
    4. An open, non-draft PR nobody handed to the queue, with no agent still working on it.
       In a repo with no queue (row["no_queue"]) there is nobody to hand it to: done.
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
            mins = row.get("minutes_in_state")
            if mins is not None and mins > question_max_min:
                return "done", f"asked {mins / 60:.0f}h ago, no answer; demoted"
            return "needs_human", why
    pr = row.get("pr") or {}
    if (pr.get("number") and (pr.get("state") or "").upper() == "OPEN" and pr.get("isDraft") is not True
            and (not row.get("handover") or row.get("handover") in NOT_HANDED) and state not in WORKING_STATES):
        if row.get("no_queue"):
            return "done", f"PR #{pr['number']} is open; no queue in this repo"
        why = "was sent back by the queue" if row.get("handover") in NOT_HANDED else "has no handover file"
        return "unhanded_pr", f"PR #{pr['number']} is open and {why}"
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


def make_row(wt, agent, now_ms, pr=None, handover=None, no_queue=False):
    """One board row from a `worktree ps` worktree and one of its agents (None for a worktree
    with no agent pane). pr, handover and no_queue come from the caller, which can call gh
    and read the queue directory and the repo's config; this function can't."""
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
        "no_queue": bool(no_queue),
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
