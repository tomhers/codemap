#!/usr/bin/env python3
"""codemap: derive a zoomable architecture diagram from a Python or C++ codebase,
and show the difference between two versions of it as a diff on that diagram.

    python codemap.py REPO                         # map of the working tree
    python codemap.py REPO --base main             # working tree vs. a git ref
    python codemap.py REPO --base main --head pr   # two git refs
    python codemap.py REPO --base DIR --head DIR   # two directory snapshots

The code is the only source of truth: everything in the diagram is derived
from it. The one thing that is NOT derived is the grouping of files into
higher-level boxes, which comes from a small JSON config (--config) so that it
stays stable between runs. Without a config, directories are used.

Python needs only the standard library. C++ needs the tree-sitter parser
(pip install tree-sitter tree-sitter-cpp); see codemap_cpp.py.
Output is one self-contained HTML file.
"""
import argparse
import ast
import difflib
import fnmatch
import io
import json
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path

SKIP_DIRS = {"__pycache__", "node_modules", "site-packages", "venv", "env", "build", "dist"}
FUNC = (ast.FunctionDef, ast.AsyncFunctionDef)
PY_EXT = {".py"}
CPP_EXT = {".h", ".hh", ".hpp", ".hxx", ".h++", ".cc", ".cpp", ".cxx", ".c++", ".cu", ".cuh",
           ".ipp", ".inl", ".tpp", ".ixx"}
LANGS = {"python": PY_EXT, "cpp": CPP_EXT, "all": PY_EXT | CPP_EXT}
SOURCE_EXT = LANGS["all"]


def wanted(name):
    return os.path.splitext(name)[1].lower() in SOURCE_EXT


# ---------------------------------------------------------------- reading code

def read_dir(root, exclude):
    """All .py files under root, as {posix relative path: text}."""
    root = Path(root)
    files = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames
            if not d.startswith(".") and d not in SKIP_DIRS
            and not (Path(dirpath) / d / "pyvenv.cfg").exists())
        for f in sorted(filenames):
            if not wanted(f):
                continue
            rel = (Path(dirpath) / f).relative_to(root).as_posix()
            if any(fnmatch.fnmatch(rel, pat) for pat in exclude):
                continue
            files[rel] = (Path(dirpath) / f).read_text(encoding="utf-8", errors="replace")
    return files


def read_git(repo, ref, exclude):
    """All .py files at a git ref, without touching the working tree."""
    out = subprocess.run(["git", "-C", str(repo), "archive", "--format=zip", ref],
                         capture_output=True)
    if out.returncode:
        sys.exit(f"git archive {ref} failed: {out.stderr.decode(errors='replace').strip()}")
    files = {}
    with zipfile.ZipFile(io.BytesIO(out.stdout)) as z:
        for name in sorted(z.namelist()):
            if not wanted(name) or any(p in SKIP_DIRS or p.startswith(".")
                                               for p in name.split("/")[:-1]):
                continue
            if any(fnmatch.fnmatch(name, pat) for pat in exclude):
                continue
            files[name] = z.read(name).decode("utf-8", errors="replace")
    return files


def read_version(repo, spec, exclude):
    """spec is a directory path or a git ref; None means repo's working tree."""
    if spec is None:
        return read_dir(repo, exclude), "working tree"
    if Path(spec).is_dir():
        return read_dir(spec, exclude), Path(spec).resolve().name
    return read_git(repo, spec, exclude), spec


# ------------------------------------------------------------ code -> diagram

def module_name(path):
    parts = path[:-3].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def tidy(text):
    """Collapse the holes left where definitions were cut out of a body."""
    return re.sub(r"\n[ \t]*\n([ \t]*\n)+", "\n\n", text).strip("\n")


def first_line(doc):
    return doc.strip().splitlines()[0].strip() if doc and doc.strip() else ""


def span(node):
    start = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])
    return start, node.end_lineno


def flatten(node):
    """a.b.c -> ['a', 'b', 'c'], or None if the base is not a plain name."""
    chain = []
    while isinstance(node, ast.Attribute):
        chain.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        chain.append(node.id)
        return chain[::-1]
    return None


class Model:
    """The diagram being built: {'nodes': {id: node}, 'edges': {(src, dst, kind)}}.

    Node hierarchy: system > group/package > module or file > class/function > method.
    Any node may carry `src`: the code that belongs to it and to none of its
    children (a function body, a class without its methods, a file's top-level code).
    Language frontends add to it; grouping into boxes above files is shared.
    """

    def __init__(self, config):
        self.nodes, self.edges = {}, set()
        self.groups = config.get("groups") or {}
        self.add("root", "system", config.get("name") or "Codebase", None, doc=config.get("desc", ""))
        for gname, spec in self.groups.items():
            self.add("group:" + gname, "group", gname, "root",
                     doc=spec.get("desc", "") if isinstance(spec, dict) else "")

    def add(self, nid, kind, name, parent, **kw):
        base, n = nid, 1
        while nid in self.nodes:                  # redefinitions / overloads
            n += 1
            nid = f"{base}#{n}"
        self.nodes[nid] = dict(id=nid, kind=kind, name=name, parent=parent, **kw)
        return nid

    def parent_for(self, path):
        for gname, spec in self.groups.items():
            pats = spec["files"] if isinstance(spec, dict) else spec
            if any(fnmatch.fnmatch(path, p) for p in pats):
                return "group:" + gname
        if self.groups:
            return "root"                         # unmapped: visible at the top
        parent, parts = "root", path.split("/")[:-1]
        for i, part in enumerate(parts):
            pid = "dir:" + "/".join(parts[:i + 1])
            if pid not in self.nodes:
                self.add(pid, "package", part, parent)
            parent = pid
        return parent

    def finish(self):
        for gname in self.groups:                 # drop groups that matched nothing
            gid = "group:" + gname
            if not any(n["parent"] == gid for n in self.nodes.values()):
                del self.nodes[gid]
        return {"nodes": self.nodes, "edges": sorted(self.edges)}


def build_model(files, config, lang="all"):
    model = Model(config)
    exts = LANGS[lang]
    py = {p: t for p, t in files.items() if os.path.splitext(p)[1].lower() in exts & PY_EXT}
    cpp = {p: t for p, t in files.items() if os.path.splitext(p)[1].lower() in exts & CPP_EXT}
    if py:
        add_python(model, py)
    if cpp:
        try:
            import codemap_cpp
        except ImportError as e:
            sys.exit(f"C++ files need the tree-sitter parser ({e}). "
                     "Install it with: pip install tree-sitter tree-sitter-cpp")
        codemap_cpp.add_cpp(model, cpp)
    return collapse_files(model.finish())


def collapse_files(model):
    """A file that holds a single class (plus at most two helper functions), or a
    single function, adds a click and no information. Show its content in its
    place: the class takes the file's position, keeps the file's top-level code
    (includes, imports, globals) as `fsrc`, and the file's edges move to it.
    model['collapsed'] maps each removed file id to the node that replaced it."""
    nodes, kids = model["nodes"], {}
    for i, n in nodes.items():
        kids.setdefault(n["parent"], []).append(i)
    ren = {}
    for fid, f in list(nodes.items()):
        if f["kind"] not in ("file", "module"):
            continue
        ch = kids.get(fid, [])
        main = [c for c in ch if nodes[c]["kind"] in ("class", "family")]
        fns = [c for c in ch if nodes[c]["kind"] == "function"]
        if len(main) + len(fns) != len(ch):
            continue
        if len(main) == 1 and len(fns) <= 2:
            top = main[0]
        elif not main and len(fns) == 1:
            top = fns[0]
        else:
            continue
        t = nodes[top]
        t["parent"] = f["parent"]
        for h in fns:
            if h != top:
                nodes[h]["parent"] = top           # helpers sit next to the methods
        t["fsrc"], t["unit"] = f.get("src") or "", f["name"]
        if not t.get("doc"):
            t["doc"] = f.get("doc", "")
        del nodes[fid]
        ren[fid] = top
    if ren:
        edges = set()
        for a, b, k in model["edges"]:
            a, b = ren.get(a, a), ren.get(b, b)
            if a != b:
                edges.add((a, b, k))
        model["edges"] = sorted(edges)
    model["collapsed"] = ren
    return model


def add_python(model, files):
    nodes, edges, add, parent_for = model.nodes, model.edges, model.add, model.parent_for

    # ---- pass 1: structure
    mods = {}
    for path in sorted(files):
        text = files[path]
        lines = text.splitlines()
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError):
            tree = None
        m = dict(path=path, tree=tree, defs={}, methods={}, bodies=[])
        mods[path] = m
        add(path, "module", path.split("/")[-1], parent_for(path), file=path, line=1,
            loc=len(lines), doc=first_line(ast.get_docstring(tree)) if tree else
            "(could not be parsed)")
        if tree is None:
            nodes[path]["src"] = text
            continue

        def add_func(st, parent, cls):
            s, e = span(st)
            qual = f"{cls}.{st.name}" if cls else st.name
            fid = add(f"{path}::{qual}", "method" if cls else "function", st.name, parent,
                      file=path, line=s, loc=e - s + 1, sig=f"({ast.unparse(st.args)})",
                      doc=first_line(ast.get_docstring(st)), src="\n".join(lines[s - 1:e]))
            m["bodies"].append((fid, [st], cls, True))
            return fid, s, e

        covered, loose = set(), []
        for st in tree.body:
            if isinstance(st, FUNC):
                fid, s, e = add_func(st, path, None)
                m["defs"][st.name] = fid
                covered.update(range(s, e + 1))
            elif isinstance(st, ast.ClassDef):
                s, e = span(st)
                cid = add(f"{path}::{st.name}", "class", st.name, path, file=path, line=s,
                          loc=e - s + 1, doc=first_line(ast.get_docstring(st)))
                m["defs"][st.name] = cid
                inner, cls_loose = set(), list(st.decorator_list) + list(st.keywords)
                for sub in st.body:
                    if isinstance(sub, FUNC):
                        fid, fs, fe = add_func(sub, cid, st.name)
                        m["methods"][(st.name, sub.name)] = fid
                        inner.update(range(fs, fe + 1))
                    else:
                        cls_loose.append(sub)
                nodes[cid]["src"] = tidy("\n".join(
                    l for i, l in enumerate(lines[s - 1:e], s) if i not in inner))
                nodes[cid]["bases"] = st.bases
                m["bodies"].append((cid, cls_loose, st.name, False))
                covered.update(range(s, e + 1))
            else:
                loose.append(st)
        nodes[path]["src"] = tidy("\n".join(
            l for i, l in enumerate(lines, 1) if i not in covered))
        m["bodies"].append((path, loose, None, False))

    # ---- pass 2: dependencies
    dirs_with_init = {p.rsplit("/", 1)[0] if "/" in p else "" for p in files
                      if p.endswith("__init__.py")}
    by_name = {module_name(p): p for p in mods}
    for p in mods:                                # also importable from a source root
        parts = module_name(p).split(".")
        dparts = p.split("/")[:-1]
        for i in range(1, len(parts)):
            if "/".join(dparts[:i]) not in dirs_with_init:
                by_name.setdefault(".".join(parts[i:]), p)

    def lookup(path, rest):
        mm = mods[path]
        if not rest or rest[0] not in mm["defs"]:
            return None
        if len(rest) > 1:
            return mm["methods"].get((rest[0], rest[1]), mm["defs"][rest[0]])
        return mm["defs"][rest[0]]

    for m in mods.values():
        if m["tree"] is None:
            continue
        path = m["path"]
        pkg = module_name(path).split(".")
        if not path.endswith("__init__.py"):
            pkg = pkg[:-1]
        alias, roots = {}, set()
        for n in ast.walk(m["tree"]):
            if isinstance(n, ast.Import):
                for a in n.names:
                    tgt = by_name.get(a.name)
                    if tgt:
                        edges.add((path, tgt, "imports"))
                    if a.asname and tgt:
                        alias[a.asname] = ("mod", tgt)
                    elif not a.asname:
                        roots.add(a.name.split(".")[0])
            elif isinstance(n, ast.ImportFrom):
                base = (pkg[:len(pkg) - (n.level - 1)] if n.level else []) + \
                       (n.module.split(".") if n.module else [])
                base = ".".join(base)
                for a in n.names:
                    local = a.asname or a.name
                    sub = by_name.get(f"{base}.{a.name}" if base else a.name)
                    tgt = by_name.get(base)
                    if a.name == "*" and tgt:
                        edges.add((path, tgt, "imports"))
                        for d in mods[tgt]["defs"]:
                            alias[d] = ("sym", tgt, d)
                    elif sub:
                        alias[local] = ("mod", sub)
                        edges.add((path, sub, "imports"))
                    elif tgt:
                        alias[local] = ("sym", tgt, a.name)
                        edges.add((path, lookup(tgt, [a.name]) or tgt, "imports"))

        def resolve(chain, cls):
            head = chain[0]
            if head in ("self", "cls") and cls and len(chain) > 1:
                return m["methods"].get((cls, chain[1]))
            if head in alias:
                a = alias[head]
                return lookup(a[1], chain[1:]) if a[0] == "mod" else lookup(a[1], [a[2]] + chain[1:])
            if head in m["defs"]:
                return lookup(path, chain)
            if head in roots:
                for k in range(len(chain) - 1, 0, -1):
                    tgt = by_name.get(".".join(chain[:k]))
                    if tgt:
                        return lookup(tgt, chain[k:])
            return None

        for src_id, stmts, cls, is_func in m["bodies"]:
            shadow = set()
            if is_func:                           # locals that hide module names
                for st in stmts:
                    for n in ast.walk(st):
                        if isinstance(n, ast.arg):
                            shadow.add(n.arg)
                        elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                            shadow.add(n.id)

            def visit(n):
                chain = None
                if isinstance(n, ast.Attribute):
                    chain = flatten(n)
                elif isinstance(n, ast.Name):
                    chain = [n.id] if isinstance(n.ctx, ast.Load) else None
                    if chain is None:
                        return
                if chain:
                    if chain[0] not in shadow or chain[0] in ("self", "cls"):
                        tgt = resolve(chain, cls)
                        if tgt and tgt != src_id:
                            edges.add((src_id, tgt, "uses"))
                    return
                for c in ast.iter_child_nodes(n):
                    visit(c)

            for st in stmts:
                visit(st)
            for b in nodes[src_id].pop("bases", []):
                chain = flatten(b) if isinstance(b, ast.Attribute) else \
                    [b.id] if isinstance(b, ast.Name) else None
                tgt = chain and resolve(chain, None)
                if tgt:
                    edges.add((src_id, tgt, "inherits"))
                else:
                    visit(b)


# --------------------------------------------------------------------- diffing

def norm(src):
    return "\n".join(l.rstrip() for l in (src or "").splitlines())


def line_diff(old, new):
    a, b = norm(old).splitlines(), norm(new).splitlines()
    out = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            out += [[" ", l] for l in a[i1:i2]]
        else:
            out += [["-", l] for l in a[i1:i2]] + [["+", l] for l in b[j1:j2]]
    return out


def pair_renames(base, head):
    """Treat a lone removed node and a lone added node with the same kind, name and
    parent as one changed node (a signature change, or an id shifted by a new
    overload), so they diff as 'modified' instead of '-1 +1'."""
    groups = {}
    for side, model, other in (("b", base, head), ("h", head, base)):
        for nid, n in model["nodes"].items():
            if nid not in other["nodes"]:
                groups.setdefault((n["kind"], n["name"], n["parent"]), {"b": [], "h": []})[side].append(nid)
    ren = {g["b"][0]: g["h"][0] for g in groups.values() if len(g["b"]) == 1 and len(g["h"]) == 1}
    ren = {b: h for b, h in ren.items()           # never pair a file that only collapsed/expanded
           if b not in head.get("collapsed", {}) and h not in base.get("collapsed", {})}
    if not ren:
        return base
    nodes = {ren.get(i, i): dict(n, id=ren.get(i, i), parent=ren.get(n["parent"], n["parent"]))
             for i, n in base["nodes"].items()}
    edges = [[ren.get(a, a), ren.get(b, b), k] for a, b, k in base["edges"]]
    collapsed = {f: ren.get(c, c) for f, c in base.get("collapsed", {}).items()}
    return {"nodes": nodes, "edges": edges, "collapsed": collapsed}


def diff_models(base, head):
    """Merge two models into one whose nodes and edges carry a status."""
    base = pair_renames(base, head)
    bc, hc = base.get("collapsed", {}), head.get("collapsed", {})
    nodes = {}
    for nid, n in head["nodes"].items():
        n = dict(n)
        b = base["nodes"].get(nid)
        if b is None and nid in bc:            # the file existed, collapsed into a class
            old = base["nodes"][bc[nid]].get("fsrc")
            if norm(old) != norm(n.get("src")):
                n["st"], n["diff"] = "modified", line_diff(old, n.get("src"))
        elif b is None:
            n["st"] = "added"
            if n.get("src"):
                n["diff"] = [["+", l] for l in norm(n["src"]).splitlines()]
            if n.get("fsrc"):
                n["fdiff"] = [["+", l] for l in norm(n["fsrc"]).splitlines()]
        else:
            if norm(b.get("src")) != norm(n.get("src")):
                n["st"], n["diff"] = "modified", line_diff(b.get("src"), n.get("src"))
            if n.get("fsrc") is not None and b.get("fsrc") is not None and norm(b["fsrc"]) != norm(n["fsrc"]):
                n["st"], n["fdiff"] = "modified", line_diff(b["fsrc"], n["fsrc"])
        nodes[nid] = n
    for nid, b in base["nodes"].items():
        if nid in nodes:
            continue
        if nid in hc:                          # the file still exists, now collapsed
            t = nodes[hc[nid]]
            if norm(b.get("src")) != norm(t.get("fsrc")):
                t["st"] = t.get("st") or "modified"
                t["fdiff"] = line_diff(b.get("src"), t.get("fsrc"))
            continue
        n = dict(b, st="removed", parent=hc.get(b["parent"], b["parent"]))
        if n.get("src"):
            n["diff"] = [["-", l] for l in norm(n["src"]).splitlines()]
        nodes[nid] = n
    # an edge counts as the same edge whichever side its file was collapsed on
    same = {**bc, **hc}
    key = lambda e: (same.get(e[0], e[0]), same.get(e[1], e[1]), e[2])
    hk, bk = {key(e) for e in head["edges"]}, {key(e) for e in base["edges"]}
    edges = [list(e) + (["added"] if key(e) not in bk else []) for e in sorted(map(tuple, head["edges"]))]
    edges += [[hc.get(e[0], e[0]), hc.get(e[1], e[1]), e[2], "removed"]
              for e in sorted(map(tuple, base["edges"])) if key(e) not in hk]
    return {"nodes": nodes, "edges": edges}


# ------------------------------------------------------------------------- cli

def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("repo", nargs="?", default=".", help="codebase root (default: .)")
    ap.add_argument("--base", help="git ref or directory to compare against")
    ap.add_argument("--head", help="git ref or directory (default: repo working tree)")
    ap.add_argument("--config", help="JSON file with name, groups and exclude patterns")
    ap.add_argument("--exclude", action="append", default=[], help="glob of paths to skip")
    ap.add_argument("--lang", choices=sorted(LANGS), default="all", help="languages to map")
    ap.add_argument("-o", "--out", default="codemap.html")
    args = ap.parse_args()
    sys.setrecursionlimit(10000)

    config = json.loads(Path(args.config).read_text(encoding="utf-8")) if args.config else {}
    config.setdefault("name", Path(args.repo).resolve().name)
    exclude = args.exclude + config.get("exclude", [])

    head_files, head_label = read_version(args.repo, args.head, exclude)
    model = build_model(head_files, config, args.lang)
    mapped = lambda files: {p: t for p, t in files.items() if os.path.splitext(p)[1].lower() in LANGS[args.lang]}
    # whole files, so the viewer can show a definition in the context of its file
    data = {"name": config["name"], "mode": "map", "head": head_label, "files": mapped(head_files)}
    if args.base:
        base_files, base_label = read_version(args.repo, args.base, exclude)
        model = diff_models(build_model(base_files, config, args.lang), model)
        data.update(mode="diff", base=base_label)
        old, new = mapped(base_files), data["files"]
        data["fdiffs"] = {p: line_diff(old.get(p), new.get(p)) for p in sorted(set(old) | set(new))
                          if norm(old.get(p)) != norm(new.get(p))}
    data.update(model)

    template = (Path(__file__).parent / "viewer.html").read_text(encoding="utf-8")
    payload = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    Path(args.out).write_text(template.replace("/*__DATA__*/null", payload), encoding="utf-8")

    kinds = {}
    for n in model["nodes"].values():
        kinds[n["kind"]] = kinds.get(n["kind"], 0) + 1
    print(f"{args.out}: " + ", ".join(f"{v} {k}" for k, v in kinds.items() if k != "system")
          + f", {len(model['edges'])} dependencies")
    if args.base:
        st = [n.get("st") for n in model["nodes"].values()]
        print(f"  {base_label} -> {head_label}: +{st.count('added')} "
              f"~{st.count('modified')} -{st.count('removed')} nodes")


if __name__ == "__main__":
    main()
