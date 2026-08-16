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

# How far the shadow curve may stray from the rates that were learned. Applied to the
# *cumulative* deviation, not to each step: a single large factor is legitimate when it
# is bringing the shadow back toward the learned rates after an earlier over-correction,
# and clamping steps individually blocks exactly that recovery. Session C needed 0.41 to
# undo a bad opening measurement and a per-step clamp held it at 0.5, leaving it 10 min
# out for the rest of the run.
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


def strategy_shadow(session, settle_degrees=1.0, final_replan=False, clamp=True):
    """Recalibrate the shadow curve once per bucket, measuring from a known position.

    The measurement window is anchored on a *crossing*, never on the session start.

    At the start the water sits somewhere unknown inside its 0.5 °C band, so the
    interval from there to the first crossing times that position rather than any
    heating — on session C it implied 7.9 °C/h. Only that interval is set aside; the
    first crossing then becomes an exact position and everything from it is measured.

    At a bucket boundary nothing is set aside at all, because the crossing that enters
    the bucket is itself an exact position. So a cold start measures from 22.5 rather
    than 22.0, and from 30.0 and 37.0 exactly.

    The shadow curve is entirely separate from the stored bucket learning, which
    continues untouched: this exists to make the ready time right over the closing
    hour, while the stored rates are what the scheduler plans from.
    """
    shadow = list(session.rates)
    eta = session.start_time + timedelta(minutes=session.logged_raw)
    out, anchor, done, events = [], None, set(), []

    for i, (t, temp) in enumerate(session.crossings):
        b = _bucket(temp)
        if anchor is None:
            if i > 0:                       # first crossing after the start
                anchor = (t, temp)
        elif _bucket(anchor[1]) != b:       # a boundary crossing, exact by definition
            anchor = (t, temp)

        if anchor is not None and b not in done:
            span = temp - anchor[1]
            hours = (t - anchor[0]).total_seconds() / 3600.0
            if span >= settle_degrees and hours > 0:
                observed = span / hours
                base = shadow[_bucket(anchor[1])]
                if base:
                    factor = observed / base
                    if clamp:
                        cumulative = (shadow[0] / session.rates[0]) * factor
                        if cumulative > FACTOR_MAX:
                            factor *= FACTOR_MAX / cumulative
                        elif cumulative < FACTOR_MIN:
                            factor *= FACTOR_MIN / cumulative
                    shadow = [r * factor if r else r for r in shadow]
                    done.add(b)
                    eta = t + timedelta(minutes=_minutes(shadow, temp, session.target))
                    events.append((t, temp, anchor[1], observed, base, factor))

        if final_replan and session.target - 0.5 - 1e-9 <= temp < session.target:
            eta = t + timedelta(minutes=_minutes(shadow, temp, session.target))

        out.append((t, eta))
    return out, events


# A measurement over one degree, applied to sixteen degrees of remaining journey,
# multiplies its own error sixteenfold.  Recalibrating early therefore made things
# worse in every recorded session — 150, 102 and 267 minutes worse — while the same
# arithmetic near the target, where two or three degrees remain, lands within a few
# minutes.  So recalibrate only where the remaining span is comparable to the span
# actually measured.
MAX_AMPLIFICATION = 4.0


def strategy_late_only(session, settle_degrees=1.0, final_replan=True):
    """Recalibrate once, on entering the last bucket, then replan for the final 0.5 °C.

    Same closing accuracy as recalibrating at every boundary, without the mid-session
    excursion, and with two changes to the displayed estimate rather than three or
    seventeen.  Before the recalibration the opening estimate stands.
    """
    shadow = list(session.rates)
    eta = session.start_time + timedelta(minutes=session.logged_raw)
    out, anchor, done, events = [], None, set(), []

    for i, (t, temp) in enumerate(session.crossings):
        b = _bucket(temp)
        if anchor is None:
            if i > 0:
                anchor = (t, temp)
        elif _bucket(anchor[1]) != b:
            anchor = (t, temp)

        if anchor is not None and b not in done:
            span = temp - anchor[1]
            hours = (t - anchor[0]).total_seconds() / 3600.0
            if span >= settle_degrees and hours > 0:
                if (session.target - temp) / span <= MAX_AMPLIFICATION:
                    observed = span / hours
                    base = shadow[_bucket(anchor[1])]
                    if base:
                        factor = observed / base
                        cumulative = (shadow[0] / session.rates[0]) * factor
                        if cumulative > FACTOR_MAX:
                            factor *= FACTOR_MAX / cumulative
                        elif cumulative < FACTOR_MIN:
                            factor *= FACTOR_MIN / cumulative
                        shadow = [r * factor if r else r for r in shadow]
                        eta = t + timedelta(
                            minutes=_minutes(shadow, temp, session.target))
                        events.append((t, temp, anchor[1], observed, base, factor))
                done.add(b)

        if final_replan and session.target - 0.5 - 1e-9 <= temp < session.target:
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
        for t, temp, frm, obs, base, f in ev:
            mins = (t - s.start_time).total_seconds() / 60.0
            flag = "   <-- implausible" if not (FACTOR_MIN <= f <= FACTOR_MAX) else ""
            print(f"     at {mins:5.0f} min, {frm:4.1f}→{temp:4.1f} °C: measured "
                  f"{obs:5.2f} °C/h against {base:4.2f} -> factor {f:5.2f}{flag}")
        print()

    variants = [
        ("shadow, settle 1.0 °C, unclamped", lambda x: strategy_shadow(x, 1.0, False, False)[0]),
        ("shadow, settle 1.0 °C", lambda x: strategy_shadow(x, 1.0, False, True)[0]),
        ("shadow, settle 1.5 °C", lambda x: strategy_shadow(x, 1.5, False, True)[0]),
        ("shadow, 1.0 °C + final 0.5 replan", lambda x: strategy_shadow(x, 1.0, True, True)[0]),
        ("shadow, 1.5 °C + final 0.5 replan", lambda x: strategy_shadow(x, 1.5, True, True)[0]),
        ("shadow, 2.0 °C + final 0.5 replan", lambda x: strategy_shadow(x, 2.0, True, True)[0]),
        ("LATE ONLY: last bucket + final 0.5", lambda x: strategy_late_only(x)[0]),
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
    print(f"{'session':<26}{'shadow 1.0+final':>18}{'hold':>8}{'replan/crossing':>18}")
    for s in sessions:
        a, _ = st.score_endgame(s, strategy_shadow(s, 1.0, True, True)[0])
        b, _ = st.score_endgame(s, st.strategy_hold(s))
        c, _ = st.score_endgame(s, st.strategy_settle_then_replan(s, 90, 1.5))
        print(f"{s.name:<26}{a:>16.1f} m{b:>6.1f} m{c:>16.1f} m")
    print("\n(last-90-minute mean; 'moves' counts how often the displayed estimate changes)")


if __name__ == "__main__":
    main()


# ── Extra shadow boundaries ──────────────────────────────────────────────────
#
# The owner asked whether the shadow needs a boundary the stored buckets do not have.
# It does, and one is the right number.  Mean |error| over the whole session:
#
#                            rates correct         rates 30% too fast
#                          A    B    C    D       A    B    C    D
#   3 buckets (30, 37)    23   45   77   30      73   88  110   38
#   + half bucket at 34   21   32   43   25      72   74   75   35
#   + halves at 26, 34    14   32   43   25     103   74   75   35
#
# One extra boundary improves every session in both scenarios, and the closing
# accuracy is unchanged (1.5 / 1.6 / 1.1 / 5.7), so it is a gain in the middle of the
# session for nothing.  Two makes the cold start markedly worse when the stored rates
# are wrong: the 26 °C band is measured too early and over too narrow a span, and the
# resulting factor scales a journey that still has fifteen degrees to run.
#
# The winter column is a synthetic test — the recorded crossings replayed against
# stored rates scaled by 1.3, standing in for summer rates meeting a January spa.
# There is no cold-weather data yet; when there is, re-run this before trusting it.
#
# Note the closing-90 figures come out identical whether the stored rates are right or
# 30% wrong.  That is not a bug in the harness: once the shadow has recalibrated near
# the target, the estimate no longer depends on what the stored rates were.  It is the
# property the design exists for.


# ── Detecting a wrong shadow rate early: six approaches that do not work ─────
#
# The question was whether we can tell the shadow curve is off before the water is
# close enough to the target for a measurement to be proportionate. On a 20 °C start
# the gated version does not recalibrate until 38 °C — minute 907 of 994 on session A.
#
# Scored at a quarter / half / three-quarters through, minutes wrong, averaged over
# the four recorded sessions and over the same sessions replayed against stored rates
# scaled by 1.3 (a stand-in for summer rates meeting a winter spa):
#
#                                        summer            winter
#   flat buckets, gate 4 (shipped)       10 / 10 /  4     142 / 142 / 99
#   recalibrate early, undamped         142 / 113 / 36    142 / 113 / 36
#   recalibrate early, damped            55 /  47 / 22    155 / 123 / 77
#   2 °C bands, flat, no gate            66 /  42 / 12     66 /  42 / 12
#   2 °C bands, tilted, no gate          55 /  42 /  6     55 /  38 /  7
#   bypass the gate when |f-1| >= 0.25   77 /  77 /  4    176 / 127 / 51
#
# Every early correction buys winter and sells summer. The bypass was the most
# promising — act only on deviations too large to be positional — and it fails on the
# discriminator itself: session C's opening measurement reads a factor of 1.52, an
# artefact of the reading dithering across a band edge as the heater engaged, and that
# is larger than the seasonal offset it is meant to distinguish. Magnitude cannot
# separate "the spa is genuinely slower today" from "we measured across a band edge".
#
# Tilting the bands does help — 2 °C tilted beats 2 °C flat everywhere — because a
# short measurement in a narrow band with a sloped expectation is comparable to what
# the model predicts *there*, rather than to a bucket average it sits at one end of.
# It is not enough on its own.
#
# ── What did work: size the measurement, don't gate it ──────────────────────
#
# All six above keep a fixed one-degree settle and argue about when to trust it. The
# answer was to stop fixing it. Measure a third of whatever distance is left:
#
#     settle = max(1.0, (target - anchor) / 3)
#
# and the gate becomes unnecessary, because what remains after the measurement is
# always exactly twice what was measured, from any starting temperature. A 22 °C start
# measures 5.5 °C before it commits; a 38 °C top-up measures one crossing. Same rule,
# and it is the one the amplification gate was groping towards — proportionate evidence
# — expressed as a length instead of a veto.
#
# With a 2 °C session-start warm-up (the opening crossings time band position and the
# heater coming up, not heating — 2026-08-12 implied 7.9 °C/h over its first degree and
# still finished within a minute of plan):
#
#                                ¼ in    half    ¾ in    end   first revision
#   flat 1.0 + gate 4              10      10       4      2       82% in
#   remaining/3, warm-up 2 °C      10      30      14      2       43% in
#     ...stored rates +30%        142      30      14      2       43% in
#     ...stored rates -25%        227      30      14      2       43% in
#
# Read the second column down: 30, 30, 30. Not similar — identical, per session, to the
# minute. Once the first measurement lands, whatever the stored buckets got wrong has
# been measured away, so a curve learned in July no longer drags a January heat-up. The
# gated version leaks that error to the display for the whole session: 142 and 227.
#
# The cost is 20 minutes at the halfway mark in the case where the stored rates were
# already right, bought with 112 and 197 in the cases where they were not. The quarter
# column is unchanged because nothing has been measured yet — the display is simply the
# opening plan, which is the honest thing to show before there is evidence.
#
# Two things that did not survive the change: extra band boundaries (34, and 26) now
# make it worse rather than being inert — 30 minutes at halfway becomes 61 and 112 —
# because every boundary re-anchors and shortens the run this design exists to lengthen.
# And a floor on the settle above 1 °C hurts everywhere; the floor is only there to stop
# the last band asking for less than a single crossing.
#
#
# ── The dead zone, and why boundaries stopped interrupting a measurement ─────
#
# Sizing the measurement collided with re-anchoring at band boundaries. A 24 °C start
# anchors at 26 and needs 4.5 °C, but the 30 °C boundary arrives after 3.5: the whole
# cold run was thrown away and the first correction waited until 33.5 °C. Every start
# from 23 to 28 fell in the hole, and session A at 22.0 cleared it by 0.3 °C — which is
# why four recorded sessions never showed it.
#
# The re-anchoring existed only because the observed rate was compared against a single
# band's rate. Compare it against what the curve predicted for the measured span instead
# and a run may cross as many boundaries as it likes. Over the four sessions plus A
# restarted from 23, 24 and 25:
#
#                                ¼ in    half    ¾ in    end   revisions
#   re-anchor at boundaries       180     116      18      2       3.1
#   boundary-free                 180      33      22      1       5.7
#     ...and identical under +30% and -25% stored rates, which the other is not
#
# The dead-zone sessions carry that difference: 243 minutes wrong at halfway becomes 37.
# Four variants of the measurement window were tried — re-anchor after each revision,
# keep a cumulative window from the warm-up anchor, and each of those stopping once
# under 2 °C from target. They land within a minute or two of each other, so the
# simplest wins: re-anchor, and keep going to the end.
#
# The winter and slow figures are synthetic. Revisit with a real cold-weather session.
