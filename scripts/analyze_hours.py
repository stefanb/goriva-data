#!/usr/bin/env python3
"""Analyse WHEN fuel price changes become visible in history/ and how that affects
collapsing the history to one price per calendar day.

Reads history/prices/*.csv, history/snapshots.csv, history/stations.csv and
history/franchises.csv (read-only) and prints ASCII histograms and statistics.

  analyze_hours.py            local time (Europe/Ljubljana)
  analyze_hours.py --utc      UTC hours
  analyze_hours.py --width 40 narrower bars
"""

import argparse
import csv
import sys
from collections import Counter, defaultdict
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parent.parent
HISTORY = REPO / "history"
LOCAL_TZ = ZoneInfo("Europe/Ljubljana")
TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def parse_ts(ts, tz):
    return datetime.strptime(ts, TS_FORMAT).replace(tzinfo=timezone.utc).astimezone(tz)


def histogram(title, counts, total, width):
    print(f"\n{title}")
    peak = max(counts.values()) if counts else 1
    for hour in range(24):
        v = counts.get(hour, 0)
        bar = "#" * round(width * v / peak)
        print(f"{hour:02d}h {bar:<{width}} {v:8d} {100 * v / total:5.1f}%")


def load(tz):
    events = []
    for path in sorted((HISTORY / "prices").glob("*.csv")):
        with path.open(newline="") as f:
            for row in csv.DictReader(f):
                events.append((row["ts"], parse_ts(row["ts"], tz), row["station_pk"], row["fuel"], row["price"]))
    with (HISTORY / "snapshots.csv").open(newline="") as f:
        snapshots = [(parse_ts(r["ts"], tz), int(r["changes"])) for r in csv.DictReader(f)]
    with (HISTORY / "stations.csv").open(newline="") as f:
        station_franchise = {r["pk"]: r["franchise_pk"] for r in csv.DictReader(f)}
    with (HISTORY / "franchises.csv").open(newline="") as f:
        franchise_name = {r["pk"]: r["name"] for r in csv.DictReader(f)}
    return events, snapshots, station_franchise, franchise_name


def daily_rule_comparison(sequences, tz):
    """Count daily change rows and residual A->B->A patterns for several collapse rules.

    Every calendar day between the first and last event is evaluated (not only days with
    events), so carried-over states are handled correctly.
    """
    cutoffs = {"state at 00:00 local": time(0, 0), "state at 03:00 local": time(3, 0), "state at 12:00 local": time(12, 0)}
    rules = list(cutoffs) + ["end-of-day state (23:59)", "time-weighted majority of the day"]
    rows = Counter()
    blips = Counter()
    first = min(evs[0][0] for evs in sequences.values()).date()
    last = max(evs[-1][0] for evs in sequences.values()).date()
    day_count = (last - first).days + 1
    for evs in sequences.values():
        series = {rule: [] for rule in rules}
        prev = {rule: "" for rule in rules}
        i = 0
        state = ""
        for n in range(day_count):
            day = first + timedelta(days=n)
            start = datetime.combine(day, time(0, 0), tzinfo=tz)
            end = start + timedelta(days=1)
            day_events = []
            while i < len(evs) and evs[i][0] < end:
                day_events.append(evs[i])
                i += 1
            if not day_events:
                # No event today: every rule yields the carried-over state (which may still
                # differ from yesterday's value if yesterday changed after its cutoff).
                values = dict.fromkeys(rules, state)
            else:
                values = {}
                for rule, at in cutoffs.items():
                    cutoff = datetime.combine(day, at, tzinfo=tz)
                    v = state
                    for d, p in day_events:
                        if d <= cutoff:
                            v = p
                    values[rule] = v
                duration = Counter()
                cur, t = state, start
                for d, p in day_events:
                    duration[cur] += (d - t).total_seconds()
                    cur, t = p, d
                duration[cur] += (end - t).total_seconds()
                values["end-of-day state (23:59)"] = cur
                values["time-weighted majority of the day"] = max(duration.items(), key=lambda kv: kv[1])[0]
                state = cur
            for rule in rules:
                if values[rule] != prev[rule]:
                    prev[rule] = values[rule]
                    series[rule].append(values[rule])
                    rows[rule] += 1
        for rule in rules:
            s = series[rule]
            blips[rule] += sum(1 for k in range(len(s) - 2) if s[k] == s[k + 2] != s[k + 1])
    print("\nDaily collapse rules (one price per (station, fuel, local day)):")
    print(f"  {'rule':38s} {'daily change rows':>18s} {'A->B->A left':>13s}")
    for rule in rules:
        print(f"  {rule:38s} {rows[rule]:18d} {blips[rule]:13d}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--utc", action="store_true", help="bucket by UTC hour instead of Europe/Ljubljana")
    parser.add_argument("--width", type=int, default=50, help="bar width in characters")
    args = parser.parse_args()
    tz = timezone.utc if args.utc else LOCAL_TZ
    tz_name = "UTC" if args.utc else "Europe/Ljubljana"

    events, snapshots, station_franchise, franchise_name = load(tz)
    n = len(events)
    print(f"{n} price change events, {len(snapshots)} snapshots, "
          f"{events[0][1].date()} .. {events[-1][1].date()}, hours in {tz_name}")

    by_hour = Counter(d.hour for _, d, *_ in events)
    histogram("Price change events by hour of day", by_hour, n, args.width)
    snaps_changed = Counter(d.hour for d, c in snapshots if c > 0)
    histogram("Snapshots with at least one price change by hour of day", snaps_changed, sum(snaps_changed.values()), args.width)
    snaps_all = Counter(d.hour for d, _ in snapshots)
    histogram("All snapshots (commits) by hour of day", snaps_all, len(snapshots), args.width)

    by_weekday = Counter(d.weekday() for _, d, *_ in events)
    print("\nEvents by weekday: " + ", ".join(f"{WEEKDAYS[k]} {100 * by_weekday[k] / n:.1f}%" for k in range(7)))

    print("\nShare of events visible in the first scrapes after midnight (00-02h) per year:")
    by_year = defaultdict(Counter)
    for _, d, *_ in events:
        by_year[d.year]["night" if d.hour <= 2 else "day"] += 1
    for year in sorted(by_year):
        c = by_year[year]
        total = c["night"] + c["day"]
        print(f"  {year}: {100 * c['night'] / total:5.1f}%  ({c['night']} of {total} events)")

    sequences = defaultdict(list)
    for _, d, pk, fuel, price in events:
        sequences[(pk, fuel)].append((d, price))

    short_blips = Counter()
    daytime_by_franchise = Counter()
    for (pk, fuel), evs in sequences.items():
        for i in range(1, len(evs) - 2):
            a1, b, a2 = evs[i], evs[i + 1], evs[i + 2]
            if a1[1] == a2[1] != b[1] and a2[0] - b[0] <= timedelta(hours=24):
                short_blips["B is the price before A (stale value)" if b[1] == evs[i - 1][1] else "B is a new value"] += 1
        for d, _ in evs:
            if d.hour > 2:
                daytime_by_franchise[franchise_name.get(station_franchise.get(pk, ""), "?")] += 1
    total_blips = sum(short_blips.values())
    print(f"\nShort-lived reversals A->B->A where B lasted <= 24h: {total_blips} ({100 * total_blips / n:.1f}% of events)")
    for k, v in short_blips.most_common():
        print(f"  {k}: {v} ({100 * v / total_blips:.0f}%)")
    print("\nDaytime (after 02h) events by franchise:")
    for name, v in daytime_by_franchise.most_common(8):
        print(f"  {v:7d}  {name}")

    daily_rule_comparison(sequences, tz)
    return 0


if __name__ == "__main__":
    sys.exit(main())
