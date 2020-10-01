#!/bin/bash

# https://goriva.si/api/v1/search/
# https://ec.haxx.se/cmdline/cmdline-globbing
curl "https://goriva.si/api/v1/search/?format=json&franchise=&name=&o=&page=[1-22]&position=&radius=" -o "data/search_page_#1.json"

# https://goriva.si/api/v1/fuel/
curl "https://goriva.si/api/v1/fuel/?format=json" -o "data/fuel.json"

# https://goriva.si/api/v1/franchise/
curl "https://goriva.si/api/v1/franchise/?format=json" -o "data/franchise.json"

for f in data/*.json
do
    echo "Prettyfying $f"
    cat "$f" | jq '.' > "$f.tmp" && mv "$f.tmp" "$f"
done
