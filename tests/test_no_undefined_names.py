"""Every name the integration uses must exist.

Added after a real failure on 2026-08-26. A call to `MSpaAmbientLearningSensor` was
added to the sensor platform's setup while the class definition itself never landed in
the file. The full suite — 409 tests — passed, because nothing imports the sensor
platform's setup path: the tests exercise functions, and a name that is only referenced
inside `async_setup_entry` is never evaluated until Home Assistant calls it.

It failed at runtime as a NameError, which aborted setup of the *whole* sensor platform.
Not one broken sensor — every mspa sensor absent, Ready at and Heat Schedule included,
on a live spa.

A unit test per class would not have caught it either, since the missing class is the
one nobody wrote a test for. What catches it is asking the question of the whole
package: is any name used that is not defined? That is a job for a static checker rather
than for a test that has to be remembered.
"""
import subprocess
import sys
from pathlib import Path

PKG = Path(__file__).parent.parent / "custom_components" / "mspa"


def test_no_undefined_names_anywhere_in_the_integration():
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pyflakes", *sorted(str(p) for p in PKG.glob("*.py"))],
            capture_output=True, text=True,
        )
        missing = "No module named" in result.stderr
    except FileNotFoundError:                      # pragma: no cover
        missing = True
    # Fails rather than skips, and that is the point.
    #
    # It skipped before, and a skip is invisible in a green run: the whole suite reported
    # "469 passed, 1 skipped" for an entire development session while the one check
    # standing between this integration and a whole-platform startup failure was not
    # running at all. A guard that quietly turns itself off when its tool is absent is
    # worse than no guard, because it is also a claim that nothing is wrong.
    if missing:                                    # pragma: no cover
        raise AssertionError(
            "pyflakes is not installed, so the undefined-name guard cannot run. "
            "Install the test requirements: pip install -r requirements-test.txt")

    # Only undefined names. pyflakes also reports unused imports and shadowing, which are
    # tidiness rather than breakage, and failing on those would make this test something
    # people turn off.
    undefined = [
        line for line in result.stdout.splitlines()
        if "undefined name" in line
    ]
    assert not undefined, (
        "a name is used that is never defined — this is the shape of failure that takes "
        "a whole platform down at startup:\n  " + "\n  ".join(undefined)
    )


def test_entity_picture_is_declared_only_where_intended():
    """Only the climate and water-temperature entities may carry entity_picture.

    Home Assistant renders a picture in preference to an icon, so an entity_picture on
    MSpaBaseEntity puts the same photograph of the spa on every row of the device panel
    and there is nothing left to tell the rows apart. But the key cannot simply be
    dropped either: `entity_picture` is what the picture cards read, so the entities a
    card would be pointed at have to declare it — climate (the spa itself) and water
    temperature (a picture card with the reading in the footer, as the MSpa Link app
    shows it). Water temperature is diagnostic and off by default, so its row is only
    present for someone who went looking.

    This guards both directions: a picture creeping back onto the shared base class, and
    a picture removed so thoroughly that no card can be pointed at anything.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "mspa"
    counts = {
        path.name: path.read_text(encoding="utf-8").count("def entity_picture")
        for path in root.glob("*.py")
    }
    declaring = {name: n for name, n in counts.items() if n}
    assert declaring == {"climate.py": 1, "sensor.py": 1}, (
        "entity_picture should be declared once in climate.py and once in sensor.py "
        f"(water temperature), found: {declaring}"
    )


def _package_attribute_index():
    """For every class in the package: what it assigns, and what its relatives assign.

    Relatives in both directions. Ancestors because state set in a base class is
    perfectly ordinary; descendants because of the template-method pattern, where a base
    reads `self._shadow_name` and every concrete subclass supplies it — the base is the
    one place the attribute is legitimately never assigned.
    """
    import ast
    classes, bases = {}, {}
    for path in sorted(PKG.glob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
            classes[cls.name] = (path, cls)
            bases[cls.name] = [b.id for b in cls.bases if isinstance(b, ast.Name)]
    return classes, bases


def _defined_on(cls):
    """Names a class body supplies: assignments to self, methods, class attributes."""
    import ast
    defined = set()
    for node in ast.walk(cls):
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign, ast.For,
                             ast.withitem)):
            targets = (node.targets if isinstance(node, ast.Assign)
                       else [getattr(node, "target", None)
                             or getattr(node, "optional_vars", None)])
            for t in targets:
                for sub in ast.walk(t) if t is not None else ():
                    if (isinstance(sub, ast.Attribute)
                            and isinstance(sub.value, ast.Name)
                            and sub.value.id == "self"):
                        defined.add(sub.attr)
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "setattr" and len(node.args) >= 2
                and isinstance(node.args[0], ast.Name) and node.args[0].id == "self"
                and isinstance(node.args[1], ast.Constant)):
            defined.add(node.args[1].value)
    for n in cls.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(n.name)
        for t in (n.targets if isinstance(n, ast.Assign)
                  else [n.target] if isinstance(n, ast.AnnAssign) else []):
            if isinstance(t, ast.Name):
                defined.add(t.id)
    return defined


def _self_attribute_report(path):
    """Attributes each class reads off `self` but never assigns anywhere.

    pyflakes does not do this: it resolves names, and `self.foo` is an attribute access,
    not a name. Proven rather than assumed — a class reading an attribute that is never
    assigned anywhere passes pyflakes with exit 0 and no output.

    Three things are deliberately not flagged, because each is a real pattern here rather
    than an exception carved out to make the test pass:

    * **Method calls.** `self.async_show_form(...)` reads an attribute the Home Assistant
      base class supplies, and nothing in this package can see those. Restricting the
      check to reads that are *not* immediately called removes every framework method at
      a stroke and loses nothing, because the failure this exists to catch is missing
      *state*, not a missing method — a missing method is a typo that any exercise of the
      path finds instantly.
    * **`getattr(self, "x", default)` and `hasattr`.** The deliberate way this codebase
      asks for something that may not be there. Flagging it would teach people to silence
      the check rather than to fix anything.
    * **Attributes a relative supplies**, in either direction. See
      `_package_attribute_index`.
    """
    import ast
    classes, bases = _package_attribute_index()

    def inherited(name, seen=None):
        seen = seen or set()
        if name in seen or name not in classes:
            return set()
        seen.add(name)
        out = _defined_on(classes[name][1])
        for b in bases.get(name, ()):
            out |= inherited(b, seen)
        return out

    def from_descendants(name):
        out = set()
        for other, bs in bases.items():
            if name in bs:
                out |= _defined_on(classes[other][1]) | from_descendants(other)
        return out

    tree = ast.parse(path.read_text(), filename=str(path))
    out = {}
    for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
        called, read, guarded = set(), {}, set()
        for node in ast.walk(cls):
            if isinstance(node, ast.Call):
                if (isinstance(node.func, ast.Attribute)
                        and isinstance(node.func.value, ast.Name)
                        and node.func.value.id == "self"):
                    called.add(node.func.attr)
                if (isinstance(node.func, ast.Name)
                        and node.func.id in ("getattr", "hasattr", "setattr")
                        and len(node.args) >= 2
                        and isinstance(node.args[0], ast.Name)
                        and node.args[0].id == "self"
                        and isinstance(node.args[1], ast.Constant)):
                    guarded.add(node.args[1].value)
            if (isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load)
                    and isinstance(node.value, ast.Name) and node.value.id == "self"):
                read.setdefault(node.attr, node.lineno)
        defined = (inherited(cls.name) | from_descendants(cls.name) | guarded | called)
        missing = {a: ln for a, ln in read.items()
                   if a not in defined and not a.startswith("__")}
        if missing:
            out[cls.name] = missing
    return out


def test_no_self_attribute_is_read_without_ever_being_assigned():
    """The gap pyflakes leaves, and it is the shape that breaks a live integration.

    An AttributeError inside the coordinator's update path does not fail loudly: it is
    caught as an update failure, so every entity goes unavailable at once and the reason
    is one line the user has to enable debug logging to see. That is worse than the
    NameError this file was written for, because at least a NameError aborts setup
    somewhere visible.

    Inherited attributes are the one thing this cannot see, so a class that legitimately
    reads a base class's attribute belongs in the allowance below rather than in a
    weakened rule.
    """
    # State Home Assistant's own base classes provide, which nothing here can see.
    FRAMEWORK = {"hass", "config_entry", "coordinator", "data", "entity_id", "platform",
                 "registry_entry", "last_update_success", "context"}
    offenders = {}
    for path in sorted(PKG.glob("*.py")):
        for cls, attrs in _self_attribute_report(path).items():
            bad = {a: ln for a, ln in attrs.items()
                   if a not in FRAMEWORK and not a.startswith("_attr_")}
            if bad:
                offenders[f"{path.name}::{cls}"] = bad
    assert not offenders, (
        "an attribute is read off self but never assigned anywhere in its class. In the "
        "coordinator this surfaces as every entity going unavailable at once:\n  "
        + "\n  ".join(f"{k}: " + ", ".join(f"{a} (line {ln})" for a, ln in sorted(v.items()))
                      for k, v in sorted(offenders.items())))
