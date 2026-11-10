# codemap

A codebase drawn as a zoomable architecture diagram, and a code change (a pull
request, two commits, two folders) drawn as a diff on that diagram.

The goal is design-first code review: see which components, classes and
dependencies a change adds, modifies or removes before reading line diffs, then
drop down to the code where it matters.

- **Code is the only source of truth.** Everything in the diagram is derived
  from the source. The only hand-written input is an optional grouping of files
  into higher-level boxes (containers), so those boxes stay stable between runs.
- **Python and C++.** Python uses the standard library parser. C++ uses
  [tree-sitter](https://tree-sitter.github.io/), so no compiler, build system
  or compile flags are needed.
- **One self-contained HTML page** per map or diff. No server needed, though
  large pages open more reliably from a local web server than as files.

## Install

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt     # Windows: .venv\Scripts\pip ...
```

The requirements are only needed for C++.

## Use

```bash
python codemap.py REPO                          # map of the working tree
python codemap.py REPO --base main              # working tree vs. a git ref
python codemap.py REPO --base main --head pr    # two git refs
python codemap.py REPO --base DIR --head DIR    # two folders
```

Options:

| option | meaning |
|---|---|
| `-o FILE` | output page (default `codemap.html`) |
| `--config FILE` | JSON with `name`, `desc`, `groups` and `exclude` (see below) |
| `--exclude GLOB` | skip matching paths (repeatable) |
| `--lang python\|cpp\|all` | which languages to map (default `all`) |

Try it on the bundled example:

```bash
python codemap.py examples/cpp_small --config examples/cpp_small.codemap.json -o cpp_small.html
```

### Config

Without a config, folders become the top-level boxes. A config can group files
into named containers instead:

```json
{
  "name": "my-project",
  "desc": "One line about it.",
  "exclude": ["third_party/*"],
  "groups": {
    "Ingestion": {"desc": "Download and convert input.", "files": ["fetch_*.py", "convert.py"]},
    "Models":    {"desc": "Train and evaluate.",         "files": ["models/*"]}
  }
}
```

Files that match no group stay at the top level.

## The page

- **Two ways to navigate** (switch in the header):
  - **Zoom** (default): semantic zoom. Boxes are sized by what they contain,
    and their own diagrams appear inside them as you zoom in, down to classes and
    functions. Scroll to zoom, drag to pan, click to select, double-click to fly
    to a box.
  - **Drill down**: click a box to open it as its own diagram.
- **Classes list their methods** inside the box, as in a UML class diagram.
  Free functions are boxes.
- **Outside boxes** sit in two bands: what depends on the current box above it,
  what it depends on below.
- **The side panel** shows the selected item's whole source file, scrolled to
  its definition, with buttons to jump between a declaration and its definition.
- **In a diff**, boxes and method rows are marked added (green), modified
  (amber) or removed (red), boxes count the changes inside them, the left panel
  lists every change, and the side panel shows the whole-file diff. In zoom mode
  unchanged parts are faded rather than hidden; "changed only" hides them.

## How it works

- `codemap.py`: reads the code (working tree, folder or `git archive` of a
  ref), builds the model for Python, collapses files that hold a single class or
  function into that class or function, diffs two models, and writes the page.
- `codemap_cpp.py`: the C++ front end. Methods defined outside their class
  (`void Foo::bar() {...}` in a `.cpp`) are placed under their class, free
  functions declared in a header are linked to their definitions, template
  specializations are grouped, and names are resolved with an approximation of
  C++ lookup restricted to what each file can see through its `#include`s.
- `viewer.html`: the page template: layered layout with edges routed between
  boxes, semantic zoom, drill-down, and the side panels.

## Limits

- Dependencies are found statically. Calls through an object
  (`obj.method()`), virtual dispatch, macros and type aliases are not followed;
  when a name is ambiguous no edge is drawn rather than a wrong one.
- Both branches of `#ifdef` blocks are included.
- A rename or move shows as a removal plus an addition, except a changed
  signature, which is matched up.
- Pages embed the full source of every mapped file, so they get large
  (about 5 MB for a 50,000-line codebase) and contain your code: share them
  accordingly.
