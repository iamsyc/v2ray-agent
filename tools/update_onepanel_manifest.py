#!/usr/bin/env python3
"""Bind the single-file bootstrap to the exact reviewed backend assets."""
import argparse
import hashlib
from pathlib import Path
import re

parser = argparse.ArgumentParser()
parser.add_argument('--check', action='store_true')
args = parser.parse_args()
root = Path(__file__).resolve().parents[1]
path = root / 'install.sh'
before = path.read_text()
after = before
for name, file in [('Backend', 'backend.sh'), ('Controller', 'controller.py')]:
    digest = hashlib.sha256((root / 'onepanel' / file).read_bytes()).hexdigest()
    after, count = re.subn(r"(onepanel" + name + r"SHA256=)'[^']*'", r"\g<1>'" + digest + "'", after)
    if count != 1:
        raise SystemExit('Missing bootstrap digest field')
if args.check:
    if after != before:
        raise SystemExit('Backend assets changed: run python3 tools/update_onepanel_manifest.py')
else:
    path.write_text(after)
