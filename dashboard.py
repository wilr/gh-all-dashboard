#!/usr/bin/env python3
"""Local web dashboard for every repo issues.sh covers.

Shows open issues, open PRs (with CI state) and commits on the default
branch since the latest release. Uses the `gh` CLI for auth, so run
`gh auth login` first.

    ./dashboard.py              # serve on http://localhost:8787
    ./dashboard.py --port 9000
    ./dashboard.py --refresh    # fetch fresh data before serving
"""

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
IGNORE_FILE = os.environ.get("IGNORE_FILE", os.path.join(SCRIPT_DIR, "ignore"))
IGNORE_EXAMPLE = os.path.join(SCRIPT_DIR, "ignore.example")
CACHE_FILE = os.path.join(SCRIPT_DIR, ".dashboard-cache.json")
HTML_FILE = os.path.join(SCRIPT_DIR, "dashboard.html")

DEFAULT_IGNORE_STARTER = """\
# Ignore organisations and repositories when listing issues / refreshing the dashboard.
# Lines starting with # are comments. Blank lines are ignored.
#
# Organisations: "OrgName", "OrgName/*", or a GitHub URL
# Repositories: "owner/repo" or a GitHub URL

# Organisations

# Repositories
"""

BATCH_SIZE = 6  # repos per GraphQL query; bigger queries hit 502s
WORKERS = 6

state_lock = threading.Lock()
state = {"running": False, "done": 0, "total": 0, "phase": "", "error": None}


# --- gh helpers -------------------------------------------------------------

def gh(*args):
    out = subprocess.run(["gh", *args], capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or "gh %s failed" % args[0])
    return out.stdout


def gh_json(*args):
    return json.loads(gh(*args))


# --- ignore file (same rules as issues.sh) ----------------------------------

def load_ignore_file():
    """Return ignore file contents, or '' when the file is missing (not an error)."""
    if not os.path.exists(IGNORE_FILE):
        return ""
    try:
        with open(IGNORE_FILE) as f:
            return f.read()
    except OSError:
        return ""


def ensure_ignore_file():
    """Create a local ignore file on first write, seeded from ignore.example if present."""
    if os.path.exists(IGNORE_FILE):
        return
    if os.path.exists(IGNORE_EXAMPLE):
        try:
            with open(IGNORE_EXAMPLE) as f:
                text = f.read()
        except OSError:
            text = DEFAULT_IGNORE_STARTER
    else:
        text = DEFAULT_IGNORE_STARTER
    parent = os.path.dirname(IGNORE_FILE)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = IGNORE_FILE + ".tmp"
    with open(tmp, "w") as f:
        f.write(text if text.endswith("\n") else text + "\n")
    os.replace(tmp, IGNORE_FILE)


def normalize_ignore_pattern(pattern):
    """Strip GitHub URL prefixes so orgs/repos can be listed as URLs."""
    p = pattern.strip()
    lower = p.lower()
    for prefix in (
        "https://github.com/",
        "http://github.com/",
        "https://www.github.com/",
        "http://www.github.com/",
        "github.com/",
    ):
        if lower.startswith(prefix):
            p = p[len(prefix):]
            break
    # https://github.com/orgs/OrgName → OrgName
    if p.lower().startswith("orgs/"):
        p = p[5:]
    p = p.split("?", 1)[0].split("#", 1)[0]
    if p.endswith("/") and not p.endswith("/*"):
        p = p.rstrip("/")
    return p


def load_ignore_patterns():
    return [normalize_ignore_pattern(l) for l in load_ignore_file().splitlines()
            if l.strip() and not l.strip().startswith("#")]


def should_ignore(repo, patterns):
    org = repo.split("/", 1)[0]
    for p in patterns:
        if "/" in p:
            if p.endswith("/*"):
                if org == p[:-2]:
                    return True
            elif repo == p:
                return True
        elif org == p:
            return True
    return False


def append_ignore_repo(repo):
    """Append owner/repo to the ignore file under # Repositories. Returns patterns.

    Creates the ignore file (from ignore.example) if it does not exist yet.
    """
    if not re.fullmatch(r"[^/\s]+/[^/\s]+", repo):
        raise ValueError("invalid repo name")
    patterns = load_ignore_patterns()
    if should_ignore(repo, patterns) or repo in patterns:
        return patterns

    ensure_ignore_file()
    text = load_ignore_file()
    if text and not text.endswith("\n"):
        text += "\n"

    marker = "# Repositories"
    m = re.search(r"^# Repositories[ \t]*$\n?", text, re.M)
    if m:
        idx = m.end()
        text = text[:idx] + repo + "\n" + text[idx:]
    else:
        text += ("\n" if text else "") + marker + "\n" + repo + "\n"

    tmp = IGNORE_FILE + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, IGNORE_FILE)
    return load_ignore_patterns()


# --- data collection --------------------------------------------------------

REPO_FRAGMENT = """
fragment R on Repository {
  nameWithOwner url description isArchived isFork isPrivate pushedAt
  defaultBranchRef { name target { oid } }
  latestRelease { tagName name url publishedAt tagCommit { oid } }
  tags: refs(refPrefix: "refs/tags/", first: 1, orderBy: {field: TAG_COMMIT_DATE, direction: DESC}) {
    nodes { name target { oid ... on Tag { target { oid } } } }
  }
  issues(states: OPEN, first: 50, orderBy: {field: UPDATED_AT, direction: DESC}) {
    totalCount
    nodes {
      number title url updatedAt createdAt
      author { login }
      labels(first: 5) { nodes { name color } }
      assignees(first: 3) { nodes { login } }
      comments { totalCount }
    }
  }
  pullRequests(states: OPEN, first: 30, orderBy: {field: UPDATED_AT, direction: DESC}) {
    totalCount
    nodes {
      number title url updatedAt isDraft reviewDecision mergeable
      author { login }
      headRefName
      commits(last: 1) { nodes { commit { statusCheckRollup { state } } } }
    }
  }
}
"""


def list_repos():
    out = gh(
        "api", "--paginate", "-X", "GET", "/user/repos",
        "-f", "affiliation=owner,collaborator,organization_member",
        "-F", "per_page=100",
        "--jq", ".[] | select(.permissions.admin == true and .archived == false and .fork == false) | .full_name",
    )
    patterns = load_ignore_patterns()
    repos = [r for r in out.split("\n") if r]
    kept = [r for r in repos if not should_ignore(r, patterns)]
    return kept, len(repos) - len(kept)


def fetch_batch(repos, attempt=0):
    """Fetch a batch, retrying and splitting it when GitHub times out."""
    try:
        return query_batch(repos)
    except RuntimeError:
        if len(repos) > 1:
            mid = len(repos) // 2
            return fetch_batch(repos[:mid]) + fetch_batch(repos[mid:])
        if attempt < 3:
            time.sleep(2 ** attempt)
            return fetch_batch(repos, attempt + 1)
        print("Skipping %s: GraphQL kept failing" % repos[0], file=sys.stderr)
        return [None]


def query_batch(repos):
    parts = []
    for i, full in enumerate(repos):
        owner, name = full.split("/", 1)
        parts.append('r%d: repository(owner: %s, name: %s) { ...R }'
                     % (i, json.dumps(owner), json.dumps(name)))
    query = REPO_FRAGMENT + "\nquery {\n" + "\n".join(parts) + "\n}"
    # GraphQL returns partial data with errors (e.g. a repo that vanished),
    # so read stdout even when gh exits non-zero.
    out = subprocess.run(["gh", "api", "graphql", "-f", "query=" + query],
                         capture_output=True, text=True)
    try:
        data = json.loads(out.stdout).get("data")
    except ValueError:
        data = None
    if data is None:
        raise RuntimeError(out.stderr.strip() or "GraphQL query failed")
    return [data.get("r%d" % i) for i in range(len(repos))]


def release_base(repo):
    """Return (label, url, published_at, oid, kind) of the latest release or tag."""
    rel = repo.get("latestRelease")
    if rel and rel.get("tagCommit"):
        return rel["tagName"], rel["url"], rel["publishedAt"], rel["tagCommit"]["oid"], "release"
    tags = (repo.get("tags") or {}).get("nodes") or []
    if tags:
        t = tags[0]
        target = t.get("target") or {}
        oid = (target.get("target") or {}).get("oid") or target.get("oid")
        if oid:
            url = "%s/releases/tag/%s" % (repo["url"], t["name"])
            return t["name"], url, None, oid, "tag"
    return None


PR_REF = re.compile(r"\(#(\d+)\)\s*$")


def fetch_unreleased(full, base_oid, branch):
    data = gh_json("api", "repos/%s/compare/%s...%s" % (full, base_oid, branch))
    commits = []
    for c in data.get("commits", []):
        if len(c.get("parents", [])) > 1:
            continue  # skip merge commits, matching GitHub's release notes
        msg = c["commit"]["message"].split("\n", 1)[0]
        m = PR_REF.search(msg)
        author = (c.get("author") or {}).get("login") or c["commit"]["author"]["name"]
        commits.append({
            "sha": c["sha"][:7],
            "message": msg,
            "url": c["html_url"],
            "author": author,
            "date": c["commit"]["author"]["date"],
            "pr": int(m.group(1)) if m else None,
        })
    commits.reverse()  # newest first
    link_prs(full, commits, [c for c in data.get("commits", []) if len(c.get("parents", [])) <= 1][::-1])
    return {"ahead": len(commits), "aheadRaw": data.get("ahead_by", 0),
            "truncated": data.get("ahead_by", 0) > len(data.get("commits", [])),
            "commits": commits}


def link_prs(full, commits, raw):
    """Replace message-parsed PR numbers with the PR GitHub associates with each commit."""
    if not commits:
        return
    owner, name = full.split("/", 1)
    parts = ['c%d: object(oid: "%s") { ... on Commit { associatedPullRequests(first: 1) '
             '{ nodes { number } } } }' % (i, c["sha"]) for i, c in enumerate(raw[:100])]
    query = "query { repository(owner: %s, name: %s) { %s } }" % (
        json.dumps(owner), json.dumps(name), " ".join(parts))
    try:
        repo = gh_json("api", "graphql", "-f", "query=" + query)["data"]["repository"]
    except Exception:
        return  # keep the parsed numbers
    for i, c in enumerate(commits[:100]):
        nodes = ((repo.get("c%d" % i) or {}).get("associatedPullRequests") or {}).get("nodes")
        if nodes:
            c["pr"] = nodes[0]["number"]


def shape(repo, unreleased):
    def checks(pr):
        nodes = pr["commits"]["nodes"]
        roll = nodes[0]["commit"].get("statusCheckRollup") if nodes else None
        return roll["state"] if roll else None

    base = release_base(repo)
    branch = (repo.get("defaultBranchRef") or {}).get("name")
    return {
        "repo": repo["nameWithOwner"],
        "url": repo["url"],
        "description": repo.get("description"),
        "private": repo["isPrivate"],
        "fork": repo["isFork"],
        "archived": repo["isArchived"],
        "pushedAt": repo.get("pushedAt"),
        "branch": branch,
        "release": {
            "tag": base[0], "url": base[1], "publishedAt": base[2], "kind": base[4],
            "compareUrl": "%s/compare/%s...%s" % (repo["url"], base[0], branch),
        } if base else None,
        "issueCount": repo["issues"]["totalCount"],
        "issues": [{
            "number": i["number"], "title": i["title"], "url": i["url"],
            "updatedAt": i["updatedAt"],
            "author": (i.get("author") or {}).get("login"),
            "labels": [l["name"] for l in i["labels"]["nodes"]],
            "assignees": [a["login"] for a in i["assignees"]["nodes"]],
            "comments": i["comments"]["totalCount"],
        } for i in repo["issues"]["nodes"]],
        "prCount": repo["pullRequests"]["totalCount"],
        "prs": [{
            "number": p["number"], "title": p["title"], "url": p["url"],
            "updatedAt": p["updatedAt"], "draft": p["isDraft"],
            "author": (p.get("author") or {}).get("login"),
            "branch": p["headRefName"],
            "review": p.get("reviewDecision"),
            "mergeable": p.get("mergeable"),
            "checks": checks(p),
        } for p in repo["pullRequests"]["nodes"]],
        "unreleased": unreleased,
    }


def set_state(**kw):
    with state_lock:
        state.update(kw)


def collect():
    set_state(running=True, done=0, total=0, phase="Listing repositories", error=None)
    try:
        repos, ignored = list_repos()
        batches = [repos[i:i + BATCH_SIZE] for i in range(0, len(repos), BATCH_SIZE)]
        set_state(phase="Fetching issues & PRs", total=len(repos))

        raw = []
        with ThreadPoolExecutor(WORKERS) as pool:
            futures = {pool.submit(fetch_batch, b): b for b in batches}
            for f in as_completed(futures):
                raw.extend(r for r in f.result() if r)
                with state_lock:
                    state["done"] += len(futures[f])

        # Only hit the compare API where the branch has moved past the release.
        need = []
        for r in raw:
            base = release_base(r)
            head = ((r.get("defaultBranchRef") or {}).get("target") or {}).get("oid")
            if base and head and base[3] != head:
                need.append(r)
        set_state(phase="Comparing branches to releases", done=0, total=len(need))

        unreleased = {}
        with ThreadPoolExecutor(WORKERS) as pool:
            futures = {
                pool.submit(fetch_unreleased, r["nameWithOwner"], release_base(r)[3],
                            r["defaultBranchRef"]["name"]): r["nameWithOwner"]
                for r in need
            }
            for f in as_completed(futures):
                try:
                    unreleased[futures[f]] = f.result()
                except Exception as e:  # e.g. tag not reachable from branch
                    unreleased[futures[f]] = {"error": str(e), "ahead": 0, "commits": []}
                with state_lock:
                    state["done"] += 1

        rows = []
        for r in raw:
            base = release_base(r)
            u = unreleased.get(r["nameWithOwner"])
            if u is None and base:
                u = {"ahead": 0, "commits": []}
            rows.append(shape(r, u))
        rows.sort(key=lambda x: x["repo"].lower())

        payload = {"generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                   "ignored": ignored, "ignorePatterns": load_ignore_patterns(),
                   "ignoreFile": load_ignore_file(), "repos": rows}
        tmp = CACHE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(payload, f)
        os.replace(tmp, CACHE_FILE)
        set_state(running=False, phase="Done")
    except Exception as e:
        set_state(running=False, phase="Failed", error=str(e))
        print("Refresh failed: %s" % e, file=sys.stderr)


def start_refresh():
    with state_lock:
        if state["running"]:
            return False
        state["running"] = True
    threading.Thread(target=collect, daemon=True).start()
    return True


# --- HTTP -------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, code, body, ctype):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            return json.loads(raw.decode() or "{}")
        except ValueError:
            return None

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/":
            with open(HTML_FILE, "rb") as f:
                self.send(200, f.read(), "text/html; charset=utf-8")
        elif path == "/api/data":
            if os.path.exists(CACHE_FILE):
                with open(CACHE_FILE, "rb") as f:
                    body = f.read()
                try:
                    payload = json.loads(body)
                except ValueError:
                    payload = None
                if isinstance(payload, dict):
                    # Always serve live ignore file contents, and apply them (and
                    # the no-forks/no-archived rules) to cached repos without needing a refresh.
                    patterns = load_ignore_patterns()
                    repos = payload.get("repos") or []
                    kept = [r for r in repos if not r.get("fork") and not r.get("archived")
                            and not should_ignore(r.get("repo", ""), patterns)]
                    payload["ignored"] = payload.get("ignored", 0) + len(repos) - len(kept)
                    payload["repos"] = kept
                    payload["ignorePatterns"] = patterns
                    payload["ignoreFile"] = load_ignore_file()
                    body = json.dumps(payload).encode()
                self.send(200, body, "application/json")
            else:
                self.send(200, "null", "application/json")
        elif path == "/api/ignore":
            self.send(200, json.dumps({
                "patterns": load_ignore_patterns(),
                "content": load_ignore_file(),
            }), "application/json")
        elif path == "/api/status":
            with state_lock:
                self.send(200, json.dumps(state), "application/json")
        else:
            self.send(404, "Not found", "text/plain")

    def do_POST(self):
        if self.path == "/api/refresh":
            start_refresh()
            with state_lock:
                self.send(202, json.dumps(state), "application/json")
        elif self.path == "/api/ignore":
            body = self.read_json()
            if not body or not body.get("repo"):
                self.send(400, json.dumps({"error": "repo required"}), "application/json")
                return
            try:
                patterns = append_ignore_repo(body["repo"].strip())
            except ValueError as e:
                self.send(400, json.dumps({"error": str(e)}), "application/json")
                return
            # Drop the repo from the cache so a reload stays consistent.
            if os.path.exists(CACHE_FILE):
                try:
                    with open(CACHE_FILE) as f:
                        cache = json.load(f)
                    before = len(cache.get("repos") or [])
                    cache["repos"] = [r for r in cache.get("repos") or []
                                      if r.get("repo") != body["repo"].strip()]
                    cache["ignored"] = cache.get("ignored", 0) + (before - len(cache["repos"]))
                    cache["ignorePatterns"] = patterns
                    cache["ignoreFile"] = load_ignore_file()
                    tmp = CACHE_FILE + ".tmp"
                    with open(tmp, "w") as f:
                        json.dump(cache, f)
                    os.replace(tmp, CACHE_FILE)
                except Exception:
                    pass
            self.send(200, json.dumps({
                "ok": True,
                "patterns": patterns,
                "content": load_ignore_file(),
            }), "application/json")
        else:
            self.send(404, "Not found", "text/plain")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--refresh", action="store_true", help="fetch fresh data on start")
    ap.add_argument("--no-open", action="store_true", help="don't open a browser")
    args = ap.parse_args()

    try:
        gh("auth", "status")
    except Exception:
        sys.exit("Error: run 'gh auth login' first.")

    if args.refresh or not os.path.exists(CACHE_FILE):
        start_refresh()

    url = "http://localhost:%d" % args.port
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print("Dashboard on %s (Ctrl-C to stop)" % url, file=sys.stderr)
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
