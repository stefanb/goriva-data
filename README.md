# Fuel prices for Slovenia

[![Periodic update](https://github.com/stefanb/goriva-data/workflows/Periodic%20update/badge.svg)](https://github.com/stefanb/goriva-data/actions?query=workflow%3A%22Periodic+update%22)

Fuel price data for Slovenia from [goriva.si](https://goriva.si)

- `data/` – latest raw API snapshots (stations with current prices, fuel types, franchises), refreshed hourly.
  - `data/search.json` – all stations merged from the paginated `search_page_N.json` responses, deduplicated and sorted by `pk`, so its git diff shows only real changes. Use this file rather than the individual pages.
  - `data/search.geojson` – the same stations as a GeoJSON `FeatureCollection` (one Point per station, `id` = `pk`, sorted by `pk`, prices flattened to `price_<fuel>` properties, `franchise_name` joined in, `geometry: null` when the API has no valid location).
- `history/` – **complete price history since 2020-10-01 as CSV** (price change events per station and fuel, stations, franchises). See [history/README.md](history/README.md) for the schema and query examples.

Ministrstvo za gospodarski razvoj in tehnologijo - https://www.rtvslo.si/gospodarstvo/potrosniki-lahko-preverijo-kje-so-cene-goriv-najugodnejse/537438
