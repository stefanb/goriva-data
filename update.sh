#!/bin/bash

# https://ec.haxx.se/cmdline/cmdline-globbing
curl "https://goriva.si/api/v1/search/?format=json&franchise=&name=&o=&page=[1-22]&position=&radius=" -o "data/page_#1.json"

for f in data/*.json
do
    echo "Prettyfying $f"
    cat "$f" | jq '.' > "$f.tmp" && mv "$f.tmp" "$f"
done
