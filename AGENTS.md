# Repository agent rules

## Delivery and worktrees

- Ordinary implementation is complete when its pull request is merged into `dev`
  after mandatory checks pass, even though the repository's default branch is `main`.
- Keep the clone's primary worktree on `dev` as a protected coordination checkout.
  Preserve its branch, changes, and running services. Perform edits, tests, commits,
  rebases, and conflict resolution in a session-specific linked worktree with a task
  branch from `origin/dev`. Remove only worktrees owned by the current session.
- Changing the protected checkout requires the user's explicit approval.

## Branch promotion safety

- Development delivery does not authorize a release. Create or mark ready a
  promotion to `preview` or a `preview` to `main` release only when the user has
  requested that promotion; passing CI or a generic instruction to finish is insufficient.
- Same-repository, non-draft pull requests targeting `dev`, `preview`, or `main` are
  automatically merged after all mandatory checks pass unless they carry the
  `no-automerge` label.
- Creating or marking ready a pull request to `preview` authorizes the repository
  workflow to merge and deploy that named promotion automatically. Add `no-automerge`
  before marking it ready when a separate review or approval stop is required.
- Normal code releases to `main` come from the same repository's `preview` branch. A
  `preview` to `main` release pull request is automatically merged and deployed after
  all mandatory checks pass unless it carries `no-automerge`.
- Creating or marking ready the `preview` to `main` release pull request is the release
  instruction. Add `no-automerge` before marking it ready when production must remain
  paused after CI.

## Changes and verification

- Use [README.md](README.md) for setup and [DESIGN.md](DESIGN.md) for public interfaces.
  Initialize the recorded submodule commits recursively; change their pointers only
  when the requested change requires it.
- For code changes, install with `uv sync --extra api` and run the relevant tests.
  The CI checks are `uv run ruff check .`, `uv run mypy src`, and `uv run pytest -q`.
- For documentation-only changes, check links, command names, and `git diff --check`.
  Mandatory CI and branch protections still apply before merge.
- Keep user uploads and generated song media out of commits. Follow the sample and
  image usage requirements in [README.md](README.md) and [docs/sample-rights.md](docs/sample-rights.md),
  and preserve existing third-party attribution and license notices.

## Agent coordination

- Default to one agent. Delegate only an explicitly requested or clearly useful,
  bounded independent subtask while the parent advances other work. Use the
  smallest useful team and a self-contained brief; avoid unnecessary full-history
  forks, recursive delegation, duplicate work, and overlapping edits.
- Prefer completion notifications. When blocked on a result, call the native wait
  tool directly with an explicit timeout suited to the expected duration and the
  active runtime and communication limits. Avoid repeated short waits, wrapping
  native agent waits in another yielding tool, and checking status after every
  unchanged timeout.
- Send follow-up messages only for new information, changed scope, or a concrete
  blocker. If a final result conflicts with a running status, inspect once and
  reconcile it instead of polling indefinitely. Respect required progress updates.
- Use bounded waits and incremental output for CI and long commands too. A timeout
  is neither completion nor approval; required checks must still pass before merge.

## Markdown-only main updates

- A same-repository PR containing only regular `.md` files may target `main`
  directly after an explicit request to publish those documentation changes.
  Renames must have Markdown names on both sides. Code, workflow files, symlinks,
  executable files, submodules, and mixed changes do not qualify.
- The workflows verify the complete live diff and recheck it before merging.
  Mandatory CI, branch protections, draft status, and `no-automerge` still apply.
  An API error or incomplete diff is not permission to use the exception.
