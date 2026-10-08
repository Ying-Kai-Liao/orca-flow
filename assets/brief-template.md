# <package name>: <the goal in one line>

## Why

<The user's or the meeting's actual words, ticket link. The worker can't see your conversation,
so the background has to be here.>

## Acceptance criteria

- [ ] <one thing that can be checked>
- [ ] <…>

## Scope

- Will change: <files or sections>
- Don't touch: <what another worker is editing, or what this package deliberately leaves alone>
- Also being edited elsewhere (from `worktrees.py overlap`): <PRs / worktrees and files; "none" if none>
- Existing features that look related but aren't this package: <e.g. the CSV import that already
  sits next to the new CSV export; don't reuse or break it>

## Edge cases this touches

Fill every line, even with "none". Most review-fix rounds catch cases from these four rows;
naming them here lets the worker write the test before the reviewer finds the bug.
- States: <which states of the records this changes can be in when the code runs — including
  cancelled, expired, finished and half-done ones — and what should happen in each>
- Existing features: <older features that read or write the same data or run on the same
  trigger (background jobs, notifications, reports), and how this change must not break them>
- Repeats: <the same request sent twice, a double click, a retry after a timeout, two people
  doing it at once — what the second one should do>
- Old data: <rows written before this change (missing columns, old formats, earlier rules)
  and whether they still work or need a backfill>

## Decisions already made (follow them; don't reopen them)

- <date>: <decision>

## Reference

- Screenshots / designs: <filename in the brief folder, e.g. ./current.png>
- Spec: <docs/…>
- Relevant code: <entry points>
- Entry points in big files, with line ranges: <`path:120-210 functionName` — the worker
  must not read these files whole; give it the ranges and the names to grep>
