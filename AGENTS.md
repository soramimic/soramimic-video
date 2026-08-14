# Repository agent rules

## Worktree isolation

- Treat the clone's primary worktree (the main worktree shown first by
  `git worktree list`) as a protected coordination checkout. Keep it on `dev`; do not
  use it for implementation work.
- In the protected checkout, do not run commands that change the checked-out branch or
  commit, including `git switch` and branch or commit forms of `git checkout`.
- Before modifying tracked files for an implementation or bug-fix task, create or select
  a session-specific linked worktree with its own task branch. Perform edits, tests,
  commits, rebases, and conflict resolution only in that worktree.
- Read-only inspection and worktree-management commands such as `git status`, `git log`,
  `git fetch`, and `git worktree add/list` may run from the protected checkout. Remove
  only worktrees owned by the current session, and never reuse or remove a worktree or
  branch owned by another active session.
- If a task genuinely requires changing the protected checkout, stop and obtain the
  user's explicit approval in the current conversation before doing so.

## Branch promotion safety

- Same-repository, non-draft pull requests targeting `dev`, `preview`, or `main` are
  automatically merged after all mandatory checks pass unless they carry the
  `no-automerge` label.
- Creating or marking ready a pull request to `preview` authorizes the repository
  workflow to merge and deploy that named promotion automatically. Add `no-automerge`
  before marking it ready when a separate review or approval stop is required.
- `main` accepts pull requests only from the same repository's `preview` branch. A
  `preview` to `main` release pull request is automatically merged and deployed after
  all mandatory checks pass unless it carries `no-automerge`.
- Creating or marking ready the `preview` to `main` release pull request is the release
  instruction. Add `no-automerge` before marking it ready when production must remain
  paused after CI.
