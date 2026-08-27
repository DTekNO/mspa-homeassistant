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


# How a learned fit is trusted, and where it stops being trusted.
#
# The gate is *extrapolation distance*, not spread. A least-squares line passes through
# the mean of its data, so a fit built from a narrow band of ambients predicts perfectly
# well inside that band however badly determined its slope is — it degrades to "the rate
# this spa achieves at this temperature", which beats the bucket and a guessed sensitivity.
# The error only becomes dangerous away from the data: from 12-14 °C observations the
# slope's standard error is about as large as the slope, and multiplied by the twenty
# degrees to a January night that is an error comparable to the rate itself.
#
# So the fit is used from the second traverse onward, which is what makes it useful in a
# climate where ambient moves slowly and today is nearly always a good forecast of
# tomorrow — and it hands back to the seed as the question moves away from the evidence.
LEARNED_SHRINK_N = 20.0      # observations at which the fit carries half its weight
LEARNED_TRUST_SD = 1.0       # within one standard deviation of the data, trusted whole
LEARNED_FADE_SD = 3.0        # beyond three, not used at all


def learned_ambient_factor(ambient_now, ambient_baseline, fit, seed_factor):
    """Blend the seed sensitivity with what this spa has actually shown.

    `fit` is a band_rate_fit result: slope and intercept of rate against ambient over
    every traverse recorded, with the effective sample size and the weighted spread of
    the ambients it was fitted across.

    Returns the seed unchanged whenever the fit cannot improve on it: too few
    observations, a degenerate line, a prediction far outside the evidence, or a fitted
    rate at the baseline that is not positive and so cannot form a ratio.
    """
    if fit is None or ambient_now is None or ambient_baseline is None:
        return seed_factor
    a, b, n, sd = fit["intercept"], fit["slope"], fit["n"], fit.get("ambient_sd") or 0.0
    at_baseline = a + b * ambient_baseline
    at_now = a + b * ambient_now
    if at_baseline <= 0 or at_now <= 0:
        return seed_factor
    # Two independent reasons to hold back, multiplied: how much evidence there is, and
    # how far the question sits from it.
    w_n = n / (n + LEARNED_SHRINK_N)
    mean_amb = ambient_now if sd <= 0 else None
    if sd > 0:
        # Distance from the centre of the evidence, in standard deviations. The centre is
        # recovered from the fit rather than stored: at x̄ the line predicts ȳ.
        z = abs(ambient_now - fit.get("ambient_mean", ambient_now)) / sd
    else:
        # No spread at all: trusted only at the temperature it was measured at.
        z = 0.0 if abs(ambient_now - fit.get("ambient_mean", ambient_now)) < 1e-9 else 99.0
    if z <= LEARNED_TRUST_SD:
        w_z = 1.0
    elif z >= LEARNED_FADE_SD:
        w_z = 0.0
    else:
        w_z = (LEARNED_FADE_SD - z) / (LEARNED_FADE_SD - LEARNED_TRUST_SD)
    w = w_n * w_z
    if w <= 0:
        return seed_factor
    learned = at_now / at_baseline
    blended = w * learned + (1.0 - w) * seed_factor
    return max(AMBIENT_FACTOR_MIN, min(AMBIENT_FACTOR_MAX, blended))


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
        band_fits=None,
        flat_rate=None,
        device_rate=None,
    ):
        self.buckets = list(buckets) + [None] * (3 - len(buckets))
        self.prediction_bias = prediction_bias if prediction_bias else 1.0
        self.session_scalar = session_scalar
        self.fresh_buckets = fresh_buckets or frozenset()
        self.ambient_temp = ambient_temp
        self.ambient_baseline = ambient_baseline
        # Per-band least-squares fits of rate against ambient, or None to use the seed
        # sensitivities alone. Learning continues whether or not these are supplied.
        self.band_fits = band_fits or {}
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
            # What this spa has shown about its own response to the weather. Absent on a
            # coordinator that has not learned any yet, which leaves the seeds standing.
            band_fits=(coordinator.band_fits()
                       if hasattr(coordinator, "band_fits") else None),
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
        # The seed sensitivity, then whatever this spa has actually shown about its own
        # response to the weather, blended by how much evidence there is and how far the
        # question sits from it. With no fit for this band the seed stands unchanged.
        seed = ambient_rate_factor(idx, self.ambient_temp, self.ambient_baseline)
        factor = learned_ambient_factor(
            self.ambient_temp, self.ambient_baseline,
            self.band_fits.get(idx) or self.band_fits.get(str(idx)),
            seed,
        )
        return rate * factor

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

# A band's measurement is only worth applying where the journey it is asked to predict is
# comparable to the journey it measured. Session B of the recorded set enters at 29.0, so
# its first band is a single degree: unguarded, a ratio of 2.48 measured over that degree
# gets applied to the nine and a half remaining, and a later factor of 0.37 is needed to
# undo it. Four is the ratio at which the recordings stop benefiting.
MAX_AMPLIFICATION = 4.0

# How far the curve may drift from the rates the session opened with, cumulatively. Inert
# across all five recorded sessions — the largest cumulative drift is 1.32 — so this is a
# guard against data nobody has seen yet rather than a tuning parameter. Applied to the
# cumulative deviation, not to each step, so a large factor that is bringing the curve
# back toward its seed is never blocked.
SHADOW_FACTOR_MIN, SHADOW_FACTOR_MAX = 0.5, 2.0

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

    def _rescale(self, frm, frm_when, to, to_when):
        """Scale the curve by how wrong the band just completed turned out to be.

        Actual minutes against planned minutes, over the identical temperature span.

        Re-anchoring alone resets the clock but keeps the opening rates, so arriving
        early buys only the time already saved while everything ahead stays priced at
        rates that have just been shown to be wrong. On 2026-08-25 that left the estimate
        192 minutes out at the first revision and 84 at the second, on a session that
        finished 243 minutes before its opening plan.

        This is not the recalibration the class docstring above rejects, and the
        difference is the measurement rather than the idea. That one compared a rate
        measured over a fixed span *into* the band just entered against that band's
        chord: a degree, quantised by 0.5 °C reporting to roughly +/-50%, and compared
        against a chord that is exact only over a full traverse — so it found differences
        that were arithmetically real and physically meaningless, and oscillated. This
        compares elapsed minutes against planned minutes over the same span, which is
        like for like at any width, and measures over a whole band rather than a degree.

        Simulated over the five recorded sessions in analysis/shadow_recalibrate.py,
        against what the card did before:

                                       whole   last 90   finish
            re-anchor only            62.5 m    15.1 m    2.5 m
            with this rescaling       43.4 m    13.5 m    2.5 m

        The worst first-revision error falls from 192 minutes to 57. Sessions whose
        opening estimate was already close come out slightly worse at the first revision
        — A from -11 to -26, C from -33 to -47 — which is the expected trade and the
        reason the whole-session figure is the one to read.

        Scaling from the band just finished rather than from the whole session so far is
        deliberate. Cumulative scored marginally better overall (42.6 against 43.4), but
        it lost on the one session where the weather actually moved during the run — the
        eleven-hour 25 August heat-up, from a 10.8 °C morning into a warm afternoon —
        because averaging across the session dilutes exactly the evidence that matters.
        The band just finished is the closest thing to current conditions the card has.
        """
        # start_time is optional, so a plan built without one has no clock to measure
        # from until its first band edge. Nothing to rescale by; re-anchoring still
        # happens, and the next band has a real entry time.
        if frm_when is None or to_when is None:
            return
        planned = self.minutes(frm, to)
        actual = (to_when - frm_when).total_seconds() / 60.0
        measured = to - frm
        if not planned or planned <= 0 or actual <= 0 or measured <= 0:
            return
        if (self.target - to) / measured > MAX_AMPLIFICATION:
            return
        factor = planned / actual
        # Clamped on the cumulative deviation from the seeded curve, never per step.
        cumulative = (self.rates[0] / self._base[0]) * factor if self._base[0] else factor
        if cumulative > SHADOW_FACTOR_MAX:
            factor *= SHADOW_FACTOR_MAX / cumulative
        elif cumulative < SHADOW_FACTOR_MIN:
            factor *= SHADOW_FACTOR_MIN / cumulative
        self.rates = [r * factor if r else r for r in self.rates]

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
            self._rescale(self._band_entry[0], self._band_entry[1], temp, when)
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
                        # Carry the same evidence into the curve, so the session does not
                        # end with its rates still describing the band it just disproved.
                        #
                        # This changes no estimate. Over a span inside one band the two
                        # routes are the same number by construction: the factor is
                        # (span/rate)/actual, so rate x factor is span/actual, which is
                        # the local rate this branch already uses. The ETA is left on the
                        # local rate anyway, so the identity does not have to hold — it
                        # also breaks for a session that began part-way up and whose entry
                        # span crosses an edge, and there the measured average is the
                        # honest answer for the half degree while the scaled curve is the
                        # honest record.
                        #
                        # Which matters because the last band is the least trustworthy
                        # part of the curve and the one nothing else corrects. It runs
                        # from 37 upward with no edge above it, while the stored bucket
                        # only learns to 39, so everything above that is extrapolation —
                        # and a target of 40 measures 37→39.5 here, including the slower
                        # tail past 39 that no bucket has ever been taught.
                        self._rescale(entry_temp, entry_when, temp, when)
            if mins is None:
                mins = self.minutes(temp, self.target)
            if mins is not None:
                self.eta = when + timedelta(minutes=mins)
                self.revisions += 1
                revised = True
        return revised



# ── The physical model, reported but not applied ─────────────────────────────
#
# Newton's law for a heated body losing heat in proportion to the water/air gap:
#
#     dT/dt = P/C − (T_water − T_air)/τ
#
# Two components, and they are the two things actually happening: a constant heater
# term set by element power against thermal mass, and a loss term that grows with the
# gap. Two *parameters* — τ and the asymptotic lift P/k = τ·P/C, which is how far above
# air temperature this heater can hold the water. That is one fewer than the three
# bucket constants, and rate falls linearly with water temperature, so the buckets are a
# piecewise-constant approximation of a straight line.
#
# **Air temperature is not learned here, it is an input.** The law fixes its coefficient
# at exactly minus the water coefficient, so there is no sensitivity to calibrate and no
# reference conditions to pin. That is the whole argument for the model: it deletes
# `AMBIENT_SENSITIVITY`, `ambient_baseline` and `learned_ambient_factor` rather than
# fixing them. See ROADMAP, *Alternative: a physical heating model instead of buckets*.
#
# Nothing below drives a prediction. It is fitted from the traverses already recorded
# and scored alongside the shipping estimate, so that "would the physical model have
# done better" is answered by finished sessions rather than by argument. Adopting it is
# a separate decision that this evidence is meant to inform.

# The constrained fit needs enough traverses to place a line; the free fit adds a third
# parameter and a collinearity problem, so it needs more before it says anything.
NEWTON_MIN_N = 8
NEWTON_FREE_MIN_N = 12
# Below this spread in the water/air gap the slope is not determined — every observation
# sits at effectively the same gap and the line is fitted to noise.
NEWTON_MIN_GAP_SD = 1.0
# Above this correlation between water and air, the free fit's two coefficients are not
# separately determined. Reported rather than enforced — see `identified`.
NEWTON_MAX_CORR = 0.95


def _usable_rows(observations):
    """Traverses that can be regressed: learned from, positive rate, weather known."""
    rows = []
    for r in observations or ():
        if not r.get("usable"):
            continue
        rate = r.get("rate")
        water = r.get("water_mean")
        air = r.get("ambient_mean")
        if rate is None or water is None or air is None or rate <= 0:
            continue
        rows.append((float(water), float(air), float(rate)))
    return rows


# Bounds a *seeded* fit must satisfy to be used at all. Deliberately generous — the
# previous spa measured tau 62 h and a lift of 68.6 °C, and both sit comfortably inside.
#
# These gate the seed and never a fit from real traverses. The asymmetry is the point: a
# fit from observations is evidence, and evidence that says something surprising is worth
# seeing. A seed is an inference drawn out of a *different* model, so when it implies a
# spa that sheds almost no heat it is the inference that is wrong, not the spa.
NEWTON_SEED_MAX_TAU_H = 120.0
NEWTON_SEED_MAX_LIFT_C = 150.0


def seed_rows_from_buckets(buckets, ambient_baseline):
    """Turn the learned rate buckets into pseudo-observations for the physical fit.

    A bucket *is* physical data already digested: "this spa climbs at r °C/h across this
    span", which is one point on the rate-against-temperature line the law describes.
    Three buckets are three points, and a straight line needs two — so a spa that has
    been learning for months does not have to start the physical model from nothing.

    Placed at each span's midpoint. The exact quantity a bucket learns is the *chord*
    rate, span over time, which is a harmonic mean along the span rather than the rate at
    its middle — so the midpoint is an approximation. It is a very good one: against the
    law itself the two differ by 0.03% to 0.42% across the three spans, and a fit built
    from ideal chords recovers tau to 1.3% and the lift to 0.8%.

    All three are attributed to `ambient_baseline`, which is what the buckets were learned
    under as far as anything here knows. That is the seed's real weakness rather than the
    midpoint: the baseline is an EMA that follows the season, so it describes recent
    conditions better than the conditions any particular bucket was learned in.

    Returns [] when there is nothing to seed from, so callers need no special case.
    """
    if ambient_baseline is None or not buckets:
        return []
    spans = ((HEAT_BUCKET_LEARN_MIN, HEAT_BUCKET_T1),
             (HEAT_BUCKET_T1, HEAT_BUCKET_T2),
             (HEAT_BUCKET_T2, HEAT_BUCKET_LEARN_MAX))
    rows = []
    for rate, (lo, hi) in zip(buckets, spans):
        if rate is None or rate <= 0:
            continue
        rows.append({
            "usable": True,
            "seeded": True,
            "rate": float(rate),
            "water_mean": (lo + hi) / 2.0,
            "ambient_mean": float(ambient_baseline),
        })
    return rows


def newton_fit(observations, seed=None):
    """Fit `rate = P/C − gap/τ` over recorded traverses, gap being water minus air.

    `seed` is a list of pseudo-observations from the learned buckets, used **only** while
    there are too few real traverses to fit from. It is a starting point, not evidence:
    the moment real observations reach `NEWTON_MIN_N` the seed is dropped entirely rather
    than blended, because a blend would go on carrying the bucket model's shape into a
    fit whose whole purpose is to replace it.

    A seeded fit is marked `seeded: True` and is checked against
    `NEWTON_SEED_MAX_TAU_H` / `NEWTON_SEED_MAX_LIFT_C` before being returned. That check
    is not decoration. Nearly-flat buckets — and this spa's live buckets have been
    reading 1.03 / 0.99 / 1.01 — imply a body that sheds almost no heat, and the line
    through them runs its asymptote away to infinity to imitate the flatness: tau 512 h
    and a lift of 531 °C, a spa whose water would never stop rising. Seeding from that
    without a gate would be worse than not seeding at all.

    This is the *constrained* form: it assumes the law and reads off its parameters.
    It cannot test itself — collapsing water and air into one regressor presupposes the
    equal-and-opposite coefficients that `newton_free_fit` exists to check.

    Returns None where the fit would be meaningless rather than returning a number
    nobody should use: too few traverses, no spread in the gap, or a slope of the wrong
    sign (rate rising with the gap is not a spa, it is a measurement problem).
    """
    rows = _usable_rows(observations)
    seeded = False
    if len(rows) < NEWTON_MIN_N and seed:
        rows = _usable_rows(seed)
        seeded = True
    n = len(rows)
    # Two points place a line, and a seed only ever offers three.
    if n < (2 if seeded else NEWTON_MIN_N):
        return None
    gaps = [w - a for w, a, _ in rows]
    rates = [r for _, _, r in rows]
    mean_g = sum(gaps) / n
    mean_r = sum(rates) / n
    sgg = sum((g - mean_g) ** 2 for g in gaps)
    sgr = sum((g - mean_g) * (r - mean_r) for g, r in zip(gaps, rates))
    gap_sd = (sgg / n) ** 0.5
    if sgg <= 0 or gap_sd < NEWTON_MIN_GAP_SD:
        return None
    slope = sgr / sgg
    if slope >= 0:
        return None
    intercept = mean_r - slope * mean_g
    tau_h = -1.0 / slope
    lift = intercept * tau_h          # P/k: the asymptote, above air temperature
    # Residual spread, so a reader can see whether the line is describing the data or
    # merely passing through it. Two parameters consumed.
    if n > 2:
        resid = [r - (intercept + slope * g) for g, r in zip(gaps, rates)]
        rms = (sum(e * e for e in resid) / (n - 2)) ** 0.5
        se_slope = rms / (sgg ** 0.5)
        # The intercept carries the thermal mass, so its uncertainty is the one that
        # matters for the volume check below — and it is the worse of the two, because
        # the intercept is the line extrapolated back to a zero water/air gap, which is
        # about twenty degrees outside anything ever observed.
        se_intercept = rms * ((1.0 / n) + mean_g * mean_g / sgg) ** 0.5
    else:
        rms = se_slope = se_intercept = None
    if seeded and (tau_h > NEWTON_SEED_MAX_TAU_H or lift > NEWTON_SEED_MAX_LIFT_C):
        # The buckets carry no usable shape. Declining is the honest answer: see the
        # docstring for what accepting one of these looks like.
        return None
    return {
        "n": n,
        "seeded": seeded,
        "tau_h": tau_h,
        "asymptote_lift_c": lift,
        "rate_at_zero_gap": intercept,
        "slope_per_deg": slope,
        "slope_se": se_slope,
        "rate_at_zero_gap_se": se_intercept,
        "gap_mean": mean_g,
        "gap_sd": gap_sd,
        "rms": rms,
    }


# Water, near enough. The spa also has to warm its shell, its liner, the water standing
# in the pipes and the inner face of the cover, so what the fit measures is an *effective*
# thermal mass and reads a little above the nameplate volume. That is the expected
# direction, and it is why the check below is a sanity check rather than a calibration.
WATER_SPECIFIC_HEAT_J_PER_KG_K = 4186.0


def physical_constants(fit, heater_power_w):
    """Thermal mass and loss coefficient, from the fit and the heater's rated power.

    This is the part the spa spec makes checkable. `P/C` is fitted, `P` is known — it is
    configured for the energy sensors and, because rates are only ever learned in
    full-heat mode, it is unambiguously the mode-3 figure rather than the pre-heat one.
    So `C` follows, and with it an equivalent volume that can be held against the
    nameplate.

    **The heater alone, deliberately — not the circulation pump.** The pump does run for
    the whole of every traverse (the start sequence gates the heater on it), so counting
    it is tempting and was briefly done here. But it turns a pump; it does not heat the
    water. The motor is air-cooled in the control box, so most of its 60 W leaves as motor
    heat to the air, and only the hydraulic work is dissipated into the water as viscous
    heating — a fraction nobody here can put a number on. Assuming all of it would be
    asserting a figure rather than measuring one, for a 2.7% shift sitting inside a
    shell-mass systematic several times larger. If it is ever worth revisiting it needs a
    measurement, not an argument.

    **The volume is derived, never supplied.** Asking an owner to measure their tub would
    put a calibration error straight into every prediction, and the whole value of the
    number is that it is an independent check: a fit that implies 900 litres for a
    600-litre spa has something wrong with it that no amount of curve-fitting will show.
    It is a second falsification test alongside the equal-and-opposite one, and it comes
    free.

    One systematic remains, and it reads high: effective mass includes the shell, the
    liner, the water standing in the pipes and the inner face of the cover, none of which
    the nameplate volume counts. Tens of percent is a finding; single digits is not.
    """
    if not fit or not heater_power_w or heater_power_w <= 0:
        return None
    rate = fit.get("rate_at_zero_gap")
    tau_h = fit.get("tau_h")
    if not rate or rate <= 0 or not tau_h or tau_h <= 0:
        return None
    # rate is °C/h, so the per-second heat capacity needs the 3600.
    heat_capacity = 3600.0 * float(heater_power_w) / rate          # J/K
    loss_w_per_k = heat_capacity / (tau_h * 3600.0)                # W/K
    se = fit.get("rate_at_zero_gap_se")
    return {
        "heater_power_w": float(heater_power_w),
        "thermal_mass_j_per_k": heat_capacity,
        "equivalent_litres": heat_capacity / WATER_SPECIFIC_HEAT_J_PER_KG_K,
        # Carried through from the intercept, which is where all of it comes from.
        "equivalent_litres_se": (
            None if not se else (heat_capacity / rate) * se
            / WATER_SPECIFIC_HEAT_J_PER_KG_K),
        "loss_w_per_k": loss_w_per_k,
        # What the tub sheds sitting at a 20 °C gap — a January night with the water at
        # 38. Easier to sanity-check against a power meter than a coefficient is.
        "standing_loss_w_at_20c_gap": loss_w_per_k * 20.0,
    }


def newton_free_fit(observations):
    """Regress rate on water *and* air separately — the test the law can fail.

    Newton's law predicts the two coefficients come out equal and opposite, both equal
    to `−1/τ` and `+1/τ`. Nothing about a curve fit forces that; it either happens or the
    model is wrong. On 639 hours from the previous spa it came out −0.0161 against
    +0.0167, a ratio of 1.04.

    The catch is collinearity: over a single heat-up the water climbs while the air does
    whatever the afternoon does, and if they happen to move together the split between
    the two coefficients is not identified. `corr_water_air` is reported for exactly that
    reason — near ±1 it means the ratio is arithmetic, not evidence, however tight the
    standard errors look.
    """
    rows = _usable_rows(observations)
    n = len(rows)
    if n < NEWTON_FREE_MIN_N:
        return None
    mw = sum(w for w, _, _ in rows) / n
    ma = sum(a for _, a, _ in rows) / n
    mr = sum(r for _, _, r in rows) / n
    sww = saa = swa = swr = sar = 0.0
    for w, a, r in rows:
        dw, da, dr = w - mw, a - ma, r - mr
        sww += dw * dw
        saa += da * da
        swa += dw * da
        swr += dw * dr
        sar += da * dr
    det = sww * saa - swa * swa
    # The guard has to be *relative*. Perfectly collinear water and air give a
    # determinant that is zero in exact arithmetic and roughly 1e-9 in floating point,
    # because it is the difference of two large products — an absolute threshold sails
    # straight past it and returns coefficients of 1e-18 with complex standard errors.
    if sww <= 0 or saa <= 0 or det <= 1e-9 * sww * saa:
        return None
    corr = swa / ((sww * saa) ** 0.5)
    b_water = (saa * swr - swa * sar) / det
    b_air = (sww * sar - swa * swr) / det
    b0 = mr - b_water * mw - b_air * ma
    resid = [r - (b0 + b_water * w + b_air * a) for w, a, r in rows]
    dof = n - 3
    s2 = sum(e * e for e in resid) / dof if dof > 0 else None
    se_w = (s2 * saa / det) ** 0.5 if s2 and s2 > 0 else None
    se_a = (s2 * sww / det) ** 0.5 if s2 and s2 > 0 else None
    return {
        "n": n,
        "coef_water": b_water,
        "coef_air": b_air,
        "se_water": se_w,
        "se_air": se_a,
        # 1.0 is the law holding. Undefined rather than infinite where water has no
        # measured effect at all, which would itself be the finding.
        "ratio": (-b_air / b_water) if b_water < 0 else None,
        "corr_water_air": corr,
        # Whether the split between the two coefficients is determined at all. Past this
        # correlation the variance inflation is an order of magnitude and the ratio is
        # arithmetic rather than evidence, however tight the standard errors look — so it
        # is reported as unidentified rather than withheld, because a run of unidentified
        # fits is itself the finding that the traverses are not varied enough.
        "identified": abs(corr) <= NEWTON_MAX_CORR,
        "tau_from_water_h": (-1.0 / b_water) if b_water < 0 else None,
        "rms": (s2 ** 0.5) if s2 and s2 > 0 else None,
    }


def newton_heating_minutes(from_temp, to_temp, ambient, tau_h, asymptote_lift_c):
    """Minutes from `from_temp` to `to_temp` under the physical model.

    Newton's law integrates in closed form — no segmentation, no buckets, no bias:

        t = τ · ln((A − T_start) / (A − T_end)),   A = T_air + P/k

    Returns None when the model says the target is unreachable (`A` at or below it),
    which is a real answer and must not be silently turned into a large number: a spa
    that cannot reach 40 °C on a January night should decline to predict rather than
    promise a time it will miss.
    """
    if None in (from_temp, to_temp, ambient, tau_h, asymptote_lift_c):
        return None
    if tau_h <= 0 or from_temp >= to_temp:
        return None
    if (to_temp - from_temp) < NEAR_TARGET_BAND:
        return 0.0
    asymptote = ambient + asymptote_lift_c
    if asymptote <= to_temp:
        return None
    from math import log
    return 60.0 * tau_h * log(
        (asymptote - from_temp) / (asymptote - to_temp))


# ── Forecast-weighted ambient ────────────────────────────────────────────────
#
# A heat-up runs for hours and the instantaneous outdoor temperature describes one
# moment of it. Measured against exact hour-by-hour integration of a forecast, planning
# from the temperature at the start is out by +14% on an autumn morning and -10% on a
# winter night — and the sign flips with the time of day, so it is not a bias any scalar
# correction could absorb.
#
# The fix is a mean rather than an integration, and that is a property of the law rather
# than a shortcut. Newton's law is linear in air temperature, so the exact solution is an
# *exponentially weighted* average of it with time constant tau:
#
#     T(t) = T0·e^(-t/tau) + (1/tau) ∫ A(s)·e^(-(t-s)/tau) ds
#
# Two things follow. Where tau is long against the run — 37.8 h against 7-13 h on the spa
# this was written for — the weights span only about 0.7 to 1.0, and a plain mean lands
# within 0.2% of piecewise integration across an autumn morning, a winter night and a
# settled day. And the weight is largest for the hours nearest the *finish*, which is why
# the window is anchored there and extended backwards rather than centred.
FORECAST_MAX_SPAN_H = 24.0


def forecast_window_mean(rows, window_end, span_hours, *, max_span_h=FORECAST_MAX_SPAN_H):
    """Mean forecast temperature over the hours leading up to `window_end`.

    `rows` is [(datetime, temp)] in any order; `span_hours` is how far back to reach,
    capped at `max_span_h`. Anchored at the end because that is where the law puts the
    weight — see above.

    Falls back to the latest hours available when the forecast stops short of the window,
    which is the ordinary case rather than an edge one: met.no through Home Assistant
    offers 48 hours and a schedule may be set further out than that. Those hours are the
    closest thing to an answer that exists, and beat a single instantaneous reading taken
    a day and a half earlier.

    Returns (mean, n_hours, kind) or None. `kind` distinguishes the two, so a caller can
    record what it actually used rather than implying a precision it does not have.
    """
    if not rows or window_end is None or not span_hours or span_hours <= 0:
        return None
    from datetime import timedelta
    span = min(float(span_hours), max_span_h)
    ordered = sorted((t, v) for t, v in rows if t is not None and v is not None)
    if not ordered:
        return None
    start = window_end - timedelta(hours=span)
    # Half-open at the end. A forecast stamp marks the *start* of the hour it describes,
    # so the hours covering a run of `span` ending at `window_end` are those starting in
    # [start, window_end) — the stamp at `window_end` belongs to the hour after the run
    # finishes. Closed at both ends took one sample too many, giving a six-hour window
    # seven hourly readings and dragging the mean toward whatever preceded the run.
    inside = [v for t, v in ordered if start <= t < window_end]
    if inside:
        return (sum(inside) / len(inside), len(inside), "window")
    latest = ordered[-1][0]
    if window_end > latest:
        tail = [v for t, v in ordered if t > latest - timedelta(hours=span)]
        if tail:
            return (sum(tail) / len(tail), len(tail), "tail")
    return None
