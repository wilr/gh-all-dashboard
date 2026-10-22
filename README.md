# gh-all

Local tools for surveying every GitHub repository where you have admin access (excluding forks and archived repos): open issues, open PRs (with CI), and commits since the latest release.

Uses the [`gh`](https://cli.github.com/) CLI for authentication — run `gh auth login` first.

## Requirements

- [GitHub CLI](https://cli.github.com/) (`gh`), authenticated
- `jq` (for `issues.sh`)
- Python 3 (for the dashboard)

## Dashboard

A local web UI over the same repo set as `issues.sh`.

```bash
./dashboard.py              # http://localhost:8787
./dashboard.py --port 9000
./dashboard.py --refresh    # fetch fresh data before serving
./dashboard.py --no-open    # don't open a browser
```

Data is cached in `.dashboard-cache.json`. Use **↻ Refresh** (or `R`) to re-fetch from GitHub.

### Remove a repo from the list

Each row has a **delete** control (trash icon). Clicking it:

1. Slides the repo out of the table
2. Appends `owner/repo` to the local `ignore` file so it stays hidden on the next refresh (creates `ignore` from [`ignore.example`](ignore.example) if needed)

Keyboard: select a row with `j`/`k`, then press `x`.

The full contents of the ignore file are shown at the bottom of the page for reference. Edit that file directly to ignore whole organisations (`Org` or `Org/*`) or to undo a removal.

## CLI: list open issues

```bash
./issues.sh
```

Prints a terminal table of open issues across admin repos (respecting the same ignore rules). Repo and issue cells are OSC 8 hyperlinks where the terminal supports them.

## Ignore file

Copy the template, then edit locally (the real file is gitignored):

```bash
cp ignore.example ignore
```

If `ignore` is missing, every admin repo is listed — that is fine. Patterns use one entry per line:

| Pattern | Meaning |
|---------|---------|
| `OrgName` or `OrgName/*` | Ignore every repo in that organisation |
| `https://github.com/OrgName` (or `/OrgName/*`, `/orgs/OrgName`) | Same as above, via GitHub URL |
| `owner/repo` | Ignore a single repository |
| `https://github.com/owner/repo` | Same as above, via GitHub URL |

Blank lines and `#` comments are skipped. Override the path with `IGNORE_FILE=/path/to/file`.

## License

[MIT](LICENSE)
