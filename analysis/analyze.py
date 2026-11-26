"""Preliminary analysis: when does bus 121 really arrive at Knektaparken?

Usage:
    uv run --group analysis analysis/analyze.py [DATA_DIR]

DATA_DIR defaults to data/ and must hold vehicle_positions_121.csv,
trip_updates_121.csv, departures_knektaparken.jsonl and trips_121.jsonl
as written by collect.py. No static GTFS zip is needed: the schedule is
taken from the Realtime API's trip view (trips_121.jsonl).

Writes analysis/derived/*.csv and analysis/figures/*.png, and prints the
tables quoted in analysis/FINDINGS.md.
"""

import json
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

HERE = Path(__file__).parent
DERIVED = HERE / "derived"
FIGURES = HERE / "figures"

TZ = "Europe/Stockholm"
STOP = "Knektaparken"
STOP_LAT, STOP_LON = 57.779287, 14.188212
ORIGIN_LAT, ORIGIN_LON = 57.784285, 14.164131  # Jönköping Centralstation
TARGET_SCHED = "07:30"  # HH:MM of the scheduled time at Knektaparken

AT_STOP_M = 40  # a fix this close to the stop counts as "at the stop"
LEFT_ORIGIN_M = 100  # this far from Centralstation counts as "left"
LEAD_BINS = [0, 2, 5, 10, 15, 20, 30, 45, 60]


def dist_m(lat, lon, lat0, lon0):
    """Equirectangular distance in metres, fine at city scale."""
    dy = (lat - lat0) * 111_195
    dx = (lon - lon0) * 111_195 * np.cos(np.radians(lat0))
    return np.hypot(dx, dy)


def to_local(s):
    return pd.to_datetime(s, utc=True).dt.tz_convert(TZ)


def naive_local(s):
    """Realtime API times have no offset; they are local."""
    return pd.to_datetime(s).dt.tz_localize(TZ)


# --- Loading -----------------------------------------------------------


def load_positions(data):
    vp = pd.read_csv(data / "vehicle_positions_121.csv", dtype=str)
    vp = vp.drop_duplicates(["vehicle_id", "vehicle_time"])
    vp = vp[vp.trip_id.notna()].copy()
    for c in ["lat", "lon", "speed_mps"]:
        vp[c] = vp[c].astype(float)
    vp["t"] = to_local(vp.vehicle_time)
    vp["date"] = vp.t.dt.strftime("%Y-%m-%d")
    vp["d_stop"] = dist_m(vp.lat, vp.lon, STOP_LAT, STOP_LON)
    vp["d_origin"] = dist_m(vp.lat, vp.lon, ORIGIN_LAT, ORIGIN_LON)
    return vp.sort_values("t")


def load_trips_api(data):
    """Schedule per trip, plus the trip API's Knektaparken predictions."""
    sched, preds = {}, []
    for line in open(data / "trips_121.jsonl", encoding="utf-8"):
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue  # half-written last line
        calls = (r.get("data") or {}).get("calls") or []
        if not calls:
            continue
        key = (r["start_date"], r["trip_id"])
        for c in calls:
            if c["stop"]["name"] != STOP:
                continue
            sched.setdefault(
                key,
                {
                    "date": r["start_date"],
                    "trip_id": r["trip_id"],
                    "sched_origin": calls[0]["scheduledDeparture"],
                    "sched_stop": c["scheduledArrival"],
                },
            )
            preds.append(
                {
                    "date": r["start_date"],
                    "trip_id": r["trip_id"],
                    "fetched": r["fetched_at"],
                    "pred": c.get("realtimeArrival") or c["scheduledArrival"],
                    "is_realtime": bool(c.get("is_realtime")),
                }
            )
    sched = pd.DataFrame(sched.values())
    sched["sched_origin"] = naive_local(sched.sched_origin)
    sched["sched_stop"] = naive_local(sched.sched_stop)
    preds = pd.DataFrame(preds)
    preds["fetched"] = to_local(preds.fetched)
    preds["pred"] = naive_local(preds.pred)
    preds["source"] = "Trip API"
    return sched, preds


def load_trip_updates(data):
    cols = ["fetched_at", "trip_id", "start_date", "stop_name"]
    cols += ["arrival_time", "departure_time"]
    tu = pd.read_csv(data / "trip_updates_121.csv", dtype=str, usecols=cols)
    tu = tu[tu.stop_name == STOP].copy()
    tu["date"] = pd.to_datetime(tu.start_date).dt.strftime("%Y-%m-%d")
    tu["fetched"] = to_local(tu.fetched_at)
    tu["pred"] = to_local(tu.arrival_time.fillna(tu.departure_time))
    tu["is_realtime"] = True
    tu["source"] = "GTFS-RT"
    return tu[["date", "trip_id", "fetched", "pred", "is_realtime", "source"]]


def load_board(data):
    rows = []
    for line in open(data / "departures_knektaparken.jsonl", encoding="utf-8"):
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        for d in (r.get("data") or {}).get("departures") or []:
            if d["route"]["designation"] != "121":
                continue
            rows.append(
                {
                    "date": d["trip"]["start_date"],
                    "trip_id": d["trip"]["trip_id"],
                    "fetched": r["fetched_at"],
                    "pred": d["realtime"],
                    "is_realtime": bool(d["is_realtime"]),
                }
            )
    b = pd.DataFrame(rows)
    b["fetched"] = to_local(b.fetched)
    b["pred"] = naive_local(b.pred)
    b["source"] = "Departure board"
    return b


# --- Ground truth ------------------------------------------------------


def crossing(t0, d0, t1, d1, r):
    """Time at which distance passes r, linear between two fixes."""
    if d0 == d1:
        return t1
    f = np.clip((d0 - r) / (d0 - d1), 0, 1)
    return t0 + (t1 - t0) * f


def actual_arrivals(vp, sched):
    out = []
    for row in sched.itertuples():
        g = vp[(vp.trip_id == row.trip_id) & (vp.date == row.date)]
        rec = {
            "date": row.date,
            "trip_id": row.trip_id,
            "sched_origin": row.sched_origin,
            "sched_stop": row.sched_stop,
            "n_fixes": len(g),
        }
        if len(g) < 5:
            out.append(rec | {"quality": "no GPS"})
            continue
        t, d = g.t.to_numpy(), g.d_stop.to_numpy()
        rec["min_dist_m"] = round(float(d.min()), 1)
        near = np.flatnonzero(d <= AT_STOP_M)
        if len(near) and near[0] > 0:
            i, j = near[0], near[-1]
            rec["actual_arr"] = crossing(
                t[i - 1], d[i - 1], t[i], d[i], AT_STOP_M
            )
            if j + 1 < len(d):
                dep = crossing(t[j], d[j], t[j + 1], d[j + 1], AT_STOP_M)
                rec["actual_dep"] = dep
            rec["quality"] = "ok"
        elif d.min() < 250:
            # Passed between two fixes without one landing near the stop.
            i = int(d.argmin())
            rec["actual_arr"] = t[i]
            rec["quality"] = "passed between fixes"
        else:
            rec["quality"] = "never near stop"
        # When the bus left the start (Jönköping Centralstation).
        do = g.d_origin.to_numpy()
        far = np.flatnonzero(do > LEFT_ORIGIN_M)
        if do[0] <= LEFT_ORIGIN_M and len(far):
            k = far[0]
            rec["left_origin"] = crossing(
                t[k - 1], -do[k - 1], t[k], -do[k], -LEFT_ORIGIN_M
            )
            rec["at_origin_from"] = t[0]
        out.append(rec)
    a = pd.DataFrame(out)
    a["delay_min"] = (a.actual_arr - a.sched_stop).dt.total_seconds() / 60
    origin_lag = a.left_origin - a.sched_origin
    a["origin_late_min"] = origin_lag.dt.total_seconds() / 60
    drive = a.actual_arr - a.left_origin
    a["drive_min"] = drive.dt.total_seconds() / 60
    sched_drive = a.sched_stop - a.sched_origin
    a["sched_drive_min"] = sched_drive.dt.total_seconds() / 60
    return a.sort_values(["date", "sched_stop"]).reset_index(drop=True)


# --- Predictions and errors --------------------------------------------


def add_errors(preds, arr):
    p = preds.merge(
        arr[["date", "trip_id", "actual_arr", "sched_stop"]],
        on=["date", "trip_id"],
    )
    p = p[p.actual_arr.notna()]
    p["lead_min"] = (p.actual_arr - p.fetched).dt.total_seconds() / 60
    p = p[(p.lead_min > 0) & (p.lead_min <= LEAD_BINS[-1])].copy()
    p["err_min"] = (p.pred - p.actual_arr).dt.total_seconds() / 60
    p["lead_bin"] = pd.cut(p.lead_min, LEAD_BINS)
    return p


def timetable_preds(board, arr):
    """The plain timetable, and timetable + typical delay.

    Both are evaluated at the board's polls. The typical delay is the
    median delay at Knektaparken over the morning trips of the *other*
    day(s) (leave-one-day-out).
    """
    t = board.drop_duplicates(["date", "trip_id", "fetched"])
    t = t.drop(columns="pred").merge(arr[["date", "trip_id", "sched_stop"]])
    t = t.rename(columns={"sched_stop": "pred"})
    t["is_realtime"] = False
    plain = t.assign(source="Timetable")
    shifted = []
    ok = arr[arr.quality.eq("ok")]
    for day in t.date.unique():
        train = ok[ok.date != day]
        if train.empty:
            continue
        offset = pd.Timedelta(minutes=train.delay_min.median())
        d = t[t.date == day]
        shifted.append(d.assign(pred=d.pred + offset))
    shifted = pd.concat(shifted).assign(source="Timetable + typical delay")
    return pd.concat([plain, shifted])


def gps_rule_preds(vp, arr):
    """Leave-one-day-out GPS rule, evaluated at every GPS fix.

    Before the bus has left Centralstation: predict
    max(now, scheduled start + typical late start) + typical drive time.
    After it has left: left time + typical drive time.
    "Typical" = median over the morning trips of the *other* day(s).
    """
    rows = []
    ok = arr[arr.quality.eq("ok") & arr.drive_min.notna()]
    for day in ok.date.unique():
        train = ok[ok.date != day]
        if train.empty:
            continue
        late = pd.Timedelta(minutes=train.origin_late_min.median())
        drive = pd.Timedelta(minutes=train.drive_min.median())
        for r in ok[ok.date == day].itertuples():
            g = vp[(vp.trip_id == r.trip_id) & (vp.date == day)]
            for now in g.t:
                if now < r.left_origin:
                    start = max(now, r.sched_origin + late)
                else:
                    start = r.left_origin
                rows.append(
                    {
                        "date": day,
                        "trip_id": r.trip_id,
                        "fetched": now,
                        "pred": start + drive,
                        "is_realtime": True,
                    }
                )
    g = pd.DataFrame(rows)
    g["source"] = "GPS rule (ours)"
    return g


def summarise(errs):
    def p90(x):
        return np.percentile(np.abs(x), 90)

    s = errs.groupby(["source", "lead_bin"], observed=True).err_min
    tab = s.agg(
        n="size",
        trips="count",
        mean="mean",
        mae=lambda x: np.abs(x).mean(),
        p90_abs=p90,
        too_late=lambda x: (x > 0).mean(),
        worst_late=lambda x: max(x.max(), 0),
    )
    tab["trips"] = (
        errs.groupby(["source", "lead_bin"], observed=True)
        .apply(lambda x: x[["date", "trip_id"]].drop_duplicates().shape[0])
        .astype(int)
    )
    return tab.round(2)


# --- Figures -----------------------------------------------------------


def fig_target(errs_all, arr, target):
    days = sorted(target.date)
    fig, axes = plt.subplots(1, len(days), figsize=(6 * len(days), 4.5))
    axes = np.atleast_1d(axes)
    styles = {
        "GTFS-RT": dict(color="tab:red", lw=1.5),
        "Departure board": dict(color="tab:orange", lw=1.2, ls="--"),
        "Trip API": dict(color="tab:purple", lw=1.0, ls=":"),
        "GPS rule (ours)": dict(color="tab:blue", lw=1.5),
    }
    for ax, day in zip(axes, days):
        tr = target[target.date == day].iloc[0]
        e = errs_all[(errs_all.date == day) & (errs_all.trip_id == tr.trip_id)]
        for src, st in styles.items():
            s = e[e.source == src].sort_values("fetched")
            if len(s):
                ax.plot(s.fetched, s.pred, label=src, **st)
        ax.axhline(tr.actual_arr, color="black", lw=2, label="Actual (GPS)")
        ax.axhline(
            tr.sched_stop, color="grey", lw=1, ls="-.", label="Timetable"
        )
        if pd.notna(tr.left_origin):
            ax.axvline(tr.left_origin, color="green", lw=1, ls=":")
            ax.annotate(
                "left Centralstation",
                (tr.left_origin, tr.sched_stop),
                rotation=90,
                fontsize=8,
                color="green",
                va="bottom",
                ha="right",
            )
        lo = tr.actual_arr - pd.Timedelta(minutes=35)
        ax.set_xlim(lo, tr.actual_arr + pd.Timedelta(minutes=1))
        ax.set_title(f"{day}: 07:30 bus at Knektaparken")
        ax.set_xlabel("Time of the poll")
        ax.set_ylabel("Predicted arrival")
        fmt = matplotlib.dates.DateFormatter("%H:%M", tz=TZ)
        ax.xaxis.set_major_formatter(fmt)
        ax.yaxis.set_major_formatter(fmt)
        ax.yaxis.set_major_locator(matplotlib.dates.MinuteLocator(tz=TZ))
        ax.grid(alpha=0.3)
    axes[0].legend(fontsize=8, loc="center left")
    fig.tight_layout()
    fig.savefig(FIGURES / "target_trip.png", dpi=120)
    plt.close(fig)


def fig_error_by_lead(errs):
    srcs = ["Timetable", "GTFS-RT", "Departure board"]
    srcs += ["Timetable + typical delay", "GPS rule (ours)"]
    mids = {b: (b.left + b.right) / 2 for b in errs.lead_bin.cat.categories}
    fig, ax = plt.subplots(figsize=(8, 4.5))
    for src in srcs:
        e = errs[errs.source == src]
        g = e.groupby("lead_bin", observed=True).err_min
        x = [mids[b] for b in g.mean().index]
        lo, hi = g.quantile(0.1), g.quantile(0.9)
        line = ax.plot(x, g.median(), marker="o", label=src)[0]
        ax.fill_between(x, lo, hi, color=line.get_color(), alpha=0.12)
    ax.axhline(0, color="black", lw=1)
    ax.invert_xaxis()
    ax.set_xlabel("Minutes before the bus actually reached Knektaparken")
    ax.set_ylabel("Predicted minus actual (min)\n< 0: says earlier than real")
    ax.set_title("Prediction error, morning 121 trips, 2 days (median, 10-90%)")
    ax.grid(alpha=0.3)
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(FIGURES / "error_by_lead.png", dpi=120)
    plt.close(fig)


def fig_delays(arr):
    ok = arr[arr.actual_arr.notna()]
    fig, ax = plt.subplots(figsize=(8, 4))
    for day, g in ok.groupby("date"):
        x = g.sched_stop.dt.strftime("%H:%M")
        ax.plot(x, g.delay_min, marker="o", label=day)
    ax.axhline(0, color="black", lw=1)
    ax.set_xlabel("Scheduled time at Knektaparken")
    ax.set_ylabel("Actual arrival minus timetable (min)")
    ax.set_title("How late each morning 121 reached Knektaparken")
    ax.grid(alpha=0.3)
    ax.legend()
    fig.tight_layout()
    fig.savefig(FIGURES / "delay_per_trip.png", dpi=120)
    plt.close(fig)


# --- Main --------------------------------------------------------------


def main():
    data = Path(sys.argv[1] if len(sys.argv) > 1 else "data")
    DERIVED.mkdir(exist_ok=True)
    FIGURES.mkdir(exist_ok=True)
    pd.set_option("display.width", 200)

    vp = load_positions(data)
    sched, trip_api = load_trips_api(data)
    arr = actual_arrivals(vp, sched)
    target = arr[arr.sched_stop.dt.strftime("%H:%M") == TARGET_SCHED]

    show = ["date", "trip_id", "sched_origin", "left_origin", "sched_stop"]
    show += ["actual_arr", "actual_dep", "delay_min", "origin_late_min"]
    show += ["drive_min", "sched_drive_min", "min_dist_m", "quality"]
    arr[show].to_csv(DERIVED / "actual_arrivals.csv", index=False)
    print("== Actual arrivals at Knektaparken (morning trips) ==")
    print(arr[show].to_string())

    board = load_board(data)
    preds = pd.concat(
        [
            load_trip_updates(data),
            board,
            trip_api,
            timetable_preds(board, arr),
            gps_rule_preds(vp, arr),
        ]
    )
    errs = add_errors(preds, arr)
    # The board/timetable only matter for morning trips we have truth for.
    keep = ["source", "date", "trip_id", "fetched", "pred", "actual_arr"]
    keep += ["lead_min", "err_min", "is_realtime"]
    errs[keep].to_csv(DERIVED / "prediction_errors.csv", index=False)

    print("\n== Prediction error by lead time, all morning trips ==")
    print(summarise(errs).to_string())

    tgt = errs.merge(target[["date", "trip_id"]])
    print("\n== Same, 07:30 trip only ==")
    print(summarise(tgt).to_string())

    print("\n== Board: share of polls showing real-time data, by lead ==")
    b = errs[errs.source == "Departure board"]
    print(b.groupby("lead_bin", observed=True).is_realtime.mean().round(2))

    print("\n== 07:30 trip: what each source said at fixed clock times ==")
    for r in target.itertuples():
        for hhmm in ["07:00", "07:10", "07:20", "07:25", "07:28", "07:30"]:
            now = pd.Timestamp(f"{r.date} {hhmm}", tz=TZ)
            said = []
            for src in ["GTFS-RT", "Departure board", "GPS rule (ours)"]:
                s = preds[
                    (preds.source == src)
                    & (preds.trip_id == r.trip_id)
                    & (preds.date == r.date)
                    & (preds.fetched <= now)
                ]
                last = s.sort_values("fetched").pred.iloc[-1:] if len(s) else []
                txt = last.iloc[0].strftime("%H:%M:%S") if len(last) else "-"
                said.append(f"{src}={txt}")
            print(r.date, hhmm, *said, sep="  ")
        print(r.date, "actual", r.actual_arr.strftime("%H:%M:%S"))

    print("\n== Spread of delays and drive times (ok trips) ==")
    ok = arr[arr.quality == "ok"]
    cols = ["delay_min", "origin_late_min", "drive_min", "sched_drive_min"]
    print(ok[cols].describe().round(2).to_string())

    fig_target(errs, arr, target)
    fig_error_by_lead(errs)
    fig_delays(arr)


if __name__ == "__main__":
    main()
