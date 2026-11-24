# Analysis guide: when does the 07:30 bus 121 really arrive?

This file is written for whoever runs the analysis (a person, or Claude Code on
the laptop where `data/` lives). Read `README.md` first for what the collector
saves.

## Goal

Predict, as early as possible on a weekday morning, **when bus 121 that is
scheduled to leave Knektaparken at 07:30 will actually arrive at
Knektaparken**, more accurately than JLT's own real-time prediction. Olof wants
to know when to walk out to the stop so he doesn't wait long, and without
missing the bus. JLT's predictions are known to be poor, so don't copy them.
Measure them, then beat them.

Only the arrival at **Knektaparken** matters. Later stops are not a target, but
the stops *before* Knektaparken are useful signals for where the bus is and how
late it's running.

Missing the bus is much worse than waiting a few minutes, so the errors aren't
symmetric. Besides average error, report how early you need to be at the stop
to catch the bus e.g. 9 or 19 days out of 20, and how much waiting that costs.

## Deliverables

Put everything under `analysis/` and commit it, so the cloud project can read
the results from GitHub:

| Path | What |
|---|---|
| `analysis/FINDINGS.md` | Plain-language results: what the data shows, how bad JLT's predictions are, which method works best and how well, and recommended next steps. Lead with the answer. |
| `analysis/figures/*.png` | Plots referenced from FINDINGS.md |
| `analysis/*.py` | The analysis code, runnable with `uv run analysis/<name>.py` |
| `analysis/derived/*.csv` | Small derived tables (e.g. actual arrival times at each stop). Keep each under ~5 MB. |

**Don't commit `data/`.** It's large, and it's in `.gitignore`.

Tooling: use uv. Add analysis libraries to a dependency group, e.g.
`uv add --group analysis pandas matplotlib`, and run with
`uv run --group analysis ...`. Follow `CLAUDE.md` (Ruff, 80 columns).

## The data

All times are local (Europe/Stockholm) ISO strings unless noted.

| Source | Use it for | Notes |
|---|---|---|
| `data/static/YYYY-MM-DD_jlt.zip` | Schedule: `routes.txt`, `trips.txt`, `stop_times.txt`, `stops.txt`, `calendar_dates.txt`, `shapes.txt` | Use the zip from the same day as the data. `stop_times` times can exceed 24:00:00. |
| `data/vehicle_positions_121.csv` | **Ground truth**: where each 121 bus was, about every 30 s in 06:30–10:00 and every 5 min otherwise | `vehicle_time` is when the GPS fix was taken. Use it, not `fetched_at`. The same fix can repeat across polls, so de-duplicate on (vehicle_id, vehicle_time). |
| `data/trip_updates_121.csv` | JLT's predictions: one row per (poll, trip, stop) | `arrival_time` is the predicted (or, for passed stops, often the actual) time. `arrival_delay_s` may be empty when only a time is given. |
| `data/departures_knektaparken.jsonl` | What the public departure board said, every minute | `realtime`, `delay`, `is_realtime` per departure. `realtime == scheduled` when `is_realtime` is false. |
| `data/trips_121.jsonl` | Realtime API's stop-by-stop view of each 121 trip | A second copy of JLT's predictions, useful for cross-checking |
| `data/raw/DATE/HOUR/{vp,tu}_*.pb.gz` | **All** JLT vehicles and trip updates, untouched | Use these for other lines (buses sharing the road before 121) |
| `data/collect.log` | Gaps and errors | Check for downtime before trusting a day |

Reading a raw snapshot:

```python
import gzip
from google.transit import gtfs_realtime_pb2

msg = gtfs_realtime_pb2.FeedMessage()
msg.ParseFromString(gzip.open(path).read())
for e in msg.entity:
    if e.HasField("vehicle"):
        v = e.vehicle  # v.trip.trip_id, v.position.latitude, v.timestamp
```

## Suggested steps

### 1. Sanity check

- Days covered, hours covered, and gaps per day (from `collect.log` and the
  snapshot timestamps).
- Count line 121 trips per day, and check that GPS points exist for them.
- Check `data/stop_lookup.json` to confirm the right Knektaparken was used.
- Put a short "data quality" section in FINDINGS.md.

### 2. Identify the target trip each day

From the static GTFS, find the line 121 trip with a scheduled **departure
of 07:30:00 at Knektaparken**. Check `stop_name` in `stops.txt`, since a stop
can have several `stop_id`s (platforms). Use `calendar_dates.txt` to confirm it
runs that day. Note its `trip_id`, direction and stop sequence. Also find the
trips just before it (the same line in the same direction, and other lines that
share its road) to use as signals.

### 3. Build ground truth: when did the bus actually arrive?

GPS is the truth. For each trip and stop:

- Get stop coordinates from `stops.txt`.
- Take the bus's GPS track for that trip and find when it was closest to the
  stop. Interpolate between fixes, using `shapes.txt` distance-along-route if
  it helps. Flag stops the bus passed between two fixes that are far apart.
- Cross-check against `current_status == STOPPED_AT` / `current_stop_sequence`
  changes, and against TripUpdates rows for stops already passed.

Knektaparken is the target. The upstream stops are what you need for the
GPS-based methods in step 5. Save the result as
`analysis/derived/actual_arrivals.csv` (date, trip_id,
stop_sequence, stop_id, stop_name, scheduled, actual, method, quality flag).

### 4. Measure how wrong JLT is

For the target trip (and all 121 trips, for more samples), at every poll before
the bus arrived:

- Compute error = JLT predicted arrival minus actual arrival.
- Express it against **lead time** (minutes before the actual arrival).

Plot the error distribution by lead time (e.g. 30, 20, 10, 5 min before).
Compare with the plain timetable. Is JLT even better than the schedule?
Characterise the failure: constant offset, late updates, or jumps.

### 5. Try better predictions

Keep it simple: there are only about 5 days. Evaluate with **leave-one-day-out**
cross-validation, reporting mean absolute error and the 90th percentile at the
same lead times as in step 4. In rough order:

1. **Timetable**, and timetable plus a constant historical offset.
2. **GPS-based**: where the bus is now (distance along route), plus the median
   historical travel time from there to the target stop at that time of day.
3. **Upstream delay propagation**: the bus's current delay at its last stop,
   adjusted by how delay typically grows or shrinks over the remaining segments.
4. **Earlier buses as traffic sensors**: travel times of the previous 121 and
   other lines on shared segments in the last 15–30 min. If they were slow, this
   one will be too.
5. A small regression combining the above. Only do this if there's enough
   data, and say so honestly if there isn't.

End with a practical rule Olof can use, e.g. "check at 07:15; leave so you're
at Knektaparken N min before the predicted time". Give the miss rate and the
average wait for that rule, compared with trusting JLT's app.

### 6. Write FINDINGS.md

Lead with the answer in a few sentences, e.g. "At 07:15, method X predicts the
arrival at Knektaparken within ±N min 90% of the time, while JLT's prediction
is within ±M min."
Then add data quality, methods, plots and limitations (only 5 days, weather,
holidays). End with what to collect or build next. Commit and push.
