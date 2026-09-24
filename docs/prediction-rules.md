# Rules for the prediction model

Constraints every change must be checked against. They exist because this model has
repeatedly been improved into something worse: each correction was reasonable on its own,
and together they produced a scheduler that could be three hours wrong and not know it.

A simple model that is a few minutes out beats an elaborate one that is occasionally
hours out and cannot be reasoned about. **Accuracy is not the goal; predictable accuracy
is.**

---

## R1 — One equation, no corrections outside it

```
dT/dt = A − (T_water − T_air) / τ
```

Outdoor temperature is a **term**, not a correction applied afterwards. Anything that
would multiply, scale or bias the result of this equation is forbidden.

*Rejected by this rule:* `ambient_rate_factor`, `learned_ambient_factor`, the session
scalar, `prediction_bias`, per-band sensitivities.

*Why:* every one of those existed to patch a model with no air term. They interacted,
they had a precedence order, and two of them could cancel each other while both were
"working".

## R2 — Learn from heating only

Both parameters must be derivable from a heat-up. Cooling data may be used if it happens
to be there, but nothing may **depend** on it.

*Why:* most users never let the tub cool for days. A model that needs a cooling curve to
calibrate is a model that never calibrates on a normal installation.

*Consequence:* `τ` comes from the slope of rate against gap across a run's crossings.

## R3 — One method, used everywhere

The scheduler, the live estimate and every replan call the same function with the same
parameters. There is no separate scheduling path.

*Test:* at the instant a schedule hands over to heating, the predicted finish must not
move. A jump at handover means two methods exist — or, as on 10.09.2026, one method
being fed two different answers to "how warm is the water".

That case is worth keeping as the worked example. The scheduler planned from 18.0 °C,
extrapolated down through a thirteen-hour dwell. The moment the heater fired,
`scheduling_temp`'s guard on a stale *direction* discarded the whole extrapolation and
returned the raw 18.5 reading, and the predicted finish jumped 24 minutes earlier. The
water had not moved; only the direction it was about to move in had. The fix carries the
position across the transition and refreshes only the direction.

## R4 — No frozen plans, no deferred revisions

Predictions re-derive from current water and current air on every poll.

*Why:* freezing existed because bucket rates were stale by construction. The band-edge
revision that went with it is what allowed a start time to be committed and then found
three hours wrong eleven hours later, far too late to act on.

*The one hold, and why it is not this:* R12 holds the displayed estimate for the ~120
minutes before a run has measured anything. The frozen plan this rule forbids held a
*measured* rate steady and revised it on a schedule; R12's hold ends the instant there
is a measurement, and revises on evidence rather than at a band edge. Where R4 forbids
holding a number that could be improved, R12 forbids publishing one that cannot.

## R5 — Plan with the forecast, looking ahead

A prediction covering future hours uses the forecast for **those** hours, not the
temperature outside now.

Under the thermal model this is a forward walk through the forecast's own blocks, each
integrated in closed form at its own temperature and the water carried into the next.
Boundaries are solved inside a block rather than apportioned across one: a run finishing
ninety minutes into a six-hour block is charged ninety minutes of it, and a run beginning
two hours into a block is charged the four that remain. If a bucket-style model is ever
used again, each band must be priced the same way — with the forecast for the period that
band is expected to occupy, walked forward, not averaged once over the whole run.

A forecast that does not reach the finish returns no answer rather than an extrapolated
one (R9), and a forecast whose first usable block is more than a couple of hours away is
treated as no forecast at all — holding one instantaneous reading across a long gap is
the failure this rule exists to prevent.

*Why:* an overnight run starting at 22:00 and finishing at 09:00 is planned while the air
is still falling and finishes after dawn. The reading at commit time describes neither
end.

## R6 — A parameter only updates when the evidence supports it

Specifically: **`τ` updates only from a run whose crossings span at least
`TAU_MIN_GAP_SPREAD_K` of water/air gap.**

*Why this exact rule:* fitted on the first 8 crossings of the 11.09.2026 run — a 4.9 K
spread — the slope gives τ = 18 h. The same run's full 40 crossings, spanning 17.2 K,
give 65.6 h, which agrees with the cooling measurement. A short lever produces a
confident, wrong answer, and the failure is silent.

`A` has no such restriction: it is the intercept, and one crossing constrains it.

## R7 — Bad samples are discarded, never clamped

A sample outside the physical bounds is dropped and the estimate left alone.

*Why:* clamping lets a stream of bad samples settle on the boundary and stay there, which
reads as a converged estimate rather than as a failure.

## R8 — Seeds are measurements, and are replaced outright

A new installation starts from values measured on a real spa, so it predicts sensibly
from the first heat-up. The first valid observation **replaces** the seed rather than
being blended toward it.

*Why:* a spa genuinely unlike the seed should converge immediately, not crawl.

## R9 — When the model cannot answer, it says so

No target beyond the asymptote is given a number. No missing outdoor reading is filled in
with a guess.

*Exception, deliberate:* the user-facing estimate may fall back to a simpler model rather
than go blank, because a blank Ready at reads as broken. The fallback must be logged, and
it must be visible in diagnostics.

## R10 — Two parameters. Adding a third requires evidence, not intuition

Any proposed new parameter must be shown to reduce error on **recorded sessions** before
it is added. Wind, humidity, solar and cover state are all plausible and none of them are
in the model.

*Why:* every term currently being removed was added because it was plausible.

## R11 — A measurement must be longer than the noise in it

Specifically: **a rate is learned from a chord of at least `THERMAL_CHORD_MIN_C`
(1.5 °C), measured from a held anchor, and chords do not overlap.**

*Why this exact rule:* the reading is quantised to 0.5 °C, so a single crossing is a rise
known only to ±0.25 °C over 25–30 minutes — ±50% on the rate, blended into `A` at
`A_ALPHA` and republished immediately. Replaying 11.09.2026, the first such chord read
`A` = 1.46 against the run's settled 1.25, and opened the estimate 206 minutes fast.
Over 1.5 °C the same quantisation is ±17%.

*What it rejects:* re-anchoring at every crossing, which is three rates each measured
over one band; and nesting, where a held anchor emits a point at every crossing so each
one shares nearly all its data with the last. The bucket window does the second, and
also re-anchors at every zone edge — so the short-chord noise it holds its anchor to
avoid was re-injected four times a run. Scored over 11.09 and 03.09: mean absolute error
38/41 min → 25/28, worst single step 169/137 min → 22/27.

*Why 1.5 and not 2.0 or 3.0:* those score better still (20/25 and 19/22 MAE) and were
rejected because the same points feed the end-of-run `τ` fit. 1.5 °C leaves 13 and 12
points on the two runs, clearing `TAU_MIN_POINTS = 10`; 2.0 °C leaves 10 and 9, so the
shorter run would silently stop learning `τ`. Buying three minutes of error by disabling
half the model is not a trade — and it is the same failure mode R6 exists to prevent.

## R12 — Do not publish an estimate that has measured nothing

Two parts, and they are the same idea at two ends of a run.

**Learning starts at an observed position.** A run opens somewhere inside a 0.5 °C band
and that position is never observed, so any rate measured from it spans an unknown
distance. The first three crossings after the heater starts are not learned from: two are
discarded — the probe sits in the pump housing and the first band after heater-on runs about
1.6× the settled rate, which put the first estimate of 17.09.2026 four hours early — and the
third anchors the chord.

**The displayed estimate is held until the first chord completes** — about two hours, five crossings.
Before that the plan rests on the seed and on a position known to within half a band.
Republishing it at every crossing shows movement where there is no new information: the
estimate walks because the water advanced against a rate that has not changed, and a
reader cannot tell that from a genuine revision. Released when the first chord of *this run* completes — every run, not only the first: a
carried-over `A` does not make the opening honest, because the position at heater-on is
still a band and the first bands after heater-on run hot.

*Why:* holding also bounds the correction. Replaying 03.09.2026 with the estimate free,
the seed plan and the first fitted plan disagreed by 123 minutes and the display crossed
that in one step. Held, the same run steps 27 minutes. Mean absolute error is unchanged
on both runs, so what this buys is specifically the worst case.

*Related, and the same principle applied to position:* an extrapolation within the band
is trusted for `_ANCHOR_MAX_AGE_BANDS` times the time a band should take at the rate
being extrapolated, and past that the reported reading is used. On 10.09.2026 an anchor
from 02:39 was still being projected at 18:50 — thirteen of those sixteen hours spent
clamped to the band floor — and the plan was built on a position 0.9 °C too cold. A plain
age cap is wrong here: crossings during a genuine cooling dwell are around three hours
apart, so any cap short enough to catch sixteen hours also fires inside an ordinary
dwell. What separates the cases is how far past its own prediction the model has run.


---

## Checking a change against these

1. Does it add a correction outside the equation? → R1
2. Does it need cooling data? → R2
3. Does it give the scheduler its own arithmetic? → R3
4. Does it hold a value that current data contradicts? → R4
5. Does it use the temperature now for a prediction about later? → R5
6. Does it update a parameter from evidence too weak to support it? → R6
7. Does it clamp? → R7
8. Does it blend away a seed? → R8
9. Does it invent a number rather than decline? → R9
10. Does it add a parameter? → R10
11. Does it learn from a span no longer than the noise in it? → R11
12. Does it publish a number the run has not measured? → R12
