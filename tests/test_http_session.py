"""Every cloud call goes through one pooled session per account.

The integration used to call `requests.get` / `requests.post` directly. Each of
those builds a throwaway Session: a fresh TCP connect, TLS handshake and DNS
lookup per request. At a 30 s poll that is ~2,900 name resolutions a day per
coordinator, and on 2026-09-11 one of them landed in a satellite-link drop and
failed the poll with

    Failed to resolve 'api.iot.the-mspa.com' ([Errno -3] Try again)

Reusing the connection removes most of those lookups. What these tests protect is
the property rather than the plumbing: a single bare `requests.post` added later
would silently reopen the hole for that call, and nothing else in the suite looks.

mspa_api is replaced by a stub in conftest so coordinator.py can be imported, so
the real module is loaded here under its own name.

Run with: python -m pytest tests/test_http_session.py -v
"""
import ast
import importlib.util
import re
from pathlib import Path

import pytest
import requests

_SRC = Path(__file__).parent.parent / "custom_components" / "mspa" / "mspa_api.py"


def _real_module():
    """The genuine mspa_api, around the conftest stub of the same name."""
    spec = importlib.util.spec_from_file_location("_mspa_api_real", _SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestTheSessionItself:

    def test_it_is_a_session_with_an_https_adapter(self):
        s = _real_module()._new_session()
        try:
            assert isinstance(s, requests.Session)
            adapter = s.get_adapter("https://api.iot.the-mspa.com/api/device/")
            assert isinstance(adapter, requests.adapters.HTTPAdapter)
        finally:
            s.close()

    def test_retries_stay_at_zero(self):
        """The coordinator retries by polling again in 30 s.

        A backoff inside the call could outlast the poll interval, and this is also
        the reason urllib3 reports "Max retries exceeded" for a single attempt — the
        budget is zero, and exhausting it raises that fixed wording.
        """
        s = _real_module()._new_session()
        try:
            retries = s.get_adapter("https://x/").max_retries
            assert retries.total in (0, None), f"retries were configured: {retries}"
        finally:
            s.close()

    def test_each_call_gets_a_new_one(self):
        """Nothing module-level and shared by accident; the store owns the instance."""
        m = _real_module()
        a, b = m._new_session(), m._new_session()
        try:
            assert a is not b
        finally:
            a.close(); b.close()


class TestNothingBypassesIt:

    @staticmethod
    def _calls(func):
        """Every `requests.<func>(...)` call site left in the module."""
        tree = ast.parse(_SRC.read_text(encoding="utf-8"))
        out = []
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute) and node.attr == func
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "requests"):
                out.append(node.lineno)
        return out

    @pytest.mark.parametrize("verb", ["get", "post", "put", "delete", "request"])
    def test_no_bare_module_level_call(self, verb):
        lines = self._calls(verb)
        assert not lines, (
            f"requests.{verb} called directly at line(s) {lines} — use self._session "
            f"so the connection and its DNS lookup are reused")

    def test_the_session_comes_from_the_shared_store(self):
        """Per account, beside the token and locks — not per client, or several
        coordinators on one account would each hold their own pool."""
        src = _SRC.read_text(encoding="utf-8")
        assert '"session": _new_session()' in src
        assert 'hass.data["mspa_auth"][self._creds_key]["session"]' in src

    def test_no_call_site_can_block_for_ever(self):
        """Every request must carry a timeout, connect and read.

        Two had none until 2026-09-11 — the write command and the status poll every
        update runs. A SYN that is never answered, which is what a dropped satellite
        link looks like, would have held an executor thread for minutes with nothing
        above it hearing a thing; the rapid-poll burst fires that same status call
        once a second, so one hung call could tie up several threads at once.
        """
        tree = ast.parse(_SRC.read_text(encoding="utf-8"))
        bad = []
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "partial"):
                continue
            target = node.args[0] if node.args else None
            if not (isinstance(target, ast.Attribute)
                    and target.attr in ("get", "post", "put", "delete", "request")):
                continue
            if "timeout" not in {kw.arg for kw in node.keywords}:
                bad.append(node.lineno)
        assert not bad, f"HTTP call with no timeout at line(s) {bad}"

    def test_the_timeout_is_a_connect_read_pair(self):
        """A scalar sets both halves to the same value, so a generous read budget
        also buys a generous connect budget — the one that matters on a dead link."""
        t = _real_module()._HTTP_TIMEOUT
        assert isinstance(t, tuple) and len(t) == 2, f"not a (connect, read) pair: {t}"
        connect, read = t
        assert 0 < connect <= read
        # Worst case must stay inside one poll interval, or a stalled call is still
        # running when the next update starts.
        from custom_components.mspa.const import DEFAULT_SCAN_INTERVAL
        assert connect + read < DEFAULT_SCAN_INTERVAL, (
            f"{connect}+{read}s can outlast the {DEFAULT_SCAN_INTERVAL}s poll interval")

    def test_every_call_site_uses_it(self):
        src = _SRC.read_text(encoding="utf-8")
        n = len(re.findall(r"functools\.partial\(self\._session\.(get|post)", src))
        assert n >= 5, f"only {n} call sites go through the session"


class TestItIsClosedOnUnload:
    """A reload would otherwise strand a socket per account until the collector ran."""

    def test_unload_closes_it_in_the_executor(self):
        src = (Path(__file__).parent.parent / "custom_components" / "mspa"
               / "__init__.py").read_text(encoding="utf-8")
        block = src[src.index("async def async_unload_entry"):]
        assert "async_add_executor_job(session.close)" in block, (
            "close() does I/O and must not run on the event loop")
        assert 'auth_store = hass.data.pop("mspa_auth", None)' in block, (
            "the store must still be dropped, not just drained")
