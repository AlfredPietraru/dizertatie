"""Small application facade over the independently testable pipeline stages."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .extraction import extract_launch_files, extract_parameter_yaml, extract_ros_node_ir
from .integration import build_system_model, extract_package_metadata
from .templating import render_configuration_bundle
from .validation import validate_system_model


class SemesterOnePipeline:
    """Expose stable stage boundaries without coupling their implementations."""

    def __init__(self, workspace: str | Path) -> None:
        self.workspace = Path(workspace).resolve()

    def extract(self) -> dict[str, Any]:
        return {
            "python": extract_ros_node_ir(self.workspace),
            "launch": extract_launch_files(self.workspace),
            "parameters": extract_parameter_yaml(self.workspace),
            "packages": extract_package_metadata(self.workspace),
        }

    def integrate(
        self,
        root_launch_files: list[str],
        facts: dict[str, Any] | None = None,
        *,
        root_arguments: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        facts = facts or self.extract()
        return build_system_model(
            workspace=self.workspace,
            step1=facts["python"],
            launch=facts["launch"],
            configuration=facts["parameters"],
            package_metadata=facts["packages"],
            root_launch_files=root_launch_files,
            root_arguments=root_arguments,
        )

    def validate(self, model: dict[str, Any]) -> dict[str, Any]:
        return validate_system_model(model)

    def render(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return render_configuration_bundle(*args, **kwargs)
