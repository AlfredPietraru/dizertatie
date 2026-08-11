#!/usr/bin/env python3
"""Analyze a parameter per matching file, then merge the saved analyses."""

from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from simple_parameter_llm_experiment import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    call_ollama,
    matching_files,
    repository_root,
    safe_name,
)


FILE_PROMPT = """Analyze parameter {parameter!r} using only this complete repository file.

Return concise Markdown with these sections:
1. Occurrences and ownership
2. Defaults and configured values
3. Hard constraints and rejection behavior
4. Other observed thresholds (do not call these allowed ranges)
5. Calculations and transformations
6. Dependencies and runtime effects
7. Missing evidence

Every factual claim must cite `{path}:LINE`. Distinguish direct evidence from
inference. If this file contains no evidence for a category, say so. Do not use
knowledge from outside the supplied file.

# Complete file: `{path}`

```{language}
{source}
```
"""

MERGE_SYSTEM_PROMPT = """You consolidate file-level static-analysis reports for one ROS
configuration parameter. Use only the supplied reports. Preserve their file and line
citations. Do not invent missing facts. Keep disagreements visible. Never merge a code
default, YAML value, hard constraint, tuning recommendation, and incidental threshold
into one value range.
"""


def file_slug(number: int, path: Path) -> str:
    flattened = "_".join(path.parts)
    normalized = "".join(c if c.isalnum() or c in "._-" else "_" for c in flattened)
    return f"{number:02d}_{normalized}"


def file_prompt(parameter: str, path: Path, source: str) -> str:
    return FILE_PROMPT.format(
        parameter=parameter,
        path=path.as_posix(),
        language="python" if path.suffix == ".py" else "yaml",
        source=source.rstrip(),
    ).rstrip() + "\n"


def analyze_one(
    parameter: str,
    path: Path,
    prompt: str,
    destination: Path,
    args: argparse.Namespace,
) -> tuple[Path, Path]:
    if args.resume and destination.is_file():
        return path, destination
    analysis = call_ollama(
        parameter,
        prompt,
        model=args.model,
        base_url=args.base_url,
        num_ctx=args.file_num_ctx,
        max_tokens=args.file_max_tokens,
        user_prompt=prompt,
    )
    destination.write_text(
        f"# File analysis: `{path.as_posix()}`\n\n{analysis.rstrip()}\n",
        encoding="utf-8",
    )
    return path, destination


def merge_prompt(parameter: str, reports: list[tuple[Path, str]]) -> str:
    lines = [
        f"Produce the final consolidated analysis for parameter `{parameter}`.",
        "",
        "The following reports were produced independently from complete files.",
        "Cross-file conclusions are allowed only when supported by these reports.",
        "",
    ]
    for path, report in reports:
        lines.extend([f"## Report for `{path.as_posix()}`", "", report.rstrip(), ""])
    lines.extend(
        [
            "## Required final structure",
            "",
            "1. Parameter instances and owners",
            "2. Defaults and YAML values",
            "3. Types and units",
            "4. Hard constraints",
            "5. Other observed thresholds",
            "6. Calculations and transformations",
            "7. Cross-file dependencies and runtime effects",
            "8. Conflicts, uncertainties, and missing evidence",
            "9. Practical conclusion",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("parameter")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--file-num-ctx", type=int, default=12288)
    parser.add_argument("--file-max-tokens", type=int, default=512)
    parser.add_argument("--merge-num-ctx", type=int, default=8192)
    parser.add_argument("--merge-max-tokens", type=int, default=1024)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--resume", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if min(
        args.workers,
        args.file_num_ctx,
        args.file_max_tokens,
        args.merge_num_ctx,
        args.merge_max_tokens,
    ) <= 0:
        raise SystemExit("Worker and token/context values must be positive")

    root = repository_root()
    files = matching_files(root, args.parameter)
    if not files:
        print(f"No files contain exact parameter {args.parameter!r}.", file=sys.stderr)
        return 2

    output_dir = root / "artifacts" / f"simple_map_reduce_{safe_name(args.parameter)}"
    prompt_dir = output_dir / "file_prompts"
    analysis_dir = output_dir / "file_analyses"
    prompt_dir.mkdir(parents=True, exist_ok=True)
    analysis_dir.mkdir(parents=True, exist_ok=True)

    jobs: list[tuple[Path, str, Path]] = []
    manifest = []
    for number, (path, source) in enumerate(files, start=1):
        slug = file_slug(number, path)
        prompt = file_prompt(args.parameter, path, source)
        prompt_path = prompt_dir / f"{slug}.md"
        analysis_path = analysis_dir / f"{slug}.md"
        prompt_path.write_text(prompt, encoding="utf-8")
        jobs.append((path, prompt, analysis_path))
        manifest.append(
            {
                "path": path.as_posix(),
                "prompt": prompt_path.relative_to(root).as_posix(),
                "analysis": analysis_path.relative_to(root).as_posix(),
                "prompt_bytes": len(prompt.encode("utf-8")),
            }
        )
    (output_dir / "manifest.json").write_text(
        json.dumps({"parameter": args.parameter, "files": manifest}, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Prepared {len(jobs)} independent file prompts in {prompt_dir}")
    if args.dry_run:
        return 0

    completed: dict[str, Path] = {}
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(analyze_one, args.parameter, path, prompt, destination, args): path
            for path, prompt, destination in jobs
        }
        for future in as_completed(futures):
            path, destination = future.result()
            completed[path.as_posix()] = destination
            print(f"Completed {path}", file=sys.stderr)

    reports = [
        (path, completed[path.as_posix()].read_text(encoding="utf-8"))
        for path, _, _ in jobs
    ]
    combined_prompt = merge_prompt(args.parameter, reports)
    merge_prompt_path = output_dir / "merge_prompt.md"
    merge_prompt_path.write_text(combined_prompt, encoding="utf-8")
    final_analysis = call_ollama(
        args.parameter,
        combined_prompt,
        model=args.model,
        base_url=args.base_url,
        num_ctx=args.merge_num_ctx,
        max_tokens=args.merge_max_tokens,
        system_prompt=MERGE_SYSTEM_PROMPT,
        user_prompt=combined_prompt,
    )
    final_path = output_dir / "final_analysis.md"
    final_path.write_text(final_analysis.rstrip() + "\n", encoding="utf-8")
    print(f"Saved final merged analysis to {final_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
