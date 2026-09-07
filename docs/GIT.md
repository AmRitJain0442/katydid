# Bounded Git workspaces and delivery

Katydid prepares every repair in a new clone at an explicit destination. A source may be a
trusted local repository directory registered in the fleet configuration, or an HTTPS GitHub URL
in the exact `https://github.com/owner/repository` form. SSH, `file://`, credential-bearing URLs,
query strings, and arbitrary Git transports are rejected.

Local clones use `--no-local --no-hardlinks`, so their object database is copied instead of shared
with the source. Preparation uses a no-checkout clone, resolves the requested remote base branch,
creates the exact candidate branch at that commit, and verifies a clean checkout. The source
working tree and refs are not changed. A destination must not exist and cannot be inside a local
source repository.

The clone gets an explicit local automation identity and disables commit signing and hooks. This
makes unattended commits independent of user-wide Git identity, signing, and hook configuration.
Katydid invokes Git and GitHub CLI commands as argument arrays with `shell=False`; branch names,
paths, titles, and commit messages are never interpolated into shell text.

## File boundary

`snapshot_files` and `apply_edits` accept exact relative POSIX file paths. They do not interpret
globs or directory prefixes. Paths with normalization, traversal, backslashes, Windows drives,
alternate data streams, `.git` components, symlinks, reparse points, or environment-secret names
such as `.env` and `.env.local` are rejected.

Snapshots include the existing regular UTF-8 files from the allowed list and skip allowed files
that do not exist yet. The byte limit applies to the combined raw content. This lets a registered
generated-test path be absent initially without weakening the allowlist.

Edit batches must contain only `path` and `content` strings. Katydid validates the whole batch,
including duplicate paths and the combined 200,000-byte limit, before changing a file. Each file
is written to a temporary file in its destination directory and atomically replaced; existing
permission bits are retained. Missing leaf files may be created when their parent exists and the
exact path is allowed. Ignored files are not created.

Katydid records the exact applied paths in private clone metadata. `commit` uses an explicit Git
path list from that record. It can commit a newly created allowed file, while unrelated staged,
modified, or untracked files stay outside the commit. `diff` returns the binary-capable patch from
the clone's `HEAD`, including new allowed files marked with intent-to-add.

## Local delivery

`publish_local` performs a normal non-force push of the candidate branch back to the recorded
local source. Git therefore rejects non-fast-forward updates and updates to a branch checked out
in the source. Publication never stages or commits source files.

`merge_local` first requires the base ref to equal the caller's full expected SHA and requires the
candidate to descend from it. Bare repositories and branches that are not checked out use
`git update-ref` with the expected old SHA, giving an atomic compare-and-swap. When the base is
checked out in the source's main working tree, Katydid requires that tree to be clean and lets
`git merge --ff-only` update its ref, index, and files under Git's normal locks. It refuses a base
checked out in another linked worktree. No force update or destructive reset is used.

Call `source_head` immediately before publication and merge while holding the controller's active
lease. A mismatch with `GitWorkspace.base_sha` makes the candidate stale. `tree_sha` resolves the
tree object for a commit already present in a local repository or clone, which lets the controller
compare the delivered tree with the tested candidate before release.

## GitHub delivery

`open_pull_request` requires the `owner/name` argument to match the workspace's original GitHub
source. It pushes the exact candidate ref with normal Git fast-forward rules, writes the body to a
temporary UTF-8 file, and invokes `gh pr create` with `--body-file`. The result is re-read from
GitHub and must confirm the PR URL, positive number, and exact head branch.

`wait_pull_request` polls only that PR's head, state, `statusCheckRollup`, and merge state. It has a
900-second default deadline, supports cooperative cancellation, rejects a changed head or failed
check, and returns only when all reported checks pass and GitHub reports `CLEAN`. A clean PR with
no checks becomes eligible after a ten-second grace period. Conflicts fail immediately; other
pending or policy-blocked states continue only until the deadline.

`merge_pull_request` reads and compares the full head SHA, then invokes `gh pr merge --merge` with
`--match-head-commit`. It does not request admin or bypass behavior, so server-side rules remain in
force. A second read must report `MERGED` and a full merge-commit SHA; queued, pending, or malformed
results are rejected.

The GitHub helpers assume `gh` and Git HTTPS authentication are already configured. They do not
log in, alter global credential configuration, enable auto-merge, bypass rules, or wait without a
deadline.
