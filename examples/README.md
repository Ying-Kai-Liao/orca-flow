# Example configs

Copy one to `<repo>/.claude/orca-flow.json` and edit it. Every key is optional; see
[`../references/configuration.md`](../references/configuration.md).

- **`minimal.json`** — the smallest useful config: workers know how to lint and how to run a few
  tests. No deploys, no status file.
- **`node-webapp.json`** — a TypeScript app with numbered SQL migrations, a hand-written status
  file, and two deploy targets: staging through a GitHub Action, production over ssh with a
  database backup first.
- **`python-service.json`** — pytest and ruff, alembic migrations, one deploy target.

Hostnames and key paths here are placeholders. If yours are sensitive, put the config in
`<repo>/.git/orca-flow/config.json` instead — it's read the same way and can't be committed.
