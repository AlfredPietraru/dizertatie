"""Command-line facade for the repository extraction package."""

from __future__ import annotations

import argparse
from pathlib import Path

from .api import extract_code_information
from .parameter_graph_tools import extract_parameter_artifacts


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="repo_code_extractor",
        description="Ingest a repository and extract static code information.",
    )
    parser.add_argument("workspace", nargs="?", type=Path, default=Path.cwd())
    parser.add_argument("--source-root", action="append", dest="source_roots")
    parser.add_argument(
        "--output", type=Path, default=Path("artifacts/repo_ingestion")
    )
    parser.add_argument("--no-chunks", action="store_true")
    parser.add_argument("--no-static-analysis", action="store_true")
    parser.add_argument(
        "--parameter",
        help="extract context for one exact parameter after ensuring artifacts are current",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.parameter:
        result = extract_parameter_artifacts(args.parameter, repo_root=args.workspace)
        print(f"Parameter artifacts saved to {result['artifact_paths']['directory']}")
        return 0 if result["status"] == "ok" else 2

    result = extract_code_information(
        args.workspace,
        source_roots=args.source_roots,
        output_directory=args.output,
        include_chunks=not args.no_chunks,
        include_static_analysis=not args.no_static_analysis,
    )
    print(f"Artifacts saved to {result.output_directory}")
    for stage, summary in result.summaries.items():
        print(f"{stage}: {summary}")
    failures = sum(
        int(summary.get("parse_failures", 0))
        for summary in result.summaries.values()
        if isinstance(summary, dict)
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
