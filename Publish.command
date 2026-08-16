#!/bin/bash
# Double-click me in Finder to publish: turns anything in posts/ into a post, then pushes.
cd "$(dirname "$0")" || exit 1
/usr/bin/env python3 tools/sync.py --push
echo
read -n 1 -s -r -p "Finished - press any key to close this window."
