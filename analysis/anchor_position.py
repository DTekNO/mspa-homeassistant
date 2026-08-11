"""Is it worth extrapolating the cooling trajectory to place the water within its band?

Reported water temperature is quantised to 0.5 °C, so a reading only locates the true
temperature to within a band.  At a *crossing* the position is known exactly — the true
temperature is the threshold between the two reported values — but between crossings it
drifts, unobserved.  The integration anchors at the band centre on every crossing and
then holds that value until the next one (coordinator._TEMP_BAND_C).

The open proposal is to do better at a heating start: take the last cooling crossing,
extrapolate forward at the learned cool rate, and start from there instead.  This asks
whether that is worth implementing.

    1. THE COOLING LAW      how fast does the water actually drift, and does one
                            stored scalar cool rate describe it?
    2. CROSS-CHECK          the same law at crossing resolution rather than hourly
    3. SCORING              a heating start makes a falsifiable prediction: if the
                            water sits depth `d` below the upper threshold, the first
                            band crossing must arrive after d / heat_rate.  So the
                            observed interval measures `d`, and each candidate anchor
                            rule can be scored against it:
                                verbatim     the reported reading (before the fix)
                                band centre  the threshold, held  (what ships today)
                                trajectory   threshold - cool_rate x dwell (proposed)
    4. MAGNITUDE            where the correction would be worth having

Data (all git-ignored, private — do not commit):
    mspa_lts_export.csv            hourly long-term statistics: water, heat_state, air
    history-full-heat-period.csv   recorder export, exact crossing timestamps (UTC)
    home-assistant_*.log           heating starts and phase-uncertain crossings

Only the `oslouvc` statistics are used.  The `frame` series is the previous spa, a
different tub with its own insulation and surface area, so neither its heating nor its
cooling characteristics transfer.

Run:  python analysis/anchor_position.py
"""
from __future__ import annotations

import csv
import math
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent

BAND = 0.5                       # reported quantisation step, °C
LOG_TZ = timedelta(hours=2)      # logs are local (CEST); recorder and LTS are UTC
IDLE_STATE = 4.0                 # heat_state while not heating (3 is full heat)
MIN_RUN_HOURS = 6                # shortest idle stretch worth fitting a slope to
MIN_RUN_DROP = 0.5               # and it must actually fall by a band

WATER = "sensor.mspa_oslouvc_bobbyspa_water_temperature"
HEAT = "sensor.mspa_oslouvc_bobbyspa_heat_state"
AIR = "sensor.nes25_outdoor_temperature"

# heat_rate_buckets from .storage/mspa_rates, cold / mid / hot
BUCKET_RATES = (1.105, 0.927, 0.766)
STORED_COOL_RATE = 0.363         # the single scalar the integration currently keeps


# ─────────────────────────────── data loading ────────────────────────────────

def load_lts():
    """Hourly means keyed by UTC hour: water, heat_state, outdoor air."""
    water, heat, air = {}, {}, {}
    for row in csv.DictReader((ROOT / "mspa_lts_export.csv").open()):
        t = datetime.fromisoformat(row["hour"]).replace(tzinfo=timezone.utc)
        mean = float(row["mean"])
        if row["statistic_id"] == WATER:
            water[t] = mean
        elif row["statistic_id"] == HEAT:
            heat[t] = mean
        elif row["statistic_id"] == AIR:
            air[t] = mean
    return water, heat, air


def water_crossings():
    """Exact reported-temperature changes from the recorder export, UTC."""
    out = []
    for row in csv.DictReader((ROOT / "history-full-heat-period.csv").open()):
        try:
            temp = float(row["state"])
        except ValueError:
            continue                                     # 'unavailable' gaps
        t = datetime.fromisoformat(row["last_changed"].replace("Z", "+00:00"))
        if out and out[-1][1] == temp:
            continue                                     # re-report after a gap
        out.append((t, temp))
    return out


def heating_starts():
    """Heating starts paired with their phase-uncertain first crossing, from the logs.

    The pairing has to come from the logs: only they record when full heat actually
    engaged.  An interval measured from anything inferable from the temperature series
    alone — a level's first sighting, say — spans the whole preceding cool-down and
    measures dwell rather than starting position.
    """
    stamp = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)")
    ansi = re.compile(r"\x1b\[[0-9;]*m")
    start = re.compile(r"PREDICTION_START: ([\d.]+)°C")
    buckets = re.compile(r"buckets=([\d.]+)/([\d.]+)")
    warm = re.compile(r"Heat rate: first crossing ([\d.]+)→([\d.]+)°C after heater-on")
    cool = re.compile(r"Cool rate: first crossing ([\d.]+)→([\d.]+)°C after heater-off")

    events, pending, last_cool = [], None, None
    for log in sorted(ROOT.glob("home-assistant_*.log")):
        for raw in log.read_text(encoding="utf-8", errors="replace").splitlines():
            if "custom_components.mspa" not in raw:
                continue
            line = ansi.sub("", raw)
            m = stamp.match(line)
            if not m:
                continue
            t = (datetime.fromisoformat(m.group(1))
                 .replace(tzinfo=timezone.utc) - LOG_TZ)

            mc = cool.search(line)
            if mc:
                last_cool = (t, float(mc.group(1)), float(mc.group(2)))

            ms = start.search(line)
            if ms:
                mb = buckets.search(line)
                pending = {"start": t, "temp": float(ms.group(1)),
                           "cold_rate": float(mb.group(1)) if mb else None,
                           "cool_cross": last_cool}

            mw = warm.search(line)
            if mw and pending is not None:
                gap = (t - pending["start"]).total_seconds() / 60.0
                # A first crossing cannot lag heating by more than a band or two.  A
                # longer gap means the start reference is stale — typically the guard
                # re-arming after a restart on a session already running.
                if 0 <= gap <= 90:
                    events.append({**pending, "interval_min": gap,
                                   "from": float(mw.group(1)),
                                   "to": float(mw.group(2))})
                pending = None
    return events


# ───────────────────────────── the cooling law ───────────────────────────────

def cooling_segments(water, heat, air):
    """Least-squares cooling rate over each idle stretch, paired with its air gap."""
    idle = sorted(t for t in water if t in air and heat.get(t) == IDLE_STATE)
    runs, cur = [], []
    for t in idle:
        if cur and t - cur[-1] == timedelta(hours=1):
            cur.append(t)
        else:
            if len(cur) >= MIN_RUN_HOURS:
                runs.append(cur)
            cur = [t]
    if len(cur) >= MIN_RUN_HOURS:
        runs.append(cur)

    segs = []
    for run in runs:
        ys = [water[t] for t in run]
        if ys[0] - ys[-1] < MIN_RUN_DROP:
            continue
        xs = [(t - run[0]).total_seconds() / 3600.0 for t in run]
        mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
        den = sum((x - mx) ** 2 for x in xs)
        if not den:
            continue
        slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den
        if slope >= 0:
            continue
        segs.append({"t": run[0], "hours": xs[-1], "rate": -slope,
                     "gap": sum(water[t] - air[t] for t in run) / len(run)})
    return segs


def fit_linear(segs):
    """Newton's law: rate = gap / tau.  Least squares in rate, through the origin."""
    return (sum(s["gap"] ** 2 for s in segs)
            / sum(s["gap"] * s["rate"] for s in segs))


def fit_power(segs):
    """rate = alpha * gap**beta, via log-log least squares."""
    xs = [math.log(s["gap"]) for s in segs]
    ys = [math.log(s["rate"]) for s in segs]
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    beta = (sum((x - mx) * (y - my) for x, y in zip(xs, ys))
            / sum((x - mx) ** 2 for x in xs))
    return math.exp(my - beta * mx), beta


def rss(segs, f):
    return sum((s["rate"] - f(s["gap"])) ** 2 for s in segs)


# ─────────────────────────────────── report ──────────────────────────────────

def main() -> None:
    water, heat, air = load_lts()
    segs = cooling_segments(water, heat, air)
    tau = fit_linear(segs)
    alpha, beta = fit_power(segs)
    observed_max_gap = max(s["gap"] for s in segs)

    def linear(g):
        return g / tau

    def power(g):
        return alpha * g ** beta

    print("=" * 78)
    print("1. THE COOLING LAW")
    print("=" * 78)
    print(f"{len(segs)} idle stretches of {MIN_RUN_HOURS} h or more, "
          f"{min(s['gap'] for s in segs):.0f}-{observed_max_gap:.0f} °C water-air gap")
    print(f"\n{'gap band':>10}{'n':>5}{'mean gap':>10}{'mean rate':>11}"
          f"{'implied tau':>13}")
    for lo, hi in ((0, 10), (10, 15), (15, 20), (20, 25), (25, 40)):
        sub = [s for s in segs if lo <= s["gap"] < hi]
        if not sub:
            continue
        g = sum(s["gap"] for s in sub) / len(sub)
        r = sum(s["rate"] for s in sub) / len(sub)
        print(f"{lo:>6}-{hi:<3}{len(sub):>5}{g:>10.1f}{r:>11.3f}{g / r:>11.0f} h")

    print(f"\nA single stored scalar cannot describe this. The integration keeps")
    print(f"cool_rate = {STORED_COOL_RATE:.3f} °C/h, which matches the water only at a gap of")
    print(f"about {STORED_COOL_RATE * tau:.0f} °C — it was learned from hot water. Near ambient")
    print(f"(gap 8 °C, true rate ~{linear(8):.3f} °C/h) it overstates cooling by "
          f"{STORED_COOL_RATE / linear(8):.0f}x;")
    print(f"at a gap of 30 °C it understates it.")

    print(f"\nTwo candidate forms, fitted over the observed range:")
    print(f"   Newton, linear   rate = gap / {tau:.0f} h            RSS "
          f"{rss(segs, linear):.3f}")
    print(f"   power law        rate = {alpha:.5f} x gap^{beta:.2f}   RSS "
          f"{rss(segs, power):.3f}")
    print("The power law fits marginally better — cooling accelerates faster than")
    print("linearly, which is what evaporation does, being driven by vapour pressure")
    print("rather than by temperature difference. Over the observed range the two")
    print("agree closely; extrapolated to winter they do not:")
    print(f"\n{'gap':>6}{'linear':>9}{'power':>9}   ")
    for g in (8, 20, 30, 38, 48, 60):
        mark = "" if g <= observed_max_gap else "   <- extrapolated, no data"
        print(f"{g:>6}{linear(g):>9.3f}{power(g):>9.3f}{mark}")
    print("\nSo the winter cool rate is uncertain by about 60%. Both forms agree it is")
    print("several times the summer figure, which is what matters below.")

    print()
    print("=" * 78)
    print("2. CROSS-CHECK AT CROSSING RESOLUTION")
    print("=" * 78)
    print("The fit above uses hourly means of a quantised signal. Recorder crossings")
    print("give the same law at full resolution over one cool-down. Only spans that run")
    print("boundary to boundary count: the first span in the export starts at the export")
    print("boundary, not a crossing, so its rate is an overestimate and it is excluded.")
    xs = water_crossings()
    print(f"\n{'span':>13}{'hours':>8}{'rate':>9}{'air':>7}{'gap':>7}{'tau':>9}")
    for i, ((t0, v0), (t1, v1)) in enumerate(zip(xs, xs[1:])):
        if v1 >= v0:
            continue
        hours = (t1 - t0).total_seconds() / 3600.0
        mid = t0 + (t1 - t0) / 2
        a = air.get(mid.replace(minute=0, second=0, microsecond=0))
        if a is None or hours <= 0:
            continue
        rate = (v0 - v1) / hours
        gap = (v0 + v1) / 2.0 - a
        if i == 0:
            print(f"{v0:>6.1f}→{v1:<6.1f}{hours:>8.2f}{rate:>9.3f}{a:>7.1f}"
                  f"{gap:>7.1f}{gap / rate:>7.0f} h   excluded, export boundary")
        else:
            print(f"{v0:>6.1f}→{v1:<6.1f}{hours:>8.2f}{rate:>9.3f}{a:>7.1f}"
                  f"{gap:>7.1f}{gap / rate:>7.0f} h")
    print(f"\nConsistent with the {tau:.0f} h fitted from 14 weeks of hourly statistics.")

    print()
    print("=" * 78)
    print("3. SCORING THE ANCHOR RULES ON RECORDED HEATING STARTS")
    print("=" * 78)
    events = [e for e in heating_starts() if e["cool_cross"] and e["cold_rate"]]
    print(f"heating starts preceded by a cooling crossing: {len(events)}\n")

    for e in events:
        ct, cfrom, cto = e["cool_cross"]
        dwell = (e["start"] - ct).total_seconds() / 60.0
        if dwell < 0:
            continue
        heat_rate = e["cold_rate"]
        threshold = (cfrom + cto) / 2.0        # true temperature at that crossing
        a = air.get(e["start"].replace(minute=0, second=0, microsecond=0))
        gap = (threshold - a) if a is not None else None

        rules = {"verbatim": threshold - e["temp"], "band centre": 0.0}
        if gap is not None:
            rules["trajectory"] = min(linear(gap) * dwell / 60.0, BAND)

        print(f"{e['start']:%d %b %Y %H:%M} UTC   reading {e['temp']:.1f} °C, "
              f"crossing into {e['from']:.1f}→{e['to']:.1f} °C")
        print(f"  cooled into this band at {ct:%H:%M}, then sat "
              f"{dwell:.0f} min before heating began")
        if gap is not None:
            print(f"  air {a:.1f} °C, gap {gap:.1f} °C, cool rate "
                  f"{linear(gap):.3f} °C/h, heat rate {heat_rate:.2f} °C/h")
        else:
            print("  air unknown (statistics end 07 Aug) — trajectory not scorable")
        print(f"  first crossing came {e['interval_min']:.1f} min after heating began, "
              f"so the water was only {e['interval_min'] / 60.0 * heat_rate:.3f} °C "
              f"below the threshold")
        print(f"  {'rule':<14}{'assumed depth':>15}{'predicts':>11}{'error':>10}")
        for name, d in rules.items():
            pred = d / heat_rate * 60.0
            print(f"  {name:<14}{d:>13.3f} °C{pred:>9.1f} m"
                  f"{pred - e['interval_min']:>+9.1f} m")
        print()

    print("Both observed intervals are exactly one poll, so they are upper bounds: the")
    print("water was already at its threshold when the heater engaged, and may have")
    print("been past it. Two readings of that are worth stating carefully.")
    print()
    print("  Against the old verbatim reading, the fix is unambiguous — it was wrong by")
    print("  12-14 min at every session start, in the direction that made Ready at")
    print("  disagree with the scheduler.")
    print()
    print("  Between band centre and trajectory, the evidence runs the wrong way for")
    print("  the proposal. Trajectory expects the water to have drifted down during the")
    print("  55 min it sat idle, and predicts a crossing 5.4 min out; it came in 1.0.")
    print("  Band centre, which ignores the drift entirely, is closer.")
    print()
    print("  A competing explanation fits that better than band position does. While")
    print("  the water is still, the sensor reads its own stratum; when circulation")
    print("  starts it reads the mixed bulk, which is warmer. The step would then be")
    print("  mixing, not heating, and no cooling model would predict it. The following")
    print("  band supports this: 22.5→23.0 took 22.6 min against a 25-28 min run")
    print("  average, so the heat revealed by mixing was real and already in the tub.")

    print()
    print("=" * 78)
    print("4. WHERE THE CORRECTION WOULD BE WORTH HAVING")
    print("=" * 78)
    print("Drift beyond band-centre anchoring, as minutes of ETA. Capped at one band,")
    print(f"priced at the hot-bucket rate {BUCKET_RATES[2]:.3f} °C/h, cooling linear.\n")
    dwells = (15, 30, 60, 120, 240)
    print(f"{'water':>6}{'air':>6}{'gap':>6}{'cool':>8}"
          + "".join(f"{d:>7}m" for d in dwells))
    for w_, a_ in ((22, 14), (30, 10), (34, 5), (38, 0), (38, -10), (40, -20)):
        g = w_ - a_
        cells = "".join(
            f"{min(linear(g) * d / 60.0, BAND) / BUCKET_RATES[2] * 60.0:>7.0f}m"
            for d in dwells)
        mark = "" if g <= observed_max_gap else "   extrapolated"
        print(f"{w_:>6}{a_:>6}{g:>6}{linear(g):>8.3f}{cells}{mark}")
    print("\n39 m is the cap — the drift has saturated a whole band, and a crossing")
    print("would have re-anchored anyway. Cells at the cap understate nothing; they")
    print("mark where the anchor has simply gone stale.")

    print()
    print("VERDICT")
    print("  Do not implement it yet. In the regime every recorded session sits in —")
    print("  summer, water near ambient — it is worth 2-8 min at dwells up to an hour,")
    print("  and on the one start where all three rules can be scored it is the worse")
    print("  of the two corrections. Band-centre anchoring already captures what is")
    print("  recoverable there.")
    print()
    print("  It becomes material at large gaps: 20-39 min once the air is near zero")
    print("  with the water hot, which is the same order as the disagreement the anchor")
    print("  fix just removed. Cancelling a heat-up and restarting it reaches that")
    print("  regime in any season, so this is not only a winter question.")
    print()
    print("  Two things must be settled first, and both are measurable rather than")
    print("  arguable:")
    print()
    print("    Whether the step at heater-on is thermal at all. If it is stratification")
    print("    clearing, the correct response is to re-anchor once circulation settles,")
    print("    which is nearly what the phase-uncertainty guard already does — and a")
    print("    cooling extrapolation would be fitting a curve to a mixing artefact.")
    print("    The soft start makes this testable: the pump now runs before the heater,")
    print("    so a mixing step should appear at pump-on, before any heat is applied.")
    print()
    print("    Whether the law extrapolates. Nothing here observes a gap beyond")
    print(f"    {observed_max_gap:.0f} °C, and the linear and power fits diverge by ~60% "
          f"by gap 48.")
    print("    Winter data settles both the form and the constant.")
    print()
    print("  If it is implemented, it must not use the stored cool_rate scalar. That is")
    print("  the part that would actively mislead — a single number cannot be right at")
    print("  more than one gap, and near ambient it is 4x too fast. It needs")
    print("  cool_rate(water - air), so the correction depends on the weather entity")
    print("  and must degrade to band-centre anchoring when there isn't one.")


if __name__ == "__main__":
    main()
