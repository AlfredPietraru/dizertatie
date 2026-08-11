#!/usr/bin/env python3
"""Whole-file parameter retrieval experiment with an optional Ollama analysis."""

from __future__ import annotations

import argparse
import http.client
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


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
SOURCE_SUFFIXES = {".py", ".yaml", ".yml"}
DEFAULT_MODEL = "deepseek-r1:7b"
DEFAULT_BASE_URL = "http://127.0.0.1:11434/api"


SYSTEM_PROMPT = """You analyze one ROS configuration parameter using complete source files.
Treat the supplied files as the only repository evidence. Do not invent constraints.

Extract and clearly separate:
1. every parameter occurrence and owning ROS node or component;
2. declared code defaults;
3. configured YAML values;
4. types and units when supported by evidence;
5. hard validity constraints and rejection behavior;
6. observed thresholds used by conditions, without claiming they are allowed ranges;
7. calculations and transformations involving the parameter;
8. dependencies on other parameters or runtime values;
9. runtime consumers and behavioral effects;
10. uncertainties and evidence that is missing.

For every claim, cite the repository-relative file and line number. Distinguish direct
evidence from inference. A current default, hard constraint, recommended tuning range,
and incidental comparison are different facts and must never be merged.
"""


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


def call_ollama(
    parameter: str,
    context: str,
    *,
    model: str,
    base_url: str,
    num_ctx: int,
    max_tokens: int,
    system_prompt: str = SYSTEM_PROMPT,
    user_prompt: str | None = None,
) -> str:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": user_prompt or (
                    f"Analyze the parameter {parameter!r} from this whole-file evidence.\n\n"
                    + context
                ),
            },
        ],
        "stream": False,
        "options": {"num_ctx": num_ctx, "num_predict": max_tokens},
    }
    request = urllib.request.Request(
        base_url.rstrip("/") + "/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=1800) as response:
            result: dict[str, Any] = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Ollama HTTP {error.code}: {body}") from error
    except (urllib.error.URLError, http.client.RemoteDisconnected) as error:
        reason = getattr(error, "reason", str(error))
        raise RuntimeError(f"Ollama unavailable or disconnected: {reason}") from error
    content = result.get("message", {}).get("content")
    if not content:
        raise RuntimeError("Ollama returned no analysis: " + json.dumps(result))
    return str(content)


def safe_name(value: str) -> str:
    return "".join(character if character.isalnum() else "_" for character in value).strip("_")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("parameter")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--num-ctx", type=int, default=16384)
    parser.add_argument("--max-tokens", type=int, default=2048)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = repository_root()
    files = matching_files(root, args.parameter)
    if not files:
        print(f"No source files contain the exact parameter {args.parameter!r}.", file=sys.stderr)
        return 2

    name = safe_name(args.parameter)
    context_path = root / f"artifacts/simple_parameter_context_{name}.md"
    context = render_context(args.parameter, files)
    context_path.parent.mkdir(parents=True, exist_ok=True)
    context_path.write_text(context, encoding="utf-8")
    print(f"Matched {len(files)} files; saved {len(context.encode('utf-8')):,} bytes to {context_path}")

    if args.dry_run:
        return 0
    analysis = call_ollama(
        args.parameter,
        context,
        model=args.model,
        base_url=args.base_url,
        num_ctx=args.num_ctx,
        max_tokens=args.max_tokens,
    )
    analysis_path = root / f"artifacts/simple_parameter_analysis_{name}.md"
    analysis_path.write_text(analysis.rstrip() + "\n", encoding="utf-8")
    print(analysis)
    print(f"\nSaved analysis to {analysis_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
