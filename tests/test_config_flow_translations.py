"""Every field the config and options flows show must have a label and help text.

Added after `ambient_correction` shipped on 2026-08-26 with neither. Home Assistant does
not complain about a missing translation — it falls back to printing the raw schema key,
so the dialog showed a checkbox labelled `ambient_correction` next to properly labelled
neighbours. Nothing failed; it just looked unfinished, and only a screenshot caught it.

Auditing then turned up four more fields in the same dialog that had never had entries
at all: track_temperature_unit, always_enforce_unit, restore_state, schedule_target_temp.
That is the giveaway that this needs a test rather than care — the gap had been sitting
there through many releases without anyone noticing.

The check is static. The tests cannot import config_flow (no voluptuous, no
homeassistant), so the schema keys are read out of the source with `ast`: every
`vol.Required(...)`/`vol.Optional(...)` inside a flow step, with `CONF_*` names resolved
against const.py.
"""
import ast
import json
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "mspa"

# Which flow step each method builds the schema for.
_STEPS = {
    "async_step_user": ("config", "user"),
    "async_step_device": ("config", "device"),
    "async_step_init": ("options", "init"),
}


# CONF_* names the flow imports from homeassistant.const rather than defining itself.
# Their values are fixed by Home Assistant, so hardcoding them here is safe and keeps the
# check honest — without them the schema looks smaller than it is and the audit passes by
# not looking.
_HA_CONF = {
    "CONF_EMAIL": "email",
    "CONF_PASSWORD": "password",
    "CONF_USERNAME": "username",
    "CONF_HOST": "host",
    "CONF_NAME": "name",
}


def _conf_constants():
    """CONF_FOO = "foo", from const.py plus the ones taken from homeassistant.const."""
    tree = ast.parse((_ROOT / "const.py").read_text(encoding="utf-8"))
    out = dict(_HA_CONF)
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id.startswith("CONF_"):
                    out[target.id] = node.value.value
    return out


def _schema_keys_by_step():
    """The field names each flow step asks for, keyed by (section, step)."""
    conf = _conf_constants()
    tree = ast.parse((_ROOT / "config_flow.py").read_text(encoding="utf-8"))
    found = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef) or node.name not in _STEPS:
            continue
        keys = set()
        for call in ast.walk(node):
            if not isinstance(call, ast.Call) or not call.args:
                continue
            fn = call.func
            if not (isinstance(fn, ast.Attribute) and fn.attr in ("Required", "Optional")):
                continue
            arg = call.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                keys.add(arg.value)
            elif isinstance(arg, ast.Name) and arg.id in conf:
                keys.add(conf[arg.id])
        found[_STEPS[node.name]] = keys
    return found


def _translations(filename):
    return json.loads((_ROOT / filename).read_text(encoding="utf-8"))


def test_strings_and_english_translations_are_identical():
    """The two files are maintained by hand and must not drift.

    strings.json is the source of truth for translators; translations/en.json is what
    Home Assistant actually reads for a custom integration. Editing one and forgetting
    the other leaves the dialog correct in English and wrong for everyone else, or the
    reverse, with no error either way.
    """
    assert _translations("strings.json") == _translations("translations/en.json")


def test_every_flow_field_has_a_label_and_help_text():
    missing = []
    en = _translations("translations/en.json")
    for (section, step), keys in _schema_keys_by_step().items():
        body = en.get(section, {}).get("step", {}).get(step, {})
        for key in sorted(keys):
            if key not in body.get("data", {}):
                missing.append(f"{section}.step.{step}.data.{key}  (shows as a raw key)")
            if key not in body.get("data_description", {}):
                missing.append(f"{section}.step.{step}.data_description.{key}  (no help text)")
    assert not missing, "Untranslated flow fields:\n  " + "\n  ".join(missing)


def test_no_translations_for_fields_that_no_longer_exist():
    """The other direction: an entry left behind after a field was removed.

    Harmless to Home Assistant, which ignores it, but it makes the file a misleading
    description of the dialog and the next person to read it has to check.
    """
    stale = []
    en = _translations("translations/en.json")
    for (section, step), keys in _schema_keys_by_step().items():
        body = en.get(section, {}).get("step", {}).get(step, {})
        for field in ("data", "data_description"):
            for key in sorted(body.get(field, {})):
                if key not in keys:
                    stale.append(f"{section}.step.{step}.{field}.{key}")
    assert not stale, "Translations for fields the flow does not ask for:\n  " + "\n  ".join(stale)
