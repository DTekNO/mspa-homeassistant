import json, sys
from datetime import datetime, timezone, timedelta

SP = sys.argv[1]
TSV = "/home/jeco/Dev/HA/mspa/docs/data/cooling-2026-10-18_20.tsv"
CEST = timezone(timedelta(hours=2))

def series(path, key=None):
    d = json.load(open(path))
    out = []
    for p in (d[0] if d else []):
        t = p.get("last_updated") or p.get("last_changed")
        if not t: continue
        when = datetime.fromisoformat(t.replace("Z", "+00:00")).astimezone(CEST)
        v = (p.get("attributes") or {}).get(key) if key else p.get("state")
        try: v = float(v)
        except (TypeError, ValueError): continue
        out.append((when, v))
    return sorted(out)

air = series(f"{SP}/weather_hist.json", "temperature")  # the model's own air source
uv  = series(f"{SP}/weather_hist.json", "uv_index")

def twa(ser, a, b):
    """time-weighted average of a step series over [a,b]"""
    if not ser: return None
    tot = num = 0.0
    for (t0, v0), (t1, _) in zip(ser, ser[1:] + [(b + timedelta(hours=1), None)]):
        lo, hi = max(t0, a), min(t1, b)
        if hi > lo:
            w = (hi - lo).total_seconds(); num += v0 * w; tot += w
    return num / tot if tot else None

water = []
for line in open(TSV):
    if line.startswith("#") or line.startswith("timestamp"): continue
    ts, t = line.split()
    water.append((datetime.fromisoformat(ts).replace(tzinfo=CEST), float(t)))

print(f"{'interval (CEST)':28} {'h':>5} {'C/h':>6} {'air':>5} {'gap':>5} {'uv':>5}  class")
rows, night, day = [], [], []
for (t0, T0), (t1, T1) in zip(water, water[1:]):
    h = (t1 - t0).total_seconds() / 3600
    rate = (T0 - T1) / h
    a = twa(air, t0, t1); u = twa(uv, t0, t1)
    if a is None or u is None: continue
    gap = (T0 + T1) / 2 - a
    cls = "DAY" if u >= 0.25 else ("night" if u <= 0.02 else "twilight")
    rows.append((rate, gap, u, cls))
    (day if cls == "DAY" else night if cls == "night" else []).append((rate, gap, u))
    print(f"{t0:%d.%m %H:%M}->{t1:%d.%m %H:%M} {h:5.2f} {rate:6.3f} {a:5.1f} {gap:5.1f} {u:5.2f}  {cls}")

print()
# normalise: rate per degree of gap removes the main confound
for name, s in (("night", night), ("DAY  ", day)):
    if not s: continue
    k = [r / g for r, g, _ in s]
    print(f"{name}: n={len(s):2d}  mean rate {sum(r for r,_,_ in s)/len(s):.3f} C/h"
          f"  mean gap {sum(g for _,g,_ in s)/len(s):5.1f}"
          f"  rate/gap {sum(k)/len(k):.5f} /h  (tau = {1/(sum(k)/len(k)):.1f} h)")

if night and day:
    kn = sum(r/g for r,g,_ in night)/len(night)
    kd = sum(r/g for r,g,_ in day)/len(day)
    gbar = sum(g for _,g,_ in day)/len(day)
    ubar = sum(u for _,_,u in day)/len(day)
    deficit = (kn - kd) * gbar          # C/h of cooling NOT happening by day
    watts = deficit * 1285 * 1.163      # 1285 L, 1.163 Wh per L per K
    print()
    print(f"day cools slower by {deficit:.3f} C/h at the daytime mean gap of {gbar:.1f} C")
    print(f"-> solar gain ~ {watts:.0f} W at mean daytime uv {ubar:.2f}")
    print(f"-> per unit uv  ~ {watts/ubar:.0f} W")

# --- duration-weighted aggregate: total dT over total hours, not a mean of rates ---
print()
print("DURATION-WEIGHTED (total dT / total hours, gap weighted the same way)")
agg = {}
for (t0, T0), (t1, T1) in zip(water, water[1:]):
    h = (t1 - t0).total_seconds() / 3600
    a = twa(air, t0, t1); u = twa(uv, t0, t1)
    if a is None or u is None: continue
    cls = "DAY" if u >= 0.25 else ("night" if u <= 0.02 else "twilight")
    d = agg.setdefault(cls, {"dT": 0.0, "h": 0.0, "gh": 0.0, "uh": 0.0})
    d["dT"] += T0 - T1; d["h"] += h
    d["gh"] += ((T0 + T1) / 2 - a) * h; d["uh"] += u * h
for cls in ("night", "twilight", "DAY"):
    if cls not in agg: continue
    d = agg[cls]
    rate = d["dT"] / d["h"]; gap = d["gh"] / d["h"]; u = d["uh"] / d["h"]
    k = rate / gap
    print(f"  {cls:9} {d['h']:5.1f} h  dT {d['dT']:5.1f} C  rate {rate:.3f} C/h"
          f"  gap {gap:5.1f}  uv {u:4.2f}  k {k:.5f}/h  tau {1/k:5.1f} h")
n, dd = agg.get("night"), agg.get("DAY")
if n and dd:
    kn = (n["dT"]/n["h"]) / (n["gh"]/n["h"])
    kd = (dd["dT"]/dd["h"]) / (dd["gh"]/dd["h"])
    gbar = dd["gh"]/dd["h"]; ubar = dd["uh"]/dd["h"]
    w = (kn - kd) * gbar * 1285 * 1.163
    print()
    print(f"  tau night {1/kn:.1f} h vs day {1/kd:.1f} h  ->  solar {w:.0f} W at uv {ubar:.2f}")
    print(f"  for scale: the 08.10 scatter of 0.44 C/h would need "
          f"{0.44*1285*1.163:.0f} W")
