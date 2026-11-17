#!/usr/bin/env python3
"""Test harness for git-forge v2.

Starts main.py on an ephemeral port (never 8080), exercises REST v3
(under /api/v3, token-authenticated, viewer adaiversen), GraphQL, MCP
and git smart HTTP — including protocol v2 ls-refs and a real `git push`
+ `git clone` round-trip when a git client is present (the *test* uses
git, the server never does) — and reports pass/fail per check.
Exit code 0 iff all pass.
"""
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
MAIN = HERE / "main.py"
TOKEN = "test-token"
OWNER = "adaiversen"

CHECKS = []


def check(name, ok, detail=""):
    CHECKS.append((name, bool(ok), detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))


def req(base, method, path, body=None, headers=None, auth=True):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(base + path, data=data, method=method)
    r.add_header("Content-Type", "application/json")
    r.add_header("Accept", "application/vnd.github+json")
    if auth:
        r.add_header("Authorization", f"Bearer {TOKEN}")
    for k, v in (headers or {}).items():
        r.add_header(k, v)
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            raw = resp.read()
            try:
                return resp.status, json.loads(raw.decode())
            except Exception:
                return resp.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw.decode())
        except Exception:
            return e.code, raw


def pkt(data: bytes) -> bytes:
    return ("%04x" % (len(data) + 4)).encode() + data


def main():
    tmp = Path(tempfile.mkdtemp(prefix="forge-test-"))
    data = tmp / "data"
    env = dict(os.environ, MAKEFUN_SHOP_TOKEN=TOKEN)
    proc = subprocess.Popen(
        [sys.executable, str(MAIN), "--host", "127.0.0.1", "--port", "0",
         "--data-dir", str(data)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
    port = None
    try:
        for _ in range(100):
            if (data / "port.txt").exists():
                port = int((data / "port.txt").read_text().strip())
                break
            time.sleep(0.05)
        if not port:
            print("server did not start; output:")
            proc.kill()
            print(proc.stdout.read() if proc.stdout else "")
            return 1
        check("server binds ephemeral port (not 8080)", port != 8080, f"port={port}")
        base = f"http://127.0.0.1:{port}"

        s, b = req(base, "GET", "/health", auth=False)
        check("GET /health", s == 200 and b.get("status") == "ok", f"{s} {b}")
        s, b = req(base, "GET", "/healthz", auth=False)
        check("GET /healthz (boot probe)", s == 200 and b.get("status") == "ok", f"{s} {b}")
        s, b = req(base, "GET", "/api/v3/meta", auth=False)
        check("GET /api/v3/meta", s == 200 and b.get("installed_version"), f"{s} {b}")
        s, b = req(base, "GET", "/api/v3/user", auth=False)
        check("anon GET /api/v3/user -> 401 JSON message",
              s == 401 and isinstance(b.get("message"), str), f"{s} {b}")
        s, b = req(base, "GET", "/api/v3/user")
        check("GET /api/v3/user (viewer)", s == 200 and b.get("login") == OWNER, f"{s} {b}")

        s, repo = req(base, "POST", "/api/v3/user/repos",
                      {"name": "demo", "description": "demo repo", "auto_init": True})
        check("REST create repo", s == 201 and repo.get("full_name") == f"{OWNER}/demo",
              f"{s} {repo}")
        s, repo2 = req(base, "GET", f"/api/v3/repos/{OWNER}/demo")
        check("REST get repo", s == 200 and repo2.get("name") == "demo", f"{s}")
        s, repos = req(base, "GET", f"/api/v3/users/{OWNER}/repos")
        check("REST list user repos", s == 200 and len(repos) >= 1, f"{s} n={len(repos) if isinstance(repos, list) else '?'}")
        s, nf = req(base, "GET", "/api/v3/repos/olgabauer/demo")
        check("REST foreign-owner repo -> 404", s == 404, f"{s}")

        s, lbl = req(base, "POST", f"/api/v3/repos/{OWNER}/demo/labels", {"name": "bug"})
        check("REST create label", s == 201 and lbl.get("name") == "bug", f"{s} {lbl}")
        s, bad = req(base, "POST", f"/api/v3/repos/{OWNER}/demo/issues",
                     {"title": "bad label", "labels": ["nope"]})
        check("REST unknown label -> 422", s == 422 and "message" in bad, f"{s} {bad}")

        s, iss = req(base, "POST", f"/api/v3/repos/{OWNER}/demo/issues",
                     {"title": "First bug", "body": "it broke", "labels": ["bug"]})
        check("REST create issue", s == 201 and iss.get("number") == 1, f"{s} {iss}")
        s, iss2 = req(base, "GET", f"/api/v3/repos/{OWNER}/demo/issues/1")
        check("REST get issue", s == 200 and iss2.get("title") == "First bug", f"{s}")
        s, lst = req(base, "GET", f"/api/v3/repos/{OWNER}/demo/issues?state=all")
        check("REST list issues", s == 200 and len(lst) == 1, f"{s}")
        s, iss3 = req(base, "PATCH", f"/api/v3/repos/{OWNER}/demo/issues/1",
                      {"title": "First bug (updated)"})
        check("REST update issue", s == 200 and iss3.get("title") == "First bug (updated)", f"{s}")
        s, cm = req(base, "POST", f"/api/v3/repos/{OWNER}/demo/issues/1/comments",
                    {"body": "ack"})
        check("REST issue comment", s == 201 and cm.get("body") == "ack", f"{s}")

        s, pr = req(base, "POST", f"/api/v3/repos/{OWNER}/demo/pulls",
                    {"title": "Add thing", "head": "main", "base": "main", "body": "please"})
        check("REST create PR", s == 201 and pr.get("number") == 2, f"{s} {pr}")
        s, pr2 = req(base, "GET", f"/api/v3/repos/{OWNER}/demo/pulls/2")
        check("REST get PR", s == 200 and pr2.get("title") == "Add thing", f"{s}")
        s, prs = req(base, "GET", f"/api/v3/repos/{OWNER}/demo/pulls")
        check("REST list PRs", s == 200 and len(prs) == 1, f"{s}")

        content = base64.b64encode(b"hello forge\n").decode()
        s, created = req(base, "PUT", f"/api/v3/repos/{OWNER}/demo/contents/hello.txt",
                         {"message": "add hello", "content": content, "branch": "main"})
        check("REST contents PUT", s in (200, 201) and created.get("content", {}).get("path") == "hello.txt",
              f"{s} {created}")
        s, got = req(base, "GET", f"/api/v3/repos/{OWNER}/demo/contents/hello.txt")
        check("REST contents GET", s == 200 and base64.b64decode(got.get("content", "")).decode() == "hello forge\n",
              f"{s} {got if s != 200 else ''}")
        old_sha = got.get("sha")
        s, upd = req(base, "PUT", f"/api/v3/repos/{OWNER}/demo/contents/hello.txt",
                     {"message": "update hello", "content": base64.b64encode(b"hello v2\n").decode(),
                      "sha": old_sha, "branch": "main"})
        check("REST contents PUT with correct sha -> 200",
              s == 200 and upd.get("content", {}).get("sha") != old_sha, f"{s} {upd}")
        s, stale = req(base, "PUT", f"/api/v3/repos/{OWNER}/demo/contents/hello.txt",
                       {"message": "stale", "content": base64.b64encode(b"stale\n").decode(),
                        "sha": old_sha, "branch": "main"})
        check("REST contents PUT with stale sha -> 409", s == 409 and "message" in stale, f"{s} {stale}")
        s, listing = req(base, "GET", f"/api/v3/repos/{OWNER}/demo/contents/")
        check("REST contents dir listing", s == 200 and isinstance(listing, list) and len(listing) >= 1, f"{s}")
        s, branches = req(base, "GET", f"/api/v3/repos/{OWNER}/demo/branches")
        check("REST list branches", s == 200 and any(x["name"] == "main" for x in branches), f"{s}")
        s, refs = req(base, "GET", f"/api/v3/repos/{OWNER}/demo/git/refs")
        check("REST git refs", s == 200 and any(x["ref"] == "refs/heads/main" for x in refs), f"{s}")
        s, nf = req(base, "GET", f"/api/v3/repos/{OWNER}/demo/issues/999")
        check("REST 404 shape", s == 404 and "message" in nf, f"{s}")
        s, bad = req(base, "POST", "/api/v3/user/repos", {"name": "demo"})
        check("REST duplicate repo -> 422", s == 422, f"{s}")

        # GraphQL
        gql_q = (f'{{ repository(owner: "{OWNER}", name: "demo") {{ name nameWithOwner '
                 'issues(first: 10) { totalCount nodes { number title state } } } }')
        s, g = req(base, "POST", "/api/graphql", {"query": gql_q})
        check("GraphQL repository+issues query",
              s == 200 and g.get("data", {}).get("repository", {}).get("name") == "demo"
              and g.get("data", {}).get("repository", {}).get("issues", {}).get("totalCount") == 1,
              f"{s} {g}")
        s, g = req(base, "POST", "/api/graphql", {"query": "{ viewer { login } }"})
        check("GraphQL viewer", s == 200 and g.get("data", {}).get("viewer", {}).get("login") == OWNER, f"{s} {g}")
        s, g = req(base, "POST", "/api/graphql",
                   {"query": f'mutation {{ createIssue(input: {{repositoryId: "{OWNER}/demo", title: "via gql"}}) {{ issue {{ number title }} }} }}'})
        check("GraphQL createIssue mutation",
              s == 200 and g.get("data", {}).get("createIssue", {}).get("issue", {}).get("number") == 3,
              f"{s} {g}")
        s, g = req(base, "POST", "/api/graphql", {"query": "{ viewer { noSuchFieldOnViewer } }"})
        check("GraphQL invalid field -> 200 with errors",
              s == 200 and any("noSuchFieldOnViewer" in (e.get("message") or "")
                               for e in g.get("errors", [])), f"{s} {g}")
        s, g = req(base, "POST", "/api/graphql",
                   [{"query": "{ viewer { login } }"}, {"query": "{ viewer { login } }"}])
        check("GraphQL batched operations",
              s == 200 and isinstance(g, list) and len(g) == 2
              and all(x.get("data", {}).get("viewer", {}).get("login") == OWNER for x in g),
              f"{s} {g}")

        # MCP
        s, m = req(base, "POST", "/mcp",
                   {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}}})
        check("MCP initialize", s == 200 and m.get("result", {}).get("serverInfo", {}).get("name") == "git-forge", f"{s} {m}")
        s, m = req(base, "POST", "/mcp",
                   {"jsonrpc": "2.0", "id": 2, "method": "server/discover", "params": {}})
        check("MCP server/discover", s == 200 and "2026-07-28" in m.get("result", {}).get("supportedVersions", []), f"{s} {m}")
        s, m = req(base, "POST", "/mcp", {"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
        names = [t["name"] for t in m.get("result", {}).get("tools", [])]
        check("MCP tools/list", s == 200 and "list_repos" in names and "create_issue" in names
              and "merge_pull_request" in names, f"{s} {names}")
        s, m = req(base, "POST", "/mcp",
                   {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                    "params": {"name": "list_repos", "arguments": {}}})
        check("MCP tools/call list_repos", s == 200 and "demo" in m.get("result", {}).get("content", [{}])[0].get("text", ""), f"{s} {m}")
        s, m = req(base, "POST", "/mcp",
                   {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                    "params": {"name": "get_issue", "arguments": {"owner": OWNER, "repo": "demo", "issue_number": 1}}})
        check("MCP tools/call get_issue", s == 200 and "First bug" in m.get("result", {}).get("content", [{}])[0].get("text", ""), f"{s} {m}")
        s, m = req(base, "POST", "/mcp",
                   {"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                    "params": {"name": "create_issue", "arguments": {"owner": OWNER, "repo": "demo", "title": "via mcp"}}})
        check("MCP tools/call create_issue", s == 200 and "via mcp" in m.get("result", {}).get("content", [{}])[0].get("text", ""), f"{s} {m}")
        s, m = req(base, "POST", "/mcp",
                   {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                    "params": {"name": "create_issue", "arguments": {}}},
                   auth=False)
        check("MCP tools/call unauthenticated -> 401", s == 401, f"{s} {m}")

        # git smart HTTP: info/refs
        r = urllib.request.Request(base + f"/{OWNER}/demo.git/info/refs?service=git-upload-pack")
        with urllib.request.urlopen(r, timeout=10) as resp:
            raw = resp.read()
            check("git info/refs upload-pack", resp.status == 200 and b"git-upload-pack" in raw and b"refs/heads/main" in raw,
                  f"{resp.status} {len(raw)}B")
        r = urllib.request.Request(base + f"/{OWNER}/demo.git/info/refs?service=git-receive-pack")
        with urllib.request.urlopen(r, timeout=10) as resp:
            raw = resp.read()
            check("git info/refs receive-pack", resp.status == 200 and b"git-receive-pack" in raw, f"{resp.status}")
        # protocol v2 advertisement + ls-refs
        r = urllib.request.Request(base + f"/{OWNER}/demo.git/info/refs?service=git-upload-pack")
        r.add_header("Git-Protocol", "version=2")
        with urllib.request.urlopen(r, timeout=10) as resp:
            raw = resp.read()
            check("git v2 advertisement", resp.status == 200 and b"version 2" in raw and b"ls-refs=unborn" in raw,
                  f"{resp.status} {raw[:200]}")
        body = (pkt(b"command=ls-refs\n") + pkt(b"agent=test/1.0\n")
                + pkt(b"object-format=sha1\n") + b"0001"
                + pkt(b"peel\n") + pkt(b"symrefs\n") + pkt(b"unborn\n") + b"0000")
        r = urllib.request.Request(base + f"/{OWNER}/demo.git/git-upload-pack", data=body, method="POST")
        r.add_header("Content-Type", "application/x-git-upload-pack-request")
        r.add_header("Git-Protocol", "version=2")
        with urllib.request.urlopen(r, timeout=10) as resp:
            raw = resp.read()
            check("git v2 ls-refs", resp.status == 200 and b"refs/heads/main" in raw
                  and b"symref-target:refs/heads/main" in raw, f"{resp.status} {raw[:200]}")

        # deterministic export: two reads identical
        s1, e1 = req(base, "GET", "/export", auth=False)
        s2, e2 = req(base, "GET", "/export", auth=False)
        check("deterministic export (two reads identical)", s1 == 200 and e1 == e2, f"{s1}/{s2}")

        # real git round-trip (test-only use of the git client)
        git = shutil.which("git")
        if git:
            work = tmp / "work"
            work.mkdir()
            env2 = dict(os.environ, GIT_AUTHOR_NAME="Tester", GIT_AUTHOR_EMAIL="t@example.com",
                        GIT_COMMITTER_NAME="Tester", GIT_COMMITTER_EMAIL="t@example.com",
                        GIT_AUTHOR_DATE="2026-01-01T00:00:00Z", GIT_COMMITTER_DATE="2026-01-01T00:00:00Z")
            def gitcmd(*a, cwd=None):
                return subprocess.run([git, *a], cwd=cwd, env=env2, capture_output=True, text=True, timeout=30)
            r1 = gitcmd("init", "-b", "feature", str(work / "src"))
            (work / "src" / "pushed.txt").write_text("pushed via git\n")
            gitcmd("add", ".", cwd=work / "src")
            gitcmd("commit", "-m", "push me", cwd=work / "src")
            r2 = gitcmd("push", f"http://127.0.0.1:{port}/{OWNER}/demo.git", "feature:feature", cwd=work / "src")
            check("git push (receive-pack)", r2.returncode == 0, r2.stderr.strip()[-300:])
            r3 = gitcmd("clone", "--depth", "1", f"http://127.0.0.1:{port}/{OWNER}/demo.git",
                        str(work / "clone1"))
            ok_clone = r3.returncode == 0 and (work / "clone1" / "hello.txt").exists()
            detail = (work / "clone1" / "hello.txt").read_text() if (work / "clone1" / "hello.txt").exists() else r3.stderr.strip()[-300:]
            check("git shallow clone of pushed repo", ok_clone, detail)
            # full clone should contain the pushed branch file too
            r4 = gitcmd("clone", f"http://127.0.0.1:{port}/{OWNER}/demo.git", str(work / "clone2"))
            ok2 = r4.returncode == 0
            if ok2:
                gitcmd("checkout", "feature", cwd=work / "clone2")
                ok2 = (work / "clone2" / "pushed.txt").read_text() == "pushed via git\n" if (work / "clone2" / "pushed.txt").exists() else False
            check("git full clone + checkout pushed branch", ok2, r4.stderr.strip()[-300:])
            # non-fast-forward push must be refused
            (work / "src" / "pushed.txt").write_text("diverged\n")
            gitcmd("add", ".", cwd=work / "src")
            gitcmd("commit", "--amend", "-m", "rewritten", cwd=work / "src")
            r5 = gitcmd("push", "-f", f"http://127.0.0.1:{port}/{OWNER}/demo.git", "feature:feature", cwd=work / "src")
            check("git non-fast-forward push refused",
                  r5.returncode != 0 and "non-fast-forward" in r5.stderr, r5.stderr.strip()[-300:])
            # real three-way merge via PUT
            s, prm = req(base, "POST", f"/api/v3/repos/{OWNER}/demo/pulls",
                         {"title": "Merge feature", "head": "feature", "base": "main", "body": "merge me"})
            pr_num = prm.get("number") if isinstance(prm, dict) else None
            check("REST create merge PR", s == 201 and pr_num, f"{s} {prm}")
            if pr_num:
                s, mg = req(base, "PUT", f"/api/v3/repos/{OWNER}/demo/pulls/{pr_num}/merge",
                            {"merge_method": "squash"})
                check("REST PUT merge PR", s == 200 and mg.get("merged") is True
                      and len(mg.get("sha", "")) == 40, f"{s} {mg}")
                s, landed = req(base, "GET", f"/api/v3/repos/{OWNER}/demo/contents/pushed.txt")
                check("merged content lands on main", s == 200
                      and base64.b64decode(landed.get("content", "")).decode() == "pushed via git\n",
                      f"{s} {landed if s != 200 else ''}")
        else:
            print("SKIP  git push/clone round-trip (no git client)")

        passed = sum(1 for _, ok, _ in CHECKS if ok)
        print(f"\n{passed}/{len(CHECKS)} checks passed")
        return 0 if passed == len(CHECKS) else 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
