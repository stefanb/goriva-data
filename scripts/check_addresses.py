#!/usr/bin/env python3
"""Check the station addresses and coordinates in history/stations.csv against the
official Slovenian address register (GURS, Register prostorskih enot) with the
`geocode` CLI.

For every station the address is geocoded and the planar distance in metres between
the coordinate published by goriva.si and the official address point is computed.
Stations whose address cannot be geocoded (no house number, "bš", typos) are reverse
geocoded instead: the nearest official address to the goriva.si coordinate is
reported as a suggestion.

Writes history/stations_geocoded.csv and prints a summary. Only the standard library
is used, but the `geocode` binary must be in PATH. This script is run by hand; the
GitHub workflow never calls it (geocode is not installed there).

  check_addresses.py             write history/stations_geocoded.csv
  check_addresses.py --out X     write somewhere else
  check_addresses.py --top 30    list the 30 largest deviations
"""

import argparse
import csv
import re
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from math import cos, radians, sqrt
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HISTORY = REPO / "history"
STATIONS = HISTORY / "stations.csv"
DEFAULT_OUT = HISTORY / "stations_geocoded.csv"
FIELDS = ["pk", "name", "address", "zip_code", "lat", "lng", "method",
          "gurs_address", "gurs_lat", "gurs_lng", "distance_m", "error"]
EARTH_RADIUS = 6_371_000  # metres
DECIMALS = 5  # precision of the GURS address points
WORKERS = 8
BUCKETS = [(25, "< 25 m"), (100, "25-100 m"), (500, "100-500 m"), (2000, "500-2000 m"), (None, ">= 2000 m")]

# "Radgonska cesta 15, 2234 Benedikt, Benedikt, Občina Benedikt, Podravska (46.61042, 15.89389)"
# optionally prefixed by "38.9 m<TAB>" (geocode reverse)
RESULT_RE = re.compile(r"^(?:[\d.]+ m\t)?(?P<addr>.+) \((?P<lat>-?[\d.]+), (?P<lon>-?[\d.]+)\)$")
ERROR_RE = re.compile(r'err="((?:[^"\\]|\\.)*)"')
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
ZIP_RE = re.compile(r"^\d{4}\b")


def planar_distance(lat1, lng1, lat2, lng2):
    """Equirectangular ("flat earth") distance in metres between two WGS84 points."""
    phi1, phi2 = radians(lat1), radians(lat2)
    x = radians(lng2 - lng1) * cos((phi1 + phi2) / 2)
    y = phi2 - phi1
    return EARTH_RADIUS * sqrt(x * x + y * y)


def prepare_query(address, zip_code):
    """Split the source address into (street + house number, zip) for geocode.

    A few source addresses embed the post office ("Trimlini 65A, 9220 Lendava",
    "3211 Arclin - Škofja Vas, Arclin 101"): keep the comma part that contains a digit
    and does not start with a 4-digit post code, and take a missing zip from the rest.
    Nothing else is corrected on purpose, so that bad source addresses show up as errors.
    """
    parts = [p.strip() for p in address.split(",")]
    query = address.strip()
    rest = []
    if len(parts) > 1:
        streets = [p for p in parts if re.search(r"\d", p) and not ZIP_RE.match(p)]
        if streets:
            query = streets[0]
            rest = [p for p in parts if p != query]
    zip_q = zip_code.strip()
    if not zip_q:
        for p in rest:
            m = ZIP_RE.match(p)
            if m:
                zip_q = m.group(0)
                break
    return query, zip_q


def geocode(*args):
    p = subprocess.run(["geocode", *args, "-v=warn"], capture_output=True, text=True, encoding="utf-8")
    stderr = ANSI_RE.sub("", p.stderr)
    m = ERROR_RE.search(stderr)
    error = m.group(1) if m else ("" if p.returncode == 0 else stderr.strip() or f"exit status {p.returncode}")
    return p.stdout.splitlines(), error


def parse_result(lines):
    """Return (address, lat, lng) from the first result line of `geocode address|reverse`."""
    for line in lines:
        m = RESULT_RE.match(line)
        if m:
            addr = ", ".join(m.group("addr").split(", ")[:2])  # "street number, zip town"
            return addr, float(m.group("lat")), float(m.group("lon"))
    return None


def geocode_batch(queries):
    """Geocode {pk: (address, zip)} (all with a zip) with one `geocode csv` run.

    Returns {pk: (address, lat, lng)} for the rows that succeeded.
    """
    if not queries:
        return {}
    with tempfile.TemporaryDirectory() as tmp:
        inp, out = Path(tmp) / "in.csv", Path(tmp) / "out.csv"
        with inp.open("w", newline="") as f:
            w = csv.writer(f, lineterminator="\n")
            w.writerow(["pk", "address", "zip"])
            for pk, (address, zip_q) in queries.items():
                w.writerow([pk, address, zip_q])
        _, error = geocode("csv", "--in", str(inp), "--out", str(out), "--addressCol", "2", "--zipCol", "3",
                           "--appendAll", "--lat", "gurs_lat", "--lon", "gurs_lng",
                           "--decimals", str(DECIMALS), "--workers", str(WORKERS))
        if error:
            sys.exit(f"geocode csv failed: {error}")
        found = {}
        with out.open(newline="") as f:
            for r in csv.DictReader(f):
                if not r["gurs_lat"]:
                    continue
                street = r["street"] + (f" / {r['streetAlt']}" if r["streetAlt"] else "")
                addr = f"{street} {r['housenumber']}{r['housenumberAppendix']}, {r['zipCode']} {r['zipName']}"
                found[r["pk"]] = (addr, float(r["gurs_lat"]), float(r["gurs_lng"]))
    return found


def geocode_address(address, zip_q):
    lines, error = geocode("address", f"{address}, {zip_q}" if zip_q else address)
    return parse_result(lines), error


def reverse_geocode(lat, lng):
    lines, error = geocode("reverse", lat, lng, "--count", "1")
    return parse_result(lines), error


def fmt_coord(v):
    return f"{v:.{DECIMALS}f}"


def check(stations):
    """Return the output rows (one per station, same order) with the geocoding results."""
    queries = {r["pk"]: prepare_query(r["address"], r["zip_code"]) for r in stations}
    batch = geocode_batch({pk: q for pk, q in queries.items() if q[0] and q[1]})
    print(f"{len(stations)} stations, {len(batch)} geocoded in batch", file=sys.stderr)

    rows = []
    for st in stations:
        row = {k: st.get(k, "") for k in FIELDS}
        row["method"] = row["gurs_address"] = row["gurs_lat"] = row["gurs_lng"] = row["distance_m"] = row["error"] = ""
        rows.append(row)

    # Rows the batch did not resolve: geocode individually to get the error reason (and the
    # result for rows without a zip, which `geocode csv` refuses).
    def resolve(row):
        pk = row["pk"]
        if pk in batch:
            return pk, batch[pk], ""
        address, zip_q = queries[pk]
        if not address:
            return pk, None, "empty address"
        return pk, *geocode_address(address, zip_q)

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for pk, result, error in pool.map(resolve, rows):
            row = next(r for r in rows if r["pk"] == pk)
            if result:
                row["method"] = "address"
                row["gurs_address"], gurs_lat, gurs_lng = result
                row["gurs_lat"], row["gurs_lng"] = fmt_coord(gurs_lat), fmt_coord(gurs_lng)
            row["error"] = error

        # Reverse geocode the failures that have a coordinate: nearest official address.
        todo = [r for r in rows if not r["method"] and r["lat"] and r["lng"]]
        print(f"{len(todo)} addresses could not be geocoded, reverse geocoding their coordinates", file=sys.stderr)
        for row, (result, error) in zip(todo, pool.map(lambda r: reverse_geocode(r["lat"], r["lng"]), todo)):
            if result:
                row["method"] = "reverse"
                row["gurs_address"], gurs_lat, gurs_lng = result
                row["gurs_lat"], row["gurs_lng"] = fmt_coord(gurs_lat), fmt_coord(gurs_lng)
            elif error:
                row["error"] = f"{row['error']}; reverse: {error}" if row["error"] else f"reverse: {error}"

    for row in rows:
        if row["gurs_lat"] and row["lat"] and row["lng"]:
            d = planar_distance(float(row["lat"]), float(row["lng"]), float(row["gurs_lat"]), float(row["gurs_lng"]))
            row["distance_m"] = str(round(d))
    return rows


def summary(rows, top):
    by_method = {m: [r for r in rows if r["method"] == m] for m in ("address", "reverse", "")}
    with_dist = [r for r in rows if r["distance_m"]]
    print(f"\n{len(rows)} stations: {len(by_method['address'])} addresses geocoded, "
          f"{len(by_method['reverse'])} reverse geocoded (invalid address), {len(by_method[''])} unresolved")

    failed = [r for r in rows if r["error"]]
    if failed:
        print(f"\nAddresses that could not be geocoded ({len(failed)}), with the nearest official address:")
        for r in failed:
            print(f"  {r['pk']:>5} {r['name'][:32]:32s} {r['address'][:36]:36s} {r['zip_code']:4s} "
                  f"{r['error']:26s} -> {r['gurs_address']} ({r['distance_m']} m)")

    checked = [r for r in by_method["address"] if r["distance_m"]]
    print(f"\nDistance between the goriva.si coordinate and the official address point ({len(checked)} stations):")
    lo = 0
    for hi, label in BUCKETS:
        n = sum(1 for r in checked if lo <= int(r["distance_m"]) and (hi is None or int(r["distance_m"]) < hi))
        print(f"  {label:12s} {n:5d} {100 * n / len(checked):5.1f}%")
        lo = hi
    dists = sorted(int(r["distance_m"]) for r in checked)
    if dists:
        print(f"  median {dists[len(dists) // 2]} m, mean {sum(dists) / len(dists):.0f} m, max {dists[-1]} m")

    print(f"\nTop {top} deviations (goriva.si coordinate vs. official address point):")
    for r in sorted(checked, key=lambda r: -int(r["distance_m"]))[:top]:
        print(f"  {int(r['distance_m']):7d} m  {r['pk']:>5} {r['name'][:32]:32s} {r['address'][:30]:30s} {r['zip_code']:4s} "
              f"-> {r['gurs_address']}  csv {r['lat']},{r['lng']}  gurs {r['gurs_lat']},{r['gurs_lng']}")
    print(f"\n{len(with_dist)} rows with a distance written")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help=f"output CSV (default {DEFAULT_OUT.relative_to(REPO)})")
    parser.add_argument("--top", type=int, default=20, help="number of largest deviations to list")
    args = parser.parse_args()
    if shutil.which("geocode") is None:
        sys.exit("The `geocode` CLI is not in PATH; nothing was written.")

    with STATIONS.open(newline="") as f:
        stations = list(csv.DictReader(f))
    rows = check(stations)
    with args.out.open("w", newline="") as f:
        w = csv.DictWriter(f, FIELDS, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    summary(rows, args.top)
    return 0


if __name__ == "__main__":
    sys.exit(main())
