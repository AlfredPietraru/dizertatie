#!/usr/bin/env python3
"""Count or estimate LLM tokens in the selected workspace source roots."""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterable


DEFAULT_SOURCE_ROOTS = (
    Path("src/antrobot_ros"),
    Path("src/antrobot_description"),
    Path("src/kiss-icp"),
    Path("src/kinematic-icp"),
)

IGNORED_DIRECTORIES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "**pycache**",
    "build",
    "dist",
    "install",
    "log",
    "logs",
    "site-packages",
    "venv",
}


@dataclass(frozen=True)
class FileCount:
    root: str
    path: str
    bytes: int
    characters: int
    tokens: int


def repository_root() -> Path:
    """Find the repository root from this script, never from the current directory."""
    script_path = Path(__file__).resolve()
    for candidate in script_path.parents:
        if (candidate / ".git").exists():
            return candidate
    raise RuntimeError(f"Could not find repository root above {script_path}")


def is_ignored_directory(name: str) -> bool:
    """Match literal ignored names and the requested **pycache** pattern."""
    return name in IGNORED_DIRECTORIES or "pycache" in name.casefold()


def is_binary(path: Path) -> bool:
    """Return whether a representative file sample appears to be binary."""
    try:
        with path.open("rb") as stream:
            sample = stream.read(8192)
    except OSError:
        return True
    if b"\x00" in sample:
        return True
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


def source_files(root: Path) -> Iterable[Path]:
    """Yield files while pruning ignored directories before traversal."""
    for directory_name, directory_names, file_names in os.walk(root):
        directory_names[:] = sorted(
            name for name in directory_names if not is_ignored_directory(name)
        )
        directory = Path(directory_name)
        for file_name in sorted(file_names):
            path = directory / file_name
            if path.is_file() and not path.is_symlink():
                yield path


def estimated_utf8_tokens(text: str) -> int:
    """Estimate tokens using the common approximation of four UTF-8 bytes each."""
    byte_count = len(text.encode("utf-8"))
    return (byte_count + 3) // 4


def tokenizer(encoding_name: str) -> tuple[Callable[[str], int], str, bool]:
    """Use tiktoken when installed; otherwise return a labeled approximation."""
    try:
        import tiktoken
    except ImportError:
        return estimated_utf8_tokens, "utf8_bytes_divided_by_4", False

    encoding = tiktoken.get_encoding(encoding_name)
    return lambda value: len(encoding.encode(value)), f"tiktoken:{encoding_name}", True


def count_sources(
    workspace: Path,
    roots: list[Path],
    count_tokens: Callable[[str], int],
) -> tuple[list[FileCount], list[str], list[str]]:
    counts: list[FileCount] = []
    skipped_binary: list[str] = []
    missing_roots: list[str] = []

    for configured_root in roots:
        if configured_root.is_absolute():
            raise ValueError(
                f"Source roots must be relative to the repository root: {configured_root}"
            )
        root = (workspace / configured_root).resolve()
        try:
            root.relative_to(workspace)
        except ValueError as error:
            raise ValueError(f"Source root is outside the workspace: {root}") from error
        if not root.is_dir():
            missing_roots.append(configured_root.as_posix())
            continue

        for path in source_files(root):
            relative = path.relative_to(workspace).as_posix()
            if is_binary(path):
                skipped_binary.append(relative)
                continue
            try:
                raw = path.read_bytes()
                content = raw.decode("utf-8")
            except (OSError, UnicodeDecodeError):
                skipped_binary.append(relative)
                continue
            counts.append(
                FileCount(
                    root=configured_root.as_posix(),
                    path=relative,
                    bytes=len(raw),
                    characters=len(content),
                    tokens=count_tokens(content),
                )
            )

    counts.sort(key=lambda item: item.path)
    skipped_binary.sort()
    return counts, skipped_binary, missing_roots


def report(
    workspace: Path,
    roots: list[Path],
    counts: list[FileCount],
    skipped_binary: list[str],
    missing_roots: list[str],
    tokenizer_name: str,
    exact: bool,
) -> dict[str, object]:
    root_totals: dict[str, dict[str, int]] = {}
    for root in roots:
        root_counts = [item for item in counts if item.root == root.as_posix()]
        root_totals[root.as_posix()] = {
            "files": len(root_counts),
            "bytes": sum(item.bytes for item in root_counts),
            "characters": sum(item.characters for item in root_counts),
            "tokens": sum(item.tokens for item in root_counts),
        }

    return {
        "workspace": workspace.as_posix(),
        "source_roots": [root.as_posix() for root in roots],
        "ignored_directories": sorted(IGNORED_DIRECTORIES),
        "tokenizer": tokenizer_name,
        "exact_token_count": exact,
        "summary": {
            "files": len(counts),
            "bytes": sum(item.bytes for item in counts),
            "characters": sum(item.characters for item in counts),
            "tokens": sum(item.tokens for item in counts),
            "skipped_binary_files": len(skipped_binary),
            "missing_roots": len(missing_roots),
        },
        "by_root": root_totals,
        "missing_roots": missing_roots,
        "skipped_binary_files": skipped_binary,
        "files": [asdict(item) for item in counts],
    }


def print_report(data: dict[str, object], include_files: bool) -> None:
    qualifier = "exact" if data["exact_token_count"] else "estimated"
    print(f"Tokenizer: {data['tokenizer']} ({qualifier})")
    print()
    print(f"{'Source root':<36} {'Files':>8} {'Bytes':>14} {'Tokens':>14}")
    print(f"{'-' * 36} {'-' * 8} {'-' * 14} {'-' * 14}")
    for root, totals in data["by_root"].items():
        print(
            f"{root:<36} {totals['files']:>8,} "
            f"{totals['bytes']:>14,} {totals['tokens']:>14,}"
        )
    summary = data["summary"]
    print(f"{'TOTAL':<36} {summary['files']:>8,} {summary['bytes']:>14,} {summary['tokens']:>14,}")
    if not data["exact_token_count"]:
        print()
        print("WARNING: tiktoken is unavailable; tokens are estimated as ceil(UTF-8 bytes / 4).")
    if data["skipped_binary_files"]:
        print(f"Skipped binary files: {len(data['skipped_binary_files'])}")
    if data["missing_roots"]:
        print(f"Missing roots: {', '.join(data['missing_roots'])}")
    if include_files:
        print()
        print(f"{'File':<80} {'Tokens':>14}")
        print(f"{'-' * 80} {'-' * 14}")
        for item in data["files"]:
            print(f"{item['path']:<80} {item['tokens']:>14,}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workspace",
        type=Path,
        default=repository_root(),
        help=(
            "Repository root used to resolve source and output paths "
            "(default: discovered from this script)"
        ),
    )
    parser.add_argument(
        "--source-root",
        action="append",
        type=Path,
        dest="source_roots",
        help="Workspace-relative source root; may be repeated",
    )
    parser.add_argument(
        "--encoding",
        default="cl100k_base",
        help="tiktoken encoding used when tiktoken is installed",
    )
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--show-files", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    workspace = args.workspace.resolve()
    roots = args.source_roots or list(DEFAULT_SOURCE_ROOTS)
    count_tokens, tokenizer_name, exact = tokenizer(args.encoding)
    counts, skipped_binary, missing_roots = count_sources(
        workspace, roots, count_tokens
    )
    data = report(
        workspace,
        roots,
        counts,
        skipped_binary,
        missing_roots,
        tokenizer_name,
        exact,
    )
    print_report(data, args.show_files)
    if args.json_output:
        output = args.json_output
        if not output.is_absolute():
            output = workspace / output
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return 1 if missing_roots else 0


if __name__ == "__main__":
    raise SystemExit(main())
