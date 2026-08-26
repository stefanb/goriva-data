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
| `prices/<year>.csv` | `ts,station_pk,fuel,price` | **Price change events**, one file per calendar year of `ts`. |
| `snapshots.csv` | `ts,commit,count,pages,stations,duplicates,changes` | One row per processed snapshot (git commit): provenance and scrape-quality info. |
| `stations.csv` | `pk,franchise_pk,name,address,zip_code,lat,lng,first_seen,last_seen` | Every station ever seen, with its latest known attributes. |
| `station_franchise.csv` | `ts,station_pk,franchise_pk` | Events: the station's franchise (brand) as of `ts`; a row is emitted on first sight and on every change. |
| `fuels.csv` | `pk,code,name,long_name` | Fuel types. `code` is the key used in `prices.fuel`. |
| `franchises.csv` | `pk,name` | Franchises (brands). Includes franchises that no longer exist at the source. |
| `state.json` | | Last processed commit; used by the incremental update. |

### `prices/<year>.csv` semantics

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

### `snapshots.csv`

- `ts` is the commit time of the snapshot, typically 1–3 minutes after the
  actual fetch. Snapshots are taken hourly but only committed when something
  changed, so gaps of many hours are normal.
- `count` is the total number of stations the API reported, `pages` the number
  of result pages fetched, `stations` the number of distinct stations in the
  snapshot, `duplicates` the number of stations that appeared on two pages
  (pagination race, see below), `changes` the number of price rows emitted.

### `stations.csv`

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

Time zone: Slovenian regulated prices change at local midnight on Tuesdays, so
convert `ts` to `Europe/Ljubljana` before grouping by day.

## Data quality notes

Everything is published as scraped; the following known artefacts are **not**
filtered out:

- **Pagination races.** The 22 result pages are fetched one after another and
  the API ordering can shift in between, so a station may appear on two pages
  while another falls between pages. Duplicates are dropped (first occurrence
  wins, prices are identical); the missing station simply produces no event
  for that snapshot. `snapshots.csv.duplicates` shows when this happened
  (128 snapshots so far).
- **Temporarily missing stations.** For the same reason, and during source
  outages, stations can be absent from a few snapshots (e.g. from
  2022-07-13 06:41Z about 150 stations were missing for roughly five days).
  Absence never produces a price event.
- **Source typos.** A handful of values are clearly wrong at the source, e.g.
  `1020.000` (January 2021, meant `1.020`), `0.001`, and a number of `1.000`/
  `2.000` placeholders. They are kept verbatim.
- **Page cap.** The scraper fetches a fixed 22 pages (550 stations) while the
  API has reported 551–555 stations at times, so a few stations may be
  unobserved in some periods.
- **Number formatting.** Older snapshots were normalised by `jq` 1.6 (which
  prints `1.56` for `1.560`); prices are compared numerically at three
  decimals, so formatting differences never produce events.

## Rebuilding

```
python3 scripts/build_history.py --rebuild   # from scratch, needs a full clone (~1 min)
python3 scripts/build_history.py             # incremental, run by the workflow
python3 scripts/build_history.py --verify    # rebuild into a temp dir and compare
```

Source: [goriva.si](https://goriva.si) (Ministry of the Economy, Tourism and
Sport / Petrol data). Please attribute the source when reusing the data.
