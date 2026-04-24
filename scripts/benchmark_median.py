#!/usr/bin/env python3

from __future__ import annotations

import argparse
import os
import re
import statistics
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any


ROW_RE_WITH_US = re.compile(
    r"^(?P<operation>[a-z0-9_]+)\s+"
    r"(?P<pattern>[a-z0-9_]+)\s+"
    r"(?P<rows>\d+)\s+"
    r"(?P<time_us>\d+)\s+"
    r"(?P<time_ms>\d+)\s+"
    r"(?P<rows_per_sec>\d+(?:\.\d+)?)\s+"
    r"(?P<note>.+)$"
)

ROW_RE_LEGACY = re.compile(
    r"^(?P<operation>[a-z0-9_]+)\s+"
    r"(?P<pattern>[a-z0-9_]+)\s+"
    r"(?P<rows>\d+)\s+"
    r"(?P<time_ms>\d+)\s+"
    r"(?P<rows_per_sec>\d+(?:\.\d+)?)\s+"
    r"(?P<note>.+)$"
)


def parse_tables(output: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    section: str | None = None
    in_table = False

    for raw_line in output.splitlines():
        line = raw_line.strip()

        if line.startswith("RookDB "):
            section = line
            in_table = False
            continue

        if section is None:
            continue

        if line.startswith("operation") and "rows_per_sec" in line:
            in_table = True
            continue

        if not in_table:
            continue

        if not line or set(line) == {"-"}:
            continue

        match = ROW_RE_WITH_US.match(line)
        if match:
            row = match.groupdict()
            rows.append(
                {
                    "section": section,
                    "operation": row["operation"],
                    "pattern": row["pattern"],
                    "rows": int(row["rows"]),
                    "time_us": int(row["time_us"]),
                    "time_ms": int(row["time_ms"]),
                    "rows_per_sec": float(row["rows_per_sec"]),
                    "note": row["note"],
                }
            )
            continue

        match = ROW_RE_LEGACY.match(line)
        if match:
            row = match.groupdict()
            rows.append(
                {
                    "section": section,
                    "operation": row["operation"],
                    "pattern": row["pattern"],
                    "rows": int(row["rows"]),
                    "time_us": None,
                    "time_ms": int(row["time_ms"]),
                    "rows_per_sec": float(row["rows_per_sec"]),
                    "note": row["note"],
                }
            )

    return rows


def median_int(values: list[int]) -> int:
    return int(round(float(statistics.median(values))))


def build_report(
    run_count: int,
    command: str,
    section_order: list[str],
    key_order: list[tuple[str, str, str, int, str]],
    buckets: dict[tuple[str, str, str, int, str], dict[str, list[float | int]]],
) -> str:
    lines: list[str] = []
    lines.append(f"## Median Benchmark Report ({run_count} runs)")
    lines.append("")
    lines.append("Command:")
    lines.append("")
    lines.append("```bash")
    lines.append(command)
    lines.append("```")

    for section in section_order:
        keys = [k for k in key_order if k[0] == section]
        if not keys:
            continue

        has_us = any(buckets[k]["time_us"] for k in keys)

        lines.append("")
        lines.append(f"### {section}")
        lines.append("")
        if has_us:
            lines.append("| operation | pattern | rows | median_time_us | median_time_ms | median_rows_per_sec | note |")
            lines.append("|---|---:|---:|---:|---:|---:|---|")
        else:
            lines.append("| operation | pattern | rows | median_time_ms | median_rows_per_sec | note |")
            lines.append("|---|---:|---:|---:|---:|---|")

        for key in keys:
            section_name, operation, pattern, rows_value, note = key
            _ = section_name
            bucket = buckets[key]
            med_ms = median_int([int(v) for v in bucket["time_ms"]])
            med_rps = statistics.median([float(v) for v in bucket["rows_per_sec"]])

            if has_us:
                us_values = [int(v) for v in bucket["time_us"] if v is not None]
                med_us = median_int(us_values) if us_values else 0
                lines.append(
                    f"| {operation} | {pattern} | {rows_value} | {med_us} | {med_ms} | {med_rps:.2f} | {note} |"
                )
            else:
                lines.append(
                    f"| {operation} | {pattern} | {rows_value} | {med_ms} | {med_rps:.2f} | {note} |"
                )

    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run benchmark command N times and emit median markdown tables"
    )
    parser.add_argument("--runs", type=int, default=5, help="number of benchmark runs")
    parser.add_argument("--cwd", type=str, default=".", help="working directory")
    parser.add_argument(
        "--output",
        type=str,
        default="",
        help="optional markdown output path",
    )
    parser.add_argument(
        "--log-dir",
        type=str,
        default="",
        help="optional directory to store per-run raw logs",
    )
    parser.add_argument("command", nargs=argparse.REMAINDER, help="command to execute after --")
    args = parser.parse_args()

    if args.runs <= 0:
        print("--runs must be > 0", file=sys.stderr)
        return 2

    command_parts = args.command
    if command_parts and command_parts[0] == "--":
        command_parts = command_parts[1:]
    if not command_parts:
        print("missing benchmark command; provide it after --", file=sys.stderr)
        return 2

    command = " ".join(command_parts)

    log_dir = Path(args.log_dir) if args.log_dir else None
    if log_dir:
        log_dir.mkdir(parents=True, exist_ok=True)

    section_order: list[str] = []
    key_order: list[tuple[str, str, str, int, str]] = []
    buckets: dict[tuple[str, str, str, int, str], dict[str, list[float | int]]] = defaultdict(
        lambda: {"time_us": [], "time_ms": [], "rows_per_sec": []}
    )

    for run_index in range(1, args.runs + 1):
        print(f"[median] run {run_index}/{args.runs}", file=sys.stderr)
        proc = subprocess.run(
            command,
            cwd=args.cwd,
            shell=True,
            env=os.environ.copy(),
            text=True,
            capture_output=True,
        )

        combined_output = f"{proc.stdout}\n{proc.stderr}".strip()
        if log_dir:
            (log_dir / f"run_{run_index}.log").write_text(combined_output + "\n")

        if proc.returncode != 0:
            print(combined_output, file=sys.stderr)
            print(f"command failed on run {run_index}", file=sys.stderr)
            return proc.returncode

        rows = parse_tables(combined_output)
        if not rows:
            print(combined_output, file=sys.stderr)
            print("no benchmark rows parsed from command output", file=sys.stderr)
            return 1

        for row in rows:
            section = str(row["section"])
            if section not in section_order:
                section_order.append(section)

            key = (
                section,
                str(row["operation"]),
                str(row["pattern"]),
                int(row["rows"]),
                str(row["note"]),
            )
            if key not in buckets:
                key_order.append(key)

            time_us = row["time_us"]
            if time_us is not None:
                buckets[key]["time_us"].append(int(time_us))
            buckets[key]["time_ms"].append(int(row["time_ms"]))
            buckets[key]["rows_per_sec"].append(float(row["rows_per_sec"]))

    report = build_report(args.runs, command, section_order, key_order, buckets)

    if args.output:
        Path(args.output).write_text(report + "\n")
    else:
        print(report)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
