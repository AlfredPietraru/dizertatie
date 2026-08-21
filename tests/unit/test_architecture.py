"""Guard the one-way dependency structure of the production package."""

from pathlib import Path
import unittest


class ArchitectureTests(unittest.TestCase):
    def test_layers_do_not_import_downstream_layers(self) -> None:
        package = Path("src/ros_config_builder")
        forbidden = {
            "schemas": ("extraction", "integration", "templating", "validation"),
            "extraction": ("integration", "templating", "validation"),
            "integration": ("templating", "validation"),
            "templating": ("validation",),
        }
        violations: list[str] = []
        for layer, downstream in forbidden.items():
            for source in (package / layer).glob("*.py"):
                text = source.read_text(encoding="utf-8")
                for target in downstream:
                    needles = (f"ros_config_builder.{target}", f"..{target}")
                    if any(needle in text for needle in needles):
                        violations.append(f"{source} imports downstream layer {target}")
        self.assertEqual([], violations)

    def test_production_tests_use_canonical_package(self) -> None:
        offenders = []
        for source in Path("tests").rglob("test_*.py"):
            if source.resolve() == Path(__file__).resolve():
                continue
            if "repo_code_extractor" in source.read_text(encoding="utf-8"):
                offenders.append(source.as_posix())
        self.assertEqual([], offenders)


if __name__ == "__main__":
    unittest.main()
