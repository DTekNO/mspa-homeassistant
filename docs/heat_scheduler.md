# Heat Scheduler

> **Experimental feature.** Available from version X.X onwards.

The Heat Scheduler lets you tell the integration *when* you want the spa to be ready and *what temperature* to reach. It works out when to start heating and does it automatically — no timers, no manual intervention.

---

## Setup

Two controls appear in your device panel under **Configuration**:

| Control | Purpose |
|---------|---------|
| **Schedule target temperature** | The water temperature you want when the spa is ready |
| **Scheduled for** | The date and time you want the spa to be ready |

Set these before you go to bed. The integration handles the rest.

---

## Sensors

Two sensors tell you what's happening:

**Ready at** — answers *"Is my spa ready right now?"*

| Display | Meaning |
|---------|---------|
| `Ready` | The spa has reached its target temperature |
| `HH:MM` or `HH:MM +Nd` | A time — either the scheduled target time (the integration's built-in scheduler is waiting to start heating) or an estimated ready time based on the current heating rate |
| *(blank / Unknown)* | Spa is idle, no useful prediction available |

**Heat Schedule** — answers *"Is the integration's built-in scheduler handling it?"*

| Display | Meaning |
|---------|---------|
| `Not scheduled` | No schedule is set, or the scheduled time has passed |
| `Ready` | Spa is already within 1°C of the target — no pre-heating needed right now |
| `Start at 14:00` | The scheduler will start the heater at this time |
| `Start now` | Heater should be running — the scheduler has fired |

---

## Scenarios

### Scenario 1 — Cold start

**Situation:** The spa has been off for days. Water is at 20°C (ambient). You want it at 40°C for Saturday at 16:00.

**You set:** Schedule temperature = 40°C · Scheduled for = Saturday 16:00

| When | Ready at | Heat Schedule |
|------|----------|---------------|
| Monday — schedule set | `16:00 +5d` | `Start at 02:40 +5d` ¹ |
| Tuesday to Friday — spa cold | `16:00 +Xd` | `Start at HH:MM +Xd` |
| Friday night — automation starts heater | `16:00` | `Start now` |
| Saturday morning — spa heating up | `16:00` | `Start now` |
| **Saturday 16:00 — spa at 40°C** | **`Ready`** | `Not scheduled` |

¹ *On the very first use, Heat Schedule may show `Not scheduled` because the integration hasn't yet learned your spa's heating rate. `Ready at` still shows your scheduled time. After the first session completes, start times become accurate.*

**What the scheduler does:** At the calculated start time (Friday night in this example), it raises the temperature setting to 40°C and turns on the heater — automatically, with no action from you. At 16:00 on Saturday, it confirms the temperature setting is at 40°C.

---

### Scenario 2 — Energy saving, overnight cooling

**Situation:** You just used the spa at 39°C. You lower the thermostat to 35°C to save energy overnight. You want the spa back at 39°C for Sunday at 16:00.

**You set:** Schedule temperature = 39°C · Scheduled for = Sunday 16:00

| When | Ready at | Heat Schedule |
|------|----------|---------------|
| Saturday evening — schedule set (spa at 39°C) | `16:00 +1d` | `Ready` ¹ |
| Overnight — water cools to 37°C | `16:00 +1d` | `Start at 14:00` |
| Early morning — water cools to 35°C | `16:00` | `Start at 12:00` |
| 12:00 — automation starts heater | `16:00` | `Start now` |
| Spa heating up | `16:00` | `Start now` |
| **16:00 — spa at 39°C** | **`Ready`** | `Not scheduled` |

¹ *At the moment you set the schedule, the water is already at 39°C — equal to the schedule temperature. Heat Schedule shows `Ready` because no lead time is needed right now. It updates to a start time as the water cools overnight.*

**What the scheduler does:** At 12:00 it raises the temperature setting to 39°C and turns on the heater. At 16:00 it confirms the temperature setting is 39°C, even if the spa was already warm.

---

### Scenario 3 — Energy saving, warm day

**Situation:** Same as Scenario 2, but it's a warm summer day. The spa barely cooled — water is at 38.8°C, just 0.2°C below your 39°C target. No pre-heating is needed, but the automation still needs to confirm the settings at 16:00.

**You set:** Schedule temperature = 39°C · Scheduled for = 16:00

| When | Ready at | Heat Schedule |
|------|----------|---------------|
| Schedule set (spa at 39°C) | `16:00 +1d` | `Ready` |
| Barely cooled to 38.8°C | `16:00` | `Ready` |
| **16:00 — automation fires** | `16:00` | `Start now` |
| A few minutes later — spa at 39°C | **`Ready`** | `Not scheduled` |

**What the scheduler does:** Because the spa is within 0.5°C of the target, no heating lead time is calculated. The scheduler fires exactly at 16:00, confirms the temperature setting at 39°C, and turns on the heater briefly. The spa reaches 39°C within a few minutes and `Ready at` shows `Ready`.

> **Note:** The temperature setting is always confirmed at the scheduled time — even when the spa was already warm and no heating was needed. This ensures the thermostat is set correctly for the session regardless of what happened earlier in the day.

---

## How it learns

The integration uses an **adaptive machine learning model** to predict heating and cooling times. Rather than a fixed rate, it updates continuously from observed data and corrects for changing conditions — ambient temperature, season, cover on or off, water fill level — without any configuration on your part.

### Online learning with exponential smoothing

During every heating run the integration samples the actual rate (°C/h) once the water has moved at least 0.5°C and at least 3 minutes have elapsed. Each sample is fed into an **exponential moving average (EMA)** with smoothing factor α = 0.25:

```
new_rate = 0.25 × observed_rate + 0.75 × stored_rate
```

Recent observations carry more weight while the full session history is retained. Outliers — rates below 0.05°C/h (sensor noise) or above 3.0°C/h (e.g. adding water) — are rejected before they can distort the model. After 4–5 sessions the estimate has settled; it continues to adapt gradually as your conditions change over the seasons.

### Temperature-segmented rates

A spa does not heat at a constant rate across its full temperature range. Heat loss increases with water temperature, so the effective rate slows as the water warms. The model maintains **three independent EMA estimates**, one per temperature band:

| Bucket | Range | Typical behaviour |
|--------|-------|-------------------|
| Cold | < 30 °C | Fastest — minimal heat loss to surroundings |
| Mid | 30–37 °C | Moderate — increasing thermal loss as water warms |
| Hot | ≥ 37 °C | Slowest — near setpoint, highest loss |

Each bucket is updated only by observations made in that temperature range. When predicting a long run (e.g. 20°C → 40°C), the range is split at 30°C and 37°C, each segment is estimated at its own learned rate, and the results are summed. This gives substantially better accuracy for long runs compared to a single flat rate.

### Real-time ambient correction

Outdoor conditions can shift the effective heating rate by 20–40% between a cold winter morning and a warm summer afternoon. The model detects this within the first few minutes of any heating session.

As soon as the first bucket receives a live observation, the ratio of observed rate to the stored base rate is computed. This ratio is smoothed into a **session scalar** and immediately applied to any bucket that has not yet been directly observed in the current session. The result: predictions adapt to today's ambient conditions before the spa has even passed through the full temperature range.

### Historical bias correction

EMA samples carry noise, and the model can develop a systematic over- or under-prediction tendency. The integration tracks the last 10 completed heating sessions (estimated vs actual duration) and derives a **prediction bias** scalar from the mean ratio of actual to estimated time, clamped to [0.5, 2.0].

If a weather entity is configured, historical sessions are weighted by **Gaussian kernel similarity** over ambient temperature and wind speed — sessions from similar weather conditions count more than sessions from very different conditions. This makes the bias correction contextual rather than a flat average.

The bias is applied as a final multiplier to the segmented estimate:

```
final_prediction = segmented_estimate × prediction_bias
```

A bias above 1.0 means past predictions were too optimistic (actual heating took longer) and future estimates are stretched accordingly. Below 1.0 the model has been conservative and estimates are compressed.

### Seasonal adaptation

Stored bucket rates decay gradually toward the global flat EMA over time — approximately 2% per day without a weather entity, 0.6% per day with one (since the weather kernel already handles ambient differences). The floor is 40% of the original weight. After two weeks of inactivity stored rates still carry roughly 75% weight, so a summer rate does not permanently anchor winter predictions.

### Cold start

On a fresh install there is no learned data. The integration seeds the heating estimate from the `device_heat_perhour` diagnostic value some MSpa models report, clamped to 0.5–2.0°C/h. Models that report zero have no seed — the sensor shows unavailable until the first real run completes. After 2–3 full sessions the learned rates outperform the device seed and the temperature-bucket model begins to fill in.

---

## Tips

- **Set the schedule temperature to what you actually want to bathe at.** The integration heats to that temperature, not the spa's current thermostat setting.
- **You can change the scheduled time at any time.** Updating `Scheduled for` resets the automation and clears the `Ready` state so the scheduler re-arms for the new time.
- **`Ready at` is the sensor to watch.** It shows `Ready` when the spa is genuinely at temperature for your session — regardless of what the automation is doing in the background.
