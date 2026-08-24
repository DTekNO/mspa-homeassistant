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
# The band edges. These are where the plan is allowed to revise itself, because they
# are the only temperatures at which a *complete* traverse has been measured — and a
# bucket rate describes exactly that, the chord between the two edges. Extra edges at
# 34 and 26 were both tried and both made things worse, and a leave-one-out test over
# five recorded sessions agreed: bands at 33 and 36 leave the worst mid-session error
# where it was, and six bands make it worse. Half-degree quantisation is why — a 1 °C
# band traversed in under an hour is measured to about ±50%, a 3 °C band to ±17%.
#
# 20 is the exception to that finding, and does not contradict it. The objection to an
# extra edge is that it shortens a *measured* run; below HEAT_BUCKET_LEARN_MIN there is
# no measured run to shorten, because nothing down there was ever learned and the rate
# in use is the 20–30 chord extrapolated. Marking where the extrapolation ends is worth
# a re-anchor precisely because that stretch is the least trustworthy in the session:
# without it, a start at 15 carries its opening error all the way to 30 before the plan
# is allowed to correct itself.
#
# It also cannot disturb any of the evidence above. The archive's floor is 22 °C, so 20
# sits below the start of all five recorded sessions and never fires on one — the
# leave-one-out result is unchanged by construction, not by argument.
SHADOW_BOUNDS = (20.0, 30.0, 37.0)

# The hot bucket learns over 37–39 and is then used for everything above 37.
#
# Left unbounded it absorbed the 39–40 tail, where a session with a 40 °C setpoint
# spends its slowest hour, and the resulting chord suited neither. Bounded at 39 the
# rate above it is extrapolated, which the recordings say costs nothing: across five
# sessions the final half degree runs at 1.05x the rate of the degree below it, flat
# within the noise of a 0.5 °C span.
HEAT_BUCKET_LEARN_MAX = 39.0

# The cold bucket's lower edge, for the same reason at the other end. The lowest water
# temperature in the archive is 22 °C, so this sits just under the observed range
# without inventing structure inside it. Only one recorded session reaches below 30 and
# it does show the rate falling — 1.20 °C/h at 22.5, 1.02 by 28.5 — but one session is
# not enough to justify a boundary in there, and putting one in on that evidence is the
# mistake this file has already made once.
HEAT_BUCKET_LEARN_MIN = 20.0

def learning_anchor_zone(temp):
    """Which measuring window a temperature belongs to, for anchoring purposes.

    The bucket index, except that everything below HEAT_BUCKET_LEARN_MIN is a zone of
    its own. The measuring window holds its anchor while the water stays in one zone and
    re-anchors when it leaves, so this is what decides where a new chord starts.

    Without the sub-range zone the anchor set at the start of a cold session never moves
    until 30 °C, and `in_learning_range` tests the *from* temperature — so a session
    beginning at 15 offers every one of its samples as 15→x and has all of them refused,
    including the full 20→30 traverse that is exactly what the cold bucket wants. The
    top end has no equivalent problem: there the anchor (37) is inside the learning
    range and only the far end of the span leaves it, so the samples up to 39 are taken
    normally and only the tail above is refused.

    Returns None for an unknown temperature, which never equals another zone, so an
    unusable reading closes the window rather than silently extending it.
    """
    if temp is None:
        return None
    try:
        t = float(temp)
    except (TypeError, ValueError):
        return None
    return -1 if t < HEAT_BUCKET_LEARN_MIN else bucket_index(t)


def in_learning_range(from_temp, to_temp) -> bool:
    """Whether a rate sample spanning `from_temp` to `to_temp` may update a bucket.

    Extracted so a test can ask it directly rather than reach into the sampling block —
    the same reason `_update_near_target` was pulled out of the coordinator, after its
    hand-written copy in the tests drifted from the real rule.

    Unknowns answer False: a sample that cannot be placed on the curve should not move
    a bucket that other sessions depend on.
    """
    if from_temp is None or to_temp is None:
        return False
    try:
        return (HEAT_BUCKET_LEARN_MIN - 1e-9 <= float(from_temp)
                and float(to_temp) <= HEAT_BUCKET_LEARN_MAX + 1e-9)
    except (TypeError, ValueError):
        return False


class ShadowPlan:
    """A private copy of the rate curve, recalibrated against today's actual heating.

    Separate from the stored buckets by design. Those are what the scheduler plans
    from and are learned slowly across sessions; this exists only to make the
    *displayed* ready time right, and is discarded when the session ends.

    The rule for *when* to recalibrate is the whole trick, and it is neither a fixed
    temperature nor a fixed delay: the span measured is always a third of the span still
    to come. Far from the target that is a long run — 5.5 °C from a 22 °C start — and
    close to it a single crossing. The measurement is therefore always proportionate to
    the journey it is asked to predict, which is what lets it be trusted whole.

    A measurement runs until it has covered its share, whatever it crosses on the way —
    band boundaries do not interrupt it, because the run is compared against what the
    whole curve predicted for that exact span rather than against any one band's rate.

    Measured over four recorded heat-ups and three of them restarted from 23, 24 and
    25 °C, minutes wrong on average, against copies with the stored rates offset as a
    change of season would offset them:

                                   ¼ in   half   ¾ in   end
        stored rates right           16     33     22     1
        stored rates 30% fast       180     33     22     1
        stored rates 25% slow       245     33     22     1

    Everything from the halfway mark rightward is identical across the three, per
    session and to the minute: once the first measurement lands, whatever the stored
    buckets got wrong has been measured away, so a curve learned in July no longer drags
    a January heat-up. Before it, the display is simply the opening plan — being wrong
    quietly until there is evidence beats being wrong loudly throughout, which is what a
    fixed one-degree settle did (116 and 133 at the halfway mark).

    It revises about six times over a long session and four over a short one, nearly all
    of them small: the first correction carries the season, and the rest converge.
    """

    def __init__(self, base_rates, start_temp, target, opening_eta,
                 bounds=SHADOW_BOUNDS, start_time=None):
        self.bounds = tuple(bounds)
        self.target = target
        self.eta = opening_eta
        self.revisions = 0
        self._start_temp = start_temp
        self._base = self._seed(base_rates)
        # Frozen for the session. Nothing measured mid-session may move them: see
        # crossing() for the two experiments that establish why.
        self.rates = list(self._base)
        # Where the water entered the band it is in, and when. Seeded from the session
        # start even though that is usually mid-band — the time is still real, and the
        # only thing it is used for is the local rate in the final half degree.
        self._band_entry = (start_temp, start_time)
        self._final_look = False

    def _seed(self, base_rates):
        """Seed each shadow band from the stored bucket covering its midpoint."""
        edges = (0.0,) + self.bounds + (100.0,)
        return tuple(base_rates[bucket_index((edges[i] + edges[i + 1]) / 2.0)]
                     for i in range(len(self.bounds) + 1))

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

        Revision happens where a band completes, and once more with half a degree to go.
        Nowhere else.

        A bucket rate is the *chord* of the heating curve between the band's two edges —
        the straight line, not the rate anywhere along it. Inside the 30–37 band the real
        rate falls from about 1.33 to 1.05 °C/h, so a sub-span measured low in the band
        runs 14% above the chord and one measured high runs 9% below, while the chord
        itself is exactly right for the whole traverse. Comparing a sub-span against it
        therefore finds a difference that is arithmetically real and physically
        meaningless, and the old proportional settle guaranteed the first such comparison
        landed low in the range, where the discrepancy is largest and always in the same
        direction. On 2026-08-20 it turned an opening estimate 8 minutes out into one 58
        minutes out, and the plan did not beat its own opening again until 38.5 °C.

        At a band edge there is nothing to compare: the elapsed time to that exact
        temperature is fact. So the revision is a re-anchor, not a recalibration — the
        remaining bands keep the rates the session opened with, and only the starting
        point moves. That is why the rates no longer change during a session.

        What is deliberately *not* done is carrying the completed band's measurement into
        the bands ahead. Tested two ways over five recorded sessions: scaling only the
        next band by the observed ratio oscillates (−3, +22, −24, +7, −30, +2, −22, +8,
        −7 at one-degree bands), and a single session-wide condition factor is worse
        still, taking the worst error from 39 to between 75 and 110 minutes. A band the
        water has not entered has no evidence about it, and inventing some costs more
        than admitting it.

        The last half degree is the one place a sub-span is used, because there the
        objection does not apply: the measurement sits immediately below the span it
        predicts, and the horizon is one crossing. Over the five sessions the rate in
        the final half degree averages 1.05× the rate in the degree below it — flat
        within the quantisation noise of a 0.5 °C span.
        """
        revised = False

        # A band edge, reached from below: elapsed time to here is known exactly.
        if (self._band_entry is not None
                and temp > self._band_entry[0]
                and any(abs(temp - b) < 1e-9 for b in self.bounds)):
            self._band_entry = (temp, when)
            mins = self.minutes(temp, self.target)
            if mins is not None:
                self.eta = when + timedelta(minutes=mins)
                self.revisions += 1
                revised = True

        # One last look with half a degree to go, from the rate measured immediately
        # below it. Falls back to the curve when this band was never entered from its
        # edge — a top-up starting inside the final band has nothing local to measure.
        if not revised and not self._final_look and (
                self.target - 0.5 - 1e-9 <= temp < self.target):
            self._final_look = True
            mins = None
            entry_temp, entry_when = self._band_entry or (None, None)
            if entry_when is not None and temp > entry_temp:
                hours = (when - entry_when).total_seconds() / 3600.0
                if hours > 0:
                    local = (temp - entry_temp) / hours
                    if local > 0:
                        mins = (self.target - temp) / local * 60.0
            if mins is None:
                mins = self.minutes(temp, self.target)
            if mins is not None:
                self.eta = when + timedelta(minutes=mins)
                self.revisions += 1
                revised = True
        return revised

