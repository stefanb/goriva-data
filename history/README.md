# Fuel price history for Slovenia (machine-readable)

This folder contains the complete price history scraped from
[goriva.si](https://goriva.si) since **2020-10-01**, derived from the git
history of the raw `data/*.json` snapshots in this repository. It is
regenerated automatically after every data update by
[`scripts/build_history.py`](../scripts/build_history.py).

All files are UTF-8 CSV with a header row, `\n` line endings, comma separated,
minimal quoting. Timestamps are ISO-8601 in **UTC** (`2026-08-26T13:17:27Z`).
Prices are in **EUR per litre** (EUR/kg for CNG/LNG) as reported by the source.

## Files

| File | Rows | Description |
| --- | --- | --- |
| [`prices/<year>.csv`](prices/) | `ts,station_pk,fuel,price` | **Price change events**, one file per calendar year of `ts`. |
| [`daily/<year>.csv`](daily/) | `date,station_pk,fuel,price` | **One price per calendar day** (local date), as change events: a row when the daily price differs from the previous day. |
| [`snapshots.csv`](snapshots.csv) | `ts,commit,count,pages,stations,duplicates,changes` | One row per processed snapshot (git commit): provenance and scrape-quality info. |
| [`stations.csv`](stations.csv) | `pk,franchise_pk,name,address,zip_code,lat,lng,first_seen,last_seen` | Every station ever seen, with its latest known attributes. |
| [`station_franchise.csv`](station_franchise.csv) | `ts,station_pk,franchise_pk` | Events: the station's franchise (brand) as of `ts`; a row is emitted on first sight and on every change. |
| [`fuels.csv`](fuels.csv) | `pk,code,name,long_name` | Fuel types. `code` is the value used in the `fuel` column of `prices/` and `daily/`. |
| [`franchises.csv`](franchises.csv) | `pk,name` | Franchises (brands). Includes franchises that no longer exist at the source. |
| [`state.json`](state.json) | | Last processed commit; used by the incremental update. |

### [`prices/<year>.csv`](prices/) semantics

- A row means: *in the snapshot taken at `ts`, the price of `fuel` at station
  `station_pk` was different from the previously recorded value and is now
  `price`.* The first time a station/fuel is observed also produces a row.
- The price stays valid until the next row for the same `(station_pk, fuel)`.
  An **empty `price`** means the station now reports no price (`null`) for that
  fuel, i.e. the fuel is no longer offered; it ends the previous interval.
- Rows are append-only and ordered by `ts`, then `station_pk`, then `fuel`.
  Files for past years never change.
- If a station is temporarily missing from a snapshot (see *Data quality*),
  **no** row is emitted; its last price simply stays in effect.
- `price` always has exactly three decimals (`1.560`). Values are published as
  reported by the source, including obvious source errors (see below).
- `fuel` codes: `95`, `dizel`, `98`, `100`, `dizel-premium`, `avtoplin-lpg`,
  `KOEL` (heating oil), `hvo`, `cng`, `lng`. `hvo`/`cng`/`lng` exist since
  2024-06.

### [`daily/<year>.csv`](daily/) semantics

Use this table when you only need a date, not a time.

- The daily price of `(station_pk, fuel)` on local calendar day `date`
  (`Europe/Ljubljana`) is **the price in effect at 03:00 local time on that
  day**, i.e. the last `prices/` event with `ts <= 03:00 local`. A row is
  written only when that value differs from the previous day's; the first
  observation also produces a row. Forward-fill by `date` to get a value for
  every day. Empty `price` = no price for that fuel at that time.
- Why 03:00: prices change at local midnight and are visible in the 00:15
  scrape (commit ~00:29 local), which is occasionally delayed by an hour or two;
  03:00 is before the daytime noise starts (05h+) and exists on both DST
  transition days. See *When do prices change?* below. The exact cutoff barely
  matters (00:00 → 427k rows, 03:00 → 429k, 12:00 → 434k).
- Files are split by the year of `date`; rows are ordered by `date`,
  `station_pk`, `fuel`. The first day with rows is 2020-10-02 (the first
  snapshot was taken on 2020-10-01 after 03:00).
- Intraday flip-flops (see *Data quality*) that revert before the next cutoff
  disappear; ones that span a cutoff remain as day-level changes.

### [`snapshots.csv`](snapshots.csv)

- `ts` is the commit time of the snapshot, typically 1–3 minutes after the
  actual fetch. Snapshots are taken hourly but only committed when something
  changed, so gaps of many hours are normal.
- `count` is the total number of stations the API reported, `pages` the number
  of result pages fetched, `stations` the number of distinct stations in the
  snapshot, `duplicates` the number of stations that appeared on two pages
  (pagination race, see below), `changes` the number of price rows emitted.

### [`stations.csv`](stations.csv)

- `first_seen`/`last_seen` are snapshot timestamps. `last_seen` is **empty for
  stations present in the latest snapshot** (so the file does not change on
  every update) and filled in only for stations that have disappeared.
- Attributes (`name`, `address`, coordinates, `franchise_pk`) are the latest
  observed values; franchise changes over time are in `station_franchise.csv`.

## Examples

Build a step series (price intervals) for one station with
[DuckDB](https://duckdb.org):

```sql
SELECT ts AS valid_from,
       LEAD(ts) OVER (PARTITION BY station_pk, fuel ORDER BY ts) AS valid_to,
       station_pk, fuel, price
FROM read_csv('history/prices/*.csv')
WHERE station_pk = 771 AND fuel = 'dizel'
ORDER BY ts;
```

Daily national median of regular petrol (price in effect at the end of each day):

```sql
WITH p AS (
  SELECT *, LEAD(ts) OVER (PARTITION BY station_pk, fuel ORDER BY ts) AS valid_to
  FROM read_csv('history/prices/*.csv') WHERE fuel = '95' AND price IS NOT NULL
),
days AS (SELECT unnest(generate_series(DATE '2020-10-01', current_date, INTERVAL 1 DAY))::DATE AS day)
SELECT day, median(price) AS median_95, count(*) AS stations
FROM days JOIN p ON p.ts <= day + INTERVAL 1 DAY AND (p.valid_to IS NULL OR p.valid_to > day + INTERVAL 1 DAY)
GROUP BY day ORDER BY day;
```

Export to Parquet: `duckdb -c "COPY (SELECT * FROM read_csv('history/prices/*.csv')) TO 'prices.parquet'"`

pandas:

```python
import glob, pandas as pd
prices = pd.concat(pd.read_csv(f, parse_dates=["ts"]) for f in sorted(glob.glob("history/prices/*.csv")))
stations = pd.read_csv("history/stations.csv")
one = prices[(prices.station_pk == 771) & (prices.fuel == "dizel")].set_index("ts").price
one.plot(drawstyle="steps-post")
```

Daily table (already local-date based), e.g. the national median per day:

```sql
WITH d AS (
  SELECT *, LEAD(date) OVER (PARTITION BY station_pk, fuel ORDER BY date) AS until
  FROM read_csv('history/daily/*.csv') WHERE fuel = 'dizel' AND price IS NOT NULL
),
days AS (SELECT unnest(generate_series(DATE '2020-10-02', current_date, INTERVAL 1 DAY))::DATE AS day)
SELECT day, median(price) AS median_dizel, count(*) AS stations
FROM days JOIN d ON d.date <= day AND (d.until IS NULL OR d.until > day)
GROUP BY day ORDER BY day;
```

Time zone: Slovenian regulated prices change at local midnight on Tuesdays, so
convert `ts` to `Europe/Ljubljana` before grouping by day (or use `daily/`).

## When do prices change?

Distribution of all 558 011 price change events by hour of day in
`Europe/Ljubljana` (as of 2026-08-26; regenerate with
`python3 scripts/analyze_hours.py`):

```
00h ##################################################  358194  64.2%
01h #                                                    10977   2.0%
02h                                                       1975   0.4%
03h                                                        327   0.1%
04h                                                       2412   0.4%
05h                                                       4540   0.8%
06h ##                                                   15019   2.7%
07h                                                       6297   1.1%
08h                                                       7060   1.3%
09h #                                                     7353   1.3%
10h                                                       6275   1.1%
11h #                                                     8123   1.5%
12h #                                                    12674   2.3%
13h #                                                    11358   2.0%
14h ##                                                   14792   2.7%
15h #                                                    13876   2.5%
16h                                                       4858   0.9%
17h                                                       5159   0.9%
18h #                                                    13416   2.4%
19h #                                                     7495   1.3%
20h #                                                    13172   2.4%
21h #                                                    10184   1.8%
22h ##                                                   19556   3.5%
23h                                                       2919   0.5%
```

- **Two thirds of all changes are visible in the first scrape after local
  midnight** (00:15 local, commit ~00:29). In 2024–2025 the share is 89–93 %;
  59 % of all events fall on a Tuesday (the regulated price cycle).
- In UTC the peak splits into 22Z (summer) and 23Z (winter). **Do not cut days
  by the UTC date** — summer midnight changes would land on the previous day.
- Most daytime events are source-side noise: 110 k events (20 %) are
  short-lived reversals `A→B→A` with `B` lasting ≤ 24 h, and in 76 % of those
  `B` is exactly the price that was in effect *before* `A`, i.e. the source
  intermittently served stale data (worst in 2021–2023, mostly Petrol and
  MOL & INA stations). The `daily/` table with its 03:00 cutoff removes the
  intraday part of this noise; multi-day stale periods remain.

## Data quality notes

Everything is published as scraped; the following known artefacts are **not**
filtered out:

- **Pagination races.** The result pages (25 stations each, currently 23
  pages) are fetched one after another and
  the API ordering can shift in between, so a station may appear on two pages
  while another falls between pages. Duplicates are dropped (first occurrence
  wins, prices are identical); the missing station simply produces no event
  for that snapshot. The `duplicates` column of `snapshots.csv` shows when this happened
  (128 snapshots so far).
- **Temporarily missing stations.** For the same reason, and during source
  outages, stations can be absent from a few snapshots (e.g. from
  2022-07-13 06:41Z about 150 stations were missing for roughly five days).
  Absence never produces a price event.
- **Source typos.** A handful of values are clearly wrong at the source, e.g.
  `1020.000` (January 2021, meant `1.020`), `0.001`, and a number of `1.000`/
  `2.000` placeholders. They are kept verbatim.
- **Page cap (until 2026-08-27).** The scraper used to fetch a fixed 22 pages
  (550 stations) while the API reported 551–555 stations at times, so up to a
  handful of stations were unobserved in some periods. Since 2026-08-27 all
  pages are fetched (the `pages` column of `snapshots.csv` shows the count).
- **Number formatting.** Older snapshots were normalised by `jq` 1.6 (which
  prints `1.56` for `1.560`); prices are compared numerically at three
  decimals, so formatting differences never produce events.

## Rebuilding

The scripts need only Python 3.9+ (standard library: `zoneinfo`, `decimal`,
`csv`, …) and `git`; there are no third-party dependencies to install. On
Windows, `zoneinfo` additionally needs `pip install tzdata`.

```
python3 scripts/build_history.py --rebuild   # from scratch, needs a full clone (~1 min)
python3 scripts/build_history.py             # incremental, run by the workflow
python3 scripts/build_history.py --verify    # rebuild into a temp dir and compare
python3 scripts/analyze_hours.py             # hour-of-day statistics (read-only, ~15 s)
```

Source: [goriva.si](https://goriva.si) (Ministry of the Economy, Tourism and
Sport / Petrol data). Please attribute the source when reusing the data.
