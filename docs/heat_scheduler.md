# Heat Scheduler — walkthrough

> **Experimental feature.** Available from version 2026.7.1 onwards.

The Heat Scheduler lets you tell the integration *when* you want the spa to be ready and *what temperature* to reach. It works out when to start heating and does it automatically — no timers, no automations, no manual intervention.

This page walks through what the sensors actually display at each stage of a session. For the algorithm behind the predictions, see [How the model learns](../README.md#how-the-model-learns) in the README.

---

## Setup

Two controls appear in your device panel under **Configuration**:

| Control | Purpose |
|---------|---------|
| **Schedule target temperature** | The water temperature you want when the spa is ready |
| **Scheduled for** | The date and time you want the spa to be ready |

Set them whenever you know your next session — minutes ahead or days ahead, both work. The integration handles the rest.

---

## Sensors

Two sensors tell you what is happening:

**Ready at** — answers *"is my spa ready, and if not, when?"*

| Display | Meaning |
|---------|---------|
| `Ready` | The spa is at the temperature that matters for this session |
| `HH:MM` or `HH:MM +Nd` | A time — either the scheduled ready time (the scheduler is waiting to start) or a live estimate once heating is under way |
| `unavailable` | Nothing is heating, the spa is not at target, and no useful prediction exists |

**Heat Schedule** — answers *"what is the scheduler doing about it?"*

| Display | Meaning |
|---------|---------|
| `Not scheduled` | No schedule set, or the scheduled time has passed |
| `Scheduled +14d` | A schedule exists but is beyond the lookahead horizon (default 5 days) — too far out for a start time |
| `Ready` | The spa is already at the scheduled temperature — no lead time needed right now |
| `Start at 14:00` | The scheduler will start the heater at this time |
| `Heating` | The scheduler has fired and the spa is working toward the target |
| `Start now` | The start moment has arrived but the heater has not been commanded yet — normally invisible (see below) |

Both sensors derive `Ready` from the same function, so they always agree — one can never claim the spa is ready while the other is still counting down.

> **`Not scheduled` means you forgot.** It is deliberately distinct from `Scheduled +Nd`, which means a schedule exists but is too far out to plan a start time for yet. If you set sessions weeks ahead, an empty dashboard tile genuinely tells you the calendar entry is missing.

> **You will rarely see `Start now`.** The scheduler fires during the same update cycle in which the start moment is detected, so by the time the sensor renders it has already moved to `Heating`. Seeing `Start now` persist means the start command did not get through — worth checking the log for an API error, since the integration will retry on the next poll.

---

## Scenarios

### Scenario 1 — Cold start

**Situation:** The spa has been off for days. Water is at 20 °C (ambient). You want it at 40 °C for Saturday at 16:00.

**You set:** Schedule temperature = 40 °C · Scheduled for = Saturday 16:00

| When | Ready at | Heat Schedule |
|------|----------|---------------|
| Monday — schedule set | `16:00 +5d` | `Start at 02:40 +5d` ¹ |
| Tuesday to Friday — spa cold | `16:00 +Xd` | `Start at HH:MM +Xd` |
| Friday, start moment reached | `16:00` | `Heating` |
| Saturday morning — spa heating | live ETA, e.g. `15:40` | `Heating` |
| **Saturday 16:00 — spa at 40 °C** | **`Ready`** | `Ready` → `Not scheduled` ² |

¹ *On the very first use Heat Schedule may show `Not scheduled` because no heating rate has been learned yet. Ready at still shows your scheduled time. After the first completed session the start times become meaningful.*

² *Both sensors show `Ready` together. Once the scheduled time itself passes, Heat Schedule returns to `Not scheduled` — its job is done. Ready at keeps showing `Ready` while the spa is at temperature.*

**What the scheduler does:** at the calculated start time it raises the temperature setting to 40 °C and turns on the heater, with no action from you. Note that the start time is *recalculated on every poll* until it fires — if the water cools further while waiting, the start moves earlier to compensate.

---

### Scenario 2 — Energy saving, cooling and recovering

**Situation:** You have just used the spa at 39 °C. You lower the thermostat to 35 °C to save energy. You want it back at 39 °C for tomorrow at 16:00.

**You set:** Schedule temperature = 39 °C · Scheduled for = tomorrow 16:00

| When | Ready at | Heat Schedule |
|------|----------|---------------|
| Schedule set (spa still at 39 °C) | `Ready` | `Ready` ¹ |
| Water cools to 37 °C | `16:00 +1d` | `Start at 14:00 +1d` |
| Water cools to 35 °C | `16:00 +1d` | `Start at 12:00 +1d` ² |
| 12:00 — start moment reached | `16:00` | `Heating` |
| Spa heating | live ETA | `Heating` |
| **16:00 — spa at 39 °C** | **`Ready`** | `Ready` → `Not scheduled` |

¹ *The water is already at the scheduled temperature, so no lead time is needed and both sensors say `Ready`. This updates to a start time as the water cools.*

² *The start time moving earlier as the water cools is the scheduler working correctly, not drifting. Each recalculation uses the current water temperature and the learned rate for the buckets it must pass through — plus, if a weather entity is configured, a correction for how cold it is outside right now.*

**What the scheduler does:** at 12:00 it raises the temperature setting to 39 °C and turns on the heater. At the scheduled time it confirms the setting is 39 °C, even if the spa was already warm.

---

### Scenario 3 — Warm day, no heating needed

**Situation:** As Scenario 2, but it is a warm day and the spa barely cooled — water is at 38.8 °C, just 0.2 °C below your 39 °C target. No pre-heating is needed, but the settings still need confirming at the scheduled time.

**You set:** Schedule temperature = 39 °C · Scheduled for = 16:00

| When | Ready at | Heat Schedule |
|------|----------|---------------|
| Schedule set (spa at 39 °C) | `Ready` | `Ready` |
| Barely cooled to 38.8 °C | `Ready` | `Ready` ¹ |
| **16:00 — scheduler fires** | `Ready` | `Not scheduled` ² |

¹ *Within 0.5 °C of the scheduled temperature counts as ready, so no heating lead time is calculated and the scheduler simply waits for the scheduled time.*

² *Because no lead time was needed, the start moment and the scheduled time are the same instant — so Heat Schedule goes straight from `Ready` to `Not scheduled` without passing through `Heating`.*

**What the scheduler does:** it fires exactly at 16:00, confirms the temperature setting at 39 °C and turns the heater on briefly. The spa reaches 39 °C within a few minutes.

> **Note:** the temperature setting is always confirmed at the scheduled time — even when the spa was already warm and no heating was needed. This ensures the thermostat is correct for the session regardless of what happened earlier in the day.

---

## Which temperature is the sensor talking about?

A subtlety worth understanding: the **schedule target temperature** and the **thermostat set-point** are two different numbers, and they are often different while a schedule is pending (Scenario 2 is exactly this — thermostat at 35 °C, schedule targeting 39 °C).

Ready at resolves this by identifying its context first:

| Context | Situation | Ready at refers to |
|---------|-----------|--------------------|
| **Schedule pending** | A schedule is set but has not fired | The **schedule** temperature and its scheduled time |
| **Scheduled heating** | The schedule has fired | The **schedule** temperature, with a live ETA |
| **Free** | No schedule set | The **thermostat** set-point |

This is why the sensor can read `Ready` while the thermostat is set well below the schedule temperature — it is answering "ready for your session", not "at the thermostat set-point".

---

## Tips

- **Set the schedule temperature to what you actually want to bathe at.** The integration heats to that temperature, not the spa's current thermostat setting.
- **You can change the scheduled time at any time.** Updating **Scheduled for** re-arms the scheduler for the new time and clears any latched `Ready` state.
- **Ready at is the sensor to watch.** It shows `Ready` when the spa is genuinely at temperature for your session, regardless of what the scheduler is doing in the background.
- **Configure a [weather entity](../README.md#optional-weather-entity)** if you want the start time to account for outdoor conditions before any of today's heating data exists. It matters most on cold, windy days, when the final approach to set-point slows dramatically.
- **Give it a few sessions.** Start times on a fresh install lean on a device-reported rate seed or nothing at all; after 2–3 completed runs the learned rates take over and accuracy improves markedly.
