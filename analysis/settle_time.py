"""Does holding the ETA for a settle period, then adapting, beat what ships?

Simulated against the two complete heat-ups in the recorder exports, before changing
any code. Strategies:

    hold          the opening estimate, never revised
    ratio(g)      hold until guard `g` is met, then scale the remaining plan by
                  observed progress *measured from the settle point*, so whatever
                  happened in the opening minutes is excluded from both the
                  numerator and the denominator
    shipped       what the integration actually displayed (session B only, read
                  from the log)

The opening plan is reconstructed exactly: bucket rates and the ambient factor are
recorded in the PREDICTION_START line, and integrating them reproduces the logged raw
estimate to within 2 minutes on both sessions, which is the check that the plan curve
below is the one the integration really used.

Run:  python analysis/settle_time.py
"""
from __future__ import annotations

import csv
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).parent.parent

# Bucket boundaries and the ambient sensitivity per bucket, from predictor.py
T1, T2 = 30.0, 37.0
AMB_SENS = (0.0, 0.02, 0.06)


class Session:
    """One heat-up, with the plan curve the integration used at the time."""

    def __init__(self, name, buckets, ambient, baseline, start_temp, target,
                 logged_raw, logged_actual, started_at):
        self.name = name
        # The clock must start at PREDICTION_START, not at the first reading of the
        # starting temperature.  On session A the water had already been sitting at
        # 22.0 for 55 min when the heater engaged, and taking the earlier timestamp
        # made "actual" 1049 min against the 994 the integration recorded — which
        # inflated every strategy's error and made holding look far worse than it is.
        self.started_at = datetime.fromisoformat(started_at)
        self.start_temp = start_temp
        self.target = target
        self.logged_raw = logged_raw
        self.logged_actual = logged_actual
        # Effective rate per bucket: learned rate scaled by the ambient correction
        self.rates = tuple(
            b * (1.0 + AMB_SENS[i] * (ambient - baseline))
            for i, b in enumerate(buckets)
        )
        self.crossings: list[tuple[datetime, float]] = []

    def plan_minutes(self, frm: float, to: float) -> float:
        """The plan's time from `frm` to `to`, integrating the piecewise rates."""
        total = 0.0
        for lo, hi, rate in ((-999, T1, self.rates[0]),
                             (T1, T2, self.rates[1]),
                             (T2, 999, self.rates[2])):
            span = max(0.0, min(to, hi) - max(frm, lo))
            if span:
                total += span / rate * 60.0
        return total

    @property
    def start_time(self):
        return self.started_at

    @property
    def finish_time(self):
        """When the target was first reached — the truth every strategy is scored on."""
        for t, v in self.crossings:
            if v >= self.target:
                return t
        return self.crossings[-1][0]


def load_crossings() -> list[tuple[datetime, float]]:
    """Distinct reported temperatures from the recorder exports, in time order."""
    rows = set()
    # Every history export in the repo — they overlap, and duplicates collapse below.
    # Both places: the exports used to sit at the repo root and now live in
    # historical-data/, which is gitignored because the recordings are private. Globbing
    # only the old location returned nothing at all and every analysis quietly reported
    # on an empty series.
    paths = sorted(ROOT.glob("history*.csv")) + sorted(
        (ROOT / "historical-data").glob("history*.csv"))
    for path in paths:
        for row in csv.DictReader(path.open()):
            # One sensor only. Exports from 2026-08-26 onward carry the floating
            # analyser's temperature alongside the spa's own, and reading both interleaves
            # a 0.1 °C series with a 0.5 °C one. The mixture is not monotonic — the
            # analyser lags by up to an hour during a heat-up, so it reports *below* the
            # spa — and rising_runs ends a run at the first fall, which silently truncated
            # the 25 August session at 32 °C and made it look as though the recording
            # stopped there.
            #
            # Matched by pattern rather than by id: the spa's entity has been renamed at
            # least once across these exports.
            entity = row.get("entity_id", "")
            if "mspa" not in entity or "water_temperature" not in entity:
                continue
            try:
                value = float(row["state"])
            except ValueError:
                continue                      # 'unavailable' gaps
            rows.add((datetime.fromisoformat(
                row["last_changed"].replace("Z", "+00:00")), value))
    series, out = sorted(rows), []
    for t, v in series:
        if not out or out[-1][1] != v:        # collapse repeated reports
            out.append((t, v))
    return out


def rising_runs(series, min_span=3.0, max_gap_h=3.0):
    """Monotonically rising stretches of at least `min_span` °C."""
    runs, cur = [], [series[0]]
    for prev, nxt in zip(series, series[1:]):
        gap = (nxt[0] - prev[0]).total_seconds() / 3600
        if nxt[1] > prev[1] and gap < max_gap_h:
            cur.append(nxt)
        else:
            if cur[-1][1] - cur[0][1] >= min_span:
                runs.append(cur)
            cur = [nxt]
    if cur[-1][1] - cur[0][1] >= min_span:
        runs.append(cur)
    return runs


# ───────────────────────────── the strategies ────────────────────────────────

def strategy_hold(s: Session):
    """eta = start + the opening estimate. Never revised."""
    eta = s.start_time + timedelta(minutes=s.logged_raw)
    return [(t, eta) for t, _ in s.crossings]


def strategy_ratio(s: Session, guard_minutes=0.0, guard_degrees=0.0):
    """Hold until the guard is met, then scale the remaining plan by observed progress.

    The ratio is anchored at the settle crossing, not at the session start.  Anchoring
    at the start makes the opening minutes part of the measurement, and on session B
    those minutes contain a degree of movement no heater could produce — which poisoned
    an earlier attempt at this and produced errors of hours.
    """
    opening = s.start_time + timedelta(minutes=s.logged_raw)
    settle = None
    out = []
    for t, temp in s.crossings:
        elapsed = (t - s.start_time).total_seconds() / 60.0
        if settle is None:
            met = elapsed >= guard_minutes and (temp - s.start_temp) >= guard_degrees
            if met:
                settle = (t, temp)
            out.append((t, opening))
            continue
        st, stemp = settle
        plan_since = s.plan_minutes(stemp, temp)
        since = (t - st).total_seconds() / 60.0
        if plan_since <= 0:
            out.append((t, opening))
            continue
        ratio = since / plan_since
        remaining = s.plan_minutes(temp, s.target) * ratio
        out.append((t, t + timedelta(minutes=remaining)))
    return out


def strategy_replan(s: Session, every_minutes=0.0):
    """Re-run the *frozen* plan from wherever the water actually is.

    eta = time of the latest crossing + the plan's remaining time from that
    temperature.  Rates stay as they were at session start, so this is not the shipped
    behaviour — that mutates the rates as it goes.  It is anchored on a crossing rather
    than on "now" because between crossings the temperature is unchanged, so recomputing
    from now would push the estimate out at one minute per minute.

    `every_minutes` throttles it: a replan only lands if that long has passed since the
    last one, which is the "replan hourly" idea.  It changes how often the estimate
    moves, not how wrong it is — the error depends on where you recompute from, not how
    often.
    """
    out, last_at, held = [], None, s.start_time + timedelta(minutes=s.logged_raw)
    for t, temp in s.crossings:
        due = last_at is None or (t - last_at).total_seconds() / 60.0 >= every_minutes
        if due and temp > s.start_temp:
            held = t + timedelta(minutes=s.plan_minutes(temp, s.target))
            last_at = t
        out.append((t, held))
    return out


def fitted_plan(s: Session):
    """Least-squares Newton curve through this session's own crossings.

    rate = a + b·T, which is what dT/dt = (T∞ − T)/τ reduces to.  Fitted *in sample*,
    so this is the ceiling rather than a usable predictor — the question it answers is
    whether a correct rate curve would make replanning viable at all, or whether even a
    perfect curve leaves the residual too large.
    """
    # Reject physically impossible rates, the same bound the integration's own rate
    # learning applies.  Without this the opening interval enters the fit at 11.5 °C/h
    # — band position, not heating — and one outlier at the cold end tips the slope so
    # far that the extrapolated rate approaches zero near the target and the integral
    # explodes to thousands of minutes.  Third time the opening minutes have had to be
    # excluded explicitly; they are not a special case, they are the norm.
    MAX_PLAUSIBLE = 2.0
    pts = []
    for (t0, v0), (t1, v1) in zip(s.crossings, s.crossings[1:]):
        hours = (t1 - t0).total_seconds() / 3600.0
        if hours <= 0 or v1 <= v0:
            continue
        rate = (v1 - v0) / hours
        if rate <= MAX_PLAUSIBLE:
            pts.append(((v0 + v1) / 2.0, rate))
    n = len(pts)
    sx = sum(x for x, _ in pts); sy = sum(y for _, y in pts)
    sxx = sum(x * x for x, _ in pts); sxy = sum(x * y for x, y in pts)
    den = n * sxx - sx * sx
    b = (n * sxy - sx * sy) / den
    a = (sy - b * sx) / n

    def minutes(frm, to, steps=200):
        """Integrate dt = dT / rate(T) over the span."""
        total, dT = 0.0, (to - frm) / steps
        for i in range(steps):
            T = frm + dT * (i + 0.5)
            r = max(a + b * T, 0.05)
            total += dT / r * 60.0
        return total
    return minutes


def strategy_replan_fitted(s: Session, every_minutes=60.0):
    """Your replan idea, but on a fitted curve instead of the buckets."""
    plan = fitted_plan(s)
    out, last_at = [], None
    held = s.start_time + timedelta(minutes=plan(s.start_temp, s.target))
    for t, temp in s.crossings:
        due = last_at is None or (t - last_at).total_seconds() / 60.0 >= every_minutes
        if due and temp > s.start_temp:
            held = t + timedelta(minutes=plan(temp, s.target))
            last_at = t
        out.append((t, held))
    return out


def strategy_replan_buckets(s: Session):
    """Replan only when the water crosses a bucket boundary (30 or 37 °C).

    The owner's proposal, and it has a real rationale: within a bucket the plan uses
    one constant rate, so a partial-bucket span is where the model is wrong. At a
    boundary the remaining journey is a whole number of buckets, which is the shape
    the model is actually good at — so ask it only there, and hold in between.
    """
    out, held = [], s.start_time + timedelta(minutes=s.logged_raw)
    seen = set()
    for t, temp in s.crossings:
        for edge in (T1, T2):
            if edge in seen or temp < edge - 1e-9 or temp <= s.start_temp:
                continue
            seen.add(edge)
            held = t + timedelta(minutes=s.plan_minutes(temp, s.target))
        out.append((t, held))
    return out


def strategy_settle_then_replan(s: Session, guard_minutes=90.0, guard_degrees=1.5):
    """Hold through the settle period, then replan at every 0.5 °C crossing.

    The synthesis of the two halves that each work in a different part of the session.
    Holding is good early, where the plan is usually right and a partial-span recompute
    is badly wrong; replanning is good late, where the remaining span — and therefore
    the error it can carry — shrinks toward zero. Switching between them at the settle
    point takes each where it wins.
    """
    out, settled = [], False
    held = s.start_time + timedelta(minutes=s.logged_raw)
    for t, temp in s.crossings:
        elapsed = (t - s.start_time).total_seconds() / 60.0
        if not settled and elapsed >= guard_minutes                 and (temp - s.start_temp) >= guard_degrees:
            settled = True
        if settled:
            held = t + timedelta(minutes=s.plan_minutes(temp, s.target))
        out.append((t, held))
    return out


def score(s: Session, series):
    """Mean and worst |error| against the true finish, in minutes."""
    fin = s.finish_time
    errs = [abs((eta - fin).total_seconds() / 60.0) for _, eta in series]
    return sum(errs) / len(errs), max(errs)


def score_endgame(s: Session, series, within_minutes=90.0):
    """Mean |error| over the closing stretch, and the error at the final reading.

    The whole-session mean treats a wrong answer six hours out as equally bad as a
    wrong answer twenty minutes out. They are not equally bad: what a user acts on is
    the estimate as the session nears its end. Replanning must converge there — the
    remaining span shrinks toward zero, so the error it can carry shrinks with it —
    while holding keeps whatever error it started with all the way to the finish.
    """
    fin = s.finish_time
    tail = [(t, eta) for t, eta in series
            if (fin - t).total_seconds() / 60.0 <= within_minutes]
    if not tail:
        tail = series[-1:]
    errs = [abs((eta - fin).total_seconds() / 60.0) for _, eta in tail]
    last = abs((series[-1][1] - fin).total_seconds() / 60.0)
    return sum(errs) / len(errs), last


# ─────────────────────────────────── report ──────────────────────────────────

# What the integration actually displayed on session C, read from the log.
SHIPPED_C = (21.4, 43.8)


def main() -> None:
    series = load_crossings()
    runs = rising_runs(series)
    print("=" * 78)
    print("SETTLE TIME BEFORE ADAPTING — SIMULATED ON RECORDED SESSIONS")
    print("=" * 78)
    print(f"{len(series)} readings in the exports, {len(runs)} heat-ups of 3 °C or more\n")

    sessions = [
        Session("A  06 Aug  22.0 → 39.5", (1.11, 1.03, 1.01), 13.7, 14.011,
                22.0, 39.5, 992.1, 994.4, "2026-08-06T19:26:24+00:00"),
        Session("B  10 Aug  29.0 → 39.5", (1.03, 0.99, 0.75), 14.1, 14.398,
                29.0, 39.5, 690.5, 688.4, "2026-08-10T13:03:21+00:00"),
        Session("C  12 Aug  31.0 → 39.5", (1.11, 0.93, 0.77), 17.5, 13.114,
                31.0, 39.5, 512.2, 511.9, "2026-08-12T14:57:18+00:00"),
        Session("D  14 Aug  33.0 → 39.5", (1.10, 0.99, 0.79), 15.2, 14.793,
                33.0, 39.5, 425.7, 391.1, "2026-08-14T03:46:08+00:00"),
    ]
    for sess, run in zip(sessions, runs):
        later = [(t, v) for t, v in run
                 if t >= sess.started_at and v <= sess.target + 1e-9]
        sess.crossings = [(sess.started_at, sess.start_temp)] + later

    for sess in sessions:
        integrated = sess.plan_minutes(sess.start_temp, sess.target)
        actual = (sess.finish_time - sess.start_time).total_seconds() / 60.0
        print(f"── {sess.name} " + "─" * (60 - len(sess.name)))
        print(f"   rates in force {tuple(round(r, 3) for r in sess.rates)}")
        print(f"   plan re-integrated {integrated:.0f} min vs logged raw "
              f"{sess.logged_raw:.0f} min   (check)")
        print(f"   actual {actual:.0f} min over {len(sess.crossings)} crossings\n")

        rows = [("hold the opening estimate", strategy_hold(sess))]
        rows.append(("replan every crossing, frozen rates",
                     strategy_replan(sess, 0)))
        rows.append(("replan hourly, frozen rates", strategy_replan(sess, 60)))
        rows.append(("replan every 90 min, frozen rates", strategy_replan(sess, 90)))
        rows.append(("settle 90/1.5 then replan each crossing",
                     strategy_settle_then_replan(sess, 90, 1.5)))
        rows.append(("settle 60/1.0 then replan each crossing",
                     strategy_settle_then_replan(sess, 60, 1.0)))
        rows.append(("replan at bucket boundaries only",
                     strategy_replan_buckets(sess)))
        rows.append(("replan hourly, FITTED curve (ceiling)",
                     strategy_replan_fitted(sess, 60)))
        for gm, gd, label in ((60, 0, "ratio, settle 60 min"),
                              (90, 0, "ratio, settle 90 min"),
                              (0, 1.0, "ratio, settle 1.0 °C"),
                              (0, 1.5, "ratio, settle 1.5 °C"),
                              (60, 1.0, "ratio, settle 60 min AND 1.0 °C"),
                              (90, 1.5, "ratio, settle 90 min AND 1.5 °C"),
                              (0, 0, "ratio, no settle at all")):
            rows.append((label, strategy_ratio(sess, gm, gd)))

        print(f"   {'strategy':<34}{'mean |err|':>12}{'worst':>9}")
        for label, ser in rows:
            mean, worst = score(sess, ser)
            print(f"   {label:<34}{mean:>10.1f} m{worst:>8.1f} m")
        if sess.name.startswith("C"):
            print(f"   {'what shipped (measured)':<34}"
                  f"{SHIPPED_C[0]:>10.1f} m{SHIPPED_C[1]:>8.1f} m")
        print()

    print("=" * 78)
    print("ACROSS ALL COMPLETE SESSIONS")
    print("=" * 78)
    labels = ["hold the opening estimate",
              "settle 90/1.5 then replan each crossing",
              "settle 60/1.0 then replan each crossing",
              "replan hourly, frozen rates",
              "replan at bucket boundaries only",
              "replan every crossing, frozen rates",
              "replan hourly, FITTED curve (ceiling)", "ratio, settle 90 min AND 1.5 °C",
              "ratio, no settle at all"]
    makers = [lambda x: strategy_hold(x),
              lambda x: strategy_settle_then_replan(x, 90, 1.5),
              lambda x: strategy_settle_then_replan(x, 60, 1.0),
              lambda x: strategy_replan(x, 60),
              lambda x: strategy_replan_buckets(x),
              lambda x: strategy_replan(x, 0),
              lambda x: strategy_replan_fitted(x, 60),
              lambda x: strategy_ratio(x, 90, 1.5),
              lambda x: strategy_ratio(x, 0, 0)]
    print(f"{'strategy':<34}{'whole session':>15}{'last 90 min':>13}"
          f"{'at the finish':>15}")
    for label, make in zip(labels, makers):
        sc = [score(x, make(x)) for x in sessions]
        eg = [score_endgame(x, make(x)) for x in sessions]
        print(f"{label:<34}{sum(m for m, _ in sc)/len(sc):>13.1f} m"
              f"{sum(m for m, _ in eg)/len(eg):>11.1f} m"
              f"{sum(l for _, l in eg)/len(eg):>13.1f} m")
    print()
    print("Scored at every crossing, against the moment the target was first reached.")
    print("A strategy that never revises has one error repeated, so its mean and worst")
    print("coincide — that is not a bug in the scoring, it is what 'never revises' means.")


if __name__ == "__main__":
    main()
