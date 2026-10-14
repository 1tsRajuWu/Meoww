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


def backslash_in_fstring_expression(src):
    """Backslashes inside ``{...}`` expressions, which are a SyntaxError below 3.12.

    A backslash in the *literal* part of an f-string is fine everywhere; one
    inside an expression (a regex replacement like ``r'\\1=x'`` interpolated
    directly) parses locally on 3.12+ and kills the process on the 3.10/3.11
    hosts this fleet runs on — at import time, so the whole bot stays down.
    Only the expression text is inspected, via the node's own segment.
    """
    found = []
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if not isinstance(node, ast.FormattedValue):
            continue
        segment = ast.get_source_segment(src, node) or ""
        if "\\" in segment[1:-1]:
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

    def test_no_backslash_in_fstring_expressions(self):
        """An f-string expression with a backslash is a SyntaxError below 3.12.

        Locally everything runs 3.14, where it parses — and the hosts run
        3.10/3.11, where the module fails at import and the bot never starts.
        Compute the value first, then interpolate the name.
        """
        problems = []
        for path in live_files():
            src = read(path)
            for line in backslash_in_fstring_expression(src):
                problems.append(f"{rel(path)}:{line}")
        self.assertEqual(problems, [], "backslashes inside {...} expressions")

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


# Discord measures these strings and rejects the whole component when one runs
# long. The rejection arrives as an HTTP 50035 when the component is *sent* — for
# a modal, that is the moment it would have opened, so the member sees only
# "An unexpected error occurred" and the form never appears. `BUG_REPORT_HINT`
# was 122 characters against the 100 a text input's placeholder allows, which
# made ``/bot bug`` unusable with nothing in the source to suggest why.
TEXT_LIMITS = {
    ("TextInput", "placeholder"): 100,
    ("TextInput", "label"): 45,
    ("Select", "placeholder"): 150,
    ("SelectOption", "label"): 100,
    ("SelectOption", "description"): 100,
    ("Button", "label"): 80,
}


def _call_name(func):
    """The last name in a call target — ``discord.ui.TextInput`` → ``TextInput``."""
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def module_strings(tree) -> dict:
    """Module-level ``NAME = "literal"``, so a constant's length can be judged.

    The offending value was a constant that was then passed by name, which is
    exactly the indirection a reader skips over.
    """
    out = {}
    for node in tree.body:
        if isinstance(node, ast.AnnAssign):
            targets, value = [node.target], node.value
        elif isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        else:
            continue
        if not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                out[target.id] = value.value
    return out


def _static_text(node, constants):
    """The literal a keyword argument carries, or ``None`` if it is computed.

    An f-string is left alone: its length is not knowable without running it, and
    guessing would produce false failures.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return constants.get(node.id)
    return None


def over_long_component_text(path, src) -> list[str]:
    found = []
    tree = ast.parse(src)
    constants = module_strings(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        widget = _call_name(node.func)
        for kw in node.keywords:
            limit = TEXT_LIMITS.get((widget, kw.arg))
            if limit is None:
                continue
            text = _static_text(kw.value, constants)
            if text is None or len(text) <= limit:
                continue
            found.append(
                f"{rel(path)}:{node.lineno} {widget}({kw.arg}=…) is "
                f"{len(text)} characters (Discord allows {limit})"
            )
    return found


# The same measurement on the surface Discord caps next: a slash command's
# description and each of its options' are both 100 characters, and at this one
# Discord **truncates** rather than refusing — discord.py does it first, appending
# an ellipsis — so the text that ships is a sentence stopping mid-clause and
# nothing says so. `/check`'s `fix` option read *"…It is required so the answer
# is…"* in the live picker out of a 204-character source string, which is how this
# was found. Measured from the **source**, not from the tree: the tree's value is
# the already-truncated one, so a law that walked the command objects would pass
# on the very text that produced the ellipsis.
COMMAND_TEXT_LIMIT = 100


def over_long_command_text(path, src) -> list[str]:
    found = []
    tree = ast.parse(src)
    constants = module_strings(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if _call_name(node.func) not in ("command", "describe", "Group"):
            continue
        for kw in node.keywords:
            text = _static_text(kw.value, constants)
            if not isinstance(text, str) or len(text) <= COMMAND_TEXT_LIMIT:
                continue
            what = "description" if kw.arg == "description" else f"the {kw.arg!r} option"
            found.append(
                f"{rel(path)}:{node.lineno} {_call_name(node.func)}({kw.arg}=…) {what} is "
                f"{len(text)} characters (Discord allows {COMMAND_TEXT_LIMIT})"
            )
    return found


class CommandTextLimits(unittest.TestCase):
    def test_no_command_or_option_description_exceeds_its_discord_limit(self):
        problems = []
        for path in live_files():
            problems += over_long_command_text(path, read(path))
        self.assertEqual(
            problems,
            [],
            "Discord truncates it, so the sentence reaches the picker cut in half",
        )

    def test_the_check_reads_a_constant_and_measures_it(self):
        """The shape that broke: a value too long, passed by name."""
        src = (
            "FIX = %r\n"
            "long = app_commands.describe(fix=FIX)\n"
            "fine = app_commands.describe(fix='short')\n"
        ) % ("x" * 101)
        found = over_long_command_text("<synthetic>", src)
        self.assertEqual(len(found), 1, found)
        self.assertIn("101 characters (Discord allows 100)", found[0])


class ComponentTextLimits(unittest.TestCase):
    def test_no_component_text_exceeds_its_discord_limit(self):
        problems = []
        for path in live_files():
            problems += over_long_component_text(path, read(path))
        self.assertEqual(
            problems,
            [],
            "Discord rejects the component, so it never reaches the member",
        )

    def test_the_check_reads_a_constant_and_measures_it(self):
        """The guard has to catch the shape that broke, not just a literal."""
        src = (
            "TOO_LONG = %r\n"
            "other = discord.ui.TextInput(placeholder=TOO_LONG)\n"
            "fine = discord.ui.TextInput(placeholder='short')\n"
        ) % ("x" * 101)
        found = over_long_component_text("<synthetic>", src)
        self.assertEqual(len(found), 1, found)
        self.assertIn("101 characters (Discord allows 100)", found[0])


if __name__ == "__main__":
    unittest.main()
