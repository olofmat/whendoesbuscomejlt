# Bus 121 data collector (Jönköping / JLT)

This collects a few days of raw data about JLT bus 121 so we can work out
when the 07:30 bus from Knektaparken *really* arrives. JLT's own predictions
are unreliable, so the script saves the raw material (GPS positions, the
timetable and JLT's predictions) instead of a single predicted time.

All data comes from **Trafiklab** (run by Samtrafiken) and is **free**.

---

## 1. Get the two API keys

You need two keys. Both are free and come from the same account.

| Key | What it gives | Needed? |
|---|---|---|
| **GTFS Regional** | Timetable (static GTFS) + live feeds: bus GPS positions every few seconds and JLT's predictions for every trip | **Yes** (the main data) |
| **Trafiklab Realtime APIs** | Easy JSON: departure board for Knektaparken and every stop of a given trip | Recommended (handy for cross-checking) |

### Steps

1. **Create an account** at <https://developer.trafiklab.se/register>.
   You can use email or GitHub. Confirm the email address from the link they send.
2. **Create a project** at <https://developer.trafiklab.se/project/list>.
   Click the purple **New project** button, give it a name (e.g. "Bus 121"),
   choose a type and write a short description. Save.
3. **Add the first key.** In the project, choose **GTFS Regional** in the
   API dropdown and click **Add API key to project**.
4. **Add the second key.** Choose **Trafiklab Realtime APIs** in the dropdown and add that too.
5. Copy both keys into a file called `.env` next to `collect.py`.
   Start from `.env.example`:

   ```
   GTFS_REGIONAL_KEY=paste-gtfs-regional-key-here
   TRAFIKLAB_REALTIME_KEY=paste-realtime-api-key-here
   ```

   Keep `.env` private and don't commit it. It's already in `.gitignore`.

New keys start on the **Bronze** level, which is enough for this. If you ever
need more, click **Upgrade quota** next to the key. That's free too and takes
1 to 2 workdays.

### Limits (Bronze)

| API | Per minute | Per month |
|---|---|---|
| GTFS Regional, live feeds | 50 | 30,000 |
| GTFS Regional, timetable zip | 10 | **50** |
| Trafiklab Realtime APIs | 25 | 100,000 |

The default settings use about **1,450 live-feed calls a day** (~7,200 for
5 days), **1 timetable download a day**, and roughly **1,000 Realtime API calls a
day**. That's well inside every limit.

---

## 2. What each source contains

### GTFS Regional (the main data)

| Feed | URL (`jlt` = JLT) | Updated | Contents |
|---|---|---|---|
| Timetable | `https://opendata.samtrafiken.se/gtfs/jlt/jlt.zip?key=KEY` | daily, ~03–07 | All routes, stops, trips and scheduled times (CSV files in a zip) |
| VehiclePositions | `https://opendata.samtrafiken.se/gtfs-rt/jlt/VehiclePositions.pb?key=KEY` | ~every 2 s | Every bus: lat/lon, trip id, current stop, timestamp |
| TripUpdates | `https://opendata.samtrafiken.se/gtfs-rt/jlt/TripUpdates.pb?key=KEY` | every 15 s | JLT's predicted arrival/departure time and delay per stop |
| ServiceAlerts | `https://opendata.samtrafiken.se/gtfs-rt/jlt/ServiceAlerts.pb?key=KEY` | every 15 s | Disruption messages (not collected) |

The live feeds are binary (Protocol Buffers, "GTFS Realtime"). The script
decodes them for you.

**The GPS positions are the key to good predictions.** They show where the
bus actually is, so we can build our own arrival estimate instead of copying
JLT's number.

### Trafiklab Realtime APIs (JSON)

| What | URL |
|---|---|
| Find a stop | `https://realtime-api.trafiklab.se/v1/stops/name/Knektaparken?key=KEY` |
| Departures (next 60 min) | `https://realtime-api.trafiklab.se/v1/departures/{area_id}?key=KEY` |
| Departures from a time | `https://realtime-api.trafiklab.se/v1/departures/{area_id}/2026-11-25T07:20?key=KEY` |
| Arrivals | same as departures with `/arrivals/` |
| Whole trip (beta) | `https://realtime-api.trafiklab.se/v1/trips/{trip_id}/{start_date}?key=KEY` |

Each departure has `scheduled`, `realtime`, `delay` (seconds), `canceled`,
`is_realtime`, `route.designation` (e.g. `121`), platform and
`trip.trip_id`/`trip.start_date`. The trip endpoint lists every stop with
scheduled and predicted arrival/departure. Results are cached for 60 s, so
polling faster than once a minute gives nothing new. When `is_realtime` is
false, `realtime` is just the timetable time.

You can paste any of these URLs (with your key) into a browser to see the data.

---

## 3. Run the collector

Uses [uv](https://docs.astral.sh/uv/). Install it once if you don't have it:
`curl -LsSf https://astral.sh/uv/install.sh | sh` (macOS/Linux) or
`powershell -c "irm https://astral.sh/uv/install.ps1 | iex"` (Windows).
uv fetches Python 3.10+ and the dependencies by itself (from `pyproject.toml` / `uv.lock`).

```bash
git clone https://github.com/olofmat/whendoesbuscomejlt.git
cd whendoesbuscomejlt
uv sync                      # create .venv and install dependencies
uv run collect.py --once     # quick test: one round, then exit
uv run collect.py            # run for real; Ctrl-C to stop
```

`--once` should log a line like `VehiclePositions: 180 vehicles, 3 on line 121`.
If it logs HTTP 401/403, check that the key is pasted correctly and that it was
added for the right API.

**It must keep running for ~5 days.** Use a computer that stays on and
doesn't sleep, such as a desktop, a Raspberry Pi or a cheap VPS. It's safe to
stop and restart: everything is appended, nothing is overwritten.

To keep it running after you close the terminal (macOS/Linux):

```bash
nohup uv run collect.py > /dev/null 2>&1 &
```

### Polling schedule (change in `.env` if you like)

| Setting | Default | Meaning |
|---|---|---|
| `BUSY_START` / `BUSY_END` | `06:00` / `10:00` | The "busy window" around your morning trip |
| `GTFS_RT_BUSY_SECONDS` | 30 | Live feeds in the busy window |
| `GTFS_RT_QUIET_SECONDS` | 300 | Live feeds the rest of the day |
| `REALTIME_API_SECONDS` | 60 | Departure board + trips (busy window only) |
| `LINE` / `STOP_NAME` | `121` / `Knektaparken` | What to filter on |
| `AREA_ID` | (looked up) | Set this if the stop lookup picks the wrong Knektaparken |

---

## 4. What you get

Everything goes into `data/`:

| File | What |
|---|---|
| `static/YYYY-MM-DD_jlt.zip` | The timetable for each day |
| `raw/DATE/HOUR/vp_*.pb.gz`, `tu_*.pb.gz` | **Every** live snapshot for all of JLT, untouched (~100–150 MB/day) |
| `vehicle_positions_121.csv` | Line 121 only: time, vehicle, trip, lat/lon, speed, current stop |
| `trip_updates_121.csv` | Line 121 only: JLT's predicted time and delay for each stop, each poll |
| `departures_knektaparken.jsonl` | Departure board every minute in the busy window |
| `trips_121.jsonl` | Full stop-by-stop data for each 121 trip seen at Knektaparken |
| `stop_lookup.json` | Which stops matched "Knektaparken" (check it's the right one) |
| `collect.log` | What happened, including any errors |

After ~5 days, share the `data/` folder (or at least the CSV/JSONL files and
one `static/*.zip`) and we'll analyse it. We'll compare JLT's predictions with
when the bus actually passed each stop (from GPS), and find a better way to
predict the 07:30 arrival.

Data licence: GTFS Regional is CC0. The Realtime APIs are CC-BY 4.0
(credit Trafiklab.se).
