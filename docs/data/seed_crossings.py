#!/usr/bin/env python3
"""Rebuild a crossing log from Home Assistant's recorder history.

The crossing log only starts the day it is switched on, and the recorder purges after
ten days. Everything in between — on 21.10.2026 a 46-hour cooldown, a full 17→39 °C
heat-up, and two days of maintenance-band cycling — exists in the recorder and nowhere
else. This turns it into the same JSON Lines rows the coordinator writes, so the log
starts with history behind it instead of from the next crossing.

Reads JSON dumps of the history API; it does no network and needs no credentials:

    . ~/.ha_env
    S=2026-10-11T00:00:00+00:00 ; E=$(date -u +%Y-%m-%dT%H:%M:%S+00:00)
    get() { curl -sG -H "Authorization: Bearer $HA_TOKEN" \
              "$HA_URL/api/history/period/$S" \
              --data-urlencode "filter_entity_id=$2" \
              --data-urlencode "end_time=$E" \
              --data-urlencode "significant_changes_only=0" \
              $3 -o "$1.json" ; }
    get water   sensor.<spa>_water_temperature --data-urlencode minimal_response=
    get heat    sensor.<spa>_heat_state        --data-urlencode minimal_response=
    get outdoor sensor.<outdoor>               --data-urlencode minimal_response=
    get weather weather.<forecast>

Then

    ./seed_crossings.py --water water.json --heat heat.json \
        --outdoor outdoor.json --weather weather.json \
        [--existing mspa_crossings_<id>.jsonl] -o mspa_crossings_<id>.jsonl

What is faithful and what is not
--------------------------------
The rows carry `"seed": true`, because their conditions are reconstructed rather than
accumulated. The coordinator time-weights `air`, `uv`, `wind`, `gust` and `air_measured`
over every poll of the interval, roughly every thirty seconds. Here the same weighting
is applied to the recorder's step function, which for the forecast means hourly steps.
Over an interval of hours that is the same number; over a short one it is coarser.

Everything else matches the live writer exactly, including the parts that look odd:
`gap` is measured from the middle of the half-degree band, a crossing with the heater on
but the water falling is logged with no air, and `secs`/`rate` are measured from the
previous crossing of the same regime — and only when no regime change intervened, so a
rate never spans a heater cycle in either direction.

A discontinuity — a refill, a long dropout, a jump of more than one band — breaks the
chain rather than inventing a rate across it. The row after one carries no `secs` and
no `rate`.

One guard here has no counterpart in the live writer: a step quicker than any heater or
any cooldown could manage is dropped. The live log records what the sensor said and is
right to; but the drain and refill of 18.10.2026 walked the reading through thirty bands
in ninety minutes, and seeding half a dozen rows at -60 °C/h into a file whose purpose
is to have rates fitted to it is seeding known nonsense. The count is reported, so what
was dropped is never silent.
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone

# The regimes, as the coordinator reads them. 3 is heating; 0, 1 and 4 are all heater-off
# and belong to the cooling recorder; 2 is preheat, which records nothing in either path.
HEAT_FULL = 3
IDLE_STATES = (0, 1, 4)

# A dropout longer than this breaks the chain. Shorter than this the water cannot have
# moved far enough to matter, and the live coordinator would have ridden straight over it.
MAX_DROPOUT_S = 900.0

# More than one band between consecutive readings is not a crossing. It is a refill, a
# sensor reset or the far side of a gap.
BAND_C = 0.5

# Faster than this is not a crossing. The heater manages about 1.5 °C/h at its best and
# the tub loses about 0.35 °C/h at its worst, so anything near this is water being moved
# rather than heated or cooled. Matches _MAX_COOL_RATE in the coordinator.
MAX_PLAUSIBLE_RATE = 3.0

CROSSING_LOG_MAX = 1000


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts).astimezone(timezone.utc)


def _series(path: str, value):
    """[(when, value_or_None)] from a history dump, oldest first.

    `value` extracts the point; returning None marks the sensor as having nothing to
    say, which the weighting then skips rather than treating as a zero.
    """
    with open(path, encoding="utf-8") as fh:
        blocks = json.load(fh)
    out = []
    for block in blocks:
        for point in block:
            when = point.get("last_updated") or point.get("last_changed")
            if not when:
                continue
            out.append((_parse(when), value(point)))
    out.sort(key=lambda row: row[0])
    return out


def _float(raw):
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _state(point):
    return _float(point.get("state"))


def _int_state(point):
    value = _state(point)
    return None if value is None else int(value)


def _wind(attrs, key):
    """A weather wind attribute in m/s, as _read_weather_entity normalises it."""
    value = _float(attrs.get(key))
    if value is None:
        return None
    unit = attrs.get("wind_speed_unit", "m/s")
    if unit in ("km/h", "kph"):
        return value / 3.6
    if unit == "mph":
        return value * 0.44704
    if unit in ("kn", "knot", "knots"):
        return value * 0.514444
    return value


def _weather(path: str):
    """The four weather series the log records, each already in the model's units."""
    def attrs(point):
        return point.get("attributes") or {}

    def temp(point):
        value = _float(attrs(point).get("temperature"))
        if value is None:
            return None
        if attrs(point).get("temperature_unit") in ("°F", "F"):
            return (value - 32.0) * 5.0 / 9.0
        return value

    def uv(point):
        return _float(attrs(point).get("uv_index"))

    def wind(point):
        # The steady wind. `ambient_wind`, which the model reads, is gust-preferred;
        # the log keeps this one, or its two wind columns are the same number twice.
        return _wind(attrs(point), "wind_speed")

    def gust(point):
        return _wind(attrs(point), "wind_gust_speed")

    return (_series(path, temp), _series(path, uv),
            _series(path, wind), _series(path, gust))


def at(series, when: datetime):
    """The value in effect at `when`, or None before the series starts."""
    found = None
    for ts, value in series:
        if ts > when:
            break
        found = value
    return found


def weighted(series, start: datetime, end: datetime):
    """Time-weighted mean over [start, end), skipping stretches with no value.

    This is the step-function equivalent of the coordinator's accumulators, which add
    `value * seconds` on every poll and divide by the seconds that had a value.
    """
    if end <= start:
        return None
    total = secs = 0.0
    current = at(series, start)
    cursor = start
    for ts, value in series:
        if ts <= start:
            continue
        if ts >= end:
            break
        span = (ts - cursor).total_seconds()
        if current is not None and span > 0:
            total += current * span
            secs += span
        cursor, current = ts, value
    span = (end - cursor).total_seconds()
    if current is not None and span > 0:
        total += current * span
        secs += span
    return total / secs if secs > 0 else None


def _round(value, digits=1):
    return None if value is None else round(float(value), digits)


def build(water_path, heat_path, outdoor_path, weather_path):
    water = [(t, v) for t, v in _series(water_path, _state) if v is not None]
    raw_water = _series(water_path, _state)
    heat = _series(heat_path, _int_state)
    outdoor = _series(outdoor_path, _state)
    air, uv, wind, gust = _weather(weather_path)

    # Where the water sensor went away, so a gap can break the chain even though the
    # readings either side are both valid.
    dropouts = [(t, v) for t, v in raw_water if v is None]

    rows = []
    prev_valid = None       # the last water reading of any kind
    prev_at = None
    prev_src = None         # the regime the last reading was in
    prev_cross = {"heat": None, "idle": None}   # (when, water) per regime
    skipped = {"jump": 0, "preheat": 0, "dropout": 0, "implausible": 0}

    for when, value in water:
        if prev_valid is None:
            prev_valid, prev_at = value, when
            continue

        gap_s = (when - prev_at).total_seconds()
        broke = any(prev_at < t < when for t, _ in dropouts) and gap_s > MAX_DROPOUT_S
        step = value - prev_valid

        if broke or abs(abs(step) - BAND_C) > 1e-9:
            # Not one band, or not one stretch. Re-anchor and measure nothing across it.
            skipped["dropout" if broke else "jump"] += 1
            prev_cross = {"heat": None, "idle": None}
            prev_valid, prev_at = value, when
            continue

        state = at(heat, when)
        if state == HEAT_FULL:
            src = "heat"
        elif state in IDLE_STATES:
            src = "idle"
        else:
            # Preheat, or the heater state unknown at that moment. The live code writes
            # nothing in either case, and clears both anchors on the way past.
            skipped["preheat"] += 1
            prev_src = "pre"
            prev_cross = {"heat": None, "idle": None}
            prev_valid, prev_at = value, when
            continue

        if prev_src is not None and src != prev_src:
            # The regime changed since the last reading. Both anchors belong to the
            # other side of it, so neither can measure this crossing: the coordinator
            # drops the nowcast window when the heater stops and clears the cooling
            # anchor when it starts.
            prev_cross = {"heat": None, "idle": None}
        prev_src = src

        prev = prev_cross[src]
        start = prev[0] if prev is not None else None
        secs = round((when - start).total_seconds()) if start is not None else None
        rate = ((value - prev[1]) / (secs / 3600.0)
                if prev is not None and secs else None)

        if rate is not None and abs(rate) > MAX_PLAUSIBLE_RATE:
            # Water being moved, not heated or cooled. Breaks the chain, because the
            # reading on the far side of it is a different body of water.
            skipped["implausible"] += 1
            prev_cross = {"heat": None, "idle": None}
            prev_valid, prev_at = value, when
            continue

        # With no previous crossing of this regime there is no interval to weight, so
        # fall back to the instantaneous reading — exactly what the coordinator does
        # when its accumulators are empty.
        def mean(series, instant_only=False):
            if start is None or instant_only:
                return at(series, when)
            return weighted(series, start, when)

        air_c = mean(air)
        if src == "heat" and step < 0:
            # A fall with the heater on. The live path logs the row and withholds the
            # air, because the window it belonged to has just been thrown away.
            air_c = None

        rows.append({
            "t": when.isoformat(timespec="seconds"),
            "src": src,
            "water": value,
            "secs": secs,
            "air": _round(air_c, 2),
            "air_start": _round(at(air, start) if start is not None else None, 2),
            "air_measured": _round(mean(outdoor), 2),
            "air_end": _round(at(air, when), 2),
            "uv": _round(mean(uv), 2),
            "wind": _round(mean(wind), 2),
            "gust": _round(mean(gust), 2),
            "rate": _round(rate, 4),
            "gap": (_round(value - 0.25 - air_c, 2) if air_c is not None else None),
            "seed": True,
        })
        prev_cross[src] = (when, value)
        prev_valid, prev_at = value, when

    return rows, skipped


def merge(seeded, existing_path):
    """Seeded rows plus whatever the live log already holds, oldest first.

    A row already in the file wins: it was accumulated rather than reconstructed. Keyed
    on the timestamp and regime, which is unique — two crossings of the same regime
    cannot share a second.
    """
    live = []
    if existing_path and os.path.exists(existing_path):
        with open(existing_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        live.append(json.loads(line))
                    except ValueError:
                        pass
    have = {(row.get("t"), row.get("src", "heat")) for row in live}
    kept = [row for row in seeded if (row["t"], row["src"]) not in have]
    out = sorted(kept + live, key=lambda row: row.get("t") or "")
    return out[-CROSSING_LOG_MAX:], len(live), len(seeded) - len(kept)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--water", required=True)
    ap.add_argument("--heat", required=True)
    ap.add_argument("--outdoor", required=True)
    ap.add_argument("--weather", required=True)
    ap.add_argument("--existing", help="a live .jsonl to merge with rather than replace")
    ap.add_argument("-o", "--out", required=True)
    args = ap.parse_args()

    rows, skipped = build(args.water, args.heat, args.outdoor, args.weather)
    merged, live_n, dup_n = merge(rows, args.existing)

    tmp = f"{args.out}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        for row in merged:
            fh.write(json.dumps(row, separators=(",", ":")) + "\n")
    os.replace(tmp, args.out)

    heat_n = sum(1 for row in rows if row["src"] == "heat")
    print(f"seeded {len(rows)} row(s): {heat_n} heating, {len(rows) - heat_n} idle")
    print(f"  skipped: {skipped['jump']} jump(s), {skipped['dropout']} dropout(s), "
          f"{skipped['preheat']} in preheat, {skipped['implausible']} implausible")
    if args.existing:
        print(f"  merged with {live_n} live row(s), {dup_n} already present")
    print(f"  wrote {len(merged)} row(s) to {args.out}")
    if merged:
        print(f"  {merged[0]['t']} .. {merged[-1]['t']}")


if __name__ == "__main__":
    main()
