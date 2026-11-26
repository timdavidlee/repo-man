# repo-man
scripts for managing my repos

Python 3.11+, standard library only. Requires `git`; the `github` command also
needs [`gh`](https://cli.github.com), logged in.

## Usage

```sh
uv run repo-man scan ~/myrepos                # state of every repo
uv run repo-man scan ~/myrepos --attention    # only the ones needing attention
uv run repo-man scan ~/myrepos --fetch        # refresh remote refs first
uv run repo-man github ~/myrepos              # what's cloned vs. what's on GitHub
```

Set `REPO_MAN_ROOT` to skip the path argument. All commands accept `--json` and
`--depth N` (for repos grouped in subfolders).

`scan` and `github` don't change your files, branches or commits. Their only write is
`scan --fetch`, which runs `git fetch --all` and updates remote-tracking refs.
Without it, ahead/behind counts are as of each repo's last fetch.

### Reading the table

| column  | meaning |
|---------|---------|
| changes | `M` modified/staged, `?` untracked, `U` conflicted |
| sync    | `↑` ahead of upstream, `↓` behind (current branch only) |
| stash   | number of stash entries |
| last    | age of the commit at HEAD |

## Marking teaching repos

```sh
uv run repo-man review ~/myrepos                         # missing a description or topics
uv run repo-man review ~/myrepos --missing description   # only missing a description
```

This writes `reports/teaching-review.csv` (git-ignored, since it lists private
repo names; keep it ignored if you choose another `--output` inside a public
repo). Fill in the `teaching` column, for example `yes` or `no`, plus `notes` if
you like. Close the file before running the command again.

Re-running refreshes the other columns from GitHub and keeps `teaching`, `notes`
and any column you added. A row you have written in stays even if it no longer
matches the filter. `review` changes nothing on GitHub.

### Saving marks as GitHub topics

```sh
uv run repo-man tag                                  # preview only
uv run repo-man tag --apply                          # add the topics
uv run repo-man tag --no-topic not-teaching          # also tag the "no" repos (preview)
```

`tag --apply` is the only command that changes anything on GitHub. Without
`--apply` it just lists what it would add. Topics on public repos are visible to
anyone.

- `yes`, `y`, `true` or `1` adds the `teaching` topic (change with `--topic`).
- `no`, `n`, `false` or `0` adds nothing unless you pass `--no-topic`.
- Blank rows are ignored; any other value is reported and skipped.
- It compares against the topics on GitHub now, so running it again is safe.
- It only adds topics. If you flip a mark after applying and both topic names are
  in play, the repo is skipped so you can remove the old topic yourself. This
  needs the same `--topic` and `--no-topic` values as before. Without
  `--no-topic`, changing `yes` to `no` leaves the `teaching` topic in place.

## Tests

```sh
uv run pytest
```
