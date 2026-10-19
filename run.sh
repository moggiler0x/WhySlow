#!/bin/sh
cd "$(dirname "$0")"
python3 -m pip install -q --user -r requirements.txt
exec python3 -m whyslow "$@"
