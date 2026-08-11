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

- Automatic merging is allowed only for pull requests whose base branch is `dev`.
- An agent may prepare and monitor a selective promotion pull request to `preview`, but
  must not merge it unless the user explicitly approves promoting the named changes in
  the current conversation.
- An agent may prepare and monitor a `preview` to `main` release pull request, but must
  not merge it or deploy production until the user explicitly approves the release after
  reviewing `preview` in the current conversation.
- A passing CI run, a schedule, a generic instruction such as "finish", or the default
  delivery goal does not count as release approval.
- Do not add or use the `emergency` label unless the user explicitly requests an
  emergency release. Corrective rollback work must remain limited to undoing the
  unintended production change.
