#!/usr/bin/env python3
"""Build the machine-readable fuel price history in ``history/`` from the git
history of the ``data/`` snapshots.

Modes:

  build_history.py            incremental: process commits after history/state.json
  build_history.py --rebuild  full rebuild from the first commit (needs full clone)
  build_history.py --verify   rebuild into a temp dir and compare with history/

Outputs: price change events (history/prices/<year>.csv), one price per local
calendar day derived from them (history/daily/<year>.csv, see finalize_daily),
snapshot provenance, stations, franchises and fuels.

Only the Python standard library is used. Git blobs are read through a single
``git cat-file --batch`` process, which makes a full rebuild take about a
minute. See history/README.md for the output schema.
"""

import argparse
import csv
import filecmp
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parent.parent
HISTORY = REPO / "history"
PRICE_QUANTUM = Decimal("0.001")
MAX_PAGES = 200  # safety bound when probing pages of a snapshot

# Daily prices: the price in effect at DAILY_CUTOFF local time counts for that calendar day.
# Prices change at local midnight and are visible in the 00:15 scrape (sometimes delayed by an
# hour or two); 03:00 exists on both DST transition days and precedes the daytime noise.
LOCAL_TZ = ZoneInfo("Europe/Ljubljana")
DAILY_CUTOFF = time(3, 0)

STATION_FIELDS = ["pk", "franchise_pk", "name", "address", "zip_code", "lat", "lng", "first_seen", "last_seen"]
STATION_ATTRS = ["name", "address", "zip_code", "lat", "lng"]
SNAPSHOT_FIELDS = ["ts", "commit", "count", "pages", "stations", "duplicates", "changes"]
PRICE_FIELDS = ["ts", "station_pk", "fuel", "price"]
DAILY_FIELDS = ["date", "station_pk", "fuel", "price"]
FRANCHISE_EVENT_FIELDS = ["ts", "station_pk", "franchise_pk"]
FUEL_FIELDS = ["pk", "code", "name", "long_name"]
FRANCHISE_FIELDS = ["pk", "name"]


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def git(*args, check=True):
    return subprocess.run(["git", *args], cwd=REPO, check=check, capture_output=True, text=True).stdout


TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def format_ts(unix):
    return datetime.fromtimestamp(int(unix), timezone.utc).strftime(TS_FORMAT)


def parse_ts(ts):
    return datetime.strptime(ts, TS_FORMAT).replace(tzinfo=timezone.utc)


def local_date(ts):
    return parse_ts(ts).astimezone(LOCAL_TZ).date()


def daily_cutoff(day):
    """UTC timestamp string of DAILY_CUTOFF local time on ``day`` (comparable with event ts)."""
    return datetime.combine(day, DAILY_CUTOFF, tzinfo=LOCAL_TZ).astimezone(timezone.utc).strftime(TS_FORMAT)


def format_price(value):
    """Canonical price string: exactly three decimals, or '' for null."""
    if value is None:
        return ""
    try:
        return str(Decimal(str(value)).quantize(PRICE_QUANTUM))
    except InvalidOperation:
        log(f"warning: unparseable price {value!r}, kept verbatim")
        return str(value)


def format_coord(value):
    return "" if value is None else str(value)


class GitBlobs:
    """Read blobs via one long-running ``git cat-file --batch`` process."""

    def __init__(self):
        self.proc = subprocess.Popen(
            ["git", "cat-file", "--batch"], cwd=REPO, stdin=subprocess.PIPE, stdout=subprocess.PIPE
        )

    def read(self, sha, path):
        """Return the blob bytes for ``<sha>:<path>`` or None if it is missing."""
        self.proc.stdin.write(f"{sha}:{path}\n".encode())
        self.proc.stdin.flush()
        header = self.proc.stdout.readline().decode().rstrip("\n")
        if header.endswith(" missing"):
            return None
        _obj, _type, size = header.split()
        data = self.proc.stdout.read(int(size))
        self.proc.stdout.read(1)  # trailing newline
        return data

    def close(self):
        self.proc.stdin.close()
        self.proc.wait()


class History:
    """In-memory state of the history plus the writers that append to it."""

    def __init__(self, out_dir):
        self.out_dir = Path(out_dir)
        self.prices = {}        # (station_pk, fuel) -> last emitted price string ('' = null/never seen)
        self.franchise = {}     # station_pk -> franchise_pk last emitted
        self.stations = {}      # station_pk -> dict(STATION_FIELDS)
        self.fuels = {}         # pk -> dict(FUEL_FIELDS)
        self.franchises = {}    # pk -> dict(FRANCHISE_FIELDS)
        self.last_commit = None
        self.last_ts = None
        self.present = set()    # station pks present in the last processed snapshot
        self.daily = {}         # (station_pk, fuel) -> daily price as of daily_cutoff(daily_through)
        self.daily_through = None  # last finalized local date
        self.pending = []       # price events with ts > daily_cutoff(daily_through), in ts order
        self._year_writers = {}

    # ---- loading existing output (incremental mode) -------------------------------------------

    def load(self):
        state_file = self.out_dir / "state.json"
        if not state_file.exists():
            raise SystemExit("history/state.json not found - run with --rebuild first")
        state = json.loads(state_file.read_text())
        self.last_commit = state["last_commit"]
        self.last_ts = state["last_ts"]
        if "daily_through" not in state:
            raise SystemExit("history/daily not built yet - run with --rebuild first")
        self.daily_through = date.fromisoformat(state["daily_through"])
        cutoff = daily_cutoff(self.daily_through)

        for path in sorted((self.out_dir / "prices").glob("*.csv")):
            with path.open(newline="") as f:
                for row in csv.DictReader(f):
                    self.prices[(row["station_pk"], row["fuel"])] = row["price"]
                    if row["ts"] > cutoff:
                        self.pending.append([row["ts"], row["station_pk"], row["fuel"], row["price"]])
        for path in sorted((self.out_dir / "daily").glob("*.csv")):
            with path.open(newline="") as f:
                for row in csv.DictReader(f):
                    self.daily[(row["station_pk"], row["fuel"])] = row["price"]
        with (self.out_dir / "station_franchise.csv").open(newline="") as f:
            for row in csv.DictReader(f):
                self.franchise[row["station_pk"]] = row["franchise_pk"]
        with (self.out_dir / "stations.csv").open(newline="") as f:
            for row in csv.DictReader(f):
                if row["last_seen"] == "":
                    row["last_seen"] = self.last_ts
                    self.present.add(row["pk"])
                self.stations[row["pk"]] = row
        with (self.out_dir / "fuels.csv").open(newline="") as f:
            for row in csv.DictReader(f):
                self.fuels[row["pk"]] = row
        with (self.out_dir / "franchises.csv").open(newline="") as f:
            for row in csv.DictReader(f):
                self.franchises[row["pk"]] = row

    # ---- appending output ---------------------------------------------------------------------

    def _append_writer(self, path, fields):
        path.parent.mkdir(parents=True, exist_ok=True)
        new = not path.exists() or path.stat().st_size == 0
        f = path.open("a", newline="")
        w = csv.writer(f, lineterminator="\n")
        if new:
            w.writerow(fields)
        return f, w

    def open_writers(self):
        self._snap_file, self.snap_writer = self._append_writer(self.out_dir / "snapshots.csv", SNAPSHOT_FIELDS)
        self._fr_file, self.fr_writer = self._append_writer(self.out_dir / "station_franchise.csv", FRANCHISE_EVENT_FIELDS)

    def _year_writer(self, subdir, year, fields):
        key = (subdir, year)
        if key not in self._year_writers:
            self._year_writers[key] = self._append_writer(self.out_dir / subdir / f"{year}.csv", fields)
        return self._year_writers[key][1]

    def price_writer(self, ts):
        return self._year_writer("prices", ts[:4], PRICE_FIELDS)

    def daily_writer(self, day):
        return self._year_writer("daily", f"{day.year:04d}", DAILY_FIELDS)

    def close_writers(self):
        for f, _ in self._year_writers.values():
            f.close()
        self._year_writers = {}
        self._snap_file.close()
        self._fr_file.close()

    def write_dimensions(self):
        def dump(name, fields, rows):
            with (self.out_dir / name).open("w", newline="") as f:
                w = csv.DictWriter(f, fields, lineterminator="\n", extrasaction="ignore")
                w.writeheader()
                for row in rows:
                    w.writerow(row)

        stations = []
        for pk in sorted(self.stations, key=int):
            row = dict(self.stations[pk])
            if pk in self.present:
                row["last_seen"] = ""
            stations.append(row)
        dump("stations.csv", STATION_FIELDS, stations)
        dump("fuels.csv", FUEL_FIELDS, (self.fuels[pk] for pk in sorted(self.fuels, key=int)))
        dump("franchises.csv", FRANCHISE_FIELDS, (self.franchises[pk] for pk in sorted(self.franchises, key=int)))
        state = {
            "last_commit": self.last_commit,
            "last_ts": self.last_ts,
            "daily_through": self.daily_through.isoformat() if self.daily_through else None,
        }
        (self.out_dir / "state.json").write_text(json.dumps(state, indent=2) + "\n")

    # ---- processing one snapshot --------------------------------------------------------------

    def process_snapshot(self, sha, ts, pages, fuel_json, franchise_json):
        """Diff one snapshot (list of parsed pages) against the current state and append events."""
        if self.daily_through and ts <= daily_cutoff(self.daily_through):
            raise SystemExit(
                f"snapshot {sha[:8]} at {ts} precedes the finalized daily cutoff for {self.daily_through} "
                "(rewritten history?) - run with --rebuild"
            )
        stations = {}
        duplicates = 0
        for page in pages:
            for st in page["results"]:
                pk = str(st["pk"])
                if pk in stations:
                    duplicates += 1  # pagination race: same station on two pages, first wins
                    continue
                stations[pk] = st

        price_rows = []
        franchise_rows = []
        for pk in sorted(stations, key=int):
            st = stations[pk]
            for fuel, value in sorted((st.get("prices") or {}).items()):
                price = format_price(value)
                if self.prices.get((pk, fuel), "") != price:
                    self.prices[(pk, fuel)] = price
                    price_rows.append([ts, pk, fuel, price])
            franchise_pk = "" if st.get("franchise") is None else str(st["franchise"])
            if self.franchise.get(pk) != franchise_pk:
                self.franchise[pk] = franchise_pk
                franchise_rows.append([ts, pk, franchise_pk])
            row = self.stations.get(pk)
            if row is None:
                row = {"pk": pk, "first_seen": ts}
                self.stations[pk] = row
            row["franchise_pk"] = franchise_pk
            row["name"] = st.get("name") or ""
            row["address"] = st.get("address") or ""
            row["zip_code"] = st.get("zip_code") or ""
            row["lat"] = format_coord(st.get("lat"))
            row["lng"] = format_coord(st.get("lng"))
            row["last_seen"] = ts

        if fuel_json is not None:
            for fuel in fuel_json:
                self.fuels[str(fuel["pk"])] = {
                    "pk": str(fuel["pk"]),
                    "code": fuel.get("code") or "",
                    "name": fuel.get("name") or "",
                    "long_name": fuel.get("long_name") or "",
                }
        if franchise_json is not None:
            for fr in franchise_json:
                self.franchises[str(fr["pk"])] = {"pk": str(fr["pk"]), "name": fr.get("name") or ""}

        writer = self.price_writer(ts)
        writer.writerows(price_rows)
        self.pending.extend(price_rows)
        self.fr_writer.writerows(franchise_rows)
        count = pages[0].get("count", "") if pages else ""
        self.snap_writer.writerow([ts, sha, count, len(pages), len(stations), duplicates, len(price_rows)])

        self.present = set(stations)
        self.last_commit = sha
        self.last_ts = ts
        self.finalize_daily()
        return len(price_rows)

    def finalize_daily(self):
        """Emit daily rows for every local day whose cutoff has been passed by the last snapshot.

        The daily price of day D is the last event with ts <= daily_cutoff(D); a row is written
        only when it differs from the previous day's daily price. Because snapshots are
        processed in ts order, day D is final once last_ts >= daily_cutoff(D).
        """
        while True:
            if self.daily_through is not None:
                day = self.daily_through + timedelta(days=1)
            elif self.pending:
                day = local_date(self.pending[0][0])
            else:
                return
            cutoff = daily_cutoff(day)
            if cutoff > self.last_ts:
                return
            window = {}
            consumed = 0
            for ts, pk, fuel, price in self.pending:
                if ts > cutoff:
                    break
                window[(pk, fuel)] = price  # last event before the cutoff wins
                consumed += 1
            del self.pending[:consumed]
            rows = []
            for key in sorted(window, key=lambda k: (int(k[0]), k[1])):
                if window[key] != self.daily.get(key, ""):
                    self.daily[key] = window[key]
                    rows.append([day.isoformat(), key[0], key[1], window[key]])
            self.daily_writer(day).writerows(rows)
            self.daily_through = day


def list_commits(rev_range):
    """Commits touching data/ in the given range, oldest first, as (sha, ts)."""
    out = git("log", "--reverse", "--format=%H%x09%ct", rev_range, "--", "data/")
    return [(sha, format_ts(ct)) for sha, ct in (line.split("\t") for line in out.splitlines())]


def load_json(blob, what):
    try:
        return json.loads(blob, parse_float=Decimal)
    except ValueError as e:
        log(f"warning: {what}: invalid JSON ({e})")
        return None


def read_snapshot(blobs, sha):
    """Return (pages, fuel_json, franchise_json) for a commit, or None if the snapshot is unusable."""
    pages = []
    for prefix in ("data/search_page_", "data/page_"):
        for n in range(1, MAX_PAGES + 1):
            blob = blobs.read(sha, f"{prefix}{n}.json")
            if blob is None:
                break
            page = load_json(blob, f"{sha[:8]}:{prefix}{n}.json")
            if page is None or not isinstance(page.get("results"), list):
                log(f"skipping snapshot {sha[:8]}: page {n} is not a valid search page")
                return None
            pages.append(page)
        if pages:
            break
    if not pages:
        return None  # e.g. the .gitkeep-only commits
    fuel_blob = blobs.read(sha, "data/fuel.json")
    franchise_blob = blobs.read(sha, "data/franchise.json")
    fuel_json = load_json(fuel_blob, f"{sha[:8]}:fuel.json") if fuel_blob else None
    franchise_json = load_json(franchise_blob, f"{sha[:8]}:franchise.json") if franchise_blob else None
    if not isinstance(fuel_json, list):
        fuel_json = None
    if not isinstance(franchise_json, list):
        franchise_json = None
    return pages, fuel_json, franchise_json


def run(history, commits):
    blobs = GitBlobs()
    history.open_writers()
    total = 0
    try:
        for i, (sha, ts) in enumerate(commits, 1):
            snap = read_snapshot(blobs, sha)
            if snap is None:
                continue
            total += history.process_snapshot(sha, ts, *snap)
            if i % 500 == 0 or i == len(commits):
                log(f"  {i}/{len(commits)} commits, {total} price changes so far ({ts})")
    finally:
        history.close_writers()
        blobs.close()
    history.write_dimensions()
    return total


def rebuild(out_dir):
    out_dir = Path(out_dir)
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    history = History(out_dir)
    commits = list_commits("HEAD")
    log(f"rebuilding from {len(commits)} commits touching data/")
    run(history, commits)
    return history


def incremental():
    history = History(HISTORY)
    history.load()
    head = git("rev-parse", "HEAD").strip()
    if head == history.last_commit:
        log("history is up to date")
        return 0
    try:
        commits = list_commits(f"{history.last_commit}..HEAD")
    except subprocess.CalledProcessError as e:
        raise SystemExit(
            f"cannot list commits since {history.last_commit[:8]} (shallow clone?): {e.stderr.strip()}"
        )
    if not commits:
        log("no new data/ commits since last processed snapshot")
        return 0
    log(f"processing {len(commits)} new commits since {history.last_commit[:8]}")
    return run(history, commits)


OUTPUT_ITEMS = ["prices", "daily", "snapshots.csv", "stations.csv", "station_franchise.csv", "fuels.csv", "franchises.csv", "state.json"]


def replace_history(tmp_dir):
    """Atomically-ish move a rebuilt history into history/, keeping README.md and other files."""
    HISTORY.mkdir(exist_ok=True)
    for item in OUTPUT_ITEMS:
        dst = HISTORY / item
        if dst.is_dir():
            shutil.rmtree(dst)
        elif dst.exists():
            dst.unlink()
        shutil.move(str(Path(tmp_dir) / item), str(dst))


def verify(tmp_dir):
    rebuild(tmp_dir)
    differing = []
    for item in OUTPUT_ITEMS:
        a, b = HISTORY / item, Path(tmp_dir) / item
        if a.is_dir():
            cmp = filecmp.dircmp(a, b)
            differing += [f"{item}/{n}" for n in cmp.diff_files + cmp.left_only + cmp.right_only]
        elif not a.exists() or not filecmp.cmp(a, b, shallow=False):
            differing.append(item)
    if differing:
        log("VERIFY FAILED - differences in: " + ", ".join(differing))
        return 1
    log("verify OK: history/ matches a full rebuild")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--rebuild", action="store_true", help="rebuild history/ from the whole git history")
    mode.add_argument("--verify", action="store_true", help="rebuild into a temp dir and compare with history/")
    args = parser.parse_args()

    if args.rebuild or args.verify:
        with tempfile.TemporaryDirectory(prefix="goriva-history-") as tmp:
            if args.verify:
                return verify(tmp)
            history = rebuild(tmp)
            replace_history(tmp)
            log(f"history/ rebuilt up to {history.last_commit[:8]} ({history.last_ts})")
            return 0
    incremental()
    return 0


if __name__ == "__main__":
    sys.exit(main())
