"""Command line entry point for one collection run.

Exit code is 0 even when the source could not be reached. That is deliberate:
the run has done its job by recording the failure, and a red build every time a
public service has a bad minute trains the maintainer to ignore red builds,
which is how a genuinely broken collector goes unnoticed for a month.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from borderflow.collect import CORRIDOR_PORTS, collect


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="borderflow", description=__doc__)
    parser.add_argument("--data", default="data", type=Path, help="output directory")
    parser.add_argument(
        "--ports",
        nargs="*",
        default=list(CORRIDOR_PORTS),
        help="ports to collect (defaults to the corridor)",
    )
    args = parser.parse_args(argv)

    record = collect(args.data, ports=tuple(args.ports))

    if record.ok:
        seen = ", ".join(record.ports_seen) or "none"
        print(f"ok   {record.rows} new observations  ports: {seen}")
        print(f"     resolved to {record.resolved_url}")
    else:
        print(f"fail {record.error}", file=sys.stderr)
        print("     recorded as a collection failure, not as an absence of activity")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
