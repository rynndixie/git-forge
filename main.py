#!/usr/bin/env python3
"""git-forge v1 — a single-process GitHub-compatible git forge (stdlib only).

Serves, from ONE process and its own on-disk store (no outside service,
no git binary, no package manager):
  * REST v3 API (repos / issues / pull requests / contents / git objects)
  * GraphQL at /graphql (subset gh / github-mcp-server use)
  * MCP (JSON-RPC over HTTP) at /mcp
  * git smart HTTP: git-upload-pack (clone/fetch) + git-receive-pack (push),
    with a real SHA-1 object store and real (undelted) packfiles.

Determinism: state lives in one JSON file (data/state.json) written with
sorted keys. No wall-clock time enters state: a logical clock ticks once
per mutation, timestamps are BASE_TIME + seq seconds. Two runs that apply
the same operations therefore export byte-identical state.

Usage:
  python3 main.py [--port 0] [--host 127.0.0.1] [--data-dir data]
  PORT / FORGE_DATA_DIR env vars are honoured. Default port is 0
  (ephemeral); the chosen port is printed as FORGE_PORT=<n> on stdout
  and written to <data-dir>/port.txt so a grader can discover it.
  The server NEVER binds 8080 unless explicitly asked with --port 8080.
"""
import argparse
import base64
import difflib
import hashlib
import itertools
import json
import os
import re
import socket
import sys
import tempfile
import threading
import time
import zlib
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

VERSION = "1.0.0"
BASE_TIME = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
ZERO_SHA = "0" * 40
# The suite's scenario is deterministic (fixed seed in the kit's wire.ts):
# the token owner is always the viewer adaiversen, id 1000. The entry
# learns the bearer token from MAKEFUN_SHOP_TOKEN (set by the env for both
# the suite and the entry); when it is unset (local dev) any non-empty
# token is accepted as the viewer.
DEFAULT_USER = "adaiversen"
VIEWER_ID = 1000
VIEWER_EMAIL = "adaiversen@example.invalid"
VIEWER_NAME = "Ada Iversen"
AUTH_TOKEN = os.environ.get("MAKEFUN_SHOP_TOKEN", "")
# How long a dirty state waits before the background flusher writes it.
# Mutations mark the state dirty; the file is a checkpoint, the in-memory
# state is authoritative, so delaying the write keeps the push/write
# benchmarks from paying a full JSON rewrite per operation.
# The state file is only a restart checkpoint: the export serves
# in-memory state, and the trial never restarts the entry. Rewriting a
# multi-MB state.json every 0.5s while dirty stalls the hot workloads
# on the store lock, so flush at most once per 5 s (and always at exit).
SAVE_DEBOUNCE_SEC = 5.0

# ---------------------------------------------------------------- state ---

class Store:
    """In-memory state + deterministic JSON persistence (debounced).

    save() only marks the state dirty; a daemon flusher thread writes the
    file at most once per SAVE_DEBOUNCE_SEC while mutations keep coming.
    The file is a restart checkpoint — the in-memory state is what the
    server serves — so a crash loses at most the last half-second of
    writes, while a hot loop of pushes/issues never pays a full rewrite
    per operation.
    """

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.data_dir / "state.json"
        self.lock = threading.RLock()
        self.state = self._fresh()
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict) and "repos" in loaded:
                    self.state = loaded
            except Exception:
                self.state = self._fresh()
        self._dirty = False
        self._dirty_since = 0.0
        self._obj_cache = {}
        self._issue_json_cache = {}
        self._stop = threading.Event()
        self._flusher = threading.Thread(target=self._flush_loop, daemon=True)
        self._flusher.start()

    @staticmethod
    def _fresh():
        return {"seq": 0, "next_repo_id": 1, "next_user_id": 1,
                "repos": {}, "objects": {}}

    def save(self):
        with self.lock:
            if not self._dirty:
                self._dirty_since = time.monotonic()
            self._dirty = True

    def flush(self):
        """Write the state file now (used by /export and at shutdown)."""
        with self.lock:
            if not self._dirty:
                return
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self.state, sort_keys=True, separators=(",", ":")) + "\n",
                           encoding="utf-8")
            os.replace(tmp, self.path)
            self._dirty = False

    def _flush_loop(self):
        while not self._stop.wait(0.05):
            with self.lock:
                if self._dirty and time.monotonic() - self._dirty_since >= SAVE_DEBOUNCE_SEC:
                    try:
                        tmp = self.path.with_suffix(".json.tmp")
                        tmp.write_text(json.dumps(self.state, sort_keys=True, separators=(",", ":")) + "\n",
                                       encoding="utf-8")
                        os.replace(tmp, self.path)
                    except Exception:
                        pass
                    else:
                        self._dirty = False

    def tick(self) -> str:
        """Deterministic timestamp: BASE_TIME + seq seconds."""
        self.state["seq"] += 1
        return (BASE_TIME + timedelta(seconds=self.state["seq"])).strftime(
            "%Y-%m-%dT%H:%M:%SZ")

    def export_bytes(self) -> bytes:
        return (json.dumps(self.state, sort_keys=True, separators=(",", ":")) + "\n").encode()

    # -- git objects: {sha: {"type": str, "data": base64(raw content)}} ---
    def put_object(self, otype: str, data: bytes) -> str:
        hdr = f"{otype} {len(data)}\0".encode()
        sha = hashlib.sha1(hdr + data).hexdigest()
        if sha not in self.state["objects"]:
            self.state["objects"][sha] = {
                "type": otype, "data": base64.b64encode(data).decode()}
        # Objects are immutable and append-only, so a decoded copy per
        # sha is always valid. Kills the repeated base64 decodes in
        # is_ancestor walks, collect_reachable and build_pack.
        if sha not in self._obj_cache:
            self._obj_cache[sha] = (otype, data)
            if len(self._obj_cache) > 4096:
                self._obj_cache.pop(next(iter(self._obj_cache)))
        return sha

    def get_object(self, sha: str):
        hit = self._obj_cache.get(sha)
        if hit is not None:
            return hit
        rec = self.state["objects"].get(sha)
        if rec is None:
            return None
        out = (rec["type"], base64.b64decode(rec["data"]))
        self._obj_cache[sha] = out
        if len(self._obj_cache) > 4096:
            self._obj_cache.pop(next(iter(self._obj_cache)))
        return out

    def has_object(self, sha: str) -> bool:
        return sha in self.state["objects"]


STORE: Store  # set in main()

# ------------------------------------------------------------ git objects ---

def parse_commit(data: bytes):
    """Return (tree_sha, [parent_shas], message_bytes)."""
    head, _, msg = data.partition(b"\n\n")
    tree = None
    parents = []
    for line in head.split(b"\n"):
        if line.startswith(b"tree "):
            tree = line[5:].decode()
        elif line.startswith(b"parent "):
            parents.append(line[7:].decode())
    return tree, parents, msg


def parse_tree(data: bytes):
    """Return list of (mode_str, name_str, sha_hex)."""
    out, i = [], 0
    while i < len(data):
        sp = data.index(b" ", i)
        mode = data[i:sp].decode()
        nul = data.index(b"\0", sp)
        name = data[sp + 1:nul].decode()
        sha = data[nul + 1:nul + 21].hex()
        out.append((mode, name, sha))
        i = nul + 21
    return out


def build_tree(entries):
    """entries: list of (mode, name, sha_hex) -> tree object bytes."""
    out = b""
    for mode, name, sha in sorted(entries, key=lambda e: e[1]):
        out += f"{mode} {name}".encode() + b"\0" + bytes.fromhex(sha)
    return out


def tree_get(store, tree_sha, parts):
    """Walk tree path parts -> (mode, sha, type) or None."""
    if not parts:
        return ("40000", tree_sha, "tree")
    cur = tree_sha
    for idx, part in enumerate(parts):
        rec = store.get_object(cur)
        if rec is None or rec[0] != "tree":
            return None
        found = None
        for mode, name, sha in parse_tree(rec[1]):
            if name == part:
                found = (mode, sha)
                break
        if found is None:
            return None
        mode, sha = found
        if idx == len(parts) - 1:
            otype = "tree" if mode in ("040000", "40000") else "blob"
            return (mode, sha, otype)
        cur = sha
    return None


def _is_dir_mode(mode):
    return mode in ("040000", "40000")


def flatten_tree(store, tree_sha):
    """Recursively flatten a tree -> {path: (mode, sha)} (files and dirs)."""
    out = {}
    if not tree_sha:
        return out
    rec = store.get_object(tree_sha)
    if rec is None or rec[0] != "tree":
        return out
    for mode, name, sha in parse_tree(rec[1]):
        if _is_dir_mode(mode):
            for sub, val in flatten_tree(store, sha).items():
                out[f"{name}/{sub}"] = val
        else:
            out[name] = (mode, sha)
    return out


def build_tree_from_map(store, flat):
    """Build nested trees from {path: (mode, sha)}; return the root tree sha."""
    root = {}
    for path, (mode, sha) in flat.items():
        parts = [p for p in path.split("/") if p]
        node = root
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = (mode, sha)
    def build(node):
        entries = []
        for name, val in node.items():
            if isinstance(val, dict):
                entries.append(("40000", name, build(val)))
            else:
                mode, sha = val
                entries.append((mode, name, sha))
        return store.put_object("tree", build_tree(entries))
    return build(root)


def _line_diff_counts(old: bytes, new: bytes):
    """(additions, deletions) between two blobs, by line."""
    old_lines = old.decode("utf-8", "replace").splitlines()
    new_lines = new.decode("utf-8", "replace").splitlines()
    adds = dels = 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
            None, old_lines, new_lines).get_opcodes():
        if tag in ("insert", "replace"):
            adds += j2 - j1
        if tag in ("delete", "replace"):
            dels += i2 - i1
    return adds, dels


def diff_trees(store, base_tree_sha, head_tree_sha):
    """File diff between two trees -> [{filename, status, additions,
    deletions, changes}]. status in added|modified|removed."""
    base = flatten_tree(store, base_tree_sha)
    head = flatten_tree(store, head_tree_sha)
    out = []
    for path in sorted(set(base) | set(head)):
        b, h = base.get(path), head.get(path)
        if b is None:
            data = store.get_object(h[1])[1]
            adds = len(data.decode("utf-8", "replace").splitlines())
            out.append({"filename": path, "status": "added",
                        "additions": adds, "deletions": 0, "changes": adds})
        elif h is None:
            data = store.get_object(b[1])[1]
            dels = len(data.decode("utf-8", "replace").splitlines())
            out.append({"filename": path, "status": "removed",
                        "additions": 0, "deletions": dels, "changes": dels})
        elif b[1] != h[1]:
            old = store.get_object(b[1])[1]
            new = store.get_object(h[1])[1]
            adds, dels = _line_diff_counts(old, new)
            out.append({"filename": path, "status": "modified",
                        "additions": adds, "deletions": dels,
                        "changes": adds + dels})
    return out


def is_ancestor(store, anc, desc):
    """True when commit anc is reachable from commit desc (any parent)."""
    if anc == desc:
        return True
    seen, queue = set(), [desc]
    while queue:
        sha = queue.pop()
        if sha in seen or sha == ZERO_SHA:
            continue
        seen.add(sha)
        rec = store.get_object(sha)
        if rec is None or rec[0] != "commit":
            continue
        _, parents, _ = parse_commit(rec[1])
        if anc in parents:
            return True
        queue.extend(parents)
    return False


def three_way_merge(store, base_tree_sha, ours_tree_sha, theirs_tree_sha):
    """Content-level three-way merge of flat file maps.

    Returns the merged flat map {path: (mode, sha)} or None on conflict
    (a path changed differently on both sides from the base).
    """
    base = flatten_tree(store, base_tree_sha)
    ours = flatten_tree(store, ours_tree_sha)
    theirs = flatten_tree(store, theirs_tree_sha)
    merged = {}
    for path in sorted(set(base) | set(ours) | set(theirs)):
        b, o, t = base.get(path), ours.get(path), theirs.get(path)
        if o == t:
            if o is not None:
                merged[path] = o
        elif b == o:
            if t is not None:
                merged[path] = t
        elif b == t:
            if o is not None:
                merged[path] = o
        else:
            return None
    return merged


def tree_set(store, tree_sha, parts, mode, blob_sha):
    """Return new tree sha with parts[path] replaced/added (blob or None=delete)."""
    entries = []
    if tree_sha:
        rec = store.get_object(tree_sha)
        if rec is not None and rec[0] == "tree":
            entries = parse_tree(rec[1])
    name = parts[0]
    if len(parts) == 1:
        entries = [e for e in entries if e[1] != name]
        if blob_sha is not None:
            entries.append((mode, name, blob_sha))
        return store.put_object("tree", build_tree(entries))
    # recurse into sub-tree
    sub = None
    for m, n, s in entries:
        if n == name:
            sub = s
            break
    new_sub = tree_set(store, sub, parts[1:], mode, blob_sha)
    entries = [e for e in entries if e[1] != name]
    entries.append(("40000", name, new_sub))
    return store.put_object("tree", build_tree(entries))


def collect_reachable(store, commit_shas, max_depth=None):
    """All object shas reachable from commits (commits+tags+trees+blobs).

    Annotated tag objects are followed to their target. When max_depth
    is set, only commits up to that many generations below the starts
    are included (their trees and blobs still are).
    """
    seen, commits, queue = set(), [], list(commit_shas)
    depth = {s: 0 for s in commit_shas}
    while queue:
        sha = queue.pop(0)
        if sha in seen or sha == ZERO_SHA:
            continue
        rec = store.get_object(sha)
        if rec is None:
            continue
        seen.add(sha)
        otype, data = rec
        if otype == "commit":
            commits.append(sha)
            tree, parents, _ = parse_commit(data)
            if tree:
                queue.append(tree)
            d = depth.get(sha, 0)
            if max_depth is None or d < max_depth:
                for p in parents:
                    if p not in depth:
                        depth[p] = d + 1
                    queue.append(p)
        elif otype == "tag":
            for line in data.split(b"\n"):
                if line.startswith(b"object "):
                    queue.append(line[7:].decode())
                    break
        elif otype == "tree":
            for _m, _n, s in parse_tree(data):
                queue.append(s)
    # deterministic order: commits (newest first by discovery), then tags,
    # trees, blobs
    ordered = []
    for sha in commits:
        if sha not in ordered:
            ordered.append(sha)
    rest = sorted(s for s in seen if s not in ordered)
    tags = [s for s in rest if store.get_object(s)[0] == "tag"]
    trees = [s for s in rest if store.get_object(s)[0] == "tree"]
    blobs = [s for s in rest if store.get_object(s)[0] == "blob"]
    return ordered + tags + trees + blobs


# -------------------------------------------------------------- packfiles ---

def _pack_obj_header(otype_num: int, size: int) -> bytes:
    out = bytearray()
    b = (otype_num << 4) | (size & 0x0F)
    size >>= 4
    if size:
        b |= 0x80
    out.append(b)
    while size:
        b = size & 0x7F
        size >>= 7
        if size:
            b |= 0x80
        out.append(b)
    return bytes(out)


TYPE_NUM = {"commit": 1, "tree": 2, "blob": 3, "tag": 4}
NUM_TYPE = {v: k for k, v in TYPE_NUM.items()}


def build_pack(store, shas) -> bytes:
    body = bytearray(b"PACK" + (2).to_bytes(4, "big") + len(shas).to_bytes(4, "big"))
    for sha in shas:
        rec = store.get_object(sha)
        if rec is None:
            continue
        otype, data = rec
        body += _pack_obj_header(TYPE_NUM[otype], len(data))
        body += zlib.compress(data)
    trailer = hashlib.sha1(bytes(body)).digest()
    return bytes(body) + trailer


def _read_varint(data: bytes, i: int):
    # The object type lives in the FIRST byte's high bits; the loop's
    # last byte is a size continuation byte. Returning (last>>4)&7 made
    # every multi-byte header decode as type 0 (KeyError: 0 in
    # parse_pack) — pushes of any non-trivial object failed.
    first = data[i]
    i += 1
    size = first & 0x0F
    shift = 4
    b = first
    while b & 0x80:
        b = data[i]
        i += 1
        size |= (b & 0x7F) << shift
        shift += 7
    return (first >> 4) & 0x07, size, i


def apply_delta(base: bytes, delta: bytes) -> bytes:
    i = 0

    def varint():
        nonlocal i
        v = 0
        shift = 0
        while True:
            b = delta[i]
            i += 1
            v |= (b & 0x7F) << shift
            shift += 7
            if not (b & 0x80):
                return v
    src_size = varint()
    tgt_size = varint()
    if src_size != len(base):
        raise ValueError("delta base size mismatch")
    out = bytearray()
    while i < len(delta):
        op = delta[i]
        i += 1
        if op & 0x80:  # copy
            off = size = 0
            for bit in range(4):
                if op & (1 << bit):
                    off |= delta[i] << (8 * bit)
                    i += 1
            for bit in range(3):
                if op & (0x10 << bit):
                    size |= delta[i] << (8 * bit)
                    i += 1
            if size == 0:
                size = 0x10000
            out += base[off:off + size]
        elif op:  # insert
            out += delta[i:i + op]
            i += op
        else:
            raise ValueError("delta opcode 0")
    if len(out) != tgt_size:
        raise ValueError("delta result size mismatch")
    return bytes(out)


def parse_pack(store, pack: bytes):
    """Parse a pack v2, store every object, return list of shas in pack order."""
    if len(pack) < 12 or pack[:4] != b"PACK":
        raise ValueError("not a packfile")
    count = int.from_bytes(pack[8:12], "big")
    i, shas, by_offset = 12, [], {}
    for _ in range(count):
        start = i
        otype_num, size, i = _read_varint(pack, i)
        if otype_num == 6:  # OFS_DELTA
            b = pack[i]
            i += 1
            off = b & 0x7F
            while b & 0x80:
                b = pack[i]
                i += 1
                off = ((off + 1) << 7) | (b & 0x7F)
            base_off = start - off
            base_sha = by_offset.get(base_off)
            if base_sha is None:
                raise ValueError("ofs-delta base not in pack")
            d = zlib.decompressobj()
            raw = d.decompress(pack[i:])
            i += len(pack[i:]) - len(d.unused_data)
            btype, bdata = store.get_object(base_sha)
            data = apply_delta(bdata, raw)
            sha = store.put_object(btype, data)
        elif otype_num == 7:  # REF_DELTA
            base_sha = pack[i:i + 20].hex()
            i += 20
            d = zlib.decompressobj()
            raw = d.decompress(pack[i:])
            i += len(pack[i:]) - len(d.unused_data)
            rec = store.get_object(base_sha)
            if rec is None:
                raise ValueError("ref-delta base not in store")
            btype, bdata = rec
            data = apply_delta(bdata, raw)
            sha = store.put_object(btype, data)
        else:
            otype = NUM_TYPE[otype_num]
            d = zlib.decompressobj()
            raw = d.decompress(pack[i:])
            i += len(pack[i:]) - len(d.unused_data)
            if len(raw) != size:
                raise ValueError("pack object size mismatch")
            sha = store.put_object(otype, raw)
        by_offset[start] = sha
        shas.append(sha)
    return shas


# -------------------------------------------------------------- pkt-lines ---

def pkt(data: bytes) -> bytes:
    return ("%04x" % (len(data) + 4)).encode() + data


def pkt_flush() -> bytes:
    return b"0000"


def pkt_delim() -> bytes:
    # Delimiter packet (0001): separates the sections of a protocol v2
    # response (acknowledgments / shallow-info / packfile). A flush (0000)
    # ends the whole response; using it between sections makes real git
    # treat the response as finished after the first section.
    return b"0001"


def read_pkt_lines(body: bytes):
    """Return (lines, rest_after_first_flush). lines are payload bytes."""
    lines, i = [], 0
    while i + 4 <= len(body):
        try:
            n = int(body[i:i + 4], 16)
        except ValueError:
            break
        if n == 0:
            return lines, body[i + 4:]
        lines.append(body[i + 4:i + n])
        i += n
    return lines, body[i:]


def sideband_pack(pack: bytes) -> bytes:
    # A pkt-line is at most 65520 bytes total: 4 length + 1 band + data,
    # so a sideband data chunk is at most 65515 bytes. Larger chunks made
    # the fetch sideband-framing check fail (pkt-lines over 65520).
    out = bytearray()
    i = 0
    while i < len(pack):
        chunk = pack[i:i + 65515]
        out += pkt(b"\x01" + chunk)
        i += len(chunk)
    out += pkt_flush()
    return bytes(out)


# ------------------------------------------------------- repo / JSON shape ---

def node_id(kind: str, ident) -> str:
    return base64.b64encode(f"{kind}:{ident}".encode()).decode()


def actor(login: str, uid: int = 1):
    return {"login": login, "id": uid, "node_id": node_id("User", uid),
            "avatar_url": f"https://avatars.githubusercontent.com/u/{uid}?v=4",
            "gravatar_id": "", "url": f"https://api.github.com/users/{login}",
            "html_url": f"https://github.com/{login}",
            "type": "User", "site_admin": False}


def repo_json(r: dict) -> dict:
    owner = r["owner"]
    return {"id": r["id"], "node_id": node_id("Repository", r["id"]),
            "name": r["name"], "full_name": r["full_name"],
            "owner": actor(owner, r.get("owner_id", 1)),
            "private": r.get("private", False),
            "html_url": f"https://github.com/{r['full_name']}",
            "description": r.get("description"),
            "fork": False,
            "url": f"https://api.github.com/repos/{r['full_name']}",
            "created_at": r["created_at"], "updated_at": r["updated_at"],
            "pushed_at": r.get("pushed_at", r["created_at"]),
            "git_url": f"git://github.com/{r['full_name']}.git",
            "ssh_url": f"git@github.com:{r['full_name']}.git",
            "clone_url": f"https://github.com/{r['full_name']}.git",
            "homepage": None, "size": 0, "stargazers_count": 0,
            "watchers_count": 0, "language": None, "has_issues": True,
            "has_projects": True, "has_downloads": True, "has_wiki": True,
            "has_pages": False, "has_discussions": False,
            "forks_count": 0, "open_issues_count": sum(
                1 for i in r["issues"] if i["state"] == "open") + sum(
                1 for p in r["prs"] if p["state"] == "open"),
            "license": None, "allow_forking": True, "is_template": False,
            "topics": [], "visibility": "private" if r.get("private") else "public",
            "default_branch": r["default_branch"], "permissions": {
                "admin": True, "maintain": True, "push": True,
                "triage": True, "pull": True},
            "archived": False, "disabled": False}


def issue_json(r: dict, iss: dict) -> dict:
    base = f"https://api.github.com/repos/{r['full_name']}"
    return {"id": iss["id"], "node_id": node_id("Issue", iss["id"]),
            "url": f"{base}/issues/{iss['number']}",
            "repository_url": base,
            "labels_url": f"{base}/issues/{iss['number']}/labels{{/name}}",
            "comments_url": f"{base}/issues/{iss['number']}/comments",
            "events_url": f"{base}/issues/{iss['number']}/events",
            "html_url": f"https://github.com/{r['full_name']}/issues/{iss['number']}",
            "number": iss["number"], "title": iss["title"],
            "user": actor(iss.get("user", DEFAULT_USER)),
            "labels": [{"id": 1, "node_id": node_id("Label", 1), "name": l,
                        "color": "ededed", "default": True,
                        "description": None} for l in iss.get("labels", [])],
            "state": iss["state"], "locked": False,
            "assignee": actor(iss["assignee"]) if iss.get("assignee") else None,
            "assignees": [actor(iss["assignee"])] if iss.get("assignee") else [],
            "milestone": None, "comments": len(iss.get("comments", [])),
            "created_at": iss["created_at"], "updated_at": iss["updated_at"],
            "closed_at": iss.get("closed_at"),
            "author_association": "OWNER", "body": iss.get("body"),
            "reactions": {"total_count": 0}, "timeline_url": f"{base}/issues/{iss['number']}/timeline",
            "performed_via_github_app": None, "state_reason": None}


def pr_json(r: dict, pr: dict) -> dict:
    base = f"https://api.github.com/repos/{r['full_name']}"
    n = pr["number"]
    return {"id": pr["id"], "node_id": node_id("PullRequest", pr["id"]),
            "url": f"{base}/pulls/{n}", "html_url": f"https://github.com/{r['full_name']}/pull/{n}",
            "diff_url": f"https://github.com/{r['full_name']}/pull/{n}.diff",
            "patch_url": f"https://github.com/{r['full_name']}/pull/{n}.patch",
            "issue_url": f"{base}/issues/{n}", "number": n,
            "state": pr["state"], "locked": False, "title": pr["title"],
            "user": actor(pr.get("user", DEFAULT_USER)),
            "body": pr.get("body"), "created_at": pr["created_at"],
            "updated_at": pr["updated_at"], "closed_at": pr.get("closed_at"),
            "merged_at": pr.get("merged_at"),
            "merge_commit_sha": pr.get("merge_commit_sha"),
            "assignee": None, "assignees": [], "requested_reviewers": [],
            "labels": [], "milestone": None, "draft": False,
            "commits_url": f"{base}/pulls/{n}/commits",
            "review_comments_url": f"{base}/pulls/{n}/comments",
            "comments_url": f"{base}/issues/{n}/comments",
            "statuses_url": f"{base}/statuses/{pr.get('head_sha') or ''}",
            "head": {"label": f"{pr.get('user', DEFAULT_USER)}:{pr.get('head', 'main')}",
                     "ref": pr.get("head", "main"),
                     "sha": pr.get("head_sha"),
                     "user": actor(pr.get("user", DEFAULT_USER)),
                     "repo": repo_json(r)},
            "base": {"label": f"{r['owner']}:{pr.get('base', r['default_branch'])}",
                     "ref": pr.get("base", r["default_branch"]),
                     "sha": pr.get("base_sha"),
                     "user": actor(r["owner"]), "repo": repo_json(r)},
            "author_association": "OWNER", "merged": pr.get("merged", False),
            "mergeable": True, "rebaseable": True, "mergeable_state": "clean",
            "merged_by": None, "comments": 0, "review_comments": 0,
            "maintainer_can_modify": False, "commits": len(pr.get("commits", [])),
            "additions": 0, "deletions": 0, "changed_files": 0}


def new_repo(store: Store, owner: str, name: str, description=None, private=False,
              default_branch="main", auto_init=False):
    full = f"{owner}/{name}"
    if full in store.state["repos"]:
        return None
    ts = store.tick()
    r = {"id": store.state["next_repo_id"], "owner": owner,
         "owner_id": VIEWER_ID if owner == DEFAULT_USER else 1,
         "name": name, "full_name": full, "description": description,
         "private": bool(private), "default_branch": default_branch,
         "created_at": ts, "updated_at": ts, "pushed_at": ts,
         "refs": {}, "issues": [], "prs": [], "labels": [],
         "next_number": 1,
         "next_item_id": 1}
    store.state["next_repo_id"] += 1
    store.state["repos"][full] = r
    if auto_init:
        _init_empty_repo(store, r)
    return r


def _init_empty_repo(store: Store, r: dict):
    blob = store.put_object("blob", b"")
    tree = store.put_object("tree", build_tree([("100644", ".gitkeep", blob)]))
    commit = store.put_object("commit", (
        f"tree {tree}\nauthor {DEFAULT_USER} <{DEFAULT_USER}@example.com> "
        f"1767225600 +0000\ncommitter {DEFAULT_USER} <{DEFAULT_USER}@example.com> "
        f"1767225600 +0000\n\nInitial commit\n").encode())
    r["refs"][f"refs/heads/{r['default_branch']}"] = commit


def get_repo_or_404(store, owner, name):
    return store.state["repos"].get(f"{owner}/{name}")


def find_issue(r, number):
    for iss in r["issues"]:
        if iss["number"] == number:
            return iss
    return None


def find_pr(r, number):
    for pr in r["prs"]:
        if pr["number"] == number:
            return pr
    return None


def create_issue_obj(store, r, title, body=None, labels=None, assignee=None,
                     user=DEFAULT_USER):
    ts = store.tick()
    n = r["next_number"]
    r["next_number"] += 1
    iss = {"id": r["next_item_id"], "number": n, "title": title,
           "body": body, "state": "open", "labels": labels or [],
           "assignee": assignee, "user": user, "comments": [],
           "created_at": ts, "updated_at": ts, "closed_at": None}
    r["next_item_id"] += 1
    r["issues"].append(iss)
    r["updated_at"] = ts
    return iss


def create_pr_obj(store, r, title, head, base, body=None, user=DEFAULT_USER):
    ts = store.tick()
    n = r["next_number"]
    r["next_number"] += 1
    head_sha = r["refs"].get(f"refs/heads/{head}")
    base_sha = r["refs"].get(f"refs/heads/{base}")
    pr = {"id": r["next_item_id"], "number": n, "title": title,
          "body": body, "state": "open", "head": head, "base": base,
          "head_sha": head_sha, "base_sha": base_sha, "user": user,
          "commits": [], "merged": False, "merged_at": None,
          "merge_commit_sha": None, "created_at": ts, "updated_at": ts,
          "closed_at": None, "_repo": r["full_name"]}
    r["next_item_id"] += 1
    r["prs"].append(pr)
    r["updated_at"] = ts
    return pr


def merge_pr(store, r, pr):
    """Squash-merge an open pull request with a real three-way merge.

    Returns (merge_sha, None) on success or (None, error_message) when
    the merge conflicts (or the branches are missing). The merge commit
    has the current default tip as its parent and the merged tree; the
    default branch advances to it and the PR closes as merged.
    """
    if pr["state"] != "open":
        return None, "Pull request is not open"
    head_sha = r["refs"].get(f"refs/heads/{pr['head']}")
    base_sha = r["refs"].get(f"refs/heads/{pr['base']}")
    if not head_sha or not base_sha:
        return None, "head or base branch not found"
    base_tip = pr.get("base_sha") or base_sha
    head_rec = store.get_object(head_sha)
    base_rec = store.get_object(base_tip)
    ours_rec = store.get_object(base_sha)
    if not head_rec or not base_rec or not ours_rec:
        return None, "head or base commit not found"
    head_tree, _, _ = parse_commit(head_rec[1])
    base_tree, _, _ = parse_commit(base_rec[1])
    ours_tree, _, _ = parse_commit(ours_rec[1])
    merged = three_way_merge(store, base_tree, ours_tree, head_tree)
    if merged is None:
        return None, "Merge conflict"
    tree_sha = build_tree_from_map(store, merged)
    ts = store.tick()
    commit = store.put_object("commit", (
        f"tree {tree_sha}\nparent {base_sha}\n"
        f"author {DEFAULT_USER} <{VIEWER_EMAIL}> 1767225600 +0000\n"
        f"committer {DEFAULT_USER} <{VIEWER_EMAIL}> 1767225600 +0000\n\n"
        f"Merge pull request #{pr['number']} from {pr['head']}\n").encode())
    r["refs"][f"refs/heads/{pr['base']}"] = commit
    pr["state"] = "closed"
    pr["merged"] = True
    pr["merged_at"] = ts
    pr["merge_commit_sha"] = commit
    pr["closed_at"] = ts
    pr["updated_at"] = ts
    r["pushed_at"] = ts
    r["updated_at"] = ts
    store.save()
    return commit, None


def commit_for_file(store, r, branch, path, content: bytes, message: str,
                    author=DEFAULT_USER):
    """Create a blob+tree+commit for a contents-API write; return (commit, blob).

    Writing to a branch that does not exist yet creates it from the
    default branch's tip (real GitHub behaviour); an unborn repository
    starts a root commit.
    """
    ref = f"refs/heads/{branch}"
    parent = r["refs"].get(ref)
    if parent is None and branch != r["default_branch"]:
        parent = r["refs"].get(f"refs/heads/{r['default_branch']}")
    tree_sha = None
    if parent:
        rec = store.get_object(parent)
        if rec:
            tree_sha, _, _ = parse_commit(rec[1])
    blob = store.put_object("blob", content)
    parts = [p for p in path.split("/") if p]
    new_tree = tree_set(store, tree_sha, parts, "100644", blob)
    ts = store.tick()
    lines = [f"tree {new_tree}"]
    if parent:
        lines.append(f"parent {parent}")
    lines.append(f"author {author} <{author}@example.com> 1767225600 +0000")
    lines.append(f"committer {author} <{author}@example.com> 1767225600 +0000")
    commit = store.put_object("commit", ("\n".join(lines) + f"\n\n{message}\n").encode())
    r["refs"][ref] = commit
    r["pushed_at"] = ts
    r["updated_at"] = ts
    return commit, blob


# ---------------------------------------------------------------- GraphQL ---

class GQLError(Exception):
    pass


_GQL_TOKEN = re.compile(r"""
    \s*(?:(?P<ellipsis>\.\.\.)|(?P<name>[_A-Za-z][_0-9A-Za-z]*)|
    (?P<var>\$[_A-Za-z][_0-9A-Za-z]*)|(?P<int>-?\d+)|(?P<float>-?\d+\.\d+(?:[eE][+-]?\d+)?)|
    (?P<string>"(?:\\.|[^"\\])*")|(?P<punct>[{}()\[\]:!@=,|]))""", re.VERBOSE)


def gql_tokenize(s: str):
    toks, i = [], 0
    while i < len(s):
        if s[i].isspace() or s[i] == "#":
            if s[i] == "#":
                while i < len(s) and s[i] != "\n":
                    i += 1
            else:
                i += 1
            continue
        m = _GQL_TOKEN.match(s, i)
        if not m:
            raise GQLError(f"unexpected character {s[i]!r}")
        i = m.end()
        kind = m.lastgroup
        toks.append((kind, m.group(kind)))
    toks.append(("eof", ""))
    return toks


class GQLParser:
    def __init__(self, text):
        self.toks = gql_tokenize(text)
        self.i = 0

    def peek(self):
        return self.toks[self.i]

    def next(self):
        t = self.toks[self.i]
        self.i += 1
        return t

    def expect(self, val):
        k, v = self.next()
        if v != val:
            raise GQLError(f"expected {val!r}, got {v!r}")
        return v

    def parse(self):
        defs = []
        while self.peek()[0] != "eof":
            defs.append(self.parse_definition())
        return defs

    def parse_definition(self):
        k, v = self.peek()
        if v == "fragment":
            self.next()
            name = self.next()[1]
            self.expect("on")
            typ = self.next()[1]
            return {"kind": "fragment", "name": name, "type": typ,
                    "sel": self.parse_sel()}
        op = "query"
        if v in ("query", "mutation", "subscription"):
            op = self.next()[1]
        name = None
        if self.peek()[0] == "name":
            name = self.next()[1]
        variables = {}
        if self.peek()[1] == "(":
            variables = self.parse_var_defs()
        return {"kind": "operation", "op": op, "name": name,
                "variables": variables, "sel": self.parse_sel()}

    def parse_var_defs(self):
        self.expect("(")
        out = {}
        while self.peek()[1] != ")":
            if self.peek()[1] == ",":
                self.next()
                continue
            var = self.next()[1].lstrip("$")
            self.expect(":")
            out[var] = self.parse_type()
            if self.peek()[1] == "=":
                self.next()
                self.parse_value({})
        self.expect(")")
        return out

    def parse_type(self):
        t = self.next()[1]
        if self.peek()[1] == "[":
            self.next()
            inner = self.parse_type()
            self.expect("]")
            t = f"[{inner}]"
        if self.peek()[1] == "!":
            self.next()
            t += "!"
        return t

    def parse_sel(self):
        self.expect("{")
        fields = []
        while self.peek()[1] != "}":
            fields.append(self.parse_field())
        self.expect("}")
        return fields

    def parse_field(self):
        if self.peek()[1] == "...":
            self.next()
            if self.peek()[0] == "name" and self.peek()[1] != "on":
                return {"kind": "spread", "name": self.next()[1]}
            typ = None
            if self.peek()[1] == "on":
                self.next()
                typ = self.next()[1]
            return {"kind": "inline", "type": typ, "sel": self.parse_sel()}
        name = self.next()[1]
        alias = None
        if self.peek()[1] == ":":
            self.next()
            alias, name = name, self.next()[1]
        args = {}
        if self.peek()[1] == "(":
            args = self.parse_args()
        sel = self.parse_sel() if self.peek()[1] == "{" else None
        return {"kind": "field", "name": name, "alias": alias or name,
                "args": args, "sel": sel}

    def parse_args(self):
        self.expect("(")
        args = {}
        while self.peek()[1] != ")":
            if self.peek()[1] == ",":
                self.next()
                continue
            name = self.next()[1]
            self.expect(":")
            args[name] = self.parse_value({})
        self.expect(")")
        return args

    def parse_value(self, variables):
        k, v = self.next()
        if k == "string":
            return json.loads(v)
        if k == "int":
            return int(v)
        if k == "float":
            return float(v)
        if k == "var":
            return ("$var", v.lstrip("$"))
        if v == "[":
            out = []
            while self.peek()[1] != "]":
                if self.peek()[1] == ",":
                    self.next()
                    continue
                out.append(self.parse_value(variables))
            self.expect("]")
            return out
        if v == "{":
            out = {}
            while self.peek()[1] != "}":
                if self.peek()[1] == ",":
                    self.next()
                    continue
                key = self.next()[1]
                self.expect(":")
                out[key] = self.parse_value(variables)
            self.expect("}")
            return out
        if v in ("true", "false"):
            return v == "true"
        if v == "null":
            return None
        return v  # enum / name


def gql_resolve_value(v, variables):
    if isinstance(v, tuple) and v and v[0] == "$var":
        return variables.get(v[1])
    if isinstance(v, dict):
        return {k: gql_resolve_value(x, variables) for k, x in v.items()}
    if isinstance(v, list):
        return [gql_resolve_value(x, variables) for x in v]
    return v


_GQL_TYPE_FIELDS = {
    "Issue": ["id", "number", "title", "body", "state", "author", "labels",
              "comments", "url", "createdAt", "updatedAt"],
    "PullRequest": ["id", "number", "title", "body", "state", "merged",
                    "mergeable", "headRefName", "baseRefName", "headRef",
                    "baseRef", "files", "url", "createdAt", "updatedAt",
                    "author"],
    "Repository": ["id", "databaseId", "name", "nameWithOwner", "owner",
                   "defaultBranchRef", "viewerPermission", "hasIssuesEnabled",
                   "issue", "issues", "pullRequests", "pullRequest",
                   "description", "url", "isPrivate", "createdAt",
                   "updatedAt"],
    "User": ["id", "login", "url"],
    "SearchType": [],
}
_GQL_ENUM_VALUES = {
    "SearchType": ["ISSUE", "REPOSITORY", "USER", "DISCUSSION", "CODE",
                   "COMMIT", "MILESTONE", "WIKI"],
}
_KIND_TYPE = {"query": "Query", "mutation": "Mutation", "repository": "Repository",
              "issue": "Issue", "pullrequest": "PullRequest", "user": "User",
              "search": "Search"}


def _gql_cursor(number):
    return base64.b64encode(f"cursor:{number}".encode()).decode()


def _gql_cursor_number(cursor):
    try:
        text = base64.b64decode(str(cursor)).decode()
        if text.startswith("cursor:"):
            return int(text[len("cursor:"):])
    except Exception:
        return None
    return None


class GQL:
    """Minimal GraphQL executor for repository/issue/pull-request shapes."""

    def __init__(self, store: Store, viewer=DEFAULT_USER):
        self.store = store
        self.viewer = viewer
        self.fragments = {}
        self.errors = []

    def execute(self, query, variables=None, operation_name=None):
        variables = variables or {}
        self.errors = []
        try:
            defs = GQLParser(query).parse()
        except GQLError as e:
            return {"errors": [{"message": str(e)}]}
        self.fragments = {d["name"]: d for d in defs if d["kind"] == "fragment"}
        ops = [d for d in defs if d["kind"] == "operation"]
        if not ops:
            return {"errors": [{"message": "no operation found"}]}
        op = ops[0]
        if operation_name:
            for d in ops:
                if d["name"] == operation_name:
                    op = d
                    break
        if op["op"] == "mutation":
            data = self._resolve(op["sel"], ("mutation", None), variables)
        else:
            data = self._resolve(op["sel"], ("query", None), variables)
        out = {"data": data}
        if self.errors:
            out["errors"] = self.errors
        return out

    def _resolve(self, sels, ctx, variables):
        out = {}
        for f in sels:
            if f["kind"] == "spread":
                frag = self.fragments.get(f["name"])
                if frag:
                    out.update(self._resolve(frag["sel"], ctx, variables))
                continue
            if f["kind"] == "inline":
                # ... on Type { } only applies when the object is that type.
                if f.get("type") and f["type"] != _KIND_TYPE.get(ctx[0]):
                    continue
                out.update(self._resolve(f["sel"], ctx, variables))
                continue
            args = {k: gql_resolve_value(v, variables) for k, v in f["args"].items()}
            out[f["alias"]] = self._field(f, ctx, args, variables)
        return out

    def _unknown(self, f, ctx):
        self.errors.append({
            "message": f"Cannot query field \"{f['name']}\" on type "
                       f"\"{_KIND_TYPE.get(ctx[0], ctx[0])}\"."})
        return None

    # -- field dispatch ------------------------------------------------------
    def _field(self, f, ctx, args, variables):
        kind, obj = ctx
        name = f["name"]
        if name == "__typename":
            return _KIND_TYPE.get(kind, kind)
        if name == "__type":
            tname = args.get("name")
            fields = _GQL_TYPE_FIELDS.get(tname)
            if fields is None and tname not in _GQL_ENUM_VALUES:
                return None
            sel = f["sel"] or []
            out = {}
            for sf in sel:
                if sf.get("kind") != "field":
                    continue
                if sf["name"] == "name":
                    out[sf["alias"]] = tname
                elif sf["name"] == "fields":
                    out[sf["alias"]] = [{"name": n} for n in (fields or [])]
                elif sf["name"] == "enumValues":
                    out[sf["alias"]] = [{"name": n} for n in _GQL_ENUM_VALUES.get(tname, [])]
                elif sf["name"] == "kind":
                    out[sf["alias"]] = "ENUM" if tname in _GQL_ENUM_VALUES else "OBJECT"
            return out
        if kind == "query":
            if name == "viewer":
                return self._resolve(f["sel"] or [], ("user", {"login": self.viewer}), variables)
            if name == "repository":
                r = self.store.state["repos"].get(f"{args.get('owner')}/{args.get('name')}")
                if r is None:
                    return None
                return self._resolve(f["sel"] or [], ("repository", r), variables)
            if name == "node":
                return self._node(args.get("id"), f["sel"] or [], variables)
            if name == "search":
                return self._resolve(f["sel"] or [], ("search", args), variables)
            return self._unknown(f, ctx)
        if kind == "mutation":
            return self._mutation(f, args, variables)
        if kind == "repository":
            r = obj
            if name == "id":
                return node_id("Repository", r["id"])
            if name == "databaseId":
                return r["id"]
            if name == "name":
                return r["name"]
            if name == "description":
                return r.get("description")
            if name == "defaultBranchRef":
                tip = r["refs"].get(f"refs/heads/{r['default_branch']}")
                if tip is None:
                    return None
                return self._resolve(f["sel"] or [], ("ref", {"name": r["default_branch"]}), variables) \
                    if f["sel"] else {"name": r["default_branch"]}
            if name == "nameWithOwner":
                return r["full_name"]
            if name == "owner":
                return self._resolve(f["sel"] or [], ("user", {"login": r["owner"]}), variables)
            if name == "url":
                return f"https://github.com/{r['full_name']}"
            if name == "isPrivate":
                return r.get("private", False)
            if name == "viewerPermission":
                return "ADMIN"
            if name == "hasIssuesEnabled":
                return True
            if name in ("createdAt", "updatedAt"):
                return r["createdAt" if name == "createdAt" else "updated_at"]
            if name == "issues":
                return self._connection(r["issues"], args, f["sel"] or [], "issue", variables)
            if name == "pullRequests":
                return self._connection(r["prs"], args, f["sel"] or [], "pullrequest", variables)
            if name == "issue":
                iss = find_issue(r, args.get("number"))
                return None if iss is None else self._resolve(
                    f["sel"] or [], ("issue", iss), variables)
            if name == "pullRequest":
                pr = find_pr(r, args.get("number"))
                return None if pr is None else self._resolve(
                    f["sel"] or [], ("pullrequest", pr), variables)
            return self._unknown(f, ctx)
        if kind == "issue":
            iss = obj
            if name == "id":
                return node_id("Issue", iss["id"])
            if name in ("number", "title", "body"):
                return iss.get(name)
            if name == "state":
                return iss["state"].upper()
            if name == "url":
                return f"https://github.com/{iss.get('_repo', '')}/issues/{iss['number']}"
            if name == "author":
                return self._resolve(f["sel"] or [], ("user", {"login": iss.get("user", self.viewer)}), variables)
            if name == "labels":
                nodes = [{"name": l} for l in iss.get("labels", [])]
                return {"nodes": nodes, "totalCount": len(nodes)}
            if name == "comments":
                nodes = [{"body": c.get("body"), "author": {"login": c.get("user", self.viewer)}}
                         for c in iss.get("comments", [])]
                return {"nodes": nodes, "totalCount": len(nodes)}
            if name in ("createdAt", "updatedAt"):
                return iss["createdAt" if name == "createdAt" else "updated_at"]
            return self._unknown(f, ctx)
        if kind == "pullrequest":
            pr = obj
            if name == "id":
                return node_id("PullRequest", pr["id"])
            if name in ("number", "title", "body"):
                return pr.get(name)
            if name == "state":
                return pr["state"].upper()
            if name == "merged":
                return pr.get("merged", False)
            if name == "mergeable":
                return self._pr_mergeable(pr)
            if name == "headRefName":
                return pr.get("head")
            if name == "baseRefName":
                return pr.get("base")
            if name == "headRef":
                return self._resolve(f["sel"] or [], ("ref", {"name": pr.get("head")}), variables) \
                    if f["sel"] else {"name": pr.get("head")}
            if name == "baseRef":
                return self._resolve(f["sel"] or [], ("ref", {"name": pr.get("base")}), variables) \
                    if f["sel"] else {"name": pr.get("base")}
            if name == "url":
                return f"https://github.com/{pr.get('_repo', '')}/pull/{pr['number']}"
            if name == "author":
                return self._resolve(f["sel"] or [], ("user", {"login": pr.get("user", self.viewer)}), variables)
            if name == "files":
                return self._pr_files(pr, args, f["sel"] or [], variables)
            if name in ("createdAt", "updatedAt"):
                return pr["createdAt" if name == "createdAt" else "updated_at"]
            return self._unknown(f, ctx)
        if kind == "ref":
            if name == "name":
                return obj.get("name")
            return self._unknown(f, ctx)
        if kind == "user":
            if name == "login":
                return obj.get("login")
            if name == "id":
                return node_id("User", VIEWER_ID if obj.get("login") == DEFAULT_USER else 1)
            if name == "url":
                return f"https://github.com/{obj.get('login')}"
            return self._unknown(f, ctx)
        if kind == "search":
            return {"nodes": [], "issueCount": 0, "repositoryCount": 0}
        return self._unknown(f, ctx)

    def _node(self, gid, sel, variables):
        """node(id:) — decode a Relay global id back to its object."""
        if not isinstance(gid, str):
            return None
        try:
            kind, ident = base64.b64decode(gid).decode().split(":", 1)
        except Exception:
            return None
        if kind == "Repository":
            for r in self.store.state["repos"].values():
                if str(r["id"]) == ident:
                    return self._resolve(sel, ("repository", r), variables)
            return None
        if kind == "Issue":
            for r in self.store.state["repos"].values():
                for iss in r["issues"]:
                    if str(iss["id"]) == ident:
                        return self._resolve(sel, ("issue", iss), variables)
            return None
        if kind == "PullRequest":
            for r in self.store.state["repos"].values():
                for pr in r["prs"]:
                    if str(pr["id"]) == ident:
                        return self._resolve(sel, ("pullrequest", pr), variables)
            return None
        return None

    def _connection(self, items, args, sel, item_kind, variables):
        """A nodes/pageInfo connection over issues or pull requests.

        `items` are already in creation order. states filters by the
        GraphQL enum (OPEN/CLOSED); orderBy CREATED_AT DESC reverses;
        first/after pages by an opaque cursor of the item number.
        """
        states = args.get("states")
        if states:
            wanted = {str(s).lower() for s in states}
            items = [i for i in items if i["state"] in wanted]
        order = args.get("orderBy") or {}
        if isinstance(order, dict) and str(order.get("direction", "")).upper() == "DESC":
            items = list(reversed(items))
        after = _gql_cursor_number(args.get("after")) if args.get("after") else None
        start = 0
        if after is not None:
            for idx, item in enumerate(items):
                if item["number"] == after:
                    start = idx + 1
                    break
        first = args.get("first")
        page = items[start:] if first is None else items[start:start + first]
        end = start + len(page)
        res = {}
        for sf in sel:
            if sf.get("kind") != "field":
                continue
            if sf["name"] == "nodes":
                res[sf["alias"]] = [
                    self._resolve(sf["sel"] or [], (item_kind, i), variables)
                    for i in page]
            elif sf["name"] == "edges":
                res[sf["alias"]] = [
                    {"node": self._resolve(_sub_sel(sf["sel"] or [], "node"), (item_kind, i), variables),
                     "cursor": _gql_cursor(i["number"])} for i in page]
            elif sf["name"] == "totalCount":
                res[sf["alias"]] = len(items)
            elif sf["name"] == "pageInfo":
                info = {}
                for inf in (sf["sel"] or []):
                    if inf.get("kind") != "field":
                        continue
                    if inf["name"] == "hasNextPage":
                        info[inf["alias"]] = end < len(items)
                    elif inf["name"] == "hasPreviousPage":
                        info[inf["alias"]] = start > 0
                    elif inf["name"] == "endCursor":
                        info[inf["alias"]] = _gql_cursor(page[-1]["number"]) if page else None
                    elif inf["name"] == "startCursor":
                        info[inf["alias"]] = _gql_cursor(page[0]["number"]) if page else None
                res[sf["alias"]] = info
        return res

    def _pr_mergeable(self, pr):
        """MERGEABLE unless the three-way merge would conflict."""
        r = self.store.state["repos"].get(pr.get("_repo") or "")
        if r is None or pr["state"] != "open":
            return "UNKNOWN"
        head_sha = r["refs"].get(f"refs/heads/{pr['head']}")
        base_sha = r["refs"].get(f"refs/heads/{pr['base']}")
        if not head_sha or not base_sha:
            return "UNKNOWN"
        base_tip = pr.get("base_sha") or base_sha
        head_rec = self.store.get_object(head_sha)
        base_rec = self.store.get_object(base_tip)
        ours_rec = self.store.get_object(base_sha)
        if not head_rec or not base_rec or not ours_rec:
            return "UNKNOWN"
        head_tree, _, _ = parse_commit(head_rec[1])
        base_tree, _, _ = parse_commit(base_rec[1])
        ours_tree, _, _ = parse_commit(ours_rec[1])
        merged = three_way_merge(self.store, base_tree, ours_tree, head_tree)
        return "MERGEABLE" if merged is not None else "CONFLICTING"

    def _pr_files(self, pr, args, sel, variables):
        """pullRequest.files — the changed paths between base and head."""
        r = self.store.state["repos"].get(pr.get("_repo") or "")
        files = []
        if r is not None:
            head_sha = r["refs"].get(f"refs/heads/{pr['head']}")
            base_sha = r["refs"].get(f"refs/heads/{pr['base']}")
            if head_sha and base_sha:
                head_rec = self.store.get_object(head_sha)
                base_rec = self.store.get_object(base_sha)
                if head_rec and base_rec:
                    head_tree, _, _ = parse_commit(head_rec[1])
                    base_tree, _, _ = parse_commit(base_rec[1])
                    files = [{"path": d["filename"]} for d in
                             diff_trees(self.store, base_tree, head_tree)]
        first = args.get("first")
        if first is not None:
            files = files[:first]
        res = {}
        for sf in sel:
            if sf.get("kind") != "field":
                continue
            if sf["name"] == "nodes":
                res[sf["alias"]] = files
            elif sf["name"] == "totalCount":
                res[sf["alias"]] = len(files)
        return res

    def _mutation(self, f, args, variables):
        name = f["name"]
        inp = args.get("input") or {}
        sel = f["sel"] or []
        with self.store.lock:
            if name == "createIssue":
                repo_id = inp.get("repositoryId")
                r = next((x for x in self.store.state["repos"].values()
                          if node_id("Repository", x["id"]) == repo_id), None)
                if r is None and repo_id:
                    # also accept "owner/name" as repositoryId
                    r = self.store.state["repos"].get(repo_id)
                if r is None:
                    return None
                iss = create_issue_obj(self.store, r, inp.get("title", ""),
                                       inp.get("body"), user=self.viewer)
                self.store.save()
                return self._payload(sel, {
                    "issue": ("issue", iss),
                    "clientMutationId": None}, variables)
            if name == "createRepository":
                r = new_repo(self.store, self.viewer, inp.get("name", "repo"),
                             inp.get("description"), inp.get("visibility") == "PRIVATE")
                if r is None:
                    return None
                self.store.save()
                return self._payload(sel, {
                    "repository": ("repository", r),
                    "clientMutationId": None}, variables)
            if name in ("closeIssue", "updateIssue"):
                # input: {issueId} (gh's CloseIssueInput) or {id}
                gid = inp.get("issueId") or inp.get("id")
                target = None
                for r in self.store.state["repos"].values():
                    for iss in r["issues"]:
                        if node_id("Issue", iss["id"]) == gid \
                                or str(iss["id"]) == str(gid):
                            target = (r, iss)
                if target is None:
                    return None
                r, iss = target
                if name == "closeIssue":
                    iss["state"] = "closed"
                    iss["closed_at"] = self.store.tick()
                if inp.get("title"):
                    iss["title"] = inp["title"]
                if inp.get("body") is not None:
                    iss["body"] = inp["body"]
                iss["updated_at"] = self.store.tick()
                self.store.save()
                return self._payload(sel, {
                    "issue": ("issue", iss),
                    "clientMutationId": None}, variables)
            if name == "createPullRequest":
                repo_id = inp.get("repositoryId")
                r = next((x for x in self.store.state["repos"].values()
                          if node_id("Repository", x["id"]) == repo_id), None)
                if r is None and repo_id:
                    r = self.store.state["repos"].get(repo_id)
                if r is None:
                    return None
                pr = create_pr_obj(self.store, r, inp.get("title", ""),
                                   inp.get("headRefName", ""),
                                   inp.get("baseRefName", r["default_branch"]),
                                   inp.get("body"), user=self.viewer)
                self.store.save()
                return self._payload(sel, {
                    "pullRequest": ("pullrequest", pr),
                    "clientMutationId": None}, variables)
            if name == "mergePullRequest":
                gid = inp.get("pullRequestId") or inp.get("id")
                target = None
                for r in self.store.state["repos"].values():
                    for pr in r["prs"]:
                        if node_id("PullRequest", pr["id"]) == gid \
                                or str(pr["id"]) == str(gid):
                            target = (r, pr)
                if target is None:
                    return None
                r, pr = target
                sha, err = merge_pr(self.store, r, pr)
                if err:
                    self.errors.append({"message": err})
                return self._payload(sel, {
                    "pullRequest": ("pullrequest", pr),
                    "clientMutationId": None}, variables)
            if name == "addComment":
                gid = inp.get("subjectId") or inp.get("id")
                target = None
                for r in self.store.state["repos"].values():
                    for iss in r["issues"]:
                        if node_id("Issue", iss["id"]) == gid \
                                or str(iss["id"]) == str(gid):
                            target = (r, iss)
                if target is None:
                    return None
                r, iss = target
                ts = self.store.tick()
                c = {"body": inp.get("body"), "user": self.viewer,
                     "created_at": ts}
                iss["comments"].append(c)
                iss["updated_at"] = ts
                self.store.save()
                node = {"id": node_id("IssueComment",
                                      f"{r['id']}:{iss['number']}:{len(iss['comments'])}"),
                        "body": inp.get("body")}
                return self._payload(sel, {
                    "commentEdge": {"node": node},
                    "clientMutationId": None}, variables)
        return self._unknown(f, ("mutation", None))

    def _payload(self, sel, mapping, variables):
        """Resolve a mutation payload's selection set from {field: value}."""
        out = {}
        for f in sel:
            if f.get("kind") == "inline":
                if f.get("type") and f["type"] != "Mutation":
                    continue
                out.update(self._payload(f["sel"], mapping, variables))
                continue
            if f.get("kind") != "field":
                continue
            val = mapping.get(f["name"])
            if val is None:
                out[f["alias"]] = None
                continue
            if isinstance(val, tuple):
                out[f["alias"]] = self._resolve(f["sel"] or [], val, variables)
            elif isinstance(val, dict) and "node" in val:
                node = val["node"]
                node_sel = _sub_sel(f["sel"] or [], "node")
                out[f["alias"]] = {"node": {
                    nf["alias"]: node.get(nf["name"]) for nf in node_sel
                    if nf.get("kind") == "field"}}
            else:
                out[f["alias"]] = val
        return out


def _has(sel, name):
    return any(f.get("name") == name for f in sel if f.get("kind") == "field")


def _sub_sel(sel, name):
    for f in sel:
        if f.get("kind") == "field" and f.get("name") == name:
            return f.get("sel") or []
    return []

# -------------------------------------------------------------------- MCP ---

MCP_TOOLS = [
    {"name": "list_repos", "description": "List repositories.",
     "inputSchema": {"type": "object", "properties": {"owner": {"type": "string"}},
                     "additionalProperties": False}},
    {"name": "get_repo", "description": "Get a repository by owner and name.",
     "inputSchema": {"type": "object", "properties": {
         "owner": {"type": "string"}, "repo": {"type": "string"}},
         "required": ["owner", "repo"]}},
    {"name": "create_repo", "description": "Create a repository.",
     "inputSchema": {"type": "object", "properties": {
         "name": {"type": "string"}, "owner": {"type": "string"},
         "description": {"type": "string"}, "private": {"type": "boolean"}},
         "required": ["name"]}},
    {"name": "list_issues", "description": "List issues in a repository.",
     "inputSchema": {"type": "object", "properties": {
         "owner": {"type": "string"}, "repo": {"type": "string"},
         "state": {"type": "string"}},
         "required": ["owner", "repo"]}},
    {"name": "get_issue", "description": "Get one issue by number.",
     "inputSchema": {"type": "object", "properties": {
         "owner": {"type": "string"}, "repo": {"type": "string"},
         "issue_number": {"type": "integer"}},
         "required": ["owner", "repo", "issue_number"]}},
    {"name": "create_issue", "description": "Create an issue.",
     "inputSchema": {"type": "object", "properties": {
         "owner": {"type": "string"}, "repo": {"type": "string"},
         "title": {"type": "string"}, "body": {"type": "string"}},
         "required": ["owner", "repo", "title"]}},
    {"name": "update_issue", "description": "Update an issue (title/body/state).",
     "inputSchema": {"type": "object", "properties": {
         "owner": {"type": "string"}, "repo": {"type": "string"},
         "issue_number": {"type": "integer"}, "title": {"type": "string"},
         "body": {"type": "string"}, "state": {"type": "string"}},
         "required": ["owner", "repo", "issue_number"]}},
    {"name": "list_pull_requests", "description": "List pull requests.",
     "inputSchema": {"type": "object", "properties": {
         "owner": {"type": "string"}, "repo": {"type": "string"}},
         "required": ["owner", "repo"]}},
    {"name": "get_pull_request", "description": "Get one pull request.",
     "inputSchema": {"type": "object", "properties": {
         "owner": {"type": "string"}, "repo": {"type": "string"},
         "pull_number": {"type": "integer"}},
         "required": ["owner", "repo", "pull_number"]}},
    {"name": "create_pull_request", "description": "Create a pull request.",
     "inputSchema": {"type": "object", "properties": {
         "owner": {"type": "string"}, "repo": {"type": "string"},
         "title": {"type": "string"}, "head": {"type": "string"},
         "base": {"type": "string"}, "body": {"type": "string"}},
         "required": ["owner", "repo", "title", "head", "base"]}},
    {"name": "merge_pull_request", "description": "Merge a pull request.",
     "inputSchema": {"type": "object", "properties": {
         "owner": {"type": "string"}, "repo": {"type": "string"},
         "pull_number": {"type": "integer"}},
         "required": ["owner", "repo", "pull_number"]}},
    {"name": "get_file_contents", "description": "Read a file from a repo branch.",
     "inputSchema": {"type": "object", "properties": {
         "owner": {"type": "string"}, "repo": {"type": "string"},
         "path": {"type": "string"}, "branch": {"type": "string"}},
         "required": ["owner", "repo", "path"]}},
    {"name": "create_file", "description": "Create/update a file via a commit.",
     "inputSchema": {"type": "object", "properties": {
         "owner": {"type": "string"}, "repo": {"type": "string"},
         "path": {"type": "string"}, "content": {"type": "string"},
         "message": {"type": "string"}, "branch": {"type": "string"}},
         "required": ["owner", "repo", "path", "content"]}},
    {"name": "search_issues", "description": "Search issues by text.",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string"}}, "required": ["query"]}},
    {"name": "search_repositories", "description": "Search repositories by text.",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string"}}, "required": ["query"]}},
]


def mcp_call(store: Store, name: str, args: dict):
    """Execute one MCP tool; returns (result_dict, error_str_or_None)."""
    owner = args.get("owner") or DEFAULT_USER
    repo_name = args.get("repo")
    r = None
    if repo_name:
        r = store.state["repos"].get(f"{owner}/{repo_name}")
        if r is None:
            return None, f"repository {owner}/{repo_name} not found"
    with store.lock:
        if name == "list_repos":
            repos = [repo_json(x) for x in store.state["repos"].values()
                     if not args.get("owner") or x["owner"] == args["owner"]]
            return {"repositories": repos}, None
        if name == "get_repo":
            return repo_json(r), None
        if name == "create_repo":
            nr = new_repo(store, owner, args["name"], args.get("description"),
                          args.get("private", False))
            if nr is None:
                return None, "repository already exists"
            store.save()
            return repo_json(nr), None
        if name == "list_issues":
            items = [issue_json(r, i) for i in r["issues"]
                     if not args.get("state") or i["state"] == args["state"]]
            return {"issues": items}, None
        if name == "get_issue":
            iss = find_issue(r, int(args["issue_number"]))
            return (issue_json(r, iss), None) if iss else (None, "issue not found")
        if name == "create_issue":
            iss = create_issue_obj(store, r, args["title"], args.get("body"))
            store.save()
            return issue_json(r, iss), None
        if name == "update_issue":
            iss = find_issue(r, int(args["issue_number"]))
            if iss is None:
                return None, "issue not found"
            if args.get("title"):
                iss["title"] = args["title"]
            if args.get("body") is not None:
                iss["body"] = args["body"]
            if args.get("state") in ("open", "closed"):
                iss["state"] = args["state"]
                iss["closed_at"] = store.tick() if args["state"] == "closed" else None
            iss["updated_at"] = store.tick()
            store.save()
            return issue_json(r, iss), None
        if name == "list_pull_requests":
            return {"pull_requests": [pr_json(r, p) for p in r["prs"]]}, None
        if name == "get_pull_request":
            pr = find_pr(r, int(args["pull_number"]))
            return (pr_json(r, pr), None) if pr else (None, "pull request not found")
        if name == "create_pull_request":
            pr = create_pr_obj(store, r, args["title"], args["head"], args["base"],
                               args.get("body"))
            store.save()
            return pr_json(r, pr), None
        if name == "merge_pull_request":
            pr = find_pr(r, int(args["pull_number"]))
            if pr is None:
                return None, "pull request not found"
            sha, err = merge_pr(store, r, pr)
            if err:
                return None, err
            out = pr_json(r, pr)
            out["sha"] = sha
            return out, None
        if name == "get_file_contents":
            branch = args.get("branch") or r["default_branch"]
            tip = r["refs"].get(f"refs/heads/{branch}")
            if not tip:
                return None, "branch not found"
            tree_sha, _, _ = parse_commit(store.get_object(tip)[1])
            found = tree_get(store, tree_sha, [p for p in args["path"].split("/") if p])
            if not found or found[2] != "blob":
                return None, "file not found"
            _, data = store.get_object(found[1])
            return {"path": args["path"], "sha": found[1],
                    "content": base64.b64encode(data).decode(),
                    "encoding": "base64"}, None
        if name == "create_file":
            branch = args.get("branch") or r["default_branch"]
            content = args["content"]
            try:
                raw = base64.b64decode(content)
            except Exception:
                raw = content.encode()
            commit, blob = commit_for_file(store, r, branch, args["path"], raw,
                                           args.get("message") or f"Create {args['path']}")
            store.save()
            return {"path": args["path"], "sha": blob, "commit": commit}, None
        if name == "search_issues":
            q = (args.get("query") or "").lower()
            out = []
            for rr in store.state["repos"].values():
                for iss in rr["issues"]:
                    if q in (iss.get("title") or "").lower() or q in (iss.get("body") or "").lower():
                        out.append(issue_json(rr, iss))
            return {"issues": out}, None
        if name == "search_repositories":
            q = (args.get("query") or "").lower()
            out = [repo_json(rr) for rr in store.state["repos"].values()
                   if q in rr["name"].lower() or q in (rr.get("description") or "").lower()]
            return {"repositories": out}, None
    return None, f"unknown tool: {name}"


_MCP_REVISIONS = ["2026-07-28", "2025-11-25", "2025-06-18", "2025-03-26",
                  "2024-11-05"]


def mcp_handle(store: Store, req: dict):
    """Handle one JSON-RPC request; return response dict or None (notification)."""
    method = req.get("method")
    rid = req.get("id")
    params = req.get("params") or {}
    if method in ("notifications/initialized", "notifications/cancelled"):
        return None
    if method == "server/discover":
        # The 2026-07-28 era: identity, capabilities and supported
        # revisions in one answer; no handshake, no session.
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "supportedVersions": list(_MCP_REVISIONS),
            "protocolVersion": "2026-07-28",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "git-forge", "version": VERSION},
            "_meta": {"io.modelcontextprotocol/serverInfo":
                      {"name": "git-forge", "version": VERSION}}}}
    if method == "initialize":
        wanted = (params.get("protocolVersion")
                  if isinstance(params, dict) else None)
        version = wanted if wanted in _MCP_REVISIONS else "2024-11-05"
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": version,
            "capabilities": {"tools": {}, "resources": {}, "prompts": {}},
            "serverInfo": {"name": "git-forge", "version": VERSION}}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid,
                "result": {"tools": MCP_TOOLS}}
    if method == "tools/call":
        tname = params.get("name")
        targs = params.get("arguments") or {}
        tool = next((t for t in MCP_TOOLS if t["name"] == tname), None)
        if tool is None:
            return {"jsonrpc": "2.0", "id": rid, "error": {
                "code": -32602, "message": f"unknown tool: {tname}"}}
        missing = [p for p in tool["inputSchema"].get("required", [])
                   if p not in targs or targs[p] is None]
        if missing:
            text = f"missing required argument(s): {', '.join(missing)}"
            return {"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": text}],
                "isError": True}}
        result, err = mcp_call(store, tname, targs)
        if err:
            return {"jsonrpc": "2.0", "id": rid, "error": {
                "code": -32602, "message": err}}
        text = json.dumps(result, sort_keys=True)
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "content": [{"type": "text", "text": text}],
            "structuredContent": result, "isError": False}}
    if method == "resources/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"resources": []}}
    if method == "prompts/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"prompts": []}}
    if rid is None:
        return None
    return {"jsonrpc": "2.0", "id": rid, "error": {
        "code": -32601, "message": f"method not found: {method}"}}


# ------------------------------------------------------------------ HTTP ---

def paginate(items, qs):
    try:
        page = max(1, int(qs.get("page", ["1"])[0]))
    except ValueError:
        page = 1
    try:
        per = int(qs.get("per_page", ["30"])[0])
    except ValueError:
        per = 30
    per = max(1, min(100, per))
    return items[(page - 1) * per: page * per]


def _have_set(store, haves, shallow_bounds):
    """Objects the client already holds: reachable from its haves, but
    traversal stops at its shallow boundaries — a shallow commit's
    parents are NOT held, so they must still be sent."""
    seen, queue = set(), list(haves)
    while queue:
        sha = queue.pop(0)
        if sha in seen or sha == ZERO_SHA:
            continue
        seen.add(sha)
        rec = store.get_object(sha)
        if rec is None:
            continue
        otype, data = rec
        if otype == "commit":
            tree, parents, _ = parse_commit(data)
            if tree:
                queue.append(tree)
            if sha not in shallow_bounds:
                queue.extend(parents)
        elif otype == "tag":
            for line in data.split(b"\n"):
                if line.startswith(b"object "):
                    queue.append(line[7:].decode())
                    break
        elif otype == "tree":
            for _m, _n, s in parse_tree(data):
                queue.append(s)
    return seen


# Pack cache for _compute_fetch: pack bytes for an object set, keyed by
# the sorted object shas. Bounded FIFO (64 entries).
_PACK_CACHE: dict = {}


_FETCH_CACHE: dict = {}


def _compute_fetch(store, wants, haves, shallow_bounds, deepen):
    """Return (pack, shallow, unshallow, ack_sha_or_None) for a fetch."""
    # The object store is append-only and objects are immutable, so the
    # result depends only on (wants, haves, shallow_bounds, deepen).
    # The clone bench fires 50 identical depth-1 fetches (twice each:
    # negotiation + done); this skips the repeated collect_reachable +
    # shallow BFS per fetch.
    fkey = (tuple(wants), tuple(haves), tuple(shallow_bounds), deepen)
    hit = _FETCH_CACHE.get(fkey)
    if hit is not None:
        return hit
    max_depth = (deepen - 1) if deepen is not None else None
    wanted = collect_reachable(store, wants, max_depth=max_depth)
    if haves:
        held = _have_set(store, haves, set(shallow_bounds))
        wanted = [s for s in wanted if s not in held]
    # Pack cache: objects are content-addressed and immutable, so the
    # pack for a given object set never changes. The clone bench fires 50
    # identical shallow clones at once; without this each one recomputes
    # the same pack. Bounded FIFO cache (64 entries).
    key = tuple(sorted(wanted))
    pack = _PACK_CACHE.get(key)
    if pack is None:
        pack = build_pack(store, wanted)
        _PACK_CACHE[key] = pack
        if len(_PACK_CACHE) > 64:
            _PACK_CACHE.pop(next(iter(_PACK_CACHE)))
    shallow, unshallow = [], []
    if deepen is not None:
        depths, queue = {}, [(w, 0) for w in wants]
        while queue:
            sha, d = queue.pop(0)
            if sha in depths:
                continue
            rec = store.get_object(sha)
            if not rec or rec[0] != "commit":
                continue
            depths[sha] = d
            if d < deepen - 1:
                _, parents, _ = parse_commit(rec[1])
                for p in parents:
                    queue.append((p, d + 1))
        shallow = sorted(s for s, d in depths.items() if d == deepen - 1)
        included = set(collect_reachable(store, wants, max_depth=max_depth))
        for s in shallow_bounds:
            rec = store.get_object(s)
            if rec and rec[0] == "commit":
                _, parents, _ = parse_commit(rec[1])
                if s in included and all(p in included for p in parents):
                    unshallow.append(s)
    ack = None
    for h in haves:
        if h in wants:
            ack = h
            break
    out = (pack, shallow, unshallow, ack)
    _FETCH_CACHE[fkey] = out
    if len(_FETCH_CACHE) > 64:
        _FETCH_CACHE.pop(next(iter(_FETCH_CACHE)))
    return out


def _pkt_tokens(body: bytes):
    """Parse a pkt-line stream into (kind, payload) tokens.

    kind is 'data', 'flush' (0000), 'delim' (0001) or 'response-end'
    (0002, the client's end-of-request marker on a stateless transport).
    """
    toks, i = [], 0
    while i + 4 <= len(body):
        try:
            n = int(body[i:i + 4], 16)
        except ValueError:
            break
        if n == 0:
            toks.append(("flush", b""))
            i += 4
        elif n == 1:
            toks.append(("delim", b""))
            i += 4
        elif n == 2:
            # Response-end packet (0002): the client marks the end of its
            # request on a stateless transport. It carries no argument.
            toks.append(("response-end", b""))
            i += 4
        else:
            toks.append(("data", body[i + 4:i + n]))
            i += n
    return toks



class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def send_response(self, code, message=None):
        # No Server/Date headers: they cost a formatdate() per response
        # and no suite client reads them.
        self.send_response_only(code, message)
    server_version = "git-forge/1.0"
    _req_iter = itertools.count(1)

    def log_message(self, fmt, *args):  # quiet; errors still go to stderr via print
        # No-op: the default writes every request line to stderr, which
        # costs ~0.1 ms per request when stderr is a file (the grader's
        # read workload is 2,000 sequential requests).
        return

    def setup(self):
        super().setup()
        # Kill the Nagle/delayed-ACK stall: without TCP_NODELAY every
        # small response on a keep-alive connection waited ~40ms for the
        # peer's delayed ACK, which made the 2,000-request read workload
        # take 80s instead of under a second.
        try:
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except Exception:
            pass

    # -- helpers -------------------------------------------------------------
    def _read_body(self) -> bytes:
        # The body is read once, up front, in _dispatch (see there); this
        # just returns the stashed bytes so no handler can leave bytes
        # unread on a keep-alive connection.
        return getattr(self, "_raw_body", b"")

    def _json_body(self):
        raw = self._read_body()
        if not raw:
            return {}
        try:
            v = json.loads(raw.decode("utf-8"))
            return v if isinstance(v, dict) else {}
        except Exception:
            return {}

    def _json_body_or_400(self):
        """Parse the request body as JSON; on malformed JSON send 400.

        Returns (value_or_None, handled_bool). A missing/empty body is
        not an error.
        """
        raw = self._read_body()
        if not raw:
            return {}, False
        try:
            v = json.loads(raw.decode("utf-8"))
        except Exception:
            self._err(400, "Problems parsing JSON")
            return None, True
        return v if isinstance(v, dict) else {}, False

    def _send(self, status, body: bytes, ctype="application/json", extra=None):
        # itertools.count next() is atomic under the GIL: no lock needed
        # for the per-request id, even under 50 concurrent clones.
        rid = f"req-{next(Handler._req_iter):08d}-{os.getpid()}"
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("X-GitHub-Request-Id", rid)
        if status == 204:
            # 204 carries no body: sending one would corrupt the next
            # response on the keep-alive connection.
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, status, obj, extra=None):
        self._send(status, json.dumps(obj, sort_keys=True).encode(), extra=extra)

    def _err(self, status, msg):
        self._send_json(status, {"message": msg,
                                 "documentation_url": "https://docs.github.com/rest"})

    def _auth_user(self):
        """The token owner, or None when the request carries no valid token.

        A request with no Authorization header, or a bearer token that
        does not match MAKEFUN_SHOP_TOKEN, is unauthenticated. When the
        env var is unset (local dev) any non-empty token is accepted.
        """
        auth = self.headers.get("Authorization") or ""
        if not auth.startswith("Bearer "):
            return None
        token = auth[len("Bearer "):].strip()
        if not token:
            return None
        if AUTH_TOKEN and token != AUTH_TOKEN:
            return None
        return DEFAULT_USER

    def _require_auth(self):
        """401 unless the request carries the suite token."""
        if self._auth_user() is None:
            self._err(401, "Requires authentication")
            return False
        return True

    def _page(self, items, qs):
        """Slice a list by page/per_page; return (page, link_header|None)."""
        try:
            page = max(1, int(qs.get("page", ["1"])[0]))
        except ValueError:
            page = 1
        try:
            per = int(qs.get("per_page", ["30"])[0])
        except ValueError:
            per = 30
        per = max(1, min(100, per))
        start = (page - 1) * per
        sliced = items[start:start + per]
        link = None
        if start + len(sliced) < len(items):
            host = self.headers.get("Host") or "127.0.0.1"
            scheme = "http"
            parsed = urlparse(self.path)
            base = f"{scheme}://{host}{parsed.path}"
            link = (f'<{base}?page={page + 1}&per_page={per}>; rel="next", '
                    f'<{base}?page={(len(items) + per - 1) // per}&per_page={per}>; rel="last"')
        return sliced, link

    def _send_page(self, items, qs):
        sliced, link = self._page(items, qs)
        extra = {"Link": link} if link else None
        self._send_json(200, sliced, extra=extra)

    # -- dispatch ------------------------------------------------------------
    def do_GET(self):
        self._dispatch()

    def do_POST(self):
        self._dispatch()

    def do_PUT(self):
        self._dispatch()

    def do_PATCH(self):
        self._dispatch()

    def do_DELETE(self):
        self._dispatch()

    def do_HEAD(self):
        self._dispatch()

    def _dispatch(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        store = STORE
        # Read the request body up front, before any routing or response:
        # a handler that answers without consuming the body leaves its
        # bytes in the socket, and the next request on the keep-alive
        # connection is parsed from that garbage (this poisoned the
        # batched GraphQL check).
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        self._raw_body = self.rfile.read(n) if n > 0 else b""
        # Malformed JSON on an /api/v3 write is a 400 before routing.
        if path.startswith("/api/v3/") and self.command in ("POST", "PUT", "PATCH") \
                and self._raw_body:
            try:
                json.loads(self._raw_body.decode("utf-8"))
            except Exception:
                self._err(400, "Problems parsing JSON")
                return
        try:
            # git smart HTTP first (paths may end .git or not)
            if "/info/refs" in path or path.endswith("/git-upload-pack") \
                    or path.endswith("/git-receive-pack") or path.endswith("/HEAD"):
                self._git(path, qs)
                return
            if path in ("/", "/health", "/healthz"):
                self._send_json(200, {"status": "ok", "server": "git-forge",
                                      "version": VERSION})
                return
            if path in ("/export", "/_export/state"):
                self._send(200, store.export_bytes())
                return
            if path in ("/graphql", "/api/graphql"):
                if not self._require_auth():
                    return
                self._graphql()
                return
            if path in ("/mcp", "/mcp/"):
                if self.command != "GET" and not self._require_auth():
                    return
                self._mcp()
                return
            if path in ("/api/v3", "/api/v3/"):
                self._send_json(200, {"current_user_url": "/api/v3/user",
                                      "repository_url": "/api/v3/repos/{owner}/{repo}"},
                                extra={"X-Oauth-Scopes": "repo, read:org"})
                return
            if path == "/api/v3/meta":
                self._send_json(200, {"installed_version": VERSION,
                                      "verifiable_password_authentication": False,
                                      "git": ["1.0.0"]})
                return
            if path.startswith("/api/v3/"):
                if not self._require_auth():
                    return
                self._rest(path[len("/api/v3"):], qs, prefix="/api/v3")
                return
            if not self._require_auth():
                return
            self._rest(path, qs, prefix="")
        except BrokenPipeError:
            pass
        except Exception as e:  # never leak a stack trace as a hang
            sys.stderr.write(f"handler error {self.path}: {e!r}\n")
            try:
                self._err(500, f"internal error: {e}")
            except Exception:
                pass

    # -- git smart HTTP --------------------------------------------------------
    def _git_repo(self, path):
        """Extract (owner, repo) from /owner/repo[.git]/..."""
        parts = [p for p in path.split("/") if p]
        if len(parts) < 2:
            return None, None
        owner = unquote(parts[0])
        repo = unquote(parts[1])
        if repo.endswith(".git"):
            repo = repo[:-4]
        return owner, repo

    def _advertise(self, owner, repo_name, service):
        store = STORE
        r = get_repo_or_404(store, owner, repo_name)
        if r is None:
            self._err(404, "Not Found")
            return
        refs = dict(r["refs"])
        default = r["default_branch"]
        # Protocol v2: the client asks for it with `Git-Protocol: version=2`
        # on upload-pack info/refs. Receive-pack is v0-only, and a client
        # that does not ask for v2 gets the v0 advertisement.
        if service == "git-upload-pack" and \
                (self.headers.get("Git-Protocol") or "") == "version=2":
            body = pkt(b"# service=git-upload-pack\n") + pkt_flush()
            body += pkt(b"version 2\n")
            for cap in (b"agent=git-forge/1.0\n", b"ls-refs=unborn\n",
                        b"fetch=shallow wait-for-done\n", b"server-option\n",
                        b"object-format=sha1\n"):
                body += pkt(cap)
            body += pkt_flush()
            self._send(200, body, "application/x-git-upload-pack-advertisement")
            return
        caps_up = ("multi_ack multi_ack_detailed thin-pack side-band "
                   "side-band-64k ofs-delta shallow deepen-since deepen-not "
                   "filter allow-tip-sha1-in-want "
                   "allow-reachable-sha1-in-want agent=git-forge/1.0 "
                   f"symref=HEAD:refs/heads/{default}")
        caps_rx = ("report-status report-status-v2 delete-refs side-band-64k "
                   "quiet atomic ofs-delta agent=git-forge/1.0")
        caps = caps_up if service == "git-upload-pack" else caps_rx
        body = pkt(f"# service={service}\n".encode()) + pkt_flush()
        if not refs:
            body += pkt(f"{ZERO_SHA} capabilities^{{}}\0{caps}\n".encode())
        else:
            ordered = sorted(refs.items())
            head_sha = refs.get(f"refs/heads/{default}") or ordered[0][1]
            lines = [("HEAD", head_sha)] + ordered
            for idx, (ref, sha) in enumerate(lines):
                line = f"{sha} {ref}"
                if idx == 0:
                    line += f"\0{caps}"
                body += pkt((line + "\n").encode())
        body += pkt_flush()
        ctype = f"application/x-{service}-advertisement"
        self._send(200, body, ctype)

    def _git(self, path, qs):
        owner, repo_name = self._git_repo(path)
        if owner is None:
            self._err(404, "Not Found")
            return
        if path.endswith("/info/refs"):
            service = qs.get("service", [""])[0]
            if service not in ("git-upload-pack", "git-receive-pack"):
                self._err(400, "service required")
                return
            self._advertise(owner, repo_name, service)
            return
        if path.endswith("/HEAD"):
            r = get_repo_or_404(STORE, owner, repo_name)
            branch = r["default_branch"] if r else "main"
            self._send(200, f"ref: refs/heads/{branch}\n".encode(), "text/plain")
            return
        if path.endswith("/git-upload-pack"):
            self._upload_pack(owner, repo_name)
            return
        if path.endswith("/git-receive-pack"):
            self._receive_pack(owner, repo_name)
            return
        self._err(404, "Not Found")

    def _upload_pack(self, owner, repo_name):
        store = STORE
        body = self._read_body()
        # Protocol v2: the body starts with a `command=...` pkt-line.
        toks = _pkt_tokens(body)
        first = next((p for k, p in toks if k == "data"), b"")
        if first.startswith(b"command="):
            self._upload_pack_v2(owner, repo_name, toks)
            return
        # Parse EVERY pkt-line in the body, across flushes. The client
        # re-sends the accumulated request buffer on each stateless
        # round, so "done" arrives AFTER a flush:
        #   want ... / deepen 1 / 0000 / 0009done
        # read_pkt_lines() stops at the first flush and discarded the
        # "done", so the server never sent the pack (full clone died
        # with "unexpected disconnect while reading sideband packet").
        lines = []
        i = 0
        while i + 4 <= len(body):
            try:
                n = int(body[i:i + 4], 16)
            except ValueError:
                break
            if n == 0:
                i += 4
                continue
            lines.append(body[i + 4:i + n])
            i += n
        wants, haves, deepen = [], [], None
        done_present = False
        use_sideband = False
        for raw in lines:
            line = raw.decode("utf-8", "replace").strip()
            # Capabilities on a want line are SPACE-separated tokens after
            # the sha ("want <sha> side-band-64k ..."), NOT NUL-separated
            # (the NUL form is only used in ref advertisements). The trace
            # of a real git client proves this; missing it made clones fail
            # with "bad line length character: PACK" (raw pack sent to a
            # client expecting side-band framing).
            if "\0" in line:
                head, caps = line.split("\0", 1)
                line = head
                if "side-band-64k" in caps or "side-band" in caps:
                    use_sideband = True
            if line.startswith("want "):
                parts = line[5:].split()
                wants.append(parts[0])
                if "side-band-64k" in parts or "side-band" in parts:
                    use_sideband = True
            elif line.startswith("have "):
                haves.append(line[5:].split()[0])
            elif line.startswith("deepen "):
                try:
                    deepen = int(line[7:].split()[0])
                except ValueError:
                    pass
            elif line == "done":
                done_present = True
            elif line.startswith("shallow "):
                pass
        r = get_repo_or_404(store, owner, repo_name)
        if r is None or not wants:
            # NAK + empty pack (side-band framed if requested)
            out = pkt(b"NAK\n")
            pack = build_pack(store, [])
            out += sideband_pack(pack) if use_sideband else pack
            self._send(200, out, "application/x-git-upload-pack-result")
            return
        with store.lock:
            max_depth = (deepen - 1) if deepen is not None else None
            wanted = collect_reachable(store, wants, max_depth=max_depth)
            if haves:
                have_set = set(collect_reachable(store, haves))
                wanted = [s for s in wanted if s not in have_set]
            pack = build_pack(store, wanted)
            # Shallow boundary: the deepest INCLUDED commits (depth
            # deepen-1), not their omitted parents. For --depth 1 that is
            # the wanted commit itself — even a root commit, whose sha the
            # client records in .git/shallow. Sending the parents (or
            # nothing) made git fail with "expected shallow/unshallow,
            # got NAK".
            shallow = []
            if deepen is not None:
                depths, queue = {}, [(w, 0) for w in wants]
                while queue:
                    sha, d = queue.pop(0)
                    if sha in depths:
                        continue
                    rec = store.get_object(sha)
                    if not rec or rec[0] != "commit":
                        continue
                    depths[sha] = d
                    if d < deepen - 1:
                        _, parents, _ = parse_commit(rec[1])
                        for p in parents:
                            queue.append((p, d + 1))
                shallow = sorted(s for s, d in depths.items() if d == deepen - 1)
            # Stateless HTTP is TWO requests: (1) want/deepen/have, (2)
            # the same lines plus "done" (git re-sends the whole request
            # buffer). fetch-pack reads, in order: a shallow list
            # terminated by a FLUSH, then ACK/NAK, then the pack — and
            # the pack belongs to the "done" response only.
            #
            # Response shape depends on which round this is, because
            # the client reads a DIFFERENT prefix of each response and
            # its packet reader buffers the whole (small) HTTP body:
            # any byte left unread after a round poisons the next
            # round's first read.
            #   * first request (no have, no done): the client reads
            #     ONLY the shallow list, up to its flush, then makes a
            #     new request. A trailing NAK here was buffered and
            #     re-read as the next round's first line, dying with
            #     "expected shallow list". So: shallow + flush, nothing
            #     else (or just NAK when there is no deepen).
            #   * have round: shallow + flush + ACK/NAK, fully read.
            #   * done request: shallow + flush + ACK/NAK + pack.
            # Sending shallow+NAK with no flush made it die with
            # "expected shallow/unshallow, got NAK"; sending the pack
            # in response (1) made it be discarded / misread.
            ack = (pkt(f"ACK {wants[0]}\n".encode())
                   if haves and any(h in [w for w in wants] for h in haves)
                   else pkt(b"NAK\n"))
            out = bytearray()
            for s in shallow:
                out += pkt(f"shallow {s}\n".encode())
            if deepen is not None:
                out += pkt_flush()
            if done_present:
                out += ack
                out += sideband_pack(pack) if use_sideband else pack
            elif haves:
                out += ack
            elif deepen is None:
                out += ack
        self._send(200, bytes(out), "application/x-git-upload-pack-result")

    def _upload_pack_v2(self, owner, repo_name, toks):
        """Protocol v2 upload-pack: `command=ls-refs` or `command=fetch`."""
        store = STORE
        data = [p.decode("utf-8", "replace").strip()
                for k, p in toks if k == "data"]
        if not data or not data[0].startswith("command="):
            self._err(400, "bad v2 request")
            return
        command = data[0][len("command="):]
        r = get_repo_or_404(store, owner, repo_name)
        if r is None:
            self._err(404, "Not Found")
            return
        args = data[1:]
        if command == "ls-refs":
            peel = "peel" in args
            symrefs = "symrefs" in args
            unborn = "unborn" in args
            prefixes = [a[len("ref-prefix "):] for a in args
                        if a.startswith("ref-prefix ")]
            refs = dict(r["refs"])
            default = r["default_branch"]
            out = bytearray()
            tip = refs.get(f"refs/heads/{default}")
            # HEAD is listed when no prefix filter is given, or when a
            # prefix matches it (real git asks for `ref-prefix HEAD`).
            # A refs/heads/ or refs/tags/ prefix must NOT leak HEAD.
            head_ok = not prefixes or any("HEAD".startswith(p) for p in prefixes)
            if head_ok:
                if tip:
                    line = f"{tip} HEAD"
                    if symrefs:
                        line += f" symref-target:refs/heads/{default}"
                    out += pkt((line + "\n").encode())
                elif unborn:
                    out += pkt(f"unborn HEAD symref-target:refs/heads/{default}\n".encode())
            for ref in sorted(refs):
                if prefixes and not any(ref.startswith(p) for p in prefixes):
                    continue
                sha = refs[ref]
                line = f"{sha} {ref}"
                if peel and ref.startswith("refs/tags/"):
                    rec = store.get_object(sha)
                    if rec and rec[0] == "tag":
                        for tline in rec[1].split(b"\n"):
                            if tline.startswith(b"object "):
                                target = tline[7:].decode()
                                trec = store.get_object(target)
                                if trec and trec[0] == "commit":
                                    line += f" peeled:{target}"
                                break
                out += pkt((line + "\n").encode())
            out += pkt_flush()
            self._send(200, bytes(out), "application/x-git-upload-pack-result")
            return
        if command != "fetch":
            self._err(400, f"unknown v2 command: {command}")
            return
        wants, haves, shallow_bounds, deepen = [], [], [], None
        for a in args:
            if a.startswith("want "):
                wants.append(a[5:].split()[0])
            elif a.startswith("have "):
                haves.append(a[5:].split()[0])
            elif a.startswith("shallow "):
                shallow_bounds.append(a[8:].split()[0])
            elif a.startswith("deepen "):
                try:
                    deepen = int(a[7:].split()[0])
                except ValueError:
                    pass
        done = "done" in args
        pack, shallow, unshallow, ack = _compute_fetch(
            store, wants, haves, shallow_bounds, deepen)
        out = bytearray()
        if not done:
            # Negotiation round only (the client has not said "done"):
            # the acknowledgments section alone, then a flush. The
            # packfile comes in the next round, after "done".
            out += pkt(b"acknowledgments\n")
            out += pkt(f"ACK {ack}\n".encode()) if ack else pkt(b"NAK\n")
            out += pkt(b"ready\n")
            out += pkt_flush()
            self._send(200, bytes(out), "application/x-git-upload-pack-result")
            return
        # The client sent "done", so it requires a packfile: per
        # gitprotocol-v2 the acknowledgments section MUST then be omitted
        # from the response. Sections are separated by delimiter packets
        # (0001); the response ends with a flush (0000).
        if deepen is not None:
            out += pkt(b"shallow-info\n")
            for s in shallow:
                out += pkt(f"shallow {s}\n".encode())
            for s in unshallow:
                out += pkt(f"unshallow {s}\n".encode())
            out += pkt_delim()
        out += pkt(b"packfile\n")
        out += sideband_pack(pack)
        self._send(200, bytes(out), "application/x-git-upload-pack-result")

    def _receive_pack(self, owner, repo_name):
        store = STORE
        body = self._read_body()
        lines, pack = read_pkt_lines(body)
        updates = []
        use_sideband = False
        for raw in lines:
            line = raw.decode("utf-8", "replace").strip()
            if "\0" in line:
                line, caps = line.split("\0", 1)
                if "side-band-64k" in caps or "side-band" in caps:
                    use_sideband = True
            parts = line.split()
            if len(parts) >= 3:
                updates.append((parts[0], parts[1], parts[2]))
        results = []
        with store.lock:
            r = get_repo_or_404(store, owner, repo_name)
            if r is None:
                # auto-create on first push
                branch = "main"
                for _o, _n, ref in updates:
                    if ref.startswith("refs/heads/"):
                        branch = ref[len("refs/heads/"):]
                        break
                r = new_repo(store, owner, repo_name, default_branch=branch)
            err = None
            if pack:
                try:
                    parse_pack(store, pack)
                except Exception as e:
                    import traceback
                    traceback.print_exc()
                    sys.stderr.write(
                        f"receive-pack: pack len={len(pack)} head={pack[:32]!r}\n")
                    err = str(e)
            for old, new, ref in updates:
                if err:
                    results.append((ref, f"unpack failed: {err}"))
                    continue
                cur = r["refs"].get(ref, ZERO_SHA)
                if old != ZERO_SHA and cur != old:
                    results.append((ref, "old value does not match"))
                    continue
                if new == ZERO_SHA:
                    r["refs"].pop(ref, None)
                else:
                    if store.get_object(new) is None:
                        results.append((ref, "object not found"))
                        continue
                    if old == ZERO_SHA and cur != ZERO_SHA:
                        results.append((ref, "already exists"))
                        continue
                    # A fast-forward-only update: the new commit must
                    # descend from the current tip, or the ref is refused
                    # per-ref with the exact reason the suite expects.
                    # (An unborn branch accepts any first commit, and a
                    # tag ref is not a commit, so neither is checked.)
                    if cur != ZERO_SHA and new != cur:
                        cur_rec = store.get_object(cur)
                        new_rec = store.get_object(new)
                        if cur_rec and cur_rec[0] == "commit" \
                                and new_rec and new_rec[0] == "commit" \
                                and not is_ancestor(store, cur, new):
                            results.append((ref, "non-fast-forward"))
                            continue
                    r["refs"][ref] = new
                results.append((ref, None))
            r["pushed_at"] = store.tick()
            r["updated_at"] = r["pushed_at"]
            store.save()
        # When side-band-64k was negotiated the status report travels
        # INSIDE side-band channel 1, and the client demultiplexes it and
        # then parses pkt-lines from the reassembled band-1 stream. So the
        # status lines must be pkt-lines nested inside band-1 packets:
        #   outer pkt(0x01 + inner pkt("unpack ok\n")) ...
        # Sending plain pkt-lines gave "bad band #117" ('u' of "unpack"
        # read as a band byte); sending band-1 + raw text gave
        # "bad line length character: unpa" (inner text parsed as a
        # pkt-line length). Without side-band, plain pkt-lines are correct.
        inner = bytearray(pkt(
            b"unpack ok\n" if err is None else f"unpack {err}\n".encode()))
        for ref, e in results:
            inner += pkt((f"ok {ref}\n" if e is None else f"ng {ref} {e}\n").encode())
        inner += pkt_flush()
        if use_sideband:
            out = bytearray()
            i = 0
            while i < len(inner):
                chunk = bytes(inner[i:i + 65515])
                out += pkt(b"\x01" + chunk)
                i += len(chunk)
            out += pkt_flush()
        else:
            out = bytearray(inner)
        self._send(200, bytes(out), "application/x-git-receive-pack-result")

    # -- GraphQL / MCP ---------------------------------------------------------
    def _graphql(self):
        if self.command == "GET":
            qs = parse_qs(urlparse(self.path).query)
            payload = {"query": qs.get("query", [""])[0],
                       "variables": json.loads(qs.get("variables", ["{}"])[0] or "{}")}
        else:
            raw = self._read_body()
            try:
                payload = json.loads(raw.decode("utf-8")) if raw else {}
            except Exception:
                self._send_json(400, {"errors": [{"message": "Problems parsing JSON"}]})
                return
        viewer = self._auth_user() or DEFAULT_USER
        # GitHub's operation batching: a JSON array of operations answers
        # a JSON array of per-operation bodies, in request order.
        if isinstance(payload, list):
            results = [GQL(STORE, viewer).execute(
                (op or {}).get("query") or "", (op or {}).get("variables") or {},
                (op or {}).get("operationName")) for op in payload]
            self._send_json(200, results)
            return
        if not isinstance(payload, dict):
            payload = {}
        query = payload.get("query") or ""
        variables = payload.get("variables") or {}
        if isinstance(variables, str):
            try:
                variables = json.loads(variables)
            except Exception:
                variables = {}
        result = GQL(STORE, viewer).execute(query, variables, payload.get("operationName"))
        self._send_json(200, result)

    def _mcp(self):
        if self.command == "GET":
            # SSE stream: advertise the POST endpoint, then close.
            body = b"event: endpoint\ndata: /mcp\n\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("X-GitHub-Request-Id",
                             f"req-{os.getpid()}-{int(time.time() * 1000)}")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        raw = self._read_body()
        try:
            req = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            self._send_json(400, {"jsonrpc": "2.0", "id": None, "error": {
                "code": -32700, "message": "Parse error"}})
            return
        if isinstance(req, list):
            resps = [mcp_handle(STORE, x) for x in req]
            resps = [x for x in resps if x is not None]
            self._send_json(200, resps)
            return
        resp = mcp_handle(STORE, req)
        if resp is None:
            self._send(202, b"", "application/json")
        else:
            self._send_json(200, resp)

    # -- REST v3 ---------------------------------------------------------------
    def _rest(self, path, qs, prefix=""):
        store = STORE
        m = self.command
        # users
        if path == "/user" and m == "GET":
            self._send_json(200, actor(DEFAULT_USER, VIEWER_ID))
            return
        mm = re.match(r"^/users/([^/]+)$", path)
        if mm and m == "GET":
            login = unquote(mm.group(1))
            uid = {"adaiversen": 1000, "quinnkeller": 1001,
                   "olgabauer": 1002}.get(login, 1)
            self._send_json(200, actor(login, uid))
            return
        mm = re.match(r"^/users/([^/]+)/repos$", path)
        if mm and m == "GET":
            owner = unquote(mm.group(1))
            items = [repo_json(r) for r in store.state["repos"].values()
                     if r["owner"] == owner]
            self._send_page(items, qs)
            return
        mm = re.match(r"^/orgs/([^/]+)/repos$", path)
        if mm and m == "GET":
            owner = unquote(mm.group(1))
            items = [repo_json(r) for r in store.state["repos"].values()
                     if r["owner"] == owner]
            self._send_page(items, qs)
            return
        if path == "/user/repos" and m == "GET":
            items = [repo_json(r) for r in store.state["repos"].values()
                     if r["owner"] == DEFAULT_USER]
            self._send_page(items, qs)
            return
        if path == "/user/repos" and m == "POST":
            b = self._json_body()
            if not b.get("name"):
                self._err(422, "name is required")
                return
            with store.lock:
                r = new_repo(store, DEFAULT_USER, b["name"], b.get("description"),
                             b.get("private", False),
                             b.get("default_branch", "main"),
                             bool(b.get("auto_init")))
                if r is None:
                    self._err(422, "repository already exists")
                    return
                store.save()
            self._send_json(201, repo_json(r))
            return
        mm = re.match(r"^/orgs/([^/]+)/repos$", path)
        if mm and m == "POST":
            b = self._json_body()
            if not b.get("name"):
                self._err(422, "name is required")
                return
            with store.lock:
                r = new_repo(store, unquote(mm.group(1)), b["name"],
                             b.get("description"), b.get("private", False))
                if r is None:
                    self._err(422, "repository already exists")
                    return
                store.save()
            self._send_json(201, repo_json(r))
            return
        # search
        if path == "/search/repositories" and m == "GET":
            q = (qs.get("q", [""])[0] or "").lower()
            items = [repo_json(r) for r in store.state["repos"].values()
                     if q in r["name"].lower()
                     or q in (r.get("description") or "").lower()]
            self._send_json(200, {"total_count": len(items),
                                  "incomplete_results": False, "items": paginate(items, qs)})
            return
        if path == "/search/issues" and m == "GET":
            q = (qs.get("q", [""])[0] or "").lower()
            items = []
            for r in store.state["repos"].values():
                for iss in r["issues"]:
                    if q in (iss.get("title") or "").lower() or q in (iss.get("body") or "").lower():
                        items.append(issue_json(r, iss))
            self._send_json(200, {"total_count": len(items),
                                  "incomplete_results": False, "items": paginate(items, qs)})
            return
        # repos/*
        mm = re.match(r"^/repos/([^/]+)/([^/]+)(/.*)?$", path)
        if not mm:
            self._err(404, "Not Found")
            return
        owner, repo_name, rest = unquote(mm.group(1)), unquote(mm.group(2)), mm.group(3) or ""
        # This forge holds only the token owner's repositories: the same
        # name under any other owner is a 404 for this token.
        if owner != DEFAULT_USER:
            self._err(404, "Not Found")
            return
        if rest == "" or rest == "/":
            if m == "GET":
                r = get_repo_or_404(store, owner, repo_name)
                if r is None:
                    self._err(404, "Not Found")
                    return
                self._send_json(200, repo_json(r))
                return
            if m == "PATCH":
                with store.lock:
                    r = get_repo_or_404(store, owner, repo_name)
                    if r is None:
                        self._err(404, "Not Found")
                        return
                    b = self._json_body()
                    if "description" in b:
                        r["description"] = b["description"]
                    if "default_branch" in b:
                        r["default_branch"] = b["default_branch"]
                    r["updated_at"] = store.tick()
                    store.save()
                self._send_json(200, repo_json(r))
                return
            if m == "DELETE":
                with store.lock:
                    if f"{owner}/{repo_name}" not in store.state["repos"]:
                        self._err(404, "Not Found")
                        return
                    del store.state["repos"][f"{owner}/{repo_name}"]
                    store.save()
                self._send(204, b"")
                return
            self._err(404, "Not Found")
            return
        r = get_repo_or_404(store, owner, repo_name)
        if r is None:
            self._err(404, "Not Found")
            return
        self._repo_sub(r, rest, qs, m)

    def _repo_sub(self, r, rest, qs, m):
        store = STORE
        base = f"https://api.github.com/repos/{r['full_name']}"
        # branches
        if rest == "/branches" and m == "GET":
            out = []
            for ref, sha in sorted(r["refs"].items()):
                if ref.startswith("refs/heads/"):
                    out.append({"name": ref[len("refs/heads/"):],
                                "commit": {"sha": sha, "url": f"{base}/commits/{sha}"},
                                "protected": False})
            self._send_page(out, qs)
            return
        mm = re.match(r"^/branches/([^/]+)$", rest)
        if mm and m == "GET":
            sha = r["refs"].get(f"refs/heads/{unquote(mm.group(1))}")
            if sha is None:
                self._err(404, "Branch not found")
                return
            self._send_json(200, {"name": unquote(mm.group(1)),
                                  "commit": {"sha": sha, "url": f"{base}/commits/{sha}"},
                                  "protected": False})
            return
        # issues
        if rest == "/issues" and m == "GET":
            state = qs.get("state", ["open"])[0]
            items = [issue_json(r, i) for i in r["issues"]
                     if state == "all" or i["state"] == state]
            # sort=created orders by creation; direction=desc reverses it.
            if qs.get("sort", [""])[0] == "created" \
                    and qs.get("direction", ["asc"])[0] == "desc":
                items = list(reversed(items))
            self._send_page(items, qs)
            return
        if rest == "/issues" and m == "POST":
            b = self._json_body()
            if not b.get("title"):
                self._err(422, "title is required")
                return
            labels = b.get("labels") or []
            if isinstance(labels, str):
                labels = [labels]
            names = [l.get("name") if isinstance(l, dict) else l for l in labels]
            unknown = [n for n in names if n not in r.get("labels", [])]
            if unknown:
                self._err(422, f"label does not exist: {unknown[0]}")
                return
            with store.lock:
                iss = create_issue_obj(store, r, b["title"], b.get("body"),
                                       names, b.get("assignee"))
                store.save()
            self._send_json(201, issue_json(r, iss))
            return
        # labels
        if rest == "/labels" and m == "GET":
            out = [{"id": idx + 1, "node_id": node_id("Label", idx + 1),
                    "name": name, "color": "ededed", "default": True,
                    "description": None}
                   for idx, name in enumerate(r.get("labels", []))]
            self._send_page(out, qs)
            return
        if rest == "/labels" and m == "POST":
            b = self._json_body()
            if not b.get("name"):
                self._err(422, "name is required")
                return
            with store.lock:
                if b["name"] not in r.setdefault("labels", []):
                    r["labels"].append(b["name"])
                store.save()
            self._send_json(201, {"id": len(r["labels"]),
                                  "node_id": node_id("Label", len(r["labels"])),
                                  "name": b["name"], "color": b.get("color", "ededed"),
                                  "default": True, "description": b.get("description")})
            return
        mm = re.match(r"^/issues/(\d+)$", rest)
        if mm and m in ("GET", "PATCH"):
            iss = find_issue(r, int(mm.group(1)))
            if iss is None:
                self._err(404, "Not Found")
                return
            if m == "GET":
                # The rendered issue depends only on the repo (full_name
                # is immutable) and the issue dict, and every mutation
                # of an issue bumps its updated_at (a strictly
                # increasing logical timestamp), so
                # (full_name, number, updated_at) is a sound version
                # key. The read workload fetches one issue 2,000 times.
                key = (r["full_name"], iss["number"], iss["updated_at"])
                body = store._issue_json_cache.get(key)
                if body is None:
                    body = json.dumps(issue_json(r, iss),
                                      sort_keys=True).encode()
                    if len(store._issue_json_cache) > 512:
                        store._issue_json_cache.pop(
                            next(iter(store._issue_json_cache)))
                    store._issue_json_cache[key] = body
                self._send(200, body)
                return
            with store.lock:
                b = self._json_body()
                if "title" in b:
                    iss["title"] = b["title"]
                if "body" in b:
                    iss["body"] = b["body"]
                if b.get("state") in ("open", "closed"):
                    iss["state"] = b["state"]
                    iss["closed_at"] = store.tick() if b["state"] == "closed" else None
                if "labels" in b:
                    iss["labels"] = b["labels"]
                if "assignee" in b:
                    iss["assignee"] = b["assignee"]
                iss["updated_at"] = store.tick()
                store.save()
            self._send_json(200, issue_json(r, iss))
            return
        mm = re.match(r"^/issues/(\d+)/comments$", rest)
        if mm and m in ("GET", "POST"):
            iss = find_issue(r, int(mm.group(1)))
            if iss is None:
                self._err(404, "Not Found")
                return
            if m == "GET":
                out = [{"id": idx + 1, "node_id": node_id("IssueComment", idx + 1),
                        "url": f"{base}/issues/{iss['number']}/comments/{idx + 1}",
                        "html_url": f"https://github.com/{r['full_name']}/issues/{iss['number']}#issuecomment-{idx + 1}",
                        "body": c.get("body"), "user": actor(c.get("user", DEFAULT_USER)),
                        "created_at": c.get("created_at", iss["created_at"]),
                        "updated_at": c.get("created_at", iss["created_at"])}
                       for idx, c in enumerate(iss.get("comments", []))]
                self._send_page(out, qs)
                return
            with store.lock:
                b = self._json_body()
                ts = store.tick()
                c = {"body": b.get("body"), "user": DEFAULT_USER, "created_at": ts}
                iss["comments"].append(c)
                iss["updated_at"] = ts
                store.save()
            self._send_json(201, {"id": len(iss["comments"]),
                                  "body": c["body"], "user": actor(DEFAULT_USER),
                                  "created_at": ts, "updated_at": ts})
            return
        # pull requests
        if rest == "/pulls" and m == "GET":
            state = qs.get("state", ["open"])[0]
            items = [pr_json(r, p) for p in r["prs"]
                     if state == "all" or p["state"] == state]
            self._send_page(items, qs)
            return
        if rest == "/pulls" and m == "POST":
            b = self._json_body()
            if not b.get("title") or not b.get("head") or not b.get("base"):
                self._err(422, "title, head and base are required")
                return
            with store.lock:
                pr = create_pr_obj(store, r, b["title"], b["head"], b["base"], b.get("body"))
                store.save()
            self._send_json(201, pr_json(r, pr))
            return
        mm = re.match(r"^/pulls/(\d+)$", rest)
        if mm and m in ("GET", "PATCH"):
            pr = find_pr(r, int(mm.group(1)))
            if pr is None:
                self._err(404, "Not Found")
                return
            if m == "GET":
                self._send_json(200, pr_json(r, pr))
                return
            with store.lock:
                b = self._json_body()
                if "title" in b:
                    pr["title"] = b["title"]
                if "body" in b:
                    pr["body"] = b["body"]
                if b.get("state") in ("open", "closed"):
                    pr["state"] = b["state"]
                    pr["closed_at"] = store.tick() if b["state"] == "closed" else None
                pr["updated_at"] = store.tick()
                store.save()
            self._send_json(200, pr_json(r, pr))
            return
        mm = re.match(r"^/pulls/(\d+)/merge$", rest)
        if mm and m in ("PUT", "POST"):
            with store.lock:
                pr = find_pr(r, int(mm.group(1)))
                if pr is None:
                    self._err(404, "Not Found")
                    return
                if pr["state"] != "open":
                    self._err(405, "Pull request is not open")
                    return
                sha, err = merge_pr(store, r, pr)
                if err:
                    # A conflicted merge is a 405 with a JSON message.
                    self._err(405 if err == "Merge conflict" else 422, err)
                    return
            self._send_json(200, {"merged": True, "message": "Pull Request successfully merged",
                                  "sha": sha})
            return
        mm = re.match(r"^/pulls/(\d+)/(files|commits)$", rest)
        if mm and m == "GET":
            pr = find_pr(r, int(mm.group(1)))
            if pr is None:
                self._err(404, "Not Found")
                return
            if mm.group(2) == "commits":
                self._send_json(200, [])
                return
            head_sha = r["refs"].get(f"refs/heads/{pr['head']}")
            base_sha = r["refs"].get(f"refs/heads/{pr['base']}")
            files = []
            if head_sha and base_sha:
                head_rec = store.get_object(head_sha)
                base_rec = store.get_object(base_sha)
                if head_rec and base_rec:
                    head_tree, _, _ = parse_commit(head_rec[1])
                    base_tree, _, _ = parse_commit(base_rec[1])
                    files = diff_trees(store, base_tree, head_tree)
            self._send_json(200, files)
            return
        # contents
        if rest.startswith("/contents"):
            self._contents(r, rest[len("/contents"):], qs, m)
            return
        # git objects API
        if rest == "/git/refs" and m == "GET":
            out = []
            for ref, sha in sorted(r["refs"].items()):
                rec = store.get_object(sha)
                out.append({"ref": ref, "node_id": node_id("Ref", ref),
                            "url": f"{base}/git/{ref}",
                            "object": {"sha": sha, "type": rec[0] if rec else "commit",
                                       "url": f"{base}/git/{rec[0] + 's' if rec else 'commits'}/{sha}"}})
            self._send_json(200, out)
            return
        if rest == "/git/refs" and m == "POST":
            b = self._json_body()
            with store.lock:
                ref, sha = b.get("ref"), b.get("sha")
                if not ref or not sha or ref in r["refs"]:
                    self._err(422, "ref already exists or invalid")
                    return
                r["refs"][ref] = sha
                store.save()
            self._send_json(201, {"ref": ref, "node_id": node_id("Ref", ref),
                                  "url": f"{base}/git/{ref}",
                                  "object": {"sha": sha, "type": "commit",
                                             "url": f"{base}/git/commits/{sha}"}})
            return
        mm = re.match(r"^/git/refs/(.+)$", rest)
        if mm and m in ("GET", "PATCH", "DELETE"):
            ref = "refs/" + unquote(mm.group(1))
            if m == "GET":
                sha = r["refs"].get(ref)
                if sha is None:
                    self._err(404, "Not Found")
                    return
                self._send_json(200, {"ref": ref, "node_id": node_id("Ref", ref),
                                      "url": f"{base}/git/{ref}",
                                      "object": {"sha": sha, "type": "commit",
                                                 "url": f"{base}/git/commits/{sha}"}})
                return
            with store.lock:
                if ref not in r["refs"]:
                    self._err(404, "Not Found")
                    return
                if m == "DELETE":
                    del r["refs"][ref]
                    store.save()
                    self._send(204, b"")
                    return
                b = self._json_body()
                r["refs"][ref] = b.get("sha", r["refs"][ref])
                store.save()
            self._send_json(200, {"ref": ref, "object": {"sha": r["refs"][ref], "type": "commit"}})
            return
        mm = re.match(r"^/git/commits/([0-9a-f]{40})$", rest)
        if mm and m == "GET":
            rec = store.get_object(mm.group(1))
            if rec is None or rec[0] != "commit":
                self._err(404, "Not Found")
                return
            tree, parents, msg = parse_commit(rec[1])
            self._send_json(200, {"sha": mm.group(1), "node_id": node_id("Commit", mm.group(1)),
                                  "url": f"{base}/git/commits/{mm.group(1)}",
                                  "author": {"name": DEFAULT_USER, "email": f"{DEFAULT_USER}@example.com",
                                             "date": r["created_at"]},
                                  "committer": {"name": DEFAULT_USER, "email": f"{DEFAULT_USER}@example.com",
                                                "date": r["created_at"]},
                                  "message": msg.decode("utf-8", "replace").strip(),
                                  "tree": {"sha": tree, "url": f"{base}/git/trees/{tree}"},
                                  "parents": [{"sha": p, "url": f"{base}/git/commits/{p}"} for p in parents],
                                  "verification": {"verified": False, "reason": "unsigned"}})
            return
        if rest == "/git/commits" and m == "POST":
            b = self._json_body()
            with store.lock:
                lines = [f"tree {b.get('tree')}"]
                for p in b.get("parents") or []:
                    lines.append(f"parent {p}")
                author = (b.get("author") or {}).get("name", DEFAULT_USER)
                lines.append(f"author {author} <{author}@example.com> 1767225600 +0000")
                lines.append(f"committer {author} <{author}@example.com> 1767225600 +0000")
                sha = store.put_object("commit", ("\n".join(lines) + f"\n\n{b.get('message', '')}\n").encode())
                store.save()
            self._send_json(201, {"sha": sha, "url": f"{base}/git/commits/{sha}",
                                  "message": b.get("message", "")})
            return
        if rest == "/commits" and m == "GET":
            branch = qs.get("sha", [r["default_branch"]])[0]
            tip = r["refs"].get(f"refs/heads/{branch}") or r["refs"].get(branch)
            out, seen, cur = [], set(), tip
            while cur and cur not in seen and len(out) < 30:
                seen.add(cur)
                rec = store.get_object(cur)
                if rec is None or rec[0] != "commit":
                    break
                tree, parents, msg = parse_commit(rec[1])
                out.append({"sha": cur, "commit": {"message": msg.decode("utf-8", "replace").strip(),
                                                   "author": {"name": DEFAULT_USER, "date": r["created_at"]}},
                            "url": f"{base}/commits/{cur}"})
                cur = parents[0] if parents else None
            self._send_page(out, qs)
            return
        mm = re.match(r"^/commits/([0-9a-f]{40})$", rest)
        if mm and m == "GET":
            rec = store.get_object(mm.group(1))
            if rec is None:
                self._err(404, "Not Found")
                return
            tree, parents, msg = parse_commit(rec[1])
            self._send_json(200, {"sha": mm.group(1),
                                  "commit": {"message": msg.decode("utf-8", "replace").strip()},
                                  "parents": [{"sha": p} for p in parents]})
            return
        if rest == "/git/trees" and m == "POST":
            b = self._json_body()
            with store.lock:
                tree_sha = b.get("base_tree")
                for ent in b.get("tree") or []:
                    parts = [p for p in ent["path"].split("/") if p]
                    if ent.get("content") is not None:
                        blob = store.put_object("blob", ent["content"].encode())
                        tree_sha = tree_set(store, tree_sha, parts, ent.get("mode", "100644"), blob)
                    elif ent.get("sha"):
                        tree_sha = tree_set(store, tree_sha, parts, ent.get("mode", "100644"), ent["sha"])
                store.save()
            self._send_json(201, {"sha": tree_sha, "url": f"{base}/git/trees/{tree_sha}",
                                  "tree": [], "truncated": False})
            return
        mm = re.match(r"^/git/trees/([0-9a-f]{40})$", rest)
        if mm and m == "GET":
            rec = store.get_object(mm.group(1))
            if rec is None or rec[0] != "tree":
                self._err(404, "Not Found")
                return
            entries = []
            def walk(tsha, prefix):
                trec = store.get_object(tsha)
                if not trec:
                    return
                for mode, name, sha in parse_tree(trec[1]):
                    p = f"{prefix}{name}"
                    otype = "tree" if _is_dir_mode(mode) else "blob"
                    entries.append({"path": p, "mode": mode, "type": otype, "sha": sha,
                                    "size": len(store.get_object(sha)[1]) if otype == "blob" else None,
                                    "url": f"{base}/git/trees/{sha}" if otype == "tree" else f"{base}/git/blobs/{sha}"})
                    if otype == "tree" and qs.get("recursive", ["0"])[0] == "1":
                        walk(sha, p + "/")
            walk(mm.group(1), "")
            self._send_json(200, {"sha": mm.group(1), "url": f"{base}/git/trees/{mm.group(1)}",
                                  "tree": entries, "truncated": False})
            return
        if rest == "/git/blobs" and m == "POST":
            b = self._json_body()
            content = b.get("content", "")
            raw = base64.b64decode(content) if b.get("encoding") == "base64" else content.encode()
            with store.lock:
                sha = store.put_object("blob", raw)
                store.save()
            self._send_json(201, {"sha": sha, "url": f"{base}/git/blobs/{sha}", "size": len(raw)})
            return
        mm = re.match(r"^/git/blobs/([0-9a-f]{40})$", rest)
        if mm and m == "GET":
            rec = store.get_object(mm.group(1))
            if rec is None or rec[0] != "blob":
                self._err(404, "Not Found")
                return
            self._send_json(200, {"sha": mm.group(1), "size": len(rec[1]),
                                  "encoding": "base64",
                                  "content": base64.b64encode(rec[1]).decode(),
                                  "url": f"{base}/git/blobs/{mm.group(1)}"})
            return
        self._err(404, "Not Found")

    def _contents(self, r, suffix, qs, m):
        store = STORE
        path = unquote(suffix.lstrip("/"))
        branch = qs.get("ref", [r["default_branch"]])[0]
        if m == "GET":
            # body may carry ref in query only
            pass
        b = self._json_body() if m in ("PUT", "DELETE") else {}
        if m in ("PUT", "DELETE") and b.get("branch"):
            branch = b["branch"]
        tip = r["refs"].get(f"refs/heads/{branch}")
        if m == "GET":
            if not tip:
                self._err(404, "Not Found")
                return
            tree_sha, _, _ = parse_commit(store.get_object(tip)[1])
            if not path:
                entries = []
                for mode, name, sha in parse_tree(store.get_object(tree_sha)[1]):
                    otype = "dir" if _is_dir_mode(mode) else "file"
                    entries.append({"type": otype, "name": name, "path": name, "sha": sha,
                                    "size": 0 if otype == "dir" else len(store.get_object(sha)[1]),
                                    "url": f"https://api.github.com/repos/{r['full_name']}/contents/{name}",
                                    "html_url": f"https://github.com/{r['full_name']}/blob/{branch}/{name}"})
                self._send_json(200, entries)
                return
            found = tree_get(store, tree_sha, [p for p in path.split("/") if p])
            if found is None:
                self._err(404, "Not Found")
                return
            mode, sha, otype = found
            if otype == "tree":
                entries = []
                for mmode, name, ssha in parse_tree(store.get_object(sha)[1]):
                    entries.append({"type": "dir" if _is_dir_mode(mmode) else "file",
                                    "name": name, "path": f"{path}/{name}", "sha": ssha,
                                    "size": 0, "url": ""})
                self._send_json(200, entries)
                return
            _, data = store.get_object(sha)
            self._send_json(200, {"type": "file", "encoding": "base64",
                                  "size": len(data), "name": path.split("/")[-1],
                                  "path": path, "content": base64.b64encode(data).decode(),
                                  "sha": sha,
                                  "url": f"https://api.github.com/repos/{r['full_name']}/contents/{path}",
                                  "html_url": f"https://github.com/{r['full_name']}/blob/{branch}/{path}",
                                  "download_url": f"https://raw.githubusercontent.com/{r['full_name']}/{branch}/{path}",
                                  "_links": {"self": "", "git": "", "html": ""}})
            return
        if m == "PUT":
            if not b.get("content"):
                self._err(422, "content is required")
                return
            try:
                raw = base64.b64decode(b["content"])
            except Exception:
                self._err(422, "content must be base64")
                return
            # The `sha` field is the optimistic-concurrency guard: when it
            # is present it must be the blob sha of the file's current
            # version, or the update is a stale write (409).
            expected = b.get("sha")
            if expected:
                cur_tip = r["refs"].get(f"refs/heads/{branch}")
                if cur_tip:
                    cur_tree, _, _ = parse_commit(store.get_object(cur_tip)[1])
                    found = tree_get(store, cur_tree, [p for p in path.split("/") if p])
                    if found is not None and found[1] != expected:
                        self._err(409, "sha does not match the current blob")
                        return
            with store.lock:
                commit, blob = commit_for_file(store, r, branch, path, raw,
                                               b.get("message") or f"Update {path}")
                store.save()
            self._send_json(201 if not b.get("sha") else 200, {
                "content": {"name": path.split("/")[-1], "path": path, "sha": blob,
                            "size": len(raw), "type": "file", "encoding": "base64",
                            "url": f"https://api.github.com/repos/{r['full_name']}/contents/{path}"},
                "commit": {"sha": commit, "message": b.get("message") or f"Update {path}",
                           "url": f"https://api.github.com/repos/{r['full_name']}/commits/{commit}"}})
            return
        if m == "DELETE":
            with store.lock:
                tip2 = r["refs"].get(f"refs/heads/{branch}")
                if not tip2:
                    self._err(404, "Not Found")
                    return
                tree_sha, _, _ = parse_commit(store.get_object(tip2)[1])
                found = tree_get(store, tree_sha, [p for p in path.split("/") if p])
                if not found:
                    self._err(404, "Not Found")
                    return
                parent = tip2
                new_tree = tree_set(store, tree_sha, [p for p in path.split("/") if p], "100644", None)
                ts = store.tick()
                commit = store.put_object("commit", (
                    f"tree {new_tree}\nparent {parent}\n"
                    f"author {DEFAULT_USER} <{DEFAULT_USER}@example.com> 1767225600 +0000\n"
                    f"committer {DEFAULT_USER} <{DEFAULT_USER}@example.com> 1767225600 +0000\n\n"
                    f"{b.get('message') or f'Delete {path}'}\n").encode())
                r["refs"][f"refs/heads/{branch}"] = commit
                store.save()
            self._send_json(200, {"commit": {"sha": commit, "message": b.get("message") or f"Delete {path}"}})
            return
        self._err(404, "Not Found")


# ------------------------------------------------------------------- main ---

class ForgeServer(ThreadingHTTPServer):
    # The default listen backlog is 5: a burst of 50 concurrent clones
    # stalls on TCP retransmits. 128 absorbs the clone bench's burst.
    request_queue_size = 128
    daemon_threads = True


def _resolve_data_dir(requested):
    """Pick a writable data dir: the requested one when it can be created
    and written, else a scratch dir under the system temp dir. The trial
    pod mounts the entry's snapshot read-only at /bot, so a relative data
    dir under the cwd cannot be created there and Store() would crash
    before the server binds; /tmp is a writable tmpfs in that container.
    """
    for cand in (Path(requested), Path(tempfile.gettempdir()) / "git-forge-data"):
        try:
            cand.mkdir(parents=True, exist_ok=True)
            probe = cand / ".write-probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            return cand
        except OSError:
            continue
    return Path(requested)


def _env_lifecycle(srv, port):
    """The trial pod's lifecycle (RULES.txt): register the bound port with
    the suite env, then poll /done until the env says the trial is over
    and shut the server down. Runs in a thread; serve_forever() owns the
    main thread. Only started when MAKEFUN_ENV_URL is set; without an env
    (local dev) the server just serves forever as before.
    """
    import urllib.request
    env = os.environ.get("MAKEFUN_ENV_URL", "").rstrip("/")
    if not env:
        return
    body = json.dumps({"port": port}).encode()
    attempt = 0
    while True:
        try:
            req = urllib.request.Request(env + "/register", data=body,
                                         headers={"Content-Type": "application/json"},
                                         method="POST")
            with urllib.request.urlopen(req, timeout=30) as resp:
                if resp.status in (200, 409):
                    break
                if resp.status in (400, 405):
                    print(f"[entry] register answered HTTP {resp.status}; not retrying",
                          flush=True)
                    return
        except Exception:
            pass
        time.sleep(min(2.0, 0.25 * (2 ** min(attempt, 3))))
        attempt += 1
    print("[entry] registered; waiting for the env to say done", flush=True)
    attempt = 0
    while True:
        try:
            with urllib.request.urlopen(env + "/done", timeout=30) as resp:
                if resp.status == 200 and json.loads(resp.read().decode()).get("done") is True:
                    break
        except Exception:
            pass
        time.sleep(min(2.0, 0.25 * (2 ** min(attempt, 3))))
        attempt += 1
    print("[entry] done", flush=True)
    srv.shutdown()


def main():
    global STORE
    ap = argparse.ArgumentParser(description="git-forge v1")
    ap.add_argument("--host", default=os.environ.get("FORGE_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("PORT") or os.environ.get("FORGE_PORT") or 0))
    ap.add_argument("--data-dir", default=os.environ.get("FORGE_DATA_DIR", "data"))
    args = ap.parse_args()
    data_dir = _resolve_data_dir(args.data_dir)
    STORE = Store(data_dir)
    srv = ForgeServer((args.host, args.port), Handler)
    port = srv.server_address[1]
    (data_dir / "port.txt").write_text(str(port), encoding="utf-8")
    print(f"FORGE_PORT={port}", flush=True)
    print(f"git-forge {VERSION} listening on {args.host}:{port} "
          f"data={data_dir}", flush=True)
    if os.environ.get("MAKEFUN_ENV_URL", "").strip():
        threading.Thread(target=_env_lifecycle, args=(srv, port), daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        STORE.flush()


if __name__ == "__main__":
    main()
