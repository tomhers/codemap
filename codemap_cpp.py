"""C++ frontend for codemap: adds files, classes, functions and their
dependencies to a codemap Model, using the tree-sitter C++ grammar.

No compiler, build system or compile flags are needed: everything is read from
the syntax tree, and names are resolved with an approximation of C++ lookup.

Shape of the result:
  file > class/struct/union (nested classes inside) > method
  file > free function
Namespaces are not a level, because one namespace spans many files; each node
records its namespace instead. A method defined outside its class
(`void Foo::bar() {...}` in a .cpp) is placed under class Foo wherever Foo is
declared, so a class shows all of its code even when it is split over a header
and a source file.
"""
import re
from collections import defaultdict
from pathlib import PurePosixPath

from tree_sitter import Language, Parser
import tree_sitter_cpp

PARSER = Parser(Language(tree_sitter_cpp.language()))
CLASS_T = {"class_specifier", "struct_specifier", "union_specifier"}
FUNC_DECL_WRAP = {"pointer_declarator", "reference_declarator", "attributed_declarator",
                  "parenthesized_declarator"}
DECL_HOLDERS = {"declaration", "field_declaration", "type_definition", "alias_declaration"}
PASS_THROUGH = {"preproc_if", "preproc_ifdef", "preproc_else", "preproc_elif", "preproc_elifdef",
                "linkage_specification", "declaration_list", "ERROR", "export_declaration"}


def text(n):
    return n.text.decode("utf-8", errors="replace") if n is not None else ""


def strip_templates(s):
    """'ilu0<B>::apply' -> 'ilu0::apply'. Leaves operator names alone."""
    if "operator" in s:
        head, _, op = s.rpartition("operator")
        return strip_templates(head) + "operator" + op if head else s
    out, depth = [], 0
    for ch in s:
        if ch == "<":
            depth += 1
        elif ch == ">" and depth:
            depth -= 1
        elif not depth:
            out.append(ch)
    return "".join(out)


def split_name(s):
    s = re.sub(r"\s+", "", strip_templates(s))
    parts = s.split("::")
    return [p for p in parts if p] if parts[0] else [""] + [p for p in parts[1:] if p]


def squash(s):
    return re.sub(r"\s+", " ", s).strip()


def doc_of(node):
    """First useful line of the comment block right above a declaration."""
    lines, n, row = [], node.prev_sibling, node.start_point[0]
    while n is not None and n.type == "comment" and n.end_point[0] >= row - 1:
        lines[:0] = text(n).splitlines()
        row = n.start_point[0]
        n = n.prev_sibling
    for l in lines:
        l = re.sub(r"^\s*(/\*+!?|\*+/?|//+!?<?|\*)", "", l).rstrip("*/ ").strip()
        l = re.sub(r"^[\\@]brief\s*", "", l)
        if l and not l.startswith(("\\", "@", "{", "}")) and not re.fullmatch(r"[-=*/]+", l):
            return l
    return ""


def doc_start(node):
    row, n = node.start_point[0], node.prev_sibling
    while n is not None and n.type == "comment" and n.end_point[0] >= row - 1:
        row = n.start_point[0]
        n = n.prev_sibling
    return row + 1


def function_declarator(decl):
    while decl is not None and decl.type in FUNC_DECL_WRAP:
        decl = decl.child_by_field_name("declarator") or next(
            (c for c in decl.named_children if c.type not in ("type_qualifier", "attribute_declaration",
                                                              "ms_call_modifier")), None)
    return decl if decl is not None and decl.type in ("function_declarator", "operator_cast") else None


def func_name(fd):
    if fd.type == "operator_cast":
        return ["operator " + squash(text(fd.child_by_field_name("type")))]
    name = fd.child_by_field_name("declarator")
    if name is None:
        return None
    if name.type == "destructor_name":
        return ["~" + text(name)[1:].strip()]
    if name.type == "operator_name":
        return [squash(text(name))]
    if name.type == "qualified_identifier":
        t = text(name)
        if "operator" in t:
            head, _, op = t.rpartition("::operator")
            return split_name(head) + ["operator" + squash(op)] if head else [squash(t)]
        if "~" in t:
            head, _, tail = t.rpartition("::")
            return split_name(head) + [squash(tail)]
    return split_name(text(name))


def params_of(fd):
    p = fd.child_by_field_name("parameters")
    if p is None:
        d = fd.child_by_field_name("declarator")
        p = d and d.child_by_field_name("parameters")
    return p


def param_types(plist):
    """Parameter types with names and defaults removed, for matching overloads."""
    if plist is None:
        return ()
    out = []
    for prm in plist.named_children:
        if prm.type == "comment":
            continue
        t = text(prm)
        cut = []
        dv = prm.child_by_field_name("default_value")
        if dv is not None:
            cut.append((dv.start_byte, prm.end_byte))
        d = prm.child_by_field_name("declarator")
        while d is not None and d.type not in ("identifier",):
            d = d.child_by_field_name("declarator") or next(
                (c for c in d.named_children if c.type in ("identifier",) or c.type.endswith("declarator")), None)
        if d is not None:
            cut.append((d.start_byte, d.end_byte))
        b = prm.text
        for s, e in sorted(cut, reverse=True):
            b = b[:s - prm.start_byte] + b[e - prm.start_byte:]
        t = b.decode("utf-8", errors="replace")
        out.append(re.sub(r"\s*=\s*$", "", squash(t)).replace(" &", "&").replace(" *", "*"))
    return tuple(out)


def declarator_leaf(d):
    while d is not None and d.type not in ("identifier", "field_identifier"):
        nxt = d.child_by_field_name("declarator")
        if nxt is None:
            nxt = next((c for c in d.named_children if c.type in ("identifier", "field_identifier")
                        or c.type.endswith("declarator")), None)
        d = nxt
    return text(d) if d is not None else None


# ------------------------------------------------------------------- pass 1

class CppFrontend:
    def __init__(self, model, files):
        self.m, self.files = model, files
        self.trees = {}
        self.spans = defaultdict(list)       # file -> [(start, end, node_id, cut)]
        self.index = defaultdict(list)       # qualname tuple -> [node ids]
        self.by_last = defaultdict(set)      # last name -> {qualname tuples}
        self.kind_of_key = {}
        self.members = defaultdict(list)     # class id -> [(name, sig, method id)]
        self.out_of_line = []
        self.protos = []                     # free-function prototypes: (qualname, params, file, line)
        self.sigs = {}                       # function id -> parameter types, to match prototypes
        self.declared = set()                # (header, function) pairs found by link_prototypes
        self.bodies = []                     # (node id, ts node, scope, file, skip-self)
        self.owner = {}                      # (file, start_byte, end_byte) -> node id
        self.tparams = {}                    # node id -> template parameter names
        self.current = None                  # node whose code is being scanned
        self.spec = set()                    # specializations and their members

    def run(self):
        for path in sorted(self.files):
            self.parse_file(path)
        self.includes()
        for item in self.out_of_line:
            self.attach_out_of_line(*item)
        self.link_prototypes()
        self.cut_sources()
        self.primary_of = {}
        for cid in sorted(self.spec):          # specialization -> primary template
            n = self.m.nodes[cid]
            if n["kind"] == "class" and "<" in n["name"]:
                key = tuple(split_name("::".join(filter(None, [n.get("ns", ""), n["q"]]))))
                prim = [h for h in self.index.get(key, []) if h not in self.spec]
                self.primary_of[cid] = self.narrow(prim, n["file"])
                for h in self.primary_of[cid]:
                    self.m.edges.add((cid, h, "specializes"))
        for b in self.bodies:
            self.scan(*b)
        for f, h in self.declared:             # a header naming its own prototypes declares them
            if (f, h, "uses") in self.m.edges:
                self.m.edges.discard((f, h, "uses"))
                self.m.edges.add((f, h, "declares"))
        for nid in self.tparams:
            self.m.nodes[nid].pop("_nslen", None)
        self.group_specializations()

    def group_specializations(self):
        """Collect a file's template specializations into boxes, so that a file that
        specializes thirty traits shows a handful of boxes instead of thirty:
          - a primary template and its specializations in the same file share a
            'Template family' box;
          - specializations of templates defined in another file are collected in
            one 'Specializations' box per defining file, with a box per template
            inside when it is specialized more than once;
          - specializations of templates outside the codebase (std::hash<...>) are
            collected per template when there are two or more.
        Only the parent of a node changes; ids stay the same, so diffs still line up."""
        nodes = self.m.nodes
        by_file = defaultdict(list)
        for cid, prim in self.primary_of.items():
            n = nodes[cid]
            if n["parent"] == n["file"]:                    # top-level in its file
                by_file[n["file"]].append((cid, prim))

        def box(nid, name, parent, label, members, doc):
            first = min(members, key=lambda m: nodes[m].get("line", 0))
            gid = self.m.add(nid, "family", name, parent, file=nodes[first]["file"], line=nodes[first]["line"],
                             kl=label, q=name, doc=doc, ns=nodes[first].get("ns", ""))
            for mem in members:
                nodes[mem]["parent"] = gid
            return gid

        for path, specs in sorted(by_file.items()):
            same, ext, unknown = defaultdict(list), defaultdict(list), defaultdict(list)
            for cid, prim in specs:
                here = [h for h in prim if nodes[h]["file"] == path and nodes[h]["parent"] == path]
                fam = nodes[cid]["q"].split("<")[0].strip()
                if here:
                    same[here[0]].append(cid)
                elif prim:
                    ext[nodes[prim[0]]["file"]].append((fam, cid))
                else:
                    unknown[fam].append(cid)
            for p, members in same.items():
                box(f"{path}::<family {nodes[p]['q']}>", nodes[p]["name"], path, "Template family",
                    [p] + members, f"The {nodes[p]['name']} template and {len(members)} specialization"
                    f"{'s' if len(members) > 1 else ''} of it")
            for src, members in sorted(ext.items()):
                if len(members) < 2:
                    continue
                short = "/".join(PurePosixPath(src).parts[-2:])     # interface.hpp alone is ambiguous
                gid = box(f"{path}::<specializations of {src}>", "of " + short, path, "Specializations",
                          [c for _, c in members], f"Specializations of templates defined in {src}")
                fams = defaultdict(list)
                for fam, c in members:
                    fams[fam].append(c)
                if len(fams) > 1:
                    for fam, cs in sorted(fams.items()):
                        if len(cs) > 1:
                            box(f"{path}::<specializations of {src}::{fam}>", fam, gid, "Specializations",
                                cs, f"{len(cs)} specializations of {fam} (defined in {src})")
            for fam, cs in sorted(unknown.items()):
                if len(cs) > 1:
                    box(f"{path}::<specializations of {fam}>", fam, path, "Specializations", cs,
                        f"{len(cs)} specializations of {fam}, which is defined outside this codebase")

    def link_prototypes(self):
        """A free function declared in a header and defined in a .cpp file stays where
        it is defined, and remembers where it is declared (decl_file/decl_line), the
        way out-of-line methods do. Code that includes only the header can then still
        reach it (see seen_from)."""
        for qual, sig, path, line in self.protos:
            key = tuple(split_name("::".join(qual)))
            hits = [h for h in self.index.get(key, []) if self.m.nodes[h]["kind"] == "function"
                    and self.m.nodes[h]["file"] != path and "decl_file" not in self.m.nodes[h]]
            if len(hits) > 1:
                hits = [h for h in hits if self.sigs.get(h) == sig]
            if len(hits) == 1:
                self.m.nodes[hits[0]].update(decl_file=path, decl_line=line)
                self.declared.add((path, hits[0]))

    def seen_from(self, nid, vis):
        """Can code in a file that sees the files in `vis` use this definition? Yes if
        it sees the file the definition is in, or the header that declares it."""
        n = self.m.nodes[nid]
        return n["file"] in vis or n.get("decl_file") in vis

    def register(self, nid, qual):
        key = tuple(split_name("::".join(qual)))
        self.index[key].append(nid)
        self.by_last[key[-1]].add(key)
        self.kind_of_key.setdefault(key, self.m.nodes[nid]["kind"])
        self.m.nodes[nid]["q"] = "::".join(qual[self.m.nodes[nid].get("_nslen", 0):])

    def add_node(self, base_id, kind, name, parent, sig_key, **kw):
        nid = base_id
        if nid in self.m.nodes and sig_key is not None:
            nid = f"{base_id}({','.join(sig_key)})"
        return self.m.add(nid, kind, name, parent, **kw)

    def parse_file(self, path):
        src = self.files[path]
        tree = PARSER.parse(src.encode("utf-8"))
        self.trees[path] = tree
        lines = src.splitlines()
        fid = self.m.add(path, "file", PurePosixPath(path).name, self.m.parent_for(path),
                         file=path, line=1, loc=len(lines), doc="", lang="cpp")
        self.spans[path].append((1, max(1, len(lines)), fid, False))
        self.bodies.append((fid, tree.root_node, [], path, True))
        first = next((c for c in tree.root_node.children if c.type == "comment"), None)
        if first is not None:
            d = doc_of_block(text(first))
            self.m.nodes[fid]["doc"] = d
        self.walk(tree.root_node, path, [], [], fid, None, ())

    def walk(self, node, path, ns, cls, parent, cls_id, tparams):
        for c in node.children:
            t = c.type
            if t == "namespace_definition":
                nm = c.child_by_field_name("name")
                parts = split_name(text(nm)) if nm is not None else ["(anonymous)"]
                body = c.child_by_field_name("body")
                if body is not None:
                    self.walk(body, path, ns + parts, cls, parent, cls_id, tparams)
            elif t == "template_declaration":
                tp = c.child_by_field_name("parameters")
                names = tuple(n for n in (declarator_leaf_tparam(x) for x in (tp.named_children if tp else [])) if n)
                inner = [x for x in c.named_children if x.type not in ("template_parameter_list", "comment")]
                for x in inner:
                    self.item(x, c, path, ns, cls, parent, cls_id, tparams + names,
                              squash("template " + text(tp)) if tp is not None else "template")
            elif t in PASS_THROUGH:
                self.walk(c, path, ns, cls, parent, cls_id, tparams)
            else:
                self.item(c, c, path, ns, cls, parent, cls_id, tparams, "")

    def item(self, c, outer, path, ns, cls, parent, cls_id, tparams, tmpl):
        t = c.type
        if t in CLASS_T:
            if c.child_by_field_name("body") is not None:
                self.add_class(c, outer, path, ns, cls, parent, tparams, tmpl, None)
        elif t in DECL_HOLDERS:
            ty = c.child_by_field_name("type")
            if ty is not None and ty.type in CLASS_T and ty.child_by_field_name("body") is not None:
                alias = None
                if ty.child_by_field_name("name") is None:
                    alias = declarator_leaf(c.child_by_field_name("declarator")) or text(c.child_by_field_name("declarator"))
                self.add_class(ty, outer, path, ns, cls, parent, tparams, tmpl, alias)
            elif cls_id is not None and t in ("field_declaration", "declaration"):
                fd = function_declarator(c.child_by_field_name("declarator"))
                if fd is not None:
                    self.add_function(c, outer, fd, path, ns, cls, parent, cls_id, tparams, tmpl, False)
            elif cls_id is None and t == "declaration":     # a free function's prototype, e.g. in a header
                fd = function_declarator(c.child_by_field_name("declarator"))
                name = func_name(fd) if fd is not None else None
                if name:
                    self.protos.append((ns + name, param_types(params_of(fd)), path, doc_start(outer)))
        elif t == "function_definition":
            fd = function_declarator(c.child_by_field_name("declarator"))
            if fd is not None:
                self.add_function(c, outer, fd, path, ns, cls, parent, cls_id, tparams, tmpl, True)
        elif t in ("friend_declaration",) or t in PASS_THROUGH:
            self.walk(c, path, ns, cls, parent, cls_id, tparams)
        elif t == "template_declaration":
            self.walk_one(c, path, ns, cls, parent, cls_id, tparams)

    def walk_one(self, c, path, ns, cls, parent, cls_id, tparams):
        holder = type("H", (), {"children": [c]})
        self.walk(holder, path, ns, cls, parent, cls_id, tparams)

    def add_class(self, c, outer, path, ns, cls, parent, tparams, tmpl, alias):
        nm = c.child_by_field_name("name")
        name = squash(text(nm)) if nm is not None else (alias or f"(anonymous {c.type.split('_')[0]})")
        qual = ns + cls + [name]
        s, e = doc_start(outer), outer.end_point[0] + 1
        cid = self.m.add(f"{path}::{'::'.join(qual)}", "class", name, parent, file=path, line=s,
                         loc=e - s + 1, doc=doc_of(outer), ns="::".join(ns), tmpl=tmpl,
                         ck=c.type.split("_")[0], _nslen=len(ns))
        self.register(cid, qual)
        if "<" in name or parent in self.spec:
            self.spec.add(cid)
        self.spans[path].append((s, e, cid, True))
        self.owner[(path, outer.start_byte, outer.end_byte)] = cid
        self.tparams[cid] = tparams
        self.bodies.append((cid, outer, [strip_templates(x) for x in ns + cls + [name]], path, True))
        body = c.child_by_field_name("body")
        self.walk(body, path, ns, cls + [name], cid, cid, tparams)

    def add_function(self, c, outer, fd, path, ns, cls, parent, cls_id, tparams, tmpl, is_def):
        name = func_name(fd)
        if not name:
            return
        sig_key = param_types(params_of(fd)) + tuple(
            text(q) for q in fd.children if q.type in ("type_qualifier", "ref_qualifier"))
        plist = params_of(fd)
        sig = squash(" ".join([text(plist) if plist is not None else "()"] + list(sig_key[len(param_types(plist)):])))
        s, e = doc_start(outer), outer.end_point[0] + 1
        info = dict(file=path, line=s, loc=e - s + 1, doc=doc_of(outer), sig=sig,
                    ns="::".join(ns), tmpl=tmpl, _nslen=len(ns))
        if len(name) > 1 and cls_id is None:            # Foo::bar defined outside Foo
            self.out_of_line.append((c, outer, name, sig_key, info, path, ns, parent, tparams))
            return
        qual = ns + cls + name
        kind = "method" if cls_id is not None else "function"
        fid = self.add_node(f"{path}::{'::'.join(qual)}", kind, name[-1], parent, sig_key,
                            decl=not is_def, **info)
        self.register(fid, qual)
        self.sigs[fid] = param_types(params_of(fd))
        if cls_id is not None:
            self.members[cls_id].append((name[-1], sig_key, fid))
            if cls_id in self.spec:
                self.spec.add(fid)
        self.spans[path].append((s, e, fid, is_def))
        self.owner[(path, outer.start_byte, outer.end_byte)] = fid
        self.tparams[fid] = tparams
        self.bodies.append((fid, outer, [strip_templates(x) for x in ns + cls], path, False))

    def attach_out_of_line(self, c, outer, name, sig_key, info, path, ns, parent, tparams):
        cls_path, short = name[:-1], name[-1]
        hits = self.lookup(cls_path, ns, path, types_only=True)
        cid = hits[0] if hits and len({self.m.nodes[h]["kind"] for h in hits}) == 1 and \
            self.m.nodes[hits[0]]["kind"] == "class" else None
        s, e = info["line"], info["line"] + info["loc"] - 1
        if cid is None:                                  # class not found: keep it in its file
            qual = ns + name
            fid = self.add_node(f"{path}::{'::'.join(qual)}", "function", "::".join(name), parent,
                                sig_key, **info)
            self.register(fid, qual)
            self.sigs[fid] = sig_key
            scope = ns + cls_path
        else:
            cands = [(n, k, i) for n, k, i in self.members[cid] if n == short]
            match = cands[0] if len(cands) == 1 else next((x for x in cands if x[1] == sig_key), None)
            if match is not None and self.m.nodes[match[2]].get("decl"):
                fid = match[2]
                node = self.m.nodes[fid]
                node.update(info, decl=False, decl_line=node["line"], decl_file=node["file"])
            else:
                cq = self.m.nodes[cid]["q"]
                cns = self.m.nodes[cid].get("ns", "")
                qual = (cns.split("::") if cns else []) + cq.split("::") + [short]
                info = dict(info, ns=cns, _nslen=len(cns.split("::")) if cns else 0)
                fid = self.add_node(f"{self.m.nodes[cid]['file']}::{'::'.join(qual)}", "method", short,
                                    cid, sig_key, **info)
                self.register(fid, qual)
                self.members[cid].append((short, sig_key, fid))
            if cid in self.spec:
                self.spec.add(fid)
            scope = (self.m.nodes[cid]["ns"].split("::") if self.m.nodes[cid].get("ns") else []) + \
                self.m.nodes[cid]["q"].split("::")
            scope = [strip_templates(x) for x in scope]
        self.spans[path].append((s, e, fid, True))
        self.owner[(path, outer.start_byte, outer.end_byte)] = fid
        self.tparams[fid] = tparams
        self.bodies.append((fid, outer, scope, path, False))

    def cut_sources(self):
        """Each node keeps the lines of its span not covered by a definition nested in it."""
        for path, spans in self.spans.items():
            lines = self.files[path].splitlines()
            cuts = [(s, e) for s, e, _, cut in spans if cut]
            for s, e, nid, _ in spans:
                node = self.m.nodes[nid]
                if node.get("file") != path or (node["kind"] in ("function", "method") and node["line"] != s):
                    continue
                hole = set()
                for cs, ce in cuts:
                    if s <= cs and ce <= e and (cs, ce) != (s, e):
                        hole.update(range(cs, ce + 1))
                keep = [l for i, l in enumerate(lines[s - 1:e], s) if i not in hole]
                node["src"] = tidy("\n".join(keep))
                if node["kind"] == "file":
                    node["loc"] = len(lines)

    # --------------------------------------------------------------- pass 2

    def includes(self):
        by_base = defaultdict(list)
        for p in self.files:
            by_base[PurePosixPath(p).name].append(p)
        self.inc = defaultdict(set)
        for path, tree in self.trees.items():
            stack = [tree.root_node]
            while stack:
                n = stack.pop()
                if n.type == "preproc_include":
                    pn = n.child_by_field_name("path")
                    if pn is None:
                        continue
                    inc = text(pn).strip('<>"')
                    tgt = self.resolve_include(path, inc, by_base)
                    if tgt:
                        self.inc[path].add(tgt)
                        self.m.edges.add((path, tgt, "includes"))
                elif n.type in PASS_THROUGH or n.type in ("translation_unit", "namespace_definition"):
                    stack.extend(n.children)
        self.visible_memo = {}

    def resolve_include(self, path, inc, by_base):
        norm = normpath(str(PurePosixPath(path).parent / inc))
        if norm in self.files:
            return norm
        cands = [p for p in by_base.get(PurePosixPath(inc).name, []) if ("/" + p).endswith("/" + inc)]
        if len(cands) > 1:                     # nearest one wins
            cands.sort(key=lambda p: -len(common_prefix(p, path)))
        return cands[0] if cands else None

    def visible(self, path):
        if path not in self.visible_memo:
            seen, stack = {path}, [path]
            while stack:
                for n in self.inc.get(stack.pop(), ()):
                    if n not in seen:
                        seen.add(n)
                        stack.append(n)
            self.visible_memo[path] = seen
        return self.visible_memo[path]

    def lookup(self, path, scope, file, types_only=False, call=False):
        """Approximate C++ name lookup. Returns node ids, or [] if unknown/ambiguous."""
        if not path:
            return []
        if path[0] == "":
            return self.narrow(self.index.get(tuple(path[1:]), []), file)
        for i in range(len(scope), -1, -1):
            key = tuple(scope[:i]) + tuple(path)
            if key in self.index:
                hits = self.index[key]
                if types_only:
                    hits = [h for h in hits if self.m.nodes[h]["kind"] == "class"]
                if hits:
                    return self.narrow(hits, file)
        keys = [k for k in self.by_last.get(path[-1], ()) if k[-len(path):] == tuple(path)]
        if types_only:
            keys = [k for k in keys if self.kind_of_key[k] == "class"]
        if len(path) >= 2:
            if len(keys) == 1:                 # a unique qualified name: trust it
                return self.narrow(self.index[keys[0]], file, strict=False)
            vis = self.visible(file)
            keys = [k for k in keys if any(self.seen_from(h, vis) for h in self.index[k])]
            return self.narrow(self.index[keys[0]], file) if len(keys) == 1 else []
        if call:                                # unqualified call: a visible free function
            vis = self.visible(file)
            keys = [k for k in keys if self.kind_of_key[k] == "function"
                    and any(self.seen_from(h, vis) for h in self.index[k])]
            return self.narrow(self.index[keys[0]], file) if len(keys) == 1 else []
        return []

    def narrow(self, hits, file, strict=True):
        """Several definitions share one name (overloads, specializations, the same
        name in several files). Prefer members of the class the reference is in,
        then primary templates, then the nearest ones. With strict, a definition
        the file cannot see through its includes is dropped."""
        hits = list(hits)
        for c in self.enclosing_classes(self.current):
            mine = [h for h in hits if c in self.ancestors(h)]
            if mine:
                return mine
        prim = [h for h in hits if h not in self.spec]
        vis = self.visible(file)
        if not prim:                           # only specializations: must be included
            hits = [h for h in hits if self.seen_from(h, vis)]
        hits = prim or hits
        same = [h for h in hits if self.m.nodes[h]["file"] == file]
        if same:
            return same
        near = [h for h in hits if self.seen_from(h, vis)]
        return near if strict else (near or hits)

    def scan(self, nid, root, scope, file, is_container):
        """Record what the code owned by nid refers to."""
        shadow = set(self.tparams.get(nid, ()))
        refs = []                                # (path, kind, is_call)
        stack = [(root, scope)]
        first = True
        while stack:
            n, sc = stack.pop()
            if not first:
                key = (file, n.start_byte, n.end_byte)
                if key in self.owner and self.owner[key] != nid:
                    continue                     # a nested definition: scanned on its own
            first = False
            t = n.type
            if t in ("comment", "string_literal", "raw_string_literal", "char_literal",
                     "preproc_include", "number_literal"):
                continue
            if t == "namespace_definition":
                nm = n.child_by_field_name("name")
                body = n.child_by_field_name("body")
                if body is not None:
                    stack.append((body, sc + (split_name(text(nm)) if nm is not None else [])))
                continue
            if t == "template_parameter_list":
                for x in n.named_children:
                    nm = declarator_leaf_tparam(x)
                    if nm:
                        shadow.add(nm)
                    d = x.child_by_field_name("default_type") or x.child_by_field_name("default_value")
                    if d is not None:
                        stack.append((d, sc))
                continue
            if t in ("parameter_declaration", "optional_parameter_declaration", "init_declarator",
                     "declaration", "for_range_loop") and not is_container:
                leaf = declarator_leaf(n.child_by_field_name("declarator"))
                if leaf:
                    shadow.add(leaf)
            if t == "base_class_clause":
                for b in n.named_children:
                    if b.type in ("type_identifier", "qualified_identifier", "template_type"):
                        refs.append((split_name(text(b)), "inherits", False, sc))
                        self.push_args(b, stack, sc)
                continue
            if t == "qualified_identifier":
                p = split_name(text(n))
                is_call = n.parent is not None and n.parent.type == "call_expression"
                refs.append((p, "uses", is_call, sc))
                self.push_args(n, stack, sc)
                continue
            if t in ("type_identifier", "identifier"):
                name = text(n)
                if name not in shadow:
                    callee, par = n, n.parent
                    if par is not None and par.type == "template_function":
                        callee, par = par, par.parent
                    is_call = par is not None and par.type == "call_expression" and \
                        par.child_by_field_name("function") == callee
                    refs.append(([name], "uses", is_call, sc))
                continue
            if t == "field_expression":
                arg = n.child_by_field_name("argument")
                fld = n.child_by_field_name("field")
                if arg is not None and arg.type == "this" and fld is not None:
                    refs.append(([strip_templates(text(fld))], "uses", True, sc))
                if arg is not None:
                    stack.append((arg, sc))
                continue
            if t in ("field_identifier", "namespace_identifier", "destructor_name"):
                continue
            stack.extend((c, sc) for c in n.children)

        self.current = nid
        for p, kind, is_call, sc in refs:
            if p and p[0] in shadow:
                continue
            for tgt in self.lookup(p, sc, file, types_only=(kind == "inherits"), call=is_call):
                if tgt != nid and not self.is_ancestor(tgt, nid) and not self.is_ancestor(nid, tgt):
                    self.m.edges.add((nid, tgt, kind))
        self.current = None

    def push_args(self, n, stack, sc):
        """Visit the template arguments inside a (qualified) name, but not the name itself."""
        todo = [n]
        while todo:
            x = todo.pop()
            for c in x.children:
                if c.type == "template_argument_list":
                    stack.extend((a, sc) for a in c.named_children)
                elif c.type in ("template_type", "qualified_identifier", "template_function", "dependent_name"):
                    todo.append(c)

    def ancestors(self, nid):
        memo = self.__dict__.setdefault("_anc", {})
        if nid not in memo:
            out, x = [], self.m.nodes[nid].get("parent")
            while x:
                out.append(x)
                x = self.m.nodes[x].get("parent")
            memo[nid] = out
        return memo[nid]

    def enclosing_classes(self, nid):
        if nid is None:
            return []
        chain = ([nid] if self.m.nodes[nid]["kind"] == "class" else []) + self.ancestors(nid)
        return [x for x in chain if self.m.nodes[x]["kind"] == "class"]

    def is_ancestor(self, a, b):
        x = self.m.nodes[b].get("parent")
        while x:
            if x == a:
                return True
            x = self.m.nodes[x].get("parent")
        return False


def declarator_leaf_tparam(x):
    if x.type in ("type_parameter_declaration", "variadic_type_parameter_declaration"):
        ids = [c for c in x.named_children if c.type == "type_identifier"]
        return text(ids[0]) if ids else None
    if x.type in ("optional_type_parameter_declaration",):
        n = x.child_by_field_name("name")
        return text(n) if n is not None else None
    if x.type in ("parameter_declaration", "optional_parameter_declaration",
                  "variadic_parameter_declaration"):
        return declarator_leaf(x.child_by_field_name("declarator"))
    if x.type == "template_template_parameter_declaration":
        for c in x.named_children:
            if c.type in ("type_parameter_declaration", "optional_type_parameter_declaration"):
                return declarator_leaf_tparam(c)
    return None


def doc_of_block(t):
    for l in t.splitlines():
        l = re.sub(r"^\s*(/\*+!?|\*+/?|//+!?<?|\*)", "", l).rstrip("*/ ").strip()
        l = re.sub(r"^[\\@](brief|file)\s*\S*\s*", "", l) if l.startswith(("\\file", "@file")) else \
            re.sub(r"^[\\@]brief\s*", "", l)
        if l and not l.startswith(("\\", "@")) and not re.fullmatch(r"[-=*/]+", l) \
                and "copyright" not in l.lower() and "license" not in l.lower():
            return l
    return ""


def tidy(s):
    return re.sub(r"\n[ \t]*\n([ \t]*\n)+", "\n\n", s).strip("\n")


def normpath(p):
    out = []
    for part in p.split("/"):
        if part == "..":
            if out:
                out.pop()
        elif part not in ("", "."):
            out.append(part)
    return "/".join(out)


def common_prefix(a, b):
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return a[:n]


def add_cpp(model, files):
    CppFrontend(model, files).run()
