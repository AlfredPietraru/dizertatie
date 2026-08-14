#!/usr/bin/env python3
"""Whole-file parameter retrieval experiment with an optional Ollama analysis."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from communication_llm import call_model


SOURCE_ROOTS = (
    Path("src/antrobot_ros"),
    Path("src/antrobot_description"),
    Path("src/kiss-icp"),
    Path("src/kinematic-icp"),
)
IGNORED_DIRECTORIES = {
    ".git", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox",
    ".venv", "__pycache__", "build", "dist", "install", "log", "logs",
    "site-packages", "venv",
}
SOURCE_SUFFIXES = {".c", ".cc", ".cpp", ".h", ".hpp", ".py", ".xml", ".xacro", ".yaml", ".yml"}
DEFAULT_PARAMETERS = (
    "wheel_radius",
    "wheel_separation",
    "encoder_cpr_left",
    "encoder_cpr_right",
    "no_load_rpm_left",
    "no_load_rpm_right",
)


SYSTEM_PROMPT = """Extract defaults and hard constraints for one ROS parameter.
Use only the supplied files. Never invent a range or reinterpret a calculation as a
constraint. Keep the answer focused on the requested parameter."""


def repository_root() -> Path:
    for candidate in Path(__file__).resolve().parents:
        if (candidate / ".git").exists():
            return candidate
    raise RuntimeError("Could not locate repository root")


def matching_files(root: Path, parameter: str) -> list[tuple[Path, str]]:
    # Identifier boundaries prevent wheel_radius_extra from matching wheel_radius.
    pattern = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(parameter)}(?![A-Za-z0-9_])")
    matches: list[tuple[Path, str]] = []
    for configured_root in SOURCE_ROOTS:
        source_root = root / configured_root
        if not source_root.is_dir():
            continue
        for path in sorted(source_root.rglob("*")):
            if (
                not path.is_file()
                or path.suffix.casefold() not in SOURCE_SUFFIXES
                or any(part in IGNORED_DIRECTORIES for part in path.parts)
            ):
                continue
            try:
                source = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue
            if pattern.search(source):
                matches.append((path.relative_to(root), source))
    return matches


def render_context(parameter: str, files: list[tuple[Path, str]]) -> str:
    lines = [
        f"# Whole-file evidence for `{parameter}`",
        "",
        f"Matching files: {len(files)}",
        "",
    ]
    for path, source in files:
        language = "python" if path.suffix == ".py" else "yaml"
        lines.extend(
            [
                f"## `{path.as_posix()}`",
                "",
                f"```{language}",
                source.rstrip(),
                "```",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def safe_name(value: str) -> str:
    return "".join(character if character.isalnum() else "_" for character in value).strip("_")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "parameters",
        nargs="*",
        default=DEFAULT_PARAMETERS,
        help="Exact parameter names (defaults to the six RDrive structural parameters)",
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = repository_root()
    artifact_directory = root / "artifacts" / "simple_parameter_experiment"
    artifact_directory.mkdir(parents=True, exist_ok=True)
    missing = False

    for parameter in args.parameters:
        files = matching_files(root, parameter)
        if not files:
            print(f"No source files contain {parameter!r}.", file=sys.stderr)
            missing = True
            continue

        name = safe_name(parameter)
        context_path = artifact_directory / f"context_{name}.md"
        context = render_context(parameter, files)
        context_path.write_text(context, encoding="utf-8")
        print(
            f"{parameter}: matched {len(files)} files; "
            f"saved {len(context.encode('utf-8')):,} bytes to {context_path}"
        )

        if args.dry_run:
            continue

        extraction_request = f"""
END OF EVIDENCE.

Now analyze only the exact parameter `{parameter}`. Ignore unrelated parameters and
do not summarize the files. Return exactly these sections:

## Configured default
- List every value configured in antrobot_params.yaml, with its owning YAML section.

## Code default
- List every value used when code declares or constructs this parameter, with file.

## Hard constraints
- List only explicit validation/rejection rules enforced by code. If none exist,
  write `No explicit hard constraint found`.

## Inferred requirements
- List requirements necessary to avoid an evident invalid operation (for example,
  division by zero), clearly labeled as inference. If none, write `None`.

Do not include a general repository, launch-file, URDF, ROS, or SLAM explanation.
"""
        analysis = call_model(
            SYSTEM_PROMPT,
            f"WHOLE-FILE EVIDENCE FOR `{parameter}`\n\n{context}\n{extraction_request}",
        )
        analysis_path = artifact_directory / f"analysis_{name}.md"
        analysis_path.write_text(analysis.rstrip() + "\n", encoding="utf-8")
        print(f"Saved model analysis to {analysis_path}")

    return 2 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
