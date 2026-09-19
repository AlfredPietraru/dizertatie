from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ros_config_builder.mission import (
    automatically_validate_candidates, dataset_quality_report, freeze_accepted_dataset,
    build_generation_prompt, generate_paraphrases, load_capability_registry,
    load_synthetic_seeds, review_candidate, semantic_validation_issues,
    validate_frozen_dataset, write_quality_report,
)
from ros_config_builder.mission.generation import SyntheticCandidate


class FakeBackend:
    model = "fake"
    def __call__(self, _system: str, user: str) -> str:
        styles = json.loads(user.split("Requested styles in order:\n", 1)[1])
        return json.dumps({"candidates": [
            {"requested_style": style, "text": f"Mission {index} in {style} style"}
            for index, style in enumerate(styles, 1)
        ]})


class OverGeneratingBackend:
    model = "fake"
    def __call__(self, _system: str, user: str) -> str:
        styles = json.loads(user.split("Requested styles in order:\n", 1)[1])
        return json.dumps({"candidates": [
            {"requested_style": style, "text": f"First {style} candidate"}
            for style in styles for _ in range(3)
        ]})


class SyntheticGenerationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        registry = load_capability_registry("configuration_templates/capability_registry.yaml")
        cls.train_seeds = load_synthetic_seeds(
            "data/antrobot_train_seed_missions_v1.jsonl", registry,
        )

    def _candidate(self, seed_id: str, text: str) -> tuple[SyntheticCandidate, object]:
        seed = next(seed for seed in self.train_seeds if seed.id == seed_id)
        return SyntheticCandidate(
            candidate_id=f"{seed_id}-test", seed_id=seed_id, outcome=seed.outcome,
            requested_style="natural", text=text, generator_model="test",
        ), seed

    def test_generation_prompt_hides_renderer_defaults(self) -> None:
        prompt, _ = build_generation_prompt(self.train_seeds[0], ["natural"])
        self.assertNotIn("expected_template_configuration", prompt)
        self.assertNotIn("launch.launch_", prompt)

    def test_train_and_test_seed_sets_are_disjoint_and_policy_valid(self) -> None:
        registry = load_capability_registry("configuration_templates/capability_registry.yaml")
        test_seeds = load_synthetic_seeds(
            "data/antrobot_test_seed_missions_v1.jsonl", registry,
        )
        self.assertEqual(len(self.train_seeds), 50)
        self.assertEqual(len(test_seeds), 50)
        self.assertTrue(
            {seed.id for seed in self.train_seeds}.isdisjoint({seed.id for seed in test_seeds})
        )

    def test_semantic_validation_rejects_frequency_period_drift(self) -> None:
        candidate, seed = self._candidate(
            "seed-108", "Publish joint-state updates every 30 milliseconds.",
        )
        self.assertTrue(semantic_validation_issues(candidate, seed))

    def test_semantic_validation_rejects_length_magnitude_drift(self) -> None:
        candidate, seed = self._candidate(
            "seed-113", "Set wheel separation to 0.25 cm everywhere it is used.",
        )
        self.assertTrue(semantic_validation_issues(candidate, seed))

    def test_semantic_validation_rejects_invented_identifier(self) -> None:
        candidate, seed = self._candidate(
            "seed-110", "Enable exploration at 2 Hz and disable explore_heavy.",
        )
        self.assertIn("invented identifiers", " ".join(semantic_validation_issues(candidate, seed)))

    def test_semantic_validation_accepts_preserved_frequency(self) -> None:
        candidate, seed = self._candidate(
            "seed-108", "Set the joint-state publication frequency to 30 Hz.",
        )
        self.assertEqual(semantic_validation_issues(candidate, seed), [])

    def test_generation_keeps_one_candidate_per_style_when_model_over_generates(self) -> None:
        registry = load_capability_registry("configuration_templates/capability_registry.yaml")
        seed = load_synthetic_seeds("data/antrobot_test_seed_missions_v1.jsonl", registry)[0]
        candidates, _ = generate_paraphrases(
            seed, OverGeneratingBackend(), requested_styles=["canonical", "natural"]
        )
        self.assertEqual([item.requested_style for item in candidates], ["canonical", "natural"])
        self.assertEqual(len(candidates), 2)

    def test_generation_validation_review_and_freeze(self) -> None:
        registry = load_capability_registry("configuration_templates/capability_registry.yaml")
        seed = load_synthetic_seeds("data/antrobot_test_seed_missions_v1.jsonl", registry)[0]
        candidates, raw = generate_paraphrases(seed, FakeBackend())
        self.assertEqual(len(candidates), seed.paraphrase_count)
        self.assertTrue(raw)
        candidates[1].text = candidates[0].text.upper()
        automatically_validate_candidates(candidates)
        self.assertEqual(candidates[1].review.rejection_reason, "duplicate")
        review_candidate(candidates[0], accepted=True, reviewer="human")
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "accepted.jsonl"
            count = freeze_accepted_dataset(candidates, [seed], output)
            self.assertEqual(count, 1)
            record = json.loads(output.read_text())
            self.assertEqual(record["source_seed"], seed.id)
            self.assertEqual(record["expected_template_configuration"], seed.expected_template_configuration)
            report = dataset_quality_report(candidates, [seed])
            paths = write_quality_report(report, Path(temporary) / "quality")
            self.assertEqual(report["accepted_candidates"], 1)
            self.assertEqual(report["rejections_by_reason"], {"duplicate": 1})
            self.assertTrue(all(path.exists() for path in paths.values()))

    def test_frozen_dataset_metadata_checks_hash_and_count(self) -> None:
        import hashlib
        with tempfile.TemporaryDirectory() as temporary:
            dataset = Path(temporary) / "dataset.jsonl"
            dataset.write_text('{"id":"one"}\n', encoding="utf-8")
            metadata = dataset.with_suffix(".metadata.json")
            metadata.write_text(json.dumps({
                "dataset_status": "frozen", "record_count": 1,
                "dataset_sha256": hashlib.sha256(dataset.read_bytes()).hexdigest(),
            }), encoding="utf-8")
            self.assertEqual(validate_frozen_dataset(dataset)["dataset_status"], "frozen")
            dataset.write_text('{"id":"changed"}\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "hash"):
                validate_frozen_dataset(dataset)


if __name__ == "__main__": unittest.main()
