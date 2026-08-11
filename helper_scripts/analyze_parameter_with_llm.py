#!/usr/bin/env python3
"""Run an evidence-grounded LLM analysis for one configuration parameter."""

from __future__ import annotations

import argparse
import http.client
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from parameter_graph_tools import (
    get_parameter_context,
    repository_root,
)


DEFAULT_MODEL = "deepseek-r1:7b"
DEFAULT_BASE_URL = "http://localhost:11434/api"
DEFAULT_CONTEXT_LENGTH = 16384


SYSTEM_PROMPT = """You are analyzing configuration behavior in a ROS codebase.

You receive a compiled context containing static value-flow facts and deduplicated
complete source methods reached by that graph. Treat the context as the
only evidence about this repository. Do not invent code, constraints, runtime
behavior, or relationships that are absent from the evidence.

For every conclusion:
- identify the parameter instance ID;
- cite the source file, qualified method, and relevant line numbers;
- distinguish declarations/defaults from runtime reads;
- explain assignments, transformations, validations, controls, calls, returns,
  and ROS consumers in execution order;
- distinguish confirmed graph relationships from reasonable interpretations;
- report static-analysis gaps and unresolved dynamic behavior;
- compare ambiguous same-name instances instead of merging them;
- when multiple instances exist, focus relationship analysis on cross-instance
  pairs and ignore trivial self-pairs.

Produce a readable technical report with these sections:
1. Parameter instances
2. Flow of each instance
3. Validations and conditions
4. ROS/runtime effects
5. Cross-instance comparison
6. Missing evidence and limitations
7. Practical conclusion
"""


def compact_regeneration(result: dict[str, Any]) -> dict[str, Any]:
    """Remove captured pipeline output that is irrelevant to LLM reasoning."""
    copied = dict(result)
    regeneration = dict(copied.get("regeneration", {}))
    regeneration["stages"] = [
        {
            "script": stage["script"],
            "return_code": stage["return_code"],
        }
        for stage in regeneration.get("stages", [])
    ]
    copied["regeneration"] = regeneration
    return copied


def collect_tool_evidence(parameter: str, refresh: bool) -> dict[str, Any]:
    """Retrieve and compile graph evidence for one exact parameter name."""
    return compact_regeneration(get_parameter_context(parameter, refresh=refresh))


def evidence_summary(evidence: dict[str, Any]) -> dict[str, Any]:
    retrieval = evidence.get("retrieval_summary", {})
    compilation = evidence.get("compilation", {})
    return {
        "parameter": evidence.get("query", {}).get("name"),
        "flow_status": evidence["status"],
        "parameter_instances": retrieval.get("parameter_instances", 0),
        "relationship_pairs": retrieval.get("relationship_pairs", 0),
        "flow_nodes": compilation.get("input_flow_nodes", 0),
        "flow_edges": compilation.get("input_flow_edges", 0),
        "deduplicated_code_chunks": compilation.get("deduplicated_code_chunks", 0),
        "llm_context_utf8_bytes": len(evidence.get("llm_context", "").encode("utf-8")),
        "graph_regenerated": evidence.get("regeneration", {}).get("performed", False),
    }


def build_user_prompt(evidence: dict[str, Any]) -> str:
    return (
        f"Analyze the exact configuration parameter name "
        f"{evidence.get('query', {}).get('name')!r}.\n\n"
        "The Markdown below was deterministically compiled from the repository "
        "graph. Code chunks are deduplicated and internal graph hashes and AST "
        "bookkeeping have been removed.\n\n"
        "COMPILED_TOOL_CONTEXT\n\n"
        + evidence.get("llm_context", "")
    )


def write_compiled_context(root: Path, parameter: str, evidence: dict[str, Any]) -> Path:
    """Save the exact Markdown context that will be sent to the LLM."""
    stem = safe_filename(parameter)
    markdown_path = root / f"artifacts/parameter_context_{stem}.md"
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.write_text(evidence.get("llm_context", ""), encoding="utf-8")
    return markdown_path


def run_llm(
    evidence: dict[str, Any],
    *,
    model: str,
    base_url: str,
    max_tokens: int,
    context_length: int,
) -> str:
    """Send the collected evidence to a locally running Ollama server."""
    request_payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_user_prompt(evidence)},
        ],
        "stream": False,
        "options": {
            "num_predict": max_tokens,
            "num_ctx": context_length,
        },
    }
    request = urllib.request.Request(
        base_url.rstrip("/") + "/chat",
        data=json.dumps(request_payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            completion = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        response_body = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"LLM request failed with HTTP {error.code}: {response_body}"
        ) from error
    except (urllib.error.URLError, http.client.RemoteDisconnected) as error:
        reason = getattr(error, "reason", str(error))
        raise RuntimeError(
            f"Ollama disconnected at {base_url}: {reason}. "
            "Make sure `ollama serve` is running and the requested model is pulled."
        ) from error

    try:
        content = completion["message"]["content"]
    except (KeyError, TypeError) as error:
        raise RuntimeError(
            "Unexpected LLM response: " + json.dumps(completion, ensure_ascii=False)
        ) from error
    if not content:
        raise RuntimeError("The model returned an empty response.")
    return content


def write_output(
    destination: Path,
    parameter: str,
    model: str,
    evidence: dict[str, Any],
    analysis: str | None,
    dry_run: bool,
) -> None:
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "parameter": parameter,
        "model": model,
        "dry_run": dry_run,
        "tool_summary": evidence_summary(evidence),
        "tool_evidence": evidence,
        "analysis": analysis,
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("parameter", help="Exact configuration parameter name")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"Ollama API base URL (default: {DEFAULT_BASE_URL})",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=4096,
        help="Maximum number of tokens Ollama may generate",
    )
    parser.add_argument(
        "--num-ctx",
        type=int,
        default=DEFAULT_CONTEXT_LENGTH,
        help=(
            "Ollama context window for prompt plus response "
            f"(default: {DEFAULT_CONTEXT_LENGTH})"
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        help=(
            "JSON output path relative to the repository root. By default, "
            "artifacts/repo_ingestion/llm_analysis_<parameter>.json is used."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run retrieval tools and save the complete prompt evidence without calling the LLM",
    )
    parser.add_argument(
        "--no-refresh",
        action="store_true",
        help="Do not regenerate stale 00-03 analysis artifacts",
    )
    return parser.parse_args()


def safe_filename(value: str) -> str:
    normalized = "".join(character if character.isalnum() else "_" for character in value)
    return normalized.strip("_") or "parameter"


def main() -> int:
    args = parse_args()
    root = repository_root()

    if args.max_tokens <= 0 or args.num_ctx <= 0:
        raise SystemExit("--max-tokens and --num-ctx must be positive integers")

    print(f"Retrieving complete forward flow for {args.parameter!r}...", file=sys.stderr)
    evidence = collect_tool_evidence(args.parameter, refresh=not args.no_refresh)
    summary = evidence_summary(evidence)
    print(json.dumps(summary, indent=2), file=sys.stderr)
    llm_context_path = write_compiled_context(root, args.parameter, evidence)
    print(f"Saved LLM-facing context to {llm_context_path}", file=sys.stderr)

    if summary["flow_status"] != "ok":
        print(
            f"Parameter {args.parameter!r} was not found. See tool evidence for similar names.",
            file=sys.stderr,
        )
        analysis = None
        exit_code = 2
    elif args.dry_run:
        analysis = None
        exit_code = 0
    else:
        print(f"Calling {args.model}...", file=sys.stderr)
        analysis = run_llm(
            evidence,
            model=args.model,
            base_url=args.base_url,
            max_tokens=args.max_tokens,
            context_length=args.num_ctx,
        )
        print("\n" + analysis)
        exit_code = 0

    output = args.output or Path(
        f"artifacts/repo_ingestion/llm_analysis_{safe_filename(args.parameter)}.json"
    )
    if not output.is_absolute():
        output = root / output
    write_output(
        output,
        args.parameter,
        args.model,
        evidence,
        analysis,
        args.dry_run,
    )
    print(f"\nSaved evidence and analysis to {output}", file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
