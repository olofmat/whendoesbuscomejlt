#!/usr/bin/env python3
"""
Collect raw JLT (Jönköpings länstrafik) data for a few days so we can analyse
how bus 121 really runs, instead of trusting JLT's own predictions.

What it saves (under ./data/ by default):

  static/YYYY-MM-DD_jlt.zip           GTFS timetable, once per day
  raw/YYYY-MM-DD/HH/vp_<ts>.pb.gz     every VehiclePositions snapshot (whole JLT)
  raw/YYYY-MM-DD/HH/tu_<ts>.pb.gz     every TripUpdates snapshot (whole JLT)
  vehicle_positions_121.csv           line 121 GPS positions, one row per snapshot
  trip_updates_121.csv                line 121 predictions, one row per stop
  departures_<stop>.jsonl             Realtime API departure board for the stop
  trips_121.jsonl                     Realtime API full trip (every stop) for 121

The raw .pb.gz files keep everything, so nothing is lost if we later want
another line or stop. The CSVs are just a convenient head start.

Keys are read from environment variables (or a .env file next to this script):
  GTFS_REGIONAL_KEY        key for "GTFS Regional"           (required)
  TRAFIKLAB_REALTIME_KEY   key for "Trafiklab Realtime APIs"  (optional)

Run:  uv run collect.py            (Ctrl-C to stop, safe to restart)
      uv run collect.py --once     (one round of everything, for testing)
"""

from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import logging
import os
import signal
import sys
import time
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from google.transit import gtfs_realtime_pb2

# ---------------------------------------------------------------- settings --

TZ = ZoneInfo("Europe/Stockholm")
OPERATOR = "jlt"
LINE = os.environ.get("LINE", "121")
STOP_NAME = os.environ.get("STOP_NAME", "Knektaparken")
AREA_ID = os.environ.get("AREA_ID")  # set to skip the stop lookup

# "Busy window": poll often. Outside it: poll rarely, to save quota.
BUSY_START = os.environ.get("BUSY_START", "06:00")
BUSY_END = os.environ.get("BUSY_END", "10:00")
GTFS_RT_BUSY_SECONDS = int(os.environ.get("GTFS_RT_BUSY_SECONDS", "30"))
GTFS_RT_QUIET_SECONDS = int(os.environ.get("GTFS_RT_QUIET_SECONDS", "300"))
REALTIME_API_SECONDS = int(os.environ.get("REALTIME_API_SECONDS", "60"))
# The Realtime API (departures/trips) is only polled in the busy window.

STATIC_URL = "https://opendata.samtrafiken.se/gtfs/{op}/{op}.zip?key={key}"
RT_URL = "https://opendata.samtrafiken.se/gtfs-rt/{op}/{feed}.pb?key={key}"
API = "https://realtime-api.trafiklab.se/v1"

log = logging.getLogger("collect")


# ----------------------------------------------------------------- helpers --

def load_dotenv(path: Path) -> None:
    """Minimal .env reader so no extra package is needed."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def now() -> datetime:
    return datetime.now(TZ)


def in_busy_window(t: datetime) -> bool:
    hhmm = t.strftime("%H:%M")
    return BUSY_START <= hhmm < BUSY_END


def safe_url(url: str) -> str:
    """URL with the key hidden, for log messages."""
    return url.split("key=")[0] + "key=***" if "key=" in url else url


def fmt_ts(epoch: int | None) -> str:
    if not epoch:
        return ""
    return datetime.fromtimestamp(epoch, TZ).isoformat(timespec="seconds")


class CsvLog:
    """Append-only CSV that writes its header once."""

    def __init__(self, path: Path, fields: list[str]):
        self.path, self.fields = path, fields
        new = not path.exists() or path.stat().st_size == 0
        self.f = path.open("a", newline="", encoding="utf-8")
        self.w = csv.DictWriter(self.f, fieldnames=fields)
        if new:
            self.w.writeheader()

    def write(self, rows: list[dict]) -> None:
        self.w.writerows(rows)
        self.f.flush()


def append_jsonl(path: Path, obj: dict) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


# --------------------------------------------------------------- collector --

class Collector:
    def __init__(self, out: Path, gtfs_key: str, rt_api_key: str | None):
        self.out = out
        self.gtfs_key = gtfs_key
        self.rt_api_key = rt_api_key
        self.http = requests.Session()
        self.http.headers["Accept-Encoding"] = "gzip"
        self.http.headers["User-Agent"] = "bus121-collector/1.0"

        (out / "static").mkdir(parents=True, exist_ok=True)
        (out / "raw").mkdir(parents=True, exist_ok=True)

        self.static_day: str | None = None
        self.line_trip_ids: set[str] = set()
        self.line_route_ids: set[str] = set()
        self.stop_names: dict[str, str] = {}

        self.area_id = AREA_ID
        self.stop_slug = STOP_NAME.lower().replace(" ", "_")
        self.seen_trips: dict[tuple[str, str], datetime] = {}

        self.vp_csv = CsvLog(out / f"vehicle_positions_{LINE}.csv", [
            "fetched_at", "feed_time", "vehicle_time", "vehicle_id", "trip_id",
            "start_date", "route_id", "direction_id", "lat", "lon", "bearing",
            "speed_mps", "current_stop_sequence", "stop_id", "stop_name",
            "current_status", "occupancy_status",
        ])
        self.tu_csv = CsvLog(out / f"trip_updates_{LINE}.csv", [
            "fetched_at", "feed_time", "update_time", "trip_id", "start_date",
            "route_id", "trip_relationship", "vehicle_id", "stop_sequence",
            "stop_id", "stop_name", "arrival_time", "arrival_delay_s",
            "arrival_uncertainty", "departure_time", "departure_delay_s",
            "stop_relationship",
        ])

    # ---- HTTP with retries ----
    def get(self, url: str, timeout: int = 30) -> requests.Response | None:
        for attempt in range(3):
            try:
                r = self.http.get(url, timeout=timeout)
                if r.status_code == 200:
                    return r
                if r.status_code == 429:
                    log.warning("Rate limited (429) on %s, backing off", safe_url(url))
                    time.sleep(60)
                    continue
                if r.status_code in (401, 403):
                    log.error("HTTP %s on %s: check that the key is right and "
                              "added for this API", r.status_code, safe_url(url))
                    return None
                log.warning("HTTP %s on %s", r.status_code, safe_url(url))
            except requests.RequestException as e:
                log.warning("Request failed (%s) on %s", e, safe_url(url))
            time.sleep(5 * (attempt + 1))
        return None

    # ---- static GTFS, once per day ----
    def ensure_static(self) -> None:
        today = now().strftime("%Y-%m-%d")
        if self.static_day == today:
            return
        path = self.out / "static" / f"{today}_{OPERATOR}.zip"
        if not path.exists():
            # Samtrafiken publishes new files roughly 03:00-07:00. Use the
            # newest file we have if today's download fails.
            log.info("Downloading static GTFS for %s", today)
            r = self.get(STATIC_URL.format(op=OPERATOR, key=self.gtfs_key), timeout=180)
            if r is not None:
                path.write_bytes(r.content)
            else:
                older = sorted((self.out / "static").glob(f"*_{OPERATOR}.zip"))
                if not older:
                    raise SystemExit("Could not download static GTFS and none saved yet.")
                path = older[-1]
                log.warning("Using older static file %s", path.name)
        self.load_static(path)
        self.static_day = today

    def load_static(self, path: Path) -> None:
        with zipfile.ZipFile(path) as z:
            def rows(name: str):
                with z.open(name) as f:
                    yield from csv.DictReader(io.TextIOWrapper(f, "utf-8-sig"))

            self.line_route_ids = {
                r["route_id"] for r in rows("routes.txt")
                if r.get("route_short_name", "").strip() == LINE
            }
            self.line_trip_ids = {
                r["trip_id"] for r in rows("trips.txt")
                if r["route_id"] in self.line_route_ids
            }
            self.stop_names = {r["stop_id"]: r.get("stop_name", "") for r in rows("stops.txt")}
        log.info("Static %s: line %s = %d route(s), %d trip(s)", path.name, LINE,
                 len(self.line_route_ids), len(self.line_trip_ids))
        if not self.line_route_ids:
            log.warning("Line %s not found in routes.txt. Raw feeds are still saved.", LINE)

    def is_line(self, trip) -> bool:
        return (trip.trip_id in self.line_trip_ids
                or (trip.route_id and trip.route_id in self.line_route_ids))

    # ---- GTFS Realtime ----
    def fetch_feed(self, feed: str, short: str, t: datetime):
        r = self.get(RT_URL.format(op=OPERATOR, feed=feed, key=self.gtfs_key))
        if r is None:
            return None
        d = self.out / "raw" / t.strftime("%Y-%m-%d") / t.strftime("%H")
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{short}_{t.strftime('%Y%m%dT%H%M%S')}.pb.gz").write_bytes(
            gzip.compress(r.content))
        msg = gtfs_realtime_pb2.FeedMessage()
        msg.ParseFromString(r.content)
        return msg

    def poll_gtfs_rt(self) -> None:
        t = now()
        fetched = t.isoformat(timespec="seconds")

        vp = self.fetch_feed("VehiclePositions", "vp", t)
        if vp is not None:
            rows = []
            for e in vp.entity:
                if not e.HasField("vehicle"):
                    continue
                v = e.vehicle
                if not self.is_line(v.trip):
                    continue
                rows.append({
                    "fetched_at": fetched,
                    "feed_time": fmt_ts(vp.header.timestamp),
                    "vehicle_time": fmt_ts(v.timestamp),
                    "vehicle_id": v.vehicle.id,
                    "trip_id": v.trip.trip_id,
                    "start_date": v.trip.start_date,
                    "route_id": v.trip.route_id,
                    "direction_id": v.trip.direction_id if v.trip.HasField("direction_id") else "",
                    "lat": v.position.latitude,
                    "lon": v.position.longitude,
                    "bearing": v.position.bearing if v.position.HasField("bearing") else "",
                    "speed_mps": v.position.speed if v.position.HasField("speed") else "",
                    "current_stop_sequence": v.current_stop_sequence if v.HasField("current_stop_sequence") else "",
                    "stop_id": v.stop_id,
                    "stop_name": self.stop_names.get(v.stop_id, ""),
                    "current_status": gtfs_realtime_pb2.VehiclePosition.VehicleStopStatus.Name(v.current_status) if v.HasField("current_status") else "",
                    "occupancy_status": gtfs_realtime_pb2.VehiclePosition.OccupancyStatus.Name(v.occupancy_status) if v.HasField("occupancy_status") else "",
                })
            self.vp_csv.write(rows)
            log.info("VehiclePositions: %d vehicles, %d on line %s",
                     len(vp.entity), len(rows), LINE)

        tu = self.fetch_feed("TripUpdates", "tu", t)
        if tu is not None:
            rows = []
            n_trips = 0
            for e in tu.entity:
                if not e.HasField("trip_update"):
                    continue
                u = e.trip_update
                if not self.is_line(u.trip):
                    continue
                n_trips += 1
                base = {
                    "fetched_at": fetched,
                    "feed_time": fmt_ts(tu.header.timestamp),
                    "update_time": fmt_ts(u.timestamp),
                    "trip_id": u.trip.trip_id,
                    "start_date": u.trip.start_date,
                    "route_id": u.trip.route_id,
                    "trip_relationship": gtfs_realtime_pb2.TripDescriptor.ScheduleRelationship.Name(u.trip.schedule_relationship),
                    "vehicle_id": u.vehicle.id,
                }
                if not u.stop_time_update:  # e.g. a cancelled trip
                    rows.append(base)
                for s in u.stop_time_update:
                    a = s.arrival if s.HasField("arrival") else None
                    dp = s.departure if s.HasField("departure") else None
                    rows.append({**base,
                        "stop_sequence": s.stop_sequence if s.HasField("stop_sequence") else "",
                        "stop_id": s.stop_id,
                        "stop_name": self.stop_names.get(s.stop_id, ""),
                        "arrival_time": fmt_ts(a.time) if a and a.HasField("time") else "",
                        "arrival_delay_s": a.delay if a and a.HasField("delay") else "",
                        "arrival_uncertainty": a.uncertainty if a and a.HasField("uncertainty") else "",
                        "departure_time": fmt_ts(dp.time) if dp and dp.HasField("time") else "",
                        "departure_delay_s": dp.delay if dp and dp.HasField("delay") else "",
                        "stop_relationship": gtfs_realtime_pb2.TripUpdate.StopTimeUpdate.ScheduleRelationship.Name(s.schedule_relationship),
                    })
            self.tu_csv.write(rows)
            log.info("TripUpdates: %d trips, %d on line %s", len(tu.entity), n_trips, LINE)

    # ---- Trafiklab Realtime API (JSON) ----
    def lookup_area(self) -> None:
        if self.area_id or not self.rt_api_key:
            return
        r = self.get(f"{API}/stops/name/{requests.utils.quote(STOP_NAME)}?key={self.rt_api_key}")
        if r is None:
            return
        groups = r.json().get("stop_groups", [])
        (self.out / "stop_lookup.json").write_text(
            json.dumps(r.json(), ensure_ascii=False, indent=2), encoding="utf-8")
        if not groups:
            log.warning("No stop found for '%s'. Set AREA_ID manually.", STOP_NAME)
            return
        g = groups[0]
        self.area_id = str(g["id"])
        log.info("Stop '%s' -> area %s (%s). All matches in stop_lookup.json",
                 STOP_NAME, self.area_id, g.get("name"))

    def poll_realtime_api(self) -> None:
        if not self.rt_api_key:
            return
        self.lookup_area()
        if not self.area_id:
            return
        t = now()
        r = self.get(f"{API}/departures/{self.area_id}?key={self.rt_api_key}")
        if r is None:
            return
        data = r.json()
        append_jsonl(self.out / f"departures_{self.stop_slug}.jsonl",
                     {"fetched_at": t.isoformat(timespec="seconds"), "data": data})

        for d in data.get("departures", []):
            if str(d.get("route", {}).get("designation")) == LINE:
                trip = d.get("trip", {})
                key = (trip.get("trip_id"), trip.get("start_date"))
                if all(key):
                    self.seen_trips[key] = t

        # Follow each line-LINE trip along its whole route until ~2 h after
        # we last saw it on the departure board, so arrival further down the
        # line is captured too.
        for (trip_id, start_date), last in list(self.seen_trips.items()):
            if t - last > timedelta(hours=2):
                del self.seen_trips[(trip_id, start_date)]
                continue
            r = self.get(f"{API}/trips/{trip_id}/{start_date}?key={self.rt_api_key}")
            if r is not None:
                append_jsonl(self.out / f"trips_{LINE}.jsonl",
                             {"fetched_at": t.isoformat(timespec="seconds"),
                              "trip_id": trip_id, "start_date": start_date,
                              "data": r.json()})
        log.info("Realtime API: %d departures, following %d line %s trip(s)",
                 len(data.get("departures", [])), len(self.seen_trips), LINE)

    # ---- main loop ----
    def run(self, once: bool = False) -> None:
        next_rt = next_api = 0.0
        stop = {"flag": False}
        signal.signal(signal.SIGINT, lambda *_: stop.update(flag=True))
        signal.signal(signal.SIGTERM, lambda *_: stop.update(flag=True))

        while not stop["flag"]:
            t = now()
            busy = in_busy_window(t)
            try:
                self.ensure_static()
                if time.monotonic() >= next_rt:
                    self.poll_gtfs_rt()
                    next_rt = time.monotonic() + (GTFS_RT_BUSY_SECONDS if busy else GTFS_RT_QUIET_SECONDS)
                if (busy or once) and time.monotonic() >= next_api:
                    self.poll_realtime_api()
                    next_api = time.monotonic() + REALTIME_API_SECONDS
            except SystemExit:
                raise
            except Exception:  # keep running for days; log and continue
                log.exception("Unexpected error, continuing")
            if once:
                break
            time.sleep(1)
        log.info("Stopped.")


def main() -> None:
    here = Path(__file__).resolve().parent
    load_dotenv(here / ".env")

    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--out", default=str(here / "data"), help="output folder")
    p.add_argument("--once", action="store_true", help="one round, then exit")
    args = p.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout),
                  logging.FileHandler(out / "collect.log", encoding="utf-8")],
    )

    gtfs_key = os.environ.get("GTFS_REGIONAL_KEY")
    rt_key = os.environ.get("TRAFIKLAB_REALTIME_KEY")
    if not gtfs_key:
        raise SystemExit("Set GTFS_REGIONAL_KEY (see README).")
    if not rt_key:
        log.warning("No TRAFIKLAB_REALTIME_KEY: skipping departure board and trip data.")

    Collector(out, gtfs_key, rt_key).run(once=args.once)


if __name__ == "__main__":
    main()
