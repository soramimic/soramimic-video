# Repository agent rules

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
