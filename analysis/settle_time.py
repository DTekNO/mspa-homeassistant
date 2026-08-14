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
    for name in ("history.csv", "history (1).csv", "history-full-heat-period.csv"):
        path = ROOT / name
        if not path.exists():
            continue
        for row in csv.DictReader(path.open()):
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


def score(s: Session, series):
    """Mean and worst |error| against the true finish, in minutes."""
    fin = s.finish_time
    errs = [abs((eta - fin).total_seconds() / 60.0) for _, eta in series]
    return sum(errs) / len(errs), max(errs)


# ─────────────────────────────────── report ──────────────────────────────────

# What the integration actually displayed on session B, read from the log.
SHIPPED_B = (21.4, 43.8)


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
        Session("B  12 Aug  31.0 → 39.5", (1.11, 0.93, 0.77), 17.5, 13.114,
                31.0, 39.5, 512.2, 511.9, "2026-08-12T14:57:18+00:00"),
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
        if sess.name.startswith("B"):
            print(f"   {'what shipped (measured)':<34}"
                  f"{SHIPPED_B[0]:>10.1f} m{SHIPPED_B[1]:>8.1f} m")
        print()

    print("Scored at every crossing, against the moment the target was first reached.")
    print("A strategy that never revises has one error repeated, so its mean and worst")
    print("coincide — that is not a bug in the scoring, it is what 'never revises' means.")


if __name__ == "__main__":
    main()
