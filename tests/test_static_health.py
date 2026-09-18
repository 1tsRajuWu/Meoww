"""Static health of the live tree — the checks an editor's "problems" panel shows.

A pass over the whole tree found six unused imports, three bare ``except:``
clauses, two f-strings with no placeholder, and several computed-but-discarded
values. Each fix is cheap; remembering to re-check by eye is not. These tests make
the properties enforced instead of aspirational, using only the standard library
(so there is nothing new to install and no linter to configure).

Everything here scans the *live* tree only: ``bot.py``, ``keep_alive.py``,
``utils/`` and ``cogs/``. ``deleted_features/`` is an archive and is deliberately
excluded, since it is never imported and still refers to removed dependencies.
"""
import ast
import builtins
import os
import symtable
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOTS = ["bot.py", "keep_alive.py", "utils", "cogs"]
SKIP_DIRS = {".venv", "__pycache__", ".git"}

BUILTINS = set(dir(builtins)) | {
    "__name__", "__file__", "__doc__", "__package__", "__spec__", "__loader__",
    "__builtins__", "self", "cls",
}


def live_files():
    out = []
    for root in ROOTS:
        path = os.path.join(ROOT, root)
        if os.path.isfile(path):
            out.append(path)
        else:
            for dirpath, dirnames, filenames in os.walk(path):
                dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
                out += [os.path.join(dirpath, f) for f in filenames if f.endswith(".py")]
    return sorted(out)


def rel(path):
    return os.path.relpath(path, ROOT)


def read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _bound(table):
    return {
        s.get_name() for s in table.get_symbols()
        if s.is_assigned() or s.is_parameter() or s.is_imported() or s.is_namespace()
    }


def undefined_names(path, src):
    """Names read but never bound anywhere reachable.

    ``symtable`` resolves closure variables, so a name bound in an enclosing
    function counts as defined. Python 3.14's synthesized ``__annotate__`` tables
    and dunder artifacts are skipped.
    """
    top = symtable.symtable(src, path, "exec")
    module_names = _bound(top)
    found = []

    def visit(table, enclosing):
        for sym in table.get_symbols():
            name = sym.get_name()
            if "__annotate__" in table.get_name() or name.startswith("__"):
                continue
            if not sym.is_referenced():
                continue
            if name in BUILTINS or name in module_names or name in enclosing:
                continue
            if sym.is_global() or sym.is_free():
                found.append((table.get_name(), name))
        child_scope = set(enclosing)
        if table.get_type() == "function":
            child_scope |= _bound(table)
        for child in table.get_children():
            visit(child, child_scope)

    visit(top, set())
    return sorted(set(found))


def unused_imports(src):
    tree = ast.parse(src)
    imported = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "*":
                    return []
                imported.setdefault(alias.asname or alias.name.split(".")[0], node.lineno)
        elif isinstance(node, ast.ImportFrom):
            if node.module == "__future__":
                continue          # a compiler directive, not a used name
            for alias in node.names:
                if alias.name == "*":
                    return []
                imported.setdefault(alias.asname or alias.name, node.lineno)

    used = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            base = node
            while isinstance(base, ast.Attribute):
                base = base.value
            if isinstance(base, ast.Name):
                used.add(base.id)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            # Names referenced from strings (e.g. a module path or __all__ entry).
            cleaned = node.value.replace("(", " ").replace(")", " ")
            for tok in cleaned.replace("'", " ").replace('"', " ").split():
                used.add(tok.strip(".,:[]"))
    return sorted((name, line) for name, line in imported.items() if name not in used)


def plain_fstrings(src):
    """f-strings that interpolate nothing.

    ``{value:,.2f}`` produces a ``JoinedStr`` for the *format spec* in 3.12+, which
    has no ``FormattedValue`` of its own — those are excluded, or almost every
    formatted number in the codebase would be reported.
    """
    tree = ast.parse(src)
    spec_ids = {
        id(node.format_spec) for node in ast.walk(tree)
        if isinstance(node, ast.FormattedValue) and node.format_spec is not None
    }
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.JoinedStr) or id(node) in spec_ids:
            continue
        if any(isinstance(v, ast.FormattedValue) for v in node.values):
            continue
        segment = (ast.get_source_segment(src, node) or "").lstrip()
        prefix = ""
        for ch in segment:
            if ch.isalpha():
                prefix += ch
            else:
                break
        if "f" in prefix.lower() and prefix:
            found.append(node.lineno)
    return sorted(found)


class StaticHealth(unittest.TestCase):
    def test_every_live_file_parses(self):
        broken = []
        for path in live_files():
            src = read(path)
            try:
                ast.parse(src, path)
            except SyntaxError as exc:
                broken.append(f"{rel(path)}:{exc.lineno}: {exc.msg}")
        self.assertEqual(broken, [], "files that do not parse")

    def test_no_undefined_names(self):
        problems = []
        for path in live_files():
            src = read(path)
            for scope, name in undefined_names(path, src):
                problems.append(f"{rel(path)}: '{name}' in {scope}")
        self.assertEqual(problems, [], "names read but never defined")

    def test_no_unused_imports(self):
        problems = []
        for path in live_files():
            src = read(path)
            for name, line in unused_imports(src):
                problems.append(f"{rel(path)}:{line}: {name}")
        self.assertEqual(problems, [], "imported but never used")

    def test_no_bare_except(self):
        """A bare ``except:`` also swallows CancelledError and KeyboardInterrupt."""
        problems = []
        for path in live_files():
            tree = ast.parse(read(path), path)
            for node in ast.walk(tree):
                if isinstance(node, ast.ExceptHandler) and node.type is None:
                    problems.append(f"{rel(path)}:{node.lineno}")
        self.assertEqual(problems, [], "bare except clauses")

    def test_no_fstrings_without_placeholders(self):
        problems = []
        for path in live_files():
            src = read(path)
            for line in plain_fstrings(src):
                problems.append(f"{rel(path)}:{line}")
        self.assertEqual(problems, [], "f-strings that interpolate nothing")

    def test_no_duplicate_top_level_definitions(self):
        """A second definition silently wins, which is how a handler goes dead."""
        problems = []
        for path in live_files():
            tree = ast.parse(read(path), path)

            def scan(body, where):
                seen = {}
                for node in body:
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        if node.name in seen:
                            problems.append(
                                f"{rel(path)}: {where}.{node.name} redefined at line {node.lineno}"
                                f" (was line {seen[node.name]})"
                            )
                        seen[node.name] = node.lineno
                        scan(node.body, f"{where}.{node.name}")
                    elif isinstance(node, (ast.If, ast.For, ast.While, ast.Try)):
                        for field in ("body", "orelse", "finalbody"):
                            sub = getattr(node, field, None)
                            if sub:
                                scan(sub, where)

            scan(tree.body, "<module>")
        self.assertEqual(problems, [], "duplicate definitions")


def _is_error_decorator(node) -> bool:
    """``@<something>.error`` — discord.py's per-command error handler."""
    return isinstance(node, ast.Attribute) and node.attr == "error"


def re_raising_error_handlers(path, src) -> list[str]:
    """Command error handlers that raise instead of letting the tree's run.

    discord.py calls a command's own ``on_error`` *before* ``tree.on_error`` and
    does not guard the call, so a ``raise`` in the cog's handler means the tree's
    handler never runs and the interaction is never answered at all — which the
    member sees as "the application did not respond", the least diagnosable
    failure the bot can produce. ``cogs/tracker.py`` re-raised every error that
    was not a permission failure.
    """
    found = []
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not any(_is_error_decorator(d) for d in node.decorator_list):
            continue
        for child in ast.walk(node):
            if isinstance(child, ast.Raise):
                found.append(f"{rel(path)}:{child.lineno} {node.name} raises")
    return found


class CommandErrorHandlers(unittest.TestCase):
    def test_no_command_error_handler_re_raises(self):
        problems = []
        for path in live_files():
            problems += re_raising_error_handlers(path, read(path))
        self.assertEqual(
            problems,
            [],
            "a cog error handler that raises leaves the command unanswered",
        )


if __name__ == "__main__":
    unittest.main()
