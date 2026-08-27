#!/bin/bash
# Fetch the current snapshot of goriva.si into data/.
#
# The search endpoint is paginated (25 stations per page) and the number of
# stations changes over time, so instead of a fixed page range we follow the
# "next" link until it is null. The API ignores page_size and ordering
# parameters. Everything is downloaded into a temporary directory first and
# moved into data/ only when all requests succeeded, so a failed run never
# leaves a partial or mixed snapshot behind.
#
# Known limitation: the default ordering is not stable between requests, so a
# station can occasionally appear on two pages (or fall between pages) while
# the pages are being fetched. This cannot be fixed on the client side; keep
# the loop fast (no sleeps) to minimise the window.
set -euo pipefail

API="https://goriva.si/api/v1"
CURL=(curl -fsS --retry 3 --retry-delay 5)
MAX_PAGES=200 # safety bound against a never-ending "next" chain

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

# https://goriva.si/api/v1/search/
# Same first-page URL as before, so the "next" URLs and file contents stay identical.
url="$API/search/?format=json&franchise=&name=&o=&page=1&position=&radius="
page=1
while [ "$url" != "null" ]; do
    if [ "$page" -gt "$MAX_PAGES" ]; then
        echo "More than $MAX_PAGES search pages, giving up" >&2
        exit 1
    fi
    "${CURL[@]}" "$url" | jq '.' > "$tmp/search_page_$page.json"
    url=$(jq -r '.next' "$tmp/search_page_$page.json")
    page=$((page + 1))
done
pages=$((page - 1))

# https://goriva.si/api/v1/fuel/
"${CURL[@]}" "$API/fuel/?format=json" | jq '.' > "$tmp/fuel.json"

# https://goriva.si/api/v1/franchise/
"${CURL[@]}" "$API/franchise/?format=json" | jq '.' > "$tmp/franchise.json"

# Replace the snapshot: drop pages that no longer exist, then move the new files in.
rm -f data/search_page_*.json
mv "$tmp"/*.json data/

echo "Fetched $pages search pages, $(jq '.count' data/search_page_1.json) stations reported by the API"
