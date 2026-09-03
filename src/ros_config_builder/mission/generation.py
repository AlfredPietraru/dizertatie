"""Controlled intent-to-language generation and review workflow."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Literal, Protocol

from pydantic import Field, model_validator

from .dataset import SyntheticSeed
from .schema import MissionModel


LinguisticStyle = Literal["canonical", "natural", "shorthand", "imperative", "negative_phrasing", "multi_clause"]
STYLES: tuple[LinguisticStyle, ...] = (
    "canonical", "natural", "shorthand", "imperative", "negative_phrasing", "multi_clause",
)


class GeneratedText(MissionModel):
    requested_style: LinguisticStyle
    text: str = Field(min_length=1, max_length=500)


class GeneratedBatch(MissionModel):
    candidates: list[GeneratedText]


class CandidateReview(MissionModel):
    status: Literal["pending", "accepted", "rejected"] = "pending"
    semantic_equivalence: bool | None = None
    natural_language_quality: Literal["acceptable", "unacceptable"] | None = None
    rejection_reason: Literal[
        "semantic_drift", "added_requirement", "removed_requirement", "unnatural_wording",
        "unnatural_language", "wrong_outcome", "duplicate", "annotation_policy_violation",
        "malformed_generation", "other",
    ] | None = None
    notes: str | None = None
    reviewer: str | None = None

    @model_validator(mode="after")
    def check_review(self) -> "CandidateReview":
        if self.status == "pending" and any(value is not None for value in (
            self.semantic_equivalence, self.natural_language_quality, self.rejection_reason, self.reviewer,
        )):
            raise ValueError("pending reviews cannot contain decisions")
        if self.status == "accepted" and (
            self.semantic_equivalence is not True or self.natural_language_quality != "acceptable" or not self.reviewer
        ):
            raise ValueError("accepted candidates require positive semantic/quality review and a reviewer")
        if self.status == "rejected" and (not self.rejection_reason or not self.reviewer):
            raise ValueError("rejected candidates require a reason and reviewer")
        return self


class SyntheticCandidate(MissionModel):
    candidate_id: str
    seed_id: str
    outcome: Literal["supported", "unsupported", "ambiguous"]
    requested_style: LinguisticStyle
    text: str
    generator_model: str
    prompt_version: Literal["paraphrase_v1", "paraphrase_v2_replacement"] = "paraphrase_v1"
    review: CandidateReview = Field(default_factory=CandidateReview)


class ParaphraseBackend(Protocol):
    model: str
    def __call__(self, system_prompt: str, user_prompt: str) -> str: ...


class OllamaParaphraseBackend:
    def __init__(self, *, host: str, model: str, timeout: float = 300) -> None:
        self.host = host.removeprefix("http://").removeprefix("https://").rstrip("/")
        self.model = model; self.timeout = timeout

    def __call__(self, system_prompt: str, user_prompt: str) -> str:
        payload = json.dumps({"model": self.model, "messages": [
            {"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
            "stream": False, "format": GeneratedBatch.model_json_schema(), "options": {"temperature": 0.7},
        }).encode()
        request = urllib.request.Request(f"http://{self.host}:11434/api/chat", data=payload,
                                         headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response: result = json.load(response)
        except (urllib.error.URLError, TimeoutError) as error:
            raise RuntimeError(f"Ollama paraphrase request failed: {error}") from error
        try: return result["message"]["content"]
        except (KeyError, TypeError) as error: raise RuntimeError("Ollama returned an unexpected response") from error


SYSTEM_PROMPT = """You generate natural-language mission variations from a human-verified structured intent.
Do not solve or alter the intent. Express exactly the supplied requirements: add nothing, remove nothing, and do not fill unspecified choices.
For unsupported and ambiguous seeds, express the problematic request itself; do not explain, reject, answer, or clarify it.
Preserve every number, unit, negation, capability, and algorithm exactly in meaning. Never convert a frequency into a period or change a physical unit's magnitude.
Never invent component names, algorithms, launch flags, parameter values, defaults, exclusions, or implementation details absent from intent_description.
For capability-only intents, do not add parameter assignments. Shorthand must remain unambiguous and must use established component names without abbreviating them.
Return only the requested structured JSON. Each candidate must use its assigned linguistic style."""

REPLACEMENT_SYSTEM_PROMPT = """You generate direct user mission requests from a verified structured intent.
Preserve the intent exactly and return only structured JSON.
For unsupported seeds, write the user's unsupported command or request. Never explain that it is unsupported, impossible, protected, or invalid.
For ambiguous seeds, write the original underspecified/conflicting user request. Never ask the clarification question, explain the ambiguity, or resolve it.
Do not add or remove requirements. Each candidate must use its assigned linguistic style."""


def _styles(count: int) -> list[LinguisticStyle]:
    return [STYLES[index % len(STYLES)] for index in range(count)]


def build_generation_prompt(
    seed: SyntheticSeed,
    requested_styles: list[LinguisticStyle] | None = None,
    excluded_texts: list[str] | None = None,
) -> tuple[str, list[LinguisticStyle]]:
    styles = requested_styles or _styles(seed.paraphrase_count)
    # Do not expose renderer configuration to the paraphraser. Models otherwise
    # tend to turn inferred launch flags and defaults into new user requirements.
    payload = {
        "id": seed.id,
        "outcome": seed.outcome,
        "intent_description": seed.intent_description,
        "strata": seed.strata,
    }
    if seed.outcome != "supported":
        payload["category"] = seed.category
    prompt = (
        "Generate one mission sentence for each requested style. Preserve the structured intent exactly.\n\n"
        f"Seed:\n{json.dumps(payload, indent=2, sort_keys=True)}\n\n"
        f"Requested styles in order:\n{json.dumps(styles)}"
    )
    if excluded_texts:
        prompt += (
            "\n\nThe following mission sentences were already generated. Every new sentence "
            "must use substantively different wording while preserving exactly the same intent:\n"
            f"{json.dumps(excluded_texts, indent=2)}"
        )
    return prompt, styles


def generate_paraphrases(seed: SyntheticSeed, backend: ParaphraseBackend, *,
                         requested_styles: list[LinguisticStyle] | None = None,
                         candidate_series: str = "p", replacement: bool = False,
                         excluded_texts: list[str] | None = None) -> tuple[list[SyntheticCandidate], str]:
    prompt, styles = build_generation_prompt(seed, requested_styles, excluded_texts)
    raw = backend(REPLACEMENT_SYSTEM_PROMPT if replacement else SYSTEM_PROMPT, prompt)
    try: batch = GeneratedBatch.model_validate_json(raw)
    except ValueError as error: raise ValueError(f"malformed generated batch for {seed.id}: {error}") from error
    returned = [item.requested_style for item in batch.candidates]
    if returned != styles:
        # Some models over-generate several alternatives for every requested style.
        # Retain one deterministic candidate per requested slot, but remain strict
        # when a requested style is absent.
        remaining = list(batch.candidates)
        selected = []
        for style in styles:
            match = next(
                (index for index, item in enumerate(remaining)
                 if item.requested_style == style),
                None,
            )
            if match is None:
                raise ValueError(f"{seed.id} returned styles {returned}, expected {styles}")
            selected.append(remaining.pop(match))
        batch = GeneratedBatch(candidates=selected)
    candidates = [SyntheticCandidate(candidate_id=f"{seed.id}-{candidate_series}{index:02d}", seed_id=seed.id,
        outcome=seed.outcome, requested_style=item.requested_style, text=item.text.strip(),
        generator_model=backend.model, prompt_version="paraphrase_v2_replacement" if replacement else "paraphrase_v1")
                  for index, item in enumerate(batch.candidates, 1)]
    return candidates, raw


def _normalized(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", text.casefold()).split())


def automatically_validate_candidates(candidates: list[SyntheticCandidate]) -> list[SyntheticCandidate]:
    seen: set[str] = set()
    commentary = re.compile(r"^(here (?:is|are)|sure[,!]|output:|paraphrase[s]?:)", re.I)
    for candidate in candidates:
        normalized = _normalized(candidate.text)
        reason = None; note = None
        if not normalized:
            reason, note = "malformed_generation", "empty natural-language text"
        elif normalized in seen:
            reason, note = "duplicate", "duplicate after case/whitespace/punctuation normalization"
        elif candidate.text.lstrip().startswith(("{", "[", "```")) or "ros__parameters:" in candidate.text:
            reason, note = "malformed_generation", "JSON, code, or YAML returned instead of mission language"
        elif commentary.search(candidate.text.strip()):
            reason, note = "malformed_generation", "generator commentary returned instead of mission language"
        if reason:
            candidate.review = CandidateReview(status="rejected", rejection_reason=reason,
                                               notes=note, reviewer="automatic-validator")
        else: seen.add(normalized)
    return candidates


_NUMBER_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40,
    "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80,
    "ninety": 90,
}
_NUMBER_PATTERN = (
    r"(?:\d+(?:\.\d+)?|(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|"
    r"eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|"
    r"twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand|and|"
    r"a|half|quarter)(?:[\s-]+(?:zero|one|two|three|four|five|six|seven|eight|nine|"
    r"ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|"
    r"twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand|and|a|"
    r"half|quarter)){0,5})"
)
_MEASUREMENT = re.compile(
    rf"(?P<number>{_NUMBER_PATTERN})\s*(?P<unit>\b(?:hz|hertz|milliseconds?|ms|seconds?|secs?|"
    r"times?\s+per\s+second|updates?\s+per\s+second|millimet(?:er|re)s?|mm|"
    r"centimet(?:er|re)s?|cm|met(?:er|re)s?|counts?|pulses?))\b",
    re.I,
)


def _number_value(text: str) -> float | None:
    try:
        return float(text)
    except ValueError:
        pass
    words = text.casefold().replace("-", " ").split()
    total = current = 0.0
    for index, word in enumerate(words):
        if word == "and":
            continue
        if word == "a" and index + 1 < len(words) and words[index + 1] in {"half", "quarter"}:
            continue
        if word == "a":
            current += 1
        elif word == "half":
            current += 0.5
        elif word == "quarter":
            current += 0.25
        elif word in _NUMBER_WORDS:
            current += _NUMBER_WORDS[word]
        elif word == "hundred":
            current = max(current, 1) * 100
        elif word == "thousand":
            total += max(current, 1) * 1000
            current = 0
        else:
            return None
    return total + current


def _quantities(text: str) -> list[tuple[str, float]]:
    result = []
    for match in _MEASUREMENT.finditer(text):
        value = _number_value(match.group("number"))
        if value is None:
            continue
        unit = match.group("unit").casefold()
        prefix = text[max(0, match.start() - 18):match.start()].casefold()
        if unit in {"hz", "hertz", "time per second", "times per second",
                    "update per second", "updates per second"}:
            result.append(("frequency", value))
        elif unit in {"millisecond", "milliseconds", "ms"}:
            result.append(("frequency", 1000.0 / value if value else 0.0))
        elif unit in {"second", "seconds", "sec", "secs"} and re.search(r"(?:every|once)\s*$", prefix):
            result.append(("frequency", 1.0 / value if value else 0.0))
        elif unit in {"millimeter", "millimeters", "millimetre", "millimetres", "mm"}:
            result.append(("length", value / 1000.0))
        elif unit in {"centimeter", "centimeters", "centimetre", "centimetres", "cm"}:
            result.append(("length", value / 100.0))
        elif unit in {"meter", "meters", "metre", "metres"}:
            result.append(("length", value))
        elif unit.startswith(("count", "pulse")):
            result.append(("count", value))
    return result


def semantic_validation_issues(candidate: SyntheticCandidate, seed: SyntheticSeed) -> list[str]:
    """Return deterministic evidence of intent drift; an empty list means no proven drift."""
    text = candidate.text.casefold()
    issues = []
    unknown_identifiers = set(re.findall(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b", text)) - {
        "explore_lite", "kinematic_icp", "kiss_icp", "laserscan_to_pointcloud",
        "joint_state_estimator", "rdrive_node", "joint_state", "publish_frequency",
        "planner_frequency", "publish_rate", "max_range", "wheel_radius",
        "wheel_separation", "encoder_cpr_left", "encoder_cpr_right",
    }
    if unknown_identifiers:
        issues.append(f"invented identifiers: {sorted(unknown_identifiers)}")
    if seed.outcome != "supported":
        return issues

    expected_parameters = {
        key: value for key, value in seed.expected_template_configuration.items()
        if key.startswith("nodes.") and isinstance(value, (int, float))
        and not isinstance(value, bool)
    }
    quantities = _quantities(candidate.text)
    bare_numbers = [float(value) for value in re.findall(r"(?<![\w.])-?\d+(?:\.\d+)?", text)]
    expected_values = list(expected_parameters.values())
    if not expected_values and quantities:
        issues.append(f"added numeric requirement: {quantities}")
    for key, expected in expected_parameters.items():
        kind = (
            "frequency" if key.endswith(("frequency", "publish_rate"))
            else "length" if key.endswith(("range", "radius", "separation"))
            else "count"
        )
        typed = [value for quantity_kind, value in quantities if quantity_kind == kind]
        candidates = typed if quantities else bare_numbers
        if not any(abs(float(expected) - value) <= 1e-6 for value in candidates):
            issues.append(f"numeric meaning differs for {key}: expected {expected}, found {candidates}")

    def polarities(term: str) -> set[str]:
        found = set()
        for match in re.finditer(rf"\b{re.escape(term)}\b", text):
            before = text[max(0, match.start() - 24):match.start()]
            after = text[match.end():match.end() + 24]
            negative = bool(re.search(
                r"(?:disable|without|skip|no|not)\s+(?:\w+[\s_-]+){0,2}$", before,
            ))
            negative |= bool(re.search(r"^\s*(?:->|=|is|should be)?\s*(?:off|false|disabled)\b", after))
            positive = bool(re.search(
                r"(?:enable|activate|launch|use|run|with)\s+(?:\w+[\s_-]+){0,2}$", before,
            ))
            if negative and positive:
                found.add("conflicting")
            elif negative:
                found.add("negative")
            elif positive:
                found.add("positive")
        return found

    capabilities = seed.capabilities
    checks = {
        "cartographer": capabilities.mapping == "cartographer",
        "nav2": capabilities.navigation == "nav2",
        "explore_lite": capabilities.exploration == "explore_lite",
        "kinematic_icp": capabilities.odometry == "kinematic_icp",
        "kiss_icp": capabilities.odometry == "kiss_icp",
    }
    for term, enabled in checks.items():
        polarity = polarities(term)
        if "conflicting" in polarity:
            issues.append(f"conflicting wording for {term}")
        elif enabled and "negative" in polarity:
            issues.append(f"disables required capability {term}")
        elif not enabled and "positive" in polarity:
            issues.append(f"enables excluded capability {term}")
    return issues


def semantically_validate_candidates(
    candidates: list[SyntheticCandidate], seeds_by_id: dict[str, SyntheticSeed],
) -> list[SyntheticCandidate]:
    for candidate in candidates:
        if candidate.review.status == "rejected":
            continue
        issues = semantic_validation_issues(candidate, seeds_by_id[candidate.seed_id])
        if issues:
            candidate.review = CandidateReview(
                status="rejected", semantic_equivalence=False,
                natural_language_quality="unacceptable", rejection_reason="semantic_drift",
                notes="; ".join(issues), reviewer="deterministic-semantic-validator",
            )
    return candidates


def write_candidate_history(candidates: list[SyntheticCandidate], path: str | Path) -> None:
    output = Path(path); output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("".join(json.dumps(item.model_dump(mode="json"), sort_keys=True) + "\n"
                              for item in candidates), encoding="utf-8")


def load_candidates(path: str | Path) -> list[SyntheticCandidate]:
    return [SyntheticCandidate.model_validate_json(line) for line in Path(path).read_text().splitlines() if line.strip()]


def review_candidate(candidate: SyntheticCandidate, *, accepted: bool, reviewer: str,
                     rejection_reason: str | None = None, notes: str | None = None) -> SyntheticCandidate:
    if accepted:
        candidate.review = CandidateReview(status="accepted", semantic_equivalence=True,
            natural_language_quality="acceptable", reviewer=reviewer, notes=notes)
    else:
        candidate.review = CandidateReview(status="rejected", semantic_equivalence=False,
            natural_language_quality="unacceptable", rejection_reason=rejection_reason, reviewer=reviewer, notes=notes)
    return candidate


def freeze_accepted_dataset(candidates: list[SyntheticCandidate], seeds: list[SyntheticSeed],
                            output_path: str | Path) -> int:
    seed_by_id = {seed.id: seed for seed in seeds}; accepted = []
    for candidate in candidates:
        if candidate.review.status != "accepted": continue
        seed = seed_by_id[candidate.seed_id]
        record = {"id": candidate.candidate_id, "mission": candidate.text, "outcome": seed.outcome,
                  "strata": [*seed.strata, candidate.requested_style], "source_seed": seed.id}
        if seed.outcome == "supported":
            record.update(expected={"capabilities": seed.capabilities.model_dump(mode="json")},
                          expected_template_configuration=seed.expected_template_configuration)
        elif seed.outcome == "unsupported": record["unsupported_reason"] = seed.reason
        else:
            record.update(clarification_reason=seed.reason,
                          clarification_question=seed.clarification_question)
        accepted.append(record)
    output = Path(output_path); output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("".join(json.dumps(item, sort_keys=True) + "\n" for item in accepted), encoding="utf-8")
    return len(accepted)


def dataset_quality_report(
    candidates: list[SyntheticCandidate], seeds: list[SyntheticSeed], *,
    candidates_per_seed: int | None = None,
) -> dict:
    seed_by_id = {seed.id: seed for seed in seeds}
    accepted = [item for item in candidates if item.review.status == "accepted"]
    rejected = [item for item in candidates if item.review.status == "rejected"]
    pending = [item for item in candidates if item.review.status == "pending"]
    def counts(values: list[SyntheticCandidate], field: str) -> dict[str, int]:
        return dict(sorted(Counter(str(getattr(item, field)) for item in values).items()))
    outcome_counts = Counter(item.outcome for item in accepted)
    return {
        "generated_candidates": len(candidates), "accepted_candidates": len(accepted),
        "rejected_candidates": len(rejected), "pending_candidates": len(pending),
        "acceptance_rate": len(accepted) / len(candidates) if candidates else 0.0,
        "accepted_by_outcome": dict(sorted(outcome_counts.items())),
        "accepted_by_style": counts(accepted, "requested_style"),
        "accepted_by_seed": dict(sorted(Counter(item.seed_id for item in accepted).items())),
        "rejections_by_reason": dict(sorted(Counter(item.review.rejection_reason for item in rejected).items())),
        "reviewers": dict(sorted(Counter(item.review.reviewer for item in candidates if item.review.reviewer).items())),
        "duplicate_rate": sum(item.review.rejection_reason == "duplicate" for item in rejected) / len(candidates)
                          if candidates else 0.0,
        "semantic_drift_rate": sum(item.review.rejection_reason == "semantic_drift" for item in rejected) / len(candidates)
                               if candidates else 0.0,
        "accepted_target_comparison": {
            outcome: {"accepted": outcome_counts[outcome],
                      "generated_target": sum(
                          candidates_per_seed if candidates_per_seed is not None
                          else seed.paraphrase_count
                          for seed in seeds if seed.outcome == outcome
                      )}
            for outcome in ("supported", "unsupported", "ambiguous")
        },
        "seed_integrity": all(item.seed_id in seed_by_id for item in candidates),
    }


def write_quality_report(report: dict, output_directory: str | Path) -> dict[str, Path]:
    output = Path(output_directory); output.mkdir(parents=True, exist_ok=True)
    json_path = output / "dataset_quality.json"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = ["# Synthetic dataset quality", "", "| Measure | Count |", "|---|---:|",
             f"| Generated | {report['generated_candidates']} |", f"| Accepted | {report['accepted_candidates']} |",
             f"| Rejected | {report['rejected_candidates']} |", f"| Pending | {report['pending_candidates']} |",
             f"| Acceptance rate | {report['acceptance_rate']:.1%} |", "", "## Accepted by outcome", "",
             "| Outcome | Accepted | Generated target |", "|---|---:|---:|"]
    for outcome, values in report["accepted_target_comparison"].items():
        lines.append(f"| {outcome} | {values['accepted']} | {values['generated_target']} |")
    lines += ["", "## Accepted by linguistic style", "", "| Style | Count |", "|---|---:|"]
    lines.extend(f"| {style} | {count} |" for style, count in report["accepted_by_style"].items())
    lines += ["", "## Rejections", ""]
    if report["rejections_by_reason"]:
        lines += ["| Reason | Count |", "|---|---:|"]
        lines.extend(f"| {reason} | {count} |" for reason, count in report["rejections_by_reason"].items())
    else: lines.append("None.")
    lines += ["", "## Reviewer provenance", "", "| Reviewer | Decisions |", "|---|---:|"]
    lines.extend(f"| {reviewer} | {count} |" for reviewer, count in report["reviewers"].items())
    markdown_path = output / "dataset_quality.md"
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"json": json_path, "markdown": markdown_path}


def write_review_queue(candidates: list[SyntheticCandidate], seeds: list[SyntheticSeed], path: str | Path) -> int:
    seed_by_id = {seed.id: seed for seed in seeds}; pending = [item for item in candidates if item.review.status == "pending"]
    lines = ["# Synthetic candidate human-review queue", "",
             "Accept or reject each candidate as-is. Do not rewrite candidate text.", "",
             f"Pending candidates: **{len(pending)}**", ""]
    current_seed = None
    for item in pending:
        seed = seed_by_id[item.seed_id]
        if seed.id != current_seed:
            current_seed = seed.id
            lines += [f"## {seed.id}", "", f"Outcome: `{seed.outcome}`  ",
                      f"Verified intent: {seed.intent_description}", ""]
            if seed.outcome == "supported":
                lines += ["```json", json.dumps(seed.capabilities.model_dump(mode="json"),
                                                 indent=2, sort_keys=True), "```", ""]
            elif seed.outcome == "unsupported": lines += [f"Reason: {seed.reason}", ""]
            else: lines += [f"Reason: {seed.reason}  ", f"Question: {seed.clarification_question}", ""]
        lines += [f"### {item.candidate_id} · {item.requested_style}", "", f"> {item.text}", "",
                  "Decision: `[ ] accept` `[ ] reject`  ", "Reason:  ", "Notes:", ""]
    output = Path(path); output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines), encoding="utf-8")
    return len(pending)
