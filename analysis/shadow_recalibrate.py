"""Shadow-bucket recalibration: measure today's rate per bucket, scale the whole curve.

The owner's proposal, simulated before any code changes.

    On entering a bucket — including at session start — wait for `settle_degrees` of
    heating, measure the rate actually achieved over that span, and compare it with the
    shadow rate for that bucket. Scale *every* shadow bucket by the ratio, then replan.
    Hold the estimate between recalibrations.

The physical argument for scaling all buckets from one measurement is that whatever
makes today different — cover, wind, a colder night, a fuller tub — acts on the whole
curve rather than on one temperature band. Measuring again at the next boundary then
corrects the residual, because the second measurement is taken against the already
corrected shadow.

Run:  python analysis/shadow_recalibrate.py
"""
from __future__ import annotations

import importlib.util
from datetime import timedelta
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "settle_time", Path(__file__).parent / "settle_time.py")
st = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(st)

T1, T2 = st.T1, st.T2

# A factor outside this range is not a spa heating differently, it is a measurement
# that means something else — see the opening-crossing problem in the report below.
FACTOR_MIN, FACTOR_MAX = 0.5, 2.0


def _bucket(temp: float) -> int:
    return 0 if temp < T1 else 1 if temp < T2 else 2


def _minutes(rates, frm: float, to: float) -> float:
    """Time from `frm` to `to` through a piecewise-constant rate curve."""
    total = 0.0
    for lo, hi, rate in ((-999.0, T1, rates[0]), (T1, T2, rates[1]), (T2, 999.0, rates[2])):
        span = max(0.0, min(to, hi) - max(frm, lo))
        if span and rate:
            total += span / rate * 60.0
    return total


def strategy_shadow(session, settle_degrees=1.0, final_replan=False, clamp=True,
                    skip_start_bucket=False):
    """Recalibrate the shadow curve once per bucket, after `settle_degrees` of heating.

    `skip_start_bucket` declines to recalibrate in the bucket the session opens in.
    Not because that measurement is unwanted, but because the opening interval is not a
    measurement of the heating rate: the water sits somewhere unknown inside its 0.5 °C
    band when the heater engages, so the first crossings time that position rather than
    any heating. `_track_heating_rate` already classifies them the same way — logged as
    "phase-uncertain — anchored, not learned" — and they still reach the stored rates
    through the growing window once it has re-anchored on a boundary.
    """
    shadow = list(session.rates)
    start_bucket = _bucket(session.start_temp)
    eta = session.start_time + timedelta(minutes=session.logged_raw)
    out, entry, done = [], None, set()
    events = []

    for t, temp in session.crossings:
        b = _bucket(temp)
        if entry is None or _bucket(entry[1]) != b:
            entry = (t, temp)                      # first reading in this bucket

        if skip_start_bucket and b == start_bucket:
            done.add(b)
        if b not in done and entry is not None:
            span = temp - entry[1]
            hours = (t - entry[0]).total_seconds() / 3600.0
            if span >= settle_degrees and hours > 0:
                observed = span / hours
                base = shadow[_bucket(entry[1])]
                if base:
                    factor = observed / base
                    if clamp:
                        factor = max(FACTOR_MIN, min(FACTOR_MAX, factor))
                    shadow = [r * factor if r else r for r in shadow]
                    done.add(b)
                    eta = t + timedelta(minutes=_minutes(shadow, temp, session.target))
                    events.append((t, temp, observed, base, factor))

        if final_replan and temp >= session.target - 0.5 - 1e-9 and temp < session.target:
            eta = t + timedelta(minutes=_minutes(shadow, temp, session.target))

        out.append((t, eta))
    return out, events


def _sessions():
    runs = st.rising_runs(st.load_crossings())
    defs = [
        ("A  06 Aug  22.0 → 39.5", (1.11, 1.03, 1.01), 13.7, 14.011, 22.0, 992.1, 994.4,
         "2026-08-06T19:26:24+00:00"),
        ("B  10 Aug  29.0 → 39.5", (1.03, 0.99, 0.75), 14.1, 14.398, 29.0, 690.5, 688.4,
         "2026-08-10T13:03:21+00:00"),
        ("C  12 Aug  31.0 → 39.5", (1.11, 0.93, 0.77), 17.5, 13.114, 31.0, 512.2, 511.9,
         "2026-08-12T14:57:18+00:00"),
        ("D  14 Aug  33.0 → 39.5", (1.10, 0.99, 0.79), 15.2, 14.793, 33.0, 425.7, 391.1,
         "2026-08-14T03:46:08+00:00"),
    ]
    out = []
    for (name, buckets, amb, base, start_temp, raw, actual, started), run in zip(defs, runs):
        s = st.Session(name, buckets, amb, base, start_temp, 39.5, raw, actual, started)
        later = [(t, v) for t, v in run if t >= s.started_at and v <= s.target + 1e-9]
        s.crossings = [(s.started_at, s.start_temp)] + later
        out.append(s)
    return out


def main() -> None:
    sessions = _sessions()
    print("=" * 78)
    print("SHADOW-BUCKET RECALIBRATION")
    print("=" * 78)

    print("\nWhat it measures, and when (settle 1.0 °C, unclamped):\n")
    for s in sessions:
        _, ev = strategy_shadow(s, 1.0, clamp=False)
        print(f"  {s.name}")
        if not ev:
            print("     never recalibrated")
        for t, temp, obs, base, f in ev:
            mins = (t - s.start_time).total_seconds() / 60.0
            flag = "   <-- implausible" if not (FACTOR_MIN <= f <= FACTOR_MAX) else ""
            print(f"     at {mins:5.0f} min, {temp:4.1f} °C: measured {obs:5.2f} °C/h "
                  f"against {base:4.2f} -> factor {f:5.2f}{flag}")
        print()

    variants = [
        ("shadow, settle 1.0 °C, unclamped", lambda x: strategy_shadow(x, 1.0, False, False)[0]),
        ("shadow, settle 1.0 °C", lambda x: strategy_shadow(x, 1.0, False, True)[0]),
        ("shadow, settle 1.5 °C", lambda x: strategy_shadow(x, 1.5, False, True)[0]),
        ("shadow, 1.0 °C + final 0.5 replan", lambda x: strategy_shadow(x, 1.0, True, True)[0]),
        ("shadow, 1.5 °C + final 0.5 replan", lambda x: strategy_shadow(x, 1.5, True, True)[0]),
        ("shadow at boundaries only, 1.0 °C",
         lambda x: strategy_shadow(x, 1.0, False, True, True)[0]),
        ("shadow at boundaries only, 1.0 + final",
         lambda x: strategy_shadow(x, 1.0, True, True, True)[0]),
        ("shadow at boundaries only, 1.5 + final",
         lambda x: strategy_shadow(x, 1.5, True, True, True)[0]),
        ("hold the opening estimate", st.strategy_hold),
        ("settle 90/1.5 then replan each crossing",
         lambda x: st.strategy_settle_then_replan(x, 90, 1.5)),
    ]

    print("=" * 78)
    print("SCORED AGAINST THE TRUE FINISH")
    print("=" * 78)
    print(f"\n{'strategy':<40}{'whole':>8}{'last 90':>9}{'finish':>8}{'moves':>7}")
    for label, make in variants:
        whole, last, fin, moves = [], [], [], []
        for s in sessions:
            ser = make(s)
            w, _ = st.score(s, ser)
            l, f = st.score_endgame(s, ser)
            whole.append(w); last.append(l); fin.append(f)
            n, prev = 0, None
            for _, e in ser:
                if prev is not None and e != prev:
                    n += 1
                prev = e
            moves.append(n)
        print(f"{label:<40}{sum(whole)/4:>6.1f} m{sum(last)/4:>7.1f} m"
              f"{sum(fin)/4:>6.1f} m{sum(moves)/4:>7.1f}")

    print("\nPer session, the best variant against the two already on the table:\n")
    print(f"{'session':<26}{'shadow bnd 1.0+fin':>19}{'hold':>8}{'replan/crossing':>18}")
    for s in sessions:
        a, _ = st.score_endgame(s, strategy_shadow(s, 1.0, True, True, True)[0])
        b, _ = st.score_endgame(s, st.strategy_hold(s))
        c, _ = st.score_endgame(s, st.strategy_settle_then_replan(s, 90, 1.5))
        print(f"{s.name:<26}{a:>16.1f} m{b:>6.1f} m{c:>16.1f} m")
    print("\n(last-90-minute mean; 'moves' counts how often the displayed estimate changes)")


if __name__ == "__main__":
    main()
