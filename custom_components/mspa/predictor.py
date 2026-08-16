"""Heat-up prediction model for a spa.

Deliberately free of Home Assistant and of this integration: it imports nothing
from `homeassistant` and nothing from the rest of `mspa`, and it never reaches into
a coordinator. State arrives through the constructor; `from_coordinator` is a
convenience for call sites inside the integration and is the only part that knows
what a coordinator looks like.

Two reasons for that shape:

* **One implementation.** The heating-time maths previously existed twice — once in
  `sensor._segmented_heating_minutes` for the display and once in
  `coordinator._compute_heating_minutes` for the trigger, the latter's docstring
  admitting it "mirrors" the former to avoid a circular import that did not
  actually exist (`sensor` imports only `const` and `entity`). They had drifted:
  different fallback ordering when a bucket was empty, different behaviour within
  0.5 °C of target, and different no-data fallbacks. Predictions that must agree
  cannot be computed twice.
* **Extractability.** Nothing here is MSpa-specific, so this is the piece that can
  become a shared module for other spa integrations. The ambient model moved here
  from `const` for the same reason: `const` imports `homeassistant.const`, and the
  weather correction is prediction logic rather than a constant.

The model itself: heating rate falls as water warms, because losses grow with the
gap to ambient. That is approximated by three temperature buckets, each with a rate
learned from observation, corrected for today's conditions and scaled by a
historical bias. (A two-parameter physical model fits the same data slightly better
— see ROADMAP — but the buckets are what ship today.)
"""
from datetime import timedelta

# ── Temperature buckets ──────────────────────────────────────────────────────
# Bucket 0: cold, minimal losses, fastest heating
# Bucket 1: mid
# Bucket 2: near setpoint, highest losses, slowest heating
HEAT_BUCKET_T1 = 30.0
HEAT_BUCKET_T2 = 37.0

# Gaps smaller than this count as "already there": it is the near-target hysteresis
# the coordinator uses, so a smaller gap must predict zero minutes or the scheduler
# would wait for heating that will never be observed.
NEAR_TARGET_BAND = 0.5

# Device-reported rate arrives in 1/10 °C per hour and is only a cold-start seed,
# so it is clamped to a physically plausible range for these units.
DEVICE_RATE_MIN = 0.5
DEVICE_RATE_MAX = 2.0

# ── Ambient (weather) correction ─────────────────────────────────────────────
# Per-bucket sensitivity to outdoor temperature, strongest near the setpoint where
# losses dominate. Measured against 831 hours of statistics on 2026-08-10: the mid
# value is confirmed (0.024 observed) but the hot one looks 2–3x too steep
# (0.027 observed) — see ROADMAP before trusting it.
AMBIENT_SENSITIVITY = (0.0, 0.02, 0.06)
AMBIENT_FACTOR_MIN = 0.3   # never slow a bucket below 30% of its learned rate
AMBIENT_FACTOR_MAX = 1.5   # never speed a bucket beyond 150% of its learned rate


# Reported temperature is quantised to this step, so a reading locates the true value
# only to within a band.  At a crossing it is known exactly — it is the threshold.
TEMP_BAND_C = 0.5


def extrapolate_within_band(anchor_temp, elapsed_hours, rate_c_per_h, *, cooling):
    """True-temperature estimate now, from the crossing that entered the current band.

    At a crossing the true temperature is the threshold between the two reported
    values, which is what `anchor_temp` records.  Afterwards it keeps moving while the
    reported value stays put until the next threshold is reached, so between crossings
    the position is unobserved.  Extrapolating at the known rate recovers it, which
    turns the scheduler's per-crossing lump into a smooth ramp.

    **The clamp to one band is a deduction, not a safety margin.** The reading has not
    changed, therefore the next threshold has not been crossed, therefore the drift
    cannot exceed a band.  It also makes the estimate continuous through a crossing: a
    full band of drift lands exactly on the anchor the next crossing will set, so
    handing over introduces no step.

    Saturating the clamp is meaningful — the model expected a crossing that has not
    arrived, so the rate is optimistic. The value stays pinned at the band edge, and
    callers may want to report the condition.

    Averaged across a dwell this is neutral against simply holding the reported value:
    it runs half a band warm just after a crossing and half a band cold just before the
    next, and the reported value is the mean of the two. So it removes the lumps
    without trading away any of the conservatism that holding the reading provided.

    Returns None when it cannot be computed, so callers fall back to the reading.
    """
    if anchor_temp is None or elapsed_hours is None:
        return None
    if rate_c_per_h is None or rate_c_per_h <= 0 or elapsed_hours < 0:
        return None
    drift = min(rate_c_per_h * elapsed_hours, TEMP_BAND_C)
    return anchor_temp - drift if cooling else anchor_temp + drift


def bucket_index(temp: float) -> int:
    """Bucket for a water temperature: 0 cold, 1 mid, 2 near-setpoint."""
    return 0 if temp < HEAT_BUCKET_T1 else 1 if temp < HEAT_BUCKET_T2 else 2


def ambient_rate_factor(bucket_idx, ambient_now, ambient_baseline) -> float:
    """Multiplicative rate correction for current outdoor conditions.

    Returns 1.0 when ambient data is unavailable, so estimates degrade gracefully
    to the plain learned rate. Otherwise scales linearly with the distance from the
    learned baseline, using a per-bucket sensitivity, clamped for stability.
    """
    if ambient_now is None or ambient_baseline is None:
        return 1.0
    if bucket_idx < 0 or bucket_idx > 2:
        return 1.0
    factor = 1.0 + AMBIENT_SENSITIVITY[bucket_idx] * (ambient_now - ambient_baseline)
    return max(AMBIENT_FACTOR_MIN, min(AMBIENT_FACTOR_MAX, factor))


class HeatPredictor:
    """Predicts heating time from learned rates and today's conditions."""

    def __init__(
        self,
        *,
        buckets=(None, None, None),
        prediction_bias: float = 1.0,
        session_scalar: float = 1.0,
        fresh_buckets=frozenset(),
        ambient_temp=None,
        ambient_baseline=None,
        flat_rate=None,
        device_rate=None,
    ):
        self.buckets = list(buckets) + [None] * (3 - len(buckets))
        self.prediction_bias = prediction_bias if prediction_bias else 1.0
        self.session_scalar = session_scalar
        self.fresh_buckets = fresh_buckets or frozenset()
        self.ambient_temp = ambient_temp
        self.ambient_baseline = ambient_baseline
        self.flat_rate = flat_rate
        self.device_rate = device_rate

    @classmethod
    def from_coordinator(cls, coordinator) -> "HeatPredictor":
        """Build from an MSpa coordinator. The only coordinator-aware code here."""
        raw = (getattr(coordinator, "_last_data", None) or {}).get("device_heat_perhour", 0)
        try:
            raw = int(raw)
        except (TypeError, ValueError):
            raw = 0
        device_rate = (
            max(DEVICE_RATE_MIN, min(DEVICE_RATE_MAX, raw / 10.0)) if raw > 0 else None
        )
        flat = getattr(coordinator, "computed_heat_rate", None)
        return cls(
            buckets=getattr(coordinator, "heat_rate_buckets", [None, None, None]),
            prediction_bias=getattr(coordinator, "prediction_bias", 1.0),
            session_scalar=getattr(coordinator, "_session_scalar", 1.0),
            fresh_buckets=getattr(coordinator, "_session_fresh_buckets", frozenset()),
            ambient_temp=getattr(coordinator, "ambient_temp", None),
            ambient_baseline=getattr(coordinator, "ambient_baseline", None),
            flat_rate=flat if (flat is not None and flat > 0) else None,
            device_rate=device_rate,
        )

    # ── rates ────────────────────────────────────────────────────────────────

    def bucket_rate(self, temp: float):
        """Best available rate (°C/h) for the bucket containing `temp`, corrected.

        Falls back to the *nearest* populated bucket rather than the lowest-indexed
        one: an unobserved hot bucket is better approximated by mid than by cold.
        (The coordinator's old copy scanned 0,1,2 and could substitute a cold rate
        for a near-setpoint one, over-predicting the rate where it matters most.)
        """
        idx = bucket_index(temp)
        rate, source_idx = None, None
        if self.buckets[idx] is not None and self.buckets[idx] > 0:
            rate, source_idx = self.buckets[idx], idx
        else:
            for offset in (1, -1, 2, -2):
                i = idx + offset
                if 0 <= i < 3 and self.buckets[i] is not None and self.buckets[i] > 0:
                    rate, source_idx = self.buckets[i], i
                    break
        if rate is None:
            rate = self.flat_rate if self.flat_rate is not None else self.device_rate
        if rate is None or rate <= 0:
            return None

        # Correction precedence for the bucket the water is actually in:
        #   1. A bucket observed this session already reflects today's conditions.
        #   2. Otherwise the empirical session scalar supersedes the weather model.
        #   3. Otherwise the weather model, which drives the pre-start estimate
        #      before any observation exists.
        if source_idx is not None and source_idx in self.fresh_buckets:
            return rate
        if self.session_scalar != 1.0:
            return rate * self.session_scalar
        return rate * ambient_rate_factor(idx, self.ambient_temp, self.ambient_baseline)

    # ── time ─────────────────────────────────────────────────────────────────

    def heating_minutes(self, from_temp: float, to_temp: float):
        """Minutes to heat between two temperatures, or None without rate data.

        Splits at bucket boundaries so each segment uses its own rate, then applies
        the historical bias.
        """
        if from_temp is None or to_temp is None:
            return None
        if from_temp >= to_temp or (to_temp - from_temp) < NEAR_TARGET_BAND:
            return 0.0

        boundaries = [from_temp]
        for threshold in (HEAT_BUCKET_T1, HEAT_BUCKET_T2):
            if from_temp < threshold < to_temp:
                boundaries.append(threshold)
        boundaries.append(to_temp)

        total = 0.0
        for seg_start, seg_end in zip(boundaries, boundaries[1:]):
            delta = seg_end - seg_start
            if delta <= 0:
                continue
            rate = self.bucket_rate(seg_start)
            if rate is None or rate <= 0:
                return None
            total += (delta / rate) * 60.0
        return total * self.prediction_bias

    def effective_rate(self, from_temp: float, to_temp: float):
        """Average °C/h actually in effect over a span, as the estimate prices it.

        Integrates the per-bucket rates with every correction and the bias — unlike
        the flat EMA, which drives nothing.
        """
        mins = self.heating_minutes(from_temp, to_temp)
        if not mins or mins <= 0:
            return None
        return (to_temp - from_temp) / (mins / 60.0)


# ── Shadow plan ──────────────────────────────────────────────────────────────
# The stored buckets' own boundaries. An extra one at 34 was tried and makes no
# difference: only a band within `max_amplification x settle` of the target ever
# recalibrates, and 34-37 is not, so the split is inert. It appeared to help while an
# earlier version of the rule let bands recalibrate late on a grown span, which was a
# bug rather than a feature.
SHADOW_BOUNDS = (30.0, 37.0)

# How much of the remaining journey one measurement may speak for. A rate measured
# over one degree and applied to sixteen multiplies its own error sixteenfold:
# recalibrating that early made every recorded session worse, by 150, 102 and 267
# minutes. Requiring the remaining span to be no more than four times the measured one
# keeps the correction proportionate to its evidence.
SHADOW_MAX_AMPLIFICATION = 4.0
SHADOW_SETTLE_C = 1.0

# How far the shadow may stray from the rates it started with. Applied to the
# cumulative deviation, not to each step: a single large factor is legitimate when it
# is undoing an earlier over-correction, and clamping steps individually blocks
# exactly that recovery.
SHADOW_DRIFT_MIN, SHADOW_DRIFT_MAX = 0.5, 2.0


class ShadowPlan:
    """A private copy of the rate curve, recalibrated against today's actual heating.

    Separate from the stored buckets by design. Those are what the scheduler plans
    from and are learned slowly across sessions; this exists only to make the
    *displayed* ready time right, and is discarded when the session ends.

    Measured over four recorded heat-ups, minutes wrong on average:

                                  ¼ in   half   ¾ in   end   revisions
        this                        10     10      4     2       2
        replanning every crossing   38     26     14     0      18

    The rule for *when* to recalibrate is the whole trick, and it is neither a fixed
    temperature nor a fixed delay: it fires once the span actually measured is worth a
    quarter of the span still to come. On a cold start from 22 °C that lands near
    26 °C, early; on a top-up from 37 °C it lands near the target. The measurement is
    always proportionate to the journey it is asked to predict.
    """

    def __init__(self, base_rates, start_temp, target, opening_eta,
                 bounds=SHADOW_BOUNDS, settle=SHADOW_SETTLE_C,
                 max_amplification=SHADOW_MAX_AMPLIFICATION):
        self.bounds = tuple(bounds)
        self.target = target
        self.settle = settle
        self.max_amplification = max_amplification
        self.eta = opening_eta
        self.revisions = 0
        self._base = self._seed(base_rates)
        self.rates = list(self._base)
        self._anchor = None          # (temp, when) — always a crossing, never the start
        self._done = set()

    def _seed(self, base_rates):
        """Seed each shadow band from the stored bucket covering its midpoint."""
        edges = (0.0,) + self.bounds + (100.0,)
        return tuple(base_rates[bucket_index((edges[i] + edges[i + 1]) / 2.0)]
                     for i in range(len(self.bounds) + 1))

    def band(self, temp):
        for i, edge in enumerate(self.bounds):
            if temp < edge:
                return i
        return len(self.bounds)

    def minutes(self, frm, to):
        """Time from `frm` to `to` through the shadow curve, or None if unusable."""
        if frm is None or to is None:
            return None
        if to <= frm:
            return 0.0
        edges = (-999.0,) + self.bounds + (999.0,)
        total = 0.0
        for i, rate in enumerate(self.rates):
            span = max(0.0, min(to, edges[i + 1]) - max(frm, edges[i]))
            if span:
                if not rate or rate <= 0:
                    return None
                total += span / rate * 60.0
        return total

    def crossing(self, temp, when):
        """Feed one reported-temperature change. True when the estimate was revised.

        The first call of a session is never used as a measurement: at that moment the
        water sits somewhere unknown inside its 0.5 °C band, so the interval leading to
        it times that position rather than any heating — session C implied 7.9 °C/h,
        which no heater here can produce. From the first crossing onward every position
        is exact, and a band boundary is exact by definition, so nothing further is set
        aside.
        """
        band = self.band(temp)
        if self._anchor is None or self.band(self._anchor[0]) != band:
            self._anchor = (temp, when)
            return False

        revised = False
        if band not in self._done:
            span = temp - self._anchor[0]
            hours = (when - self._anchor[1]).total_seconds() / 3600.0
            if span >= self.settle and hours > 0:
                # A band gets exactly one chance, at the moment it has been measured
                # for `settle` degrees — and it is taken only if what remains is worth
                # no more than `max_amplification` times that.  Marking the band done
                # either way is deliberate: letting it keep waiting lets the span grow
                # until the test passes on arithmetic alone, which fires the
                # recalibration hundreds of minutes too early.  Doing exactly that put
                # the estimate 81 minutes out at the quarter mark against 10 for this.
                if self.target - temp <= self.max_amplification * self.settle:
                    revised = self._recalibrate(
                        span / hours, self._anchor[0], temp, when)
                self._done.add(band)

        # One last look with half a degree to go, using whatever the curve has become.
        if not revised and self.target - 0.5 - 1e-9 <= temp < self.target:
            mins = self.minutes(temp, self.target)
            if mins is not None:
                self.eta = when + timedelta(minutes=mins)
                self.revisions += 1
                revised = True
        return revised

    def _recalibrate(self, observed, from_temp, temp, when):
        base = self.rates[self.band(from_temp)]
        if not base or base <= 0:
            return False
        factor = observed / base
        cumulative = (self.rates[0] / self._base[0]) * factor
        if cumulative > SHADOW_DRIFT_MAX:
            factor *= SHADOW_DRIFT_MAX / cumulative
        elif cumulative < SHADOW_DRIFT_MIN:
            factor *= SHADOW_DRIFT_MIN / cumulative
        self.rates = [r * factor for r in self.rates]
        mins = self.minutes(temp, self.target)
        if mins is None:
            return False
        self.eta = when + timedelta(minutes=mins)
        self.revisions += 1
        return True
