from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ros_config_builder.mission import (
    automatically_validate_candidates, dataset_quality_report, freeze_accepted_dataset,
    generate_paraphrases, load_capability_registry, load_synthetic_seeds,
    review_candidate, write_quality_report,
)


class FakeBackend:
    model = "fake"
    def __call__(self, _system: str, user: str) -> str:
        styles = json.loads(user.split("Requested styles in order:\n", 1)[1])
        return json.dumps({"candidates": [{"requested_style": style, "text": f"Mission in {style} style"}
                                          for style in styles]})


class SyntheticGenerationTests(unittest.TestCase):
    def test_generation_validation_review_and_freeze(self) -> None:
        registry = load_capability_registry("configuration_templates/capability_registry.yaml")
        seed = load_synthetic_seeds("data/synthetic_seed_intents_v1.jsonl", registry)[0]
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


if __name__ == "__main__": unittest.main()
