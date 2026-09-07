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
#
# Because of that unstable ordering the per-page diffs are noisy, so all pages
# are also merged into data/search.json, deduplicated by pk and sorted by pk.
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
page_files=()
while [ "$url" != "null" ]; do
    if [ "$page" -gt "$MAX_PAGES" ]; then
        echo "More than $MAX_PAGES search pages, giving up" >&2
        exit 1
    fi
    "${CURL[@]}" "$url" | jq '.' > "$tmp/search_page_$page.json"
    page_files+=("$tmp/search_page_$page.json")
    url=$(jq -r '.next' "$tmp/search_page_$page.json")
    page=$((page + 1))
done
pages=$((page - 1))

# Merge all pages into one deterministically ordered file so the git diff shows
# only real changes. unique_by(.pk) keeps the first occurrence of a station that
# the pagination race put on two pages, and its output is sorted by pk. Pages are
# passed in numeric order (not a glob, which would put page 10 before page 2).
# Sorted by pk only, on purpose: sorting by franchise would move a station in
# the file whenever it changes franchise.
jq -s '[.[].results[]] | unique_by(.pk) | {count: length, results: .}' \
    "${page_files[@]}" > "$tmp/search.json"

# https://goriva.si/api/v1/fuel/
"${CURL[@]}" "$API/fuel/?format=json" | jq '.' > "$tmp/fuel.json"

# https://goriva.si/api/v1/franchise/
"${CURL[@]}" "$API/franchise/?format=json" | jq '.' > "$tmp/franchise.json"

# Replace the snapshot: drop pages that no longer exist, then move the new files in.
rm -f data/search_page_*.json
mv "$tmp"/*.json data/

echo "Fetched $pages search pages, $(jq '.count' data/search_page_1.json) stations reported by the API, $(jq '.count' data/search.json) unique stations in search.json"
