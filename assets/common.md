# Worker rules (read this before the brief)

You are in an Orca worktree of **{{PROJECT}}**, on a branch cut from `{{BASE}}`. What this
package has to do is in `brief.md`, next to this file. Other workers may be building other
packages on the same machine right now. Merging and deploying{{STATE_FILE_INLINE}} are handled
by one merge-queue session, not by you.

You are a worker: you build this one package. Don't create another worktree or worker, don't
merge, don't deploy. If the package turns out to be too big to finish, report BLOCKED as
described in the last section.

## Before you touch anything

- Read what you're going to change and what calls it. For a small file that means the whole
  file; for a big one it means the functions you touch plus their callers, found with grep.
{{BIG_FILE_RULE}}- Confirm the brief's goal isn't already on `{{BASE}}`. If it's already there, report BLOCKED
  instead of building it twice.
- Screenshots mentioned in the brief sit next to the brief. Open them with Read. If you can't
  open one, report BLOCKED rather than guessing what the screen looks like.
- Touch only the files this package needs. Another worker may be editing the same large file,
  and an "while I'm here" change outside your scope becomes a merge conflict.
{{STATE_FILE_RULE}}
## How to write it

- Write code that reads like the code around it. Comments in {{LANGUAGE}}, and comments say
  **why** (why this way, what breaks otherwise), not what the code already says.
- Change a rule, change its tests. Don't delete tests. A new rule needs a test pinning it down.
- No drive-by refactors or renames.
{{MIGRATION_RULE}}{{EXTRA_RULES}}
## Verifying (several worktrees share this machine)

{{FULL_CHECK_RULE}}{{CHECKS_RULE}}{{TEST_RULE}}- The Setup terminal may still be installing dependencies. Check that they're installed before
  running anything.
{{RUN_RULE}}
## Finishing

1. Commit. A one-line message in {{LANGUAGE}} saying what changed and why; add whatever
   attribution lines your session was told to use.
2. `git push -u origin HEAD`, then `gh pr create --base {{PR_BASE}}`. If your branch was not cut
   from `{{BASE}}`, say in the PR's first line which branch it stacks on. **Don't merge.**
3. The PR description carries what the merge queue needs: a one-line status, which rules
   changed, which calls you made yourself (mark them), what you verified, **what you did not
   verify** (say so explicitly), whether there's a migration, and what to watch after deploy.

## Reporting (the manager watches your Orca card, not your terminal)

- Done: `orca worktree set --worktree active --comment "PR #<n>:<one line>" --workspace-status in-review --json`
- Stuck: `orca worktree set --worktree active --comment "BLOCKED:<reason, one line>" --json`, and
  write the details in your terminal.
- `--worktree active` resolves from your cwd, so run it inside your own worktree.

{{HANDOFF_RULE}}
## When you're unsure

- Product decisions: don't stop to ask, and don't invent something ambitious. Take the
  conservative option, mark it as needing a decision in the PR description and in a code
  comment, and finish the rest.
- Contradictory spec, or a brief that doesn't match the code well enough to continue: BLOCKED.
- Review comments from the manager: fix them on the same branch and push (the PR updates
  itself), then set your card back to `in-review`.
