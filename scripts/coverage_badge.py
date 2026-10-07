#!/usr/bin/env python
"""Render ``coverage.svg`` from the coverage data already on disk.

Why this exists rather than ``coverage-badge``: that package imports
``pkg_resources``, which setuptools 84 removed, so it fails outright on a current
toolchain. A shields.io-shaped badge is a dozen lines of SVG, and generating it
here removes a dependency that is already broken rather than pinning an old
setuptools to keep it alive.

Reads the total from ``coverage report --format=total``, so it always reflects the
same number the CI floor is checked against.

Usage:
    python scripts/coverage_badge.py -o coverage.svg
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

#: shields.io's colour ramp, so the badge reads the same as every other one in
#: the README rather than inventing a palette.
THRESHOLDS: list[tuple[int, str]] = [
    (90, "#4c1"),  # brightgreen
    (80, "#97ca00"),  # green
    (70, "#a4a61d"),  # yellowgreen
    (60, "#dfb317"),  # yellow
    (50, "#fe7d37"),  # orange
    (0, "#e05d44"),  # red
]

TEMPLATE = """<svg xmlns="http://www.w3.org/2000/svg" width="108" height="20" role="img" \
aria-label="coverage: {pct}%">
  <title>coverage: {pct}%</title>
  <linearGradient id="s" x2="0" y2="100%">
    <stop offset="0" stop-color="#bbb" stop-opacity=".1"/>
    <stop offset="1" stop-opacity=".1"/>
  </linearGradient>
  <clipPath id="r"><rect width="108" height="20" rx="3" fill="#fff"/></clipPath>
  <g clip-path="url(#r)">
    <rect width="63" height="20" fill="#555"/>
    <rect x="63" width="45" height="20" fill="{color}"/>
    <rect width="108" height="20" fill="url(#s)"/>
  </g>
  <g fill="#fff" text-anchor="middle" \
font-family="Verdana,Geneva,DejaVu Sans,sans-serif" text-rendering="geometricPrecision" \
font-size="110">
    <text aria-hidden="true" x="325" y="150" fill="#010101" fill-opacity=".3" \
transform="scale(.1)" textLength="530">coverage</text>
    <text x="325" y="140" transform="scale(.1)" fill="#fff" textLength="530">coverage</text>
    <text aria-hidden="true" x="845" y="150" fill="#010101" fill-opacity=".3" \
transform="scale(.1)" textLength="350">{pct}%</text>
    <text x="845" y="140" transform="scale(.1)" fill="#fff" textLength="350">{pct}%</text>
  </g>
</svg>
"""


def total_percent() -> int:
    """Total coverage as a whole percent, straight from coverage.py."""
    try:
        raw = subprocess.run(
            # sys.executable rather than a bare "coverage": the script must read
            # the data with the same interpreter it was started with, whether that
            # is the project venv or a CI runner.
            [sys.executable, "-m", "coverage", "report", "--format=total"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        sys.exit(f"could not read coverage data: {exc}")
    return round(float(raw))


def colour_for(pct: int) -> str:
    return next(colour for floor, colour in THRESHOLDS if pct >= floor)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-o", "--output", default="coverage.svg", help="SVG path to write")
    args = parser.parse_args()

    pct = total_percent()
    Path(args.output).write_text(TEMPLATE.format(pct=pct, color=colour_for(pct)), encoding="utf-8")
    print(f"{args.output}: coverage {pct}% ({colour_for(pct)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
