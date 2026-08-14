#!/usr/bin/env python3
"""Collect simple static observations about a source directory."""

from __future__ import annotations

import argparse
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


CODE_EXTENSIONS = {
    ".asm",
    ".bash",
    ".c",
    ".cc",
    ".clj",
    ".cljs",
    ".cmake",
    ".cpp",
    ".cs",
    ".cxx",
    ".dart",
    ".ex",
    ".exs",
    ".f",
    ".f90",
    ".fish",
    ".fs",
    ".fsx",
    ".go",
    ".groovy",
    ".h",
    ".hh",
    ".hpp",
    ".hxx",
    ".java",
    ".js",
    ".jsx",
    ".kt",
    ".kts",
    ".lua",
    ".m",
    ".mm",
    ".php",
    ".pl",
    ".pm",
    ".ps1",
    ".py",
    ".pyx",
    ".r",
    ".rb",
    ".rs",
    ".scala",
    ".sh",
    ".sql",
    ".swift",
    ".tcl",
    ".ts",
    ".tsx",
    ".vb",
    ".vue",
    ".zig",
}

CODE_FILENAMES = {
    "CMakeLists.txt",
    "Dockerfile",
    "Makefile",
    "Rakefile",
    "SConstruct",
}

CODE_SHEBANG_MARKERS = (
    b"python",
    b"bash",
    b"/sh",
    b"node",
    b"ruby",
    b"perl",
    b"php",
    b"lua",
)

IGNORED_DIRECTORY_NAMES = {".git", "__pycache__", "log", "logs"}

IGNORED_FILE_NAMES = {
    ".clang-format",
    ".cmake-format.yaml",
    ".gitattributes",
    ".gitignore",
    ".pre-commit-config.yaml",
    "CITATION.cff",
    "LICENSE",
    "Makefile",
    "README.md",
    "__init__.py",
}


def is_ignored_directory(path: Path) -> bool:
    """Return whether a directory contains metadata or logs to exclude."""
    return path.name.casefold() in IGNORED_DIRECTORY_NAMES


def is_log_file(path: Path) -> bool:
    """Recognize conventional log filenames, including rotated logs."""
    name = path.name.casefold()
    return name.endswith(".log") or ".log." in name


def is_binary_file(path: Path) -> bool:
    """Detect binary content from a representative sample of the file."""
    try:
        with path.open("rb") as stream:
            sample = stream.read(8192)
    except OSError:
        return True

    if not sample:
        return False
    if b"\x00" in sample:
        return True

    # UTF-8 source and documentation are text. Invalid UTF-8 or a high ratio of
    # control bytes indicates binary data.
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError:
        return True
    text_controls = {8, 9, 10, 12, 13, 27}
    control_count = sum(
        byte < 32 and byte not in text_controls for byte in sample
    )
    return control_count / len(sample) > 0.10


def included_file(path: Path) -> bool:
    """Return whether a file belongs in the reports."""
    return (
        path.name not in IGNORED_FILE_NAMES
        and not is_log_file(path)
        and not is_binary_file(path)
    )


def extension_label(path: Path) -> str:
    """Return a normalized extension label for a file."""
    if path.name.startswith(".") and path.name.count(".") == 1:
        return "[no extension]"
    return path.suffix.lower() or "[no extension]"


def has_code_shebang(path: Path) -> bool:
    """Recognize executable scripts that do not have a code extension."""
    try:
        with path.open("rb") as stream:
            first_line = stream.readline(512).lower()
    except OSError:
        return False
    return first_line.startswith(b"#!") and any(
        marker in first_line for marker in CODE_SHEBANG_MARKERS
    )


def contains_code(path: Path) -> bool:
    """Classify a file as code using its name, extension, or shebang."""
    return (
        path.name in CODE_FILENAMES
        or path.suffix.lower() in CODE_EXTENSIONS
        or has_code_shebang(path)
    )


def build_tree(directory: Path, prefix: str = "") -> list[str]:
    """Build a deterministic, Markdown-friendly directory tree."""
    try:
        entries = sorted(
            (
                entry
                for entry in directory.iterdir()
                if not (entry.is_dir() and is_ignored_directory(entry))
                and not (entry.is_file() and not included_file(entry))
            ),
            key=lambda item: (item.is_file(), item.name.casefold(), item.name),
        )
    except OSError as error:
        return [f"{prefix}[unable to read: {error}]"]

    lines: list[str] = []
    for index, entry in enumerate(entries):
        is_last = index == len(entries) - 1
        connector = "└── " if is_last else "├── "
        suffix = "/" if entry.is_dir() and not entry.is_symlink() else ""
        lines.append(f"{prefix}{connector}{entry.name}{suffix}")
        if entry.is_dir() and not entry.is_symlink():
            child_prefix = prefix + ("    " if is_last else "│   ")
            lines.extend(build_tree(entry, child_prefix))
    return lines


def analyze(source_dir: Path, output_dir: Path) -> tuple[Path, Path]:
    """Analyze source_dir and save the resulting reports in output_dir."""
    if not source_dir.is_dir():
        raise NotADirectoryError(f"Source directory does not exist: {source_dir}")

    files: list[Path] = []
    for directory_name, directory_names, file_names in os.walk(source_dir):
        directory = Path(directory_name)
        directory_names[:] = [
            name
            for name in directory_names
            if not is_ignored_directory(directory / name)
        ]
        files.extend(
            path
            for name in file_names
            if included_file(path := directory / name)
        )
    files.sort(key=lambda path: path.as_posix())
    extension_counts = Counter(extension_label(path) for path in files)
    code_files = [path for path in files if contains_code(path)]

    output_dir.mkdir(parents=True, exist_ok=True)
    summary_path = output_dir / "source_analysis.md"
    tree_path = output_dir / "src_tree.md"
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    summary_lines = [
        "# Source Folder Analysis",
        "",
        f"- Source directory: `{source_dir.resolve()}`",
        f"- Generated at: `{generated_at}`",
        f"- Total files: **{len(files)}**",
        f"- Files containing code: **{len(code_files)}**",
        f"- Distinct extension groups: **{len(extension_counts)}**",
        (
            "- Excluded: binary files; logs; `.git` and `__pycache__` directories; "
            "repository metadata/documentation files; and `__init__.py` files"
        ),
        "",
        "## Files by extension",
        "",
        "| Extension | Number of files |",
        "|---|---:|",
    ]
    summary_lines.extend(
        f"| `{extension}` | {count} |"
        for extension, count in sorted(
            extension_counts.items(), key=lambda item: (-item[1], item[0])
        )
    )
    summary_lines.extend(
        [
            "",
            "## Code classification",
            "",
            (
                "Files are classified as code by recognized source-code extensions, "
                "well-known build filenames, or a recognized script shebang."
            ),
            "",
            "The complete directory tree is saved in [`src_tree.md`](src_tree.md).",
            "",
        ]
    )
    summary_path.write_text("\n".join(summary_lines), encoding="utf-8")

    tree_lines = [
        "# Source Folder Tree",
        "",
        f"Tree for `{source_dir.resolve()}`:",
        "",
        "```text",
        f"{source_dir.name}/",
        *build_tree(source_dir),
        "```",
        "",
    ]
    tree_path.write_text("\n".join(tree_lines), encoding="utf-8")
    return summary_path, tree_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Analyze files in a source folder and produce Markdown reports."
    )
    parser.add_argument(
        "source",
        nargs="?",
        type=Path,
        default=Path("src"),
        help="directory to analyze (default: src)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/observations"),
        help="report directory (default: artifacts/observations)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        summary_path, tree_path = analyze(args.source, args.output)
    except (OSError, ValueError) as error:
        print(f"Error: {error}")
        return 1

    print(f"Analysis saved to {summary_path}")
    print(f"Directory tree saved to {tree_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
