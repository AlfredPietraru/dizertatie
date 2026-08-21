"""Command-line entry point for the stable Semester 1 pipeline."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .pipeline import SemesterOnePipeline


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ros-config-builder")
    subcommands = parser.add_subparsers(dest="command", required=True)
    analyze = subcommands.add_parser("analyze", help="extract and integrate a ROS workspace")
    analyze.add_argument("workspace", nargs="?", type=Path, default=Path.cwd())
    analyze.add_argument(
        "--root-launch",
        action="append",
        required=True,
        help="workspace-relative root launch file; repeat for multiple roots",
    )
    analyze.add_argument("--output", type=Path)
    validate = subcommands.add_parser("validate", help="validate a system-model JSON file")
    validate.add_argument("model", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "analyze":
        pipeline = SemesterOnePipeline(args.workspace)
        model = pipeline.integrate(args.root_launch)
        report = pipeline.validate(model)
        payload = {"model": model, "validation": report}
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        else:
            print(json.dumps(payload, indent=2))
        return 0 if not report.get("errors") else 1
    model = json.loads(args.model.read_text(encoding="utf-8"))
    report = SemesterOnePipeline(args.model.parent).validate(model)
    print(json.dumps(report, indent=2))
    return 0 if not report.get("errors") else 1
