#!/usr/bin/env python3
"""Helper to write rank_image.py - reads from smash source and adapts paths."""
import shutil
import re

# Source file path
SRC = r"C:\Users\pixlb\Downloads\MainSmash-update-ui-fix\MainSmash-update-ui-fix\smash\modules\utils\image_generator.py"
DST = r"C:\Users\pixlb\OneDrive\Documents\Pi\bot\rank_image.py"

# Read source
with open(SRC, "r", encoding="utf-8") as f:
    content = f.read()

print(f"Read {len(content)} chars from source")

# Write destination (same content, just at new location)
with open(DST, "w", encoding="utf-8") as f:
    f.write(content)

print(f"Wrote {DST}")
print("Done!")
