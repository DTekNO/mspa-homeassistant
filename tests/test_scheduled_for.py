"""Tests for the Scheduled for datetime entity.

Focus: re-asserting the same ready time must not disturb an in-progress
heat-up.  Committing a value clears _schedule_triggered and ready_latched, so
an automation that re-syncs the schedule on a timer (the documented
calendar-sync pattern polls every 15 minutes because a calendar entity does not
emit a state-change event when its next-event attribute is unchanged) would
otherwise re-arm the scheduler on every run.

Run with: python -m pytest tests/test_scheduled_for.py -v
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from custom_components.mspa.datetime import MSpaScheduledReadyAt, _same_instant

_T = datetime(2026, 8, 1, 15, 0, tzinfo=timezone.utc)


# ── Helpers ───────────────────────────────────────────────────────────────────

class _Coord:
    def __init__(self, scheduled=None, triggered=False, latched=False):
        self.scheduled_ready_at = scheduled
        self._schedule_triggered = triggered
        self.ready_latched = latched
        self.schedule_target_temp = 39.5
        self._last_data = {"water_temperature": "37.0"}
        self.listeners_pushed = 0

    def async_update_listeners(self):
        self.listeners_pushed += 1


def _entity(coord) -> MSpaScheduledReadyAt:
    e = object.__new__(MSpaScheduledReadyAt)
    e.coordinator = coord
    e._debounce_cancel = None
    e._pending_value = None
    e.async_write_ha_state = lambda: None
    e.hass = None
    return e


def _run(coro):
    return asyncio.run(coro)


# ═══════════════════════════════════════════════════════════════════════════════
# _same_instant
# ═══════════════════════════════════════════════════════════════════════════════

class TestSameInstant:

    def test_identical_aware_datetimes_match(self):
        assert _same_instant(_T, _T) is True

    def test_different_times_do_not_match(self):
        assert _same_instant(_T, _T + timedelta(minutes=1)) is False

    def test_same_moment_in_different_zones_matches(self):
        other = _T.astimezone(timezone(timedelta(hours=2)))
        assert _same_instant(_T, other) is True

    def test_naive_aware_mix_compares_wall_clock_instead_of_raising(self):
        assert _same_instant(_T, _T.replace(tzinfo=None)) is True

    def test_none_never_matches(self):
        assert _same_instant(None, _T) is False
        assert _same_instant(_T, None) is False
        assert _same_instant(None, None) is False


# ═══════════════════════════════════════════════════════════════════════════════
# Re-assert must not re-arm the scheduler
# ═══════════════════════════════════════════════════════════════════════════════

class TestUnchangedReAssert:

    def test_same_value_does_not_schedule_a_commit(self):
        """The documented 15-min calendar sync re-sends the same time repeatedly."""
        c = _Coord(scheduled=_T, triggered=True)
        e = _entity(c)
        _run(e.async_set_value(_T))
        assert e._debounce_cancel is None, "no commit should have been scheduled"

    def test_same_value_preserves_triggered_flag(self):
        """The regression: a mid-heat-up re-sync must not re-arm the trigger."""
        c = _Coord(scheduled=_T, triggered=True, latched=True)
        e = _entity(c)
        _run(e.async_set_value(_T))
        assert c._schedule_triggered is True
        assert c.ready_latched is True

    def test_same_value_does_not_push_listeners(self):
        c = _Coord(scheduled=_T, triggered=True)
        e = _entity(c)
        _run(e.async_set_value(_T))
        assert c.listeners_pushed == 0

    def test_repeated_re_asserts_stay_inert(self):
        c = _Coord(scheduled=_T, triggered=True)
        e = _entity(c)
        for _ in range(20):          # ~5 h of heating at one sync per 15 min
            _run(e.async_set_value(_T))
        assert c._schedule_triggered is True
        assert e._debounce_cancel is None

    def test_re_assert_matching_a_pending_pick_is_also_inert(self):
        """Second field of the UI picker, or a sync landing inside the debounce."""
        c = _Coord(scheduled=None)
        e = _entity(c)
        e._pending_value = _T
        e._debounce_cancel = lambda: None
        _run(e.async_set_value(_T))
        assert e._pending_value == _T


# ═══════════════════════════════════════════════════════════════════════════════
# A genuine change must still be accepted
# ═══════════════════════════════════════════════════════════════════════════════

class TestChangedValue:

    def test_new_value_schedules_a_commit(self):
        c = _Coord(scheduled=_T, triggered=True)
        e = _entity(c)
        e.hass = object()
        _run(e.async_set_value(_T + timedelta(hours=1)))
        assert e._pending_value == _T + timedelta(hours=1)

    def test_first_ever_value_is_accepted(self):
        c = _Coord(scheduled=None)
        e = _entity(c)
        e.hass = object()
        _run(e.async_set_value(_T))
        assert e._pending_value == _T

    def test_commit_rearms_scheduler_and_clears_latch(self):
        """A real reschedule must still reset trigger state — that is the point."""
        c = _Coord(scheduled=_T, triggered=True, latched=True)
        e = _entity(c)
        new = _T + timedelta(hours=2)
        e._pending_value = new
        e._debounce_cancel = lambda: None
        e._commit_pending_value(None)
        assert c.scheduled_ready_at == new
        assert c._schedule_triggered is False
        assert c.ready_latched is False
        assert c.listeners_pushed == 1
