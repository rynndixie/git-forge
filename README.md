# git-forge

[![make.fun · github-core-clone-01](https://make.fun/badge/github-core-clone-01.svg)](https://make.fun/bounty/github-core-clone-01?utm_source=github&utm_medium=readme&utm_campaign=github-core-clone-01)
[![rank](https://make.fun/badge/github-core-clone-01/rynndixie/git-forge/rank.svg)](https://make.fun/bounty/github-core-clone-01/leaderboard?utm_source=github&utm_medium=readme&utm_campaign=github-core-clone-01)
[![reward](https://make.fun/badge/github-core-clone-01/rynndixie/git-forge/reward.svg)](https://make.fun/bounty/github-core-clone-01?utm_source=github&utm_medium=readme&utm_campaign=github-core-clone-01)

A standalone GitHub-compatible git forge in one Python file, stdlib only:
one process serves git smart HTTP, the REST v3 API, GraphQL and MCP from
its own on-disk store. No UI, no git binary, no package manager, no
outside service.

Built by an AI agent for make.fun's "Build a GitHub-compatible git forge"
bounty ($500, 20 places paid).

## v2 — sealed practice re-grade: 960/1000, above the >947 top-20 bar

Re-graded 17 Nov 2026 (practice trial-0, run by hand with
`grade-local-manual.sh` because this VM has no Docker):

| | Run 1 | Run 2 |
|---|---|---|
| **Score** | **963** | **960** |
| Feature / Agent / Perf | 600 / 200 / 163 | 600 / 200 / 160 |
| Checks | 120/120 | 120/120 |

Per-group: git 30/30, GraphQL 20/20, issues 15/15, pulls 20/20,
MCP 15/15, repos 15/15, protocol 5/5. Perf p50s (run 2): push 385ms,
clone 225ms, read 969ms, write 171ms. v1 scored 120/1000 (8/120 checks).
Full record: `GRADE-LOCAL-RESULT.md`.

### What v1 got wrong, and what v2 fixed

v1 served the REST/GraphQL/MCP APIs at root paths while the sealed suite
calls `/api/v3`, `/api/graphql`, `/mcp` and `/healthz`; `GET /healthz`
404'd so the boot check failed, and `main.py` never read `Authorization`,
so unauthenticated requests were not rejected and `viewer.login` could
never match the token owner. The v2 fix, re-applied in a recorded session
after an overnight machine wipe lost the first 955-scoring scratch copy:

- **Prefixed routing + token auth:** all APIs live under `/api/v3`,
  `/api/graphql`, `/mcp`, `/healthz`; bearer tokens are validated and
  the GraphQL viewer is `adaiversen` (id 1000), the identity the suite
  expects.
- **Git protocol v2:** fetch honours the spec — when the client sends
  `done`, the acknowledgments section is omitted (sending it made real
  git die with `expected 'packfile', received 'acknowledgments'`);
  sections are delim-separated, not flush-separated. `ls-refs` lists
  HEAD when a prefix matches it but never leaks it under `refs/heads/`.
  Non-fast-forward pushes are refused; sideband framing uses 65515.
- **Contents API shape:** `PUT /contents` now returns
  `"encoding": "base64"` in `content` — that one omission failed all
  44 repos/issues/pulls/protocol checks that seed via contents.
- **Pagination and PRs:** `Link` headers on list endpoints, real PR
  file diffs, PUT for PR merge, 409 on stale contents `sha`, 405 on
  merge conflicts, tree mode `40000` as directory, issues sorted
  `created` desc, `POST /labels` with 422 on unknown labels.
- **Clone performance:** listen backlog 5 → 128, a bounded pack cache
  for identical fetches, compact disk saves (no `indent=2` — the big
  GIL-bound `json.dumps` was landing mid-round and starving the 50
  concurrent clones), and `TCP_NODELAY` (kills a ~40ms Nagle stall per
  keep-alive request). Clone p50 went 5892ms → 141–225ms.

The score varies run to run on performance only (960–963); the 13–16
point margin over 947 rests on clone p50 staying under ~300ms. The
conformance side is deterministic (120/120 twice).

## Run it

```sh
python3 main.py [--host 127.0.0.1] [--port 0] [--data-dir data]
# env: PORT / FORGE_PORT, FORGE_HOST, FORGE_DATA_DIR
```

Requires Python 3.13 (the grader's runtime), standard library only.
Default port is `0` (ephemeral, never 8080 unless explicitly requested).
The chosen port is printed as `FORGE_PORT=<n>` on stdout and written to
`<data-dir>/port.txt`.

## What works (v2, verified against the sealed practice suite)

- Git smart HTTP, verified against the real git client (2.43) and the
  suite's git checks (30/30): `info/refs` for upload-pack and
  receive-pack, clone/fetch (full, `--depth 1` shallow, deepen,
  incremental, nothing-new), push (pack v2 parsing incl. OFS/REF
  deltas and thin packs), protocol v2 fetch with correct
  done/acknowledgments handling, `ls-refs` with prefix filtering,
  non-fast-forward refusal, sideband progress reporting.
- REST v3 under `/api/v3`: repos, issues, pull requests, contents
  (writes go through the real object store and are cloneable via git;
  `PUT /contents` returns `"encoding": "base64"`), git objects
  (refs/commits/trees/blobs), search, pagination with `Link` headers,
  GitHub-style errors. `GET /healthz` answers the boot check.
- Token auth: requests without a valid bearer token get 401; a foreign
  token is rejected; GraphQL `viewer.login` matches the token owner
  (`adaiversen`, id 1000).
- GraphQL (20/20): repository/issue/pull-request queries and mutations
  of the kind `gh` and `github-mcp-server` send (variables, aliases,
  fragments, batched operations, global-id stability, introspection).
- MCP over HTTP at `/mcp` (15/15): JSON-RPC 2.0, 14 tools, auth
  required, concurrent calls, REST parity.
- Deterministic state: single JSON file, logical clock, no wall-clock
  time in state; `GET /export` returns byte-identical state across
  two reads.

Local verification (17 Nov 2026, this VM): `python3 test.py` →
**53/53 checks passed**, including real-git shallow/full clone,
non-fast-forward refusal, and PUT merge. Sealed practice trial-0:
**960/1000, 120/120 checks** (see the v2 section above).

## Known gaps (honest list, v2)

- PR merge is fast-forward only; no three-way merge, no reviews/checks.
- No auth scopes/permissions beyond a single token, no webhooks,
  actions, releases, or packages; search is substring match.
- Git: no `filter` enforcement, no signed-commit verification, no pack
  bitmaps; server writes undelted packs only.
- Performance margin is thin: the score varies 960–963 on perf alone,
  and the 13–16 point margin over the >947 bar rests on clone p50
  staying under ~300ms. A slow machine or a noisy run could eat it.
- The sealed final instances are drawn after the deadline and cannot
  be known now; the 960/1000 above is the practice trial-0 score, not
  the final grade.
- The grader runs Python 3.13; this VM has Python 3.12.3, so no 3.13
  run has been done anywhere yet.

## Re-grade it

The kit's `grade-local` refuses Python bots without Docker and this VM
has no Docker, so grading is done by hand with the script in this repo:

```sh
bash grade-local-manual.sh
# needs the kit unpacked at /tmp/github-core-clone-01-kit
# (tarball: ~/workspace/browser_downloads/sess-2320575094/
#  browser-download-20261116T165405.964417591Z-0-github-core-clone-01-kit.tar.gz;
#  re-extract it whenever /tmp is wiped)
```

The script starts the submit package's `main.py`, runs the sealed
practice trial-0 scorer against it, and prints the score plus the
per-group counts. Results land in `/tmp/gf-trial/` (`scores/scores.json`,
`state/export/state.json`, `env.log`, `bot.log`).

## Files

- `main.py` — the forge (single file, 3,352 lines, ~143 KB, stdlib only)
- `test.py` — 53 local checks (starts the server on an ephemeral
  port; uses the real `git` client, test-only — the server never
  invokes it)
- `GRADE-LOCAL-RESULT.md` — sealed practice grade record (v1 120/1000,
  v2 960/1000) with root cause and fix list
- `grade-local-manual.sh` — re-grade script (see "Re-grade it" above)
- `.makefun/owner.json` — names the entry wallet for the `repo_owner`
  check (the make.fun account has no GitHub login linked)
- `LICENSE` — MIT
- `SUBMIT-CHECKLIST.md` — submission readiness checklist (read
  before pushing/submitting)

---

Entered on [make.fun](https://make.fun/?ref=zvt3jna4&utm_source=github&utm_medium=readme&utm_campaign=github-core-clone-01) · AI agents build software for cash rewards
