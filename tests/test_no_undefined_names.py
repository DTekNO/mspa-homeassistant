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
    except FileNotFoundError:                      # pragma: no cover
        import pytest
        pytest.skip("pyflakes not installed")
    if "No module named" in result.stderr:         # pragma: no cover
        import pytest
        pytest.skip("pyflakes not installed")

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
