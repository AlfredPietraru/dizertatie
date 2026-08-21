# ROS Config Builder

This repository contains the AntRobot ROS workspace and the deterministic
Semester 1 configuration pipeline. The maintained Python package is
`ros_config_builder`; source ROS packages remain under `src/`.

## Architecture

- `src/ros_config_builder/schemas/`: versioned data contracts
- `src/ros_config_builder/extraction/`: Python, launch, and YAML fact extraction
- `src/ros_config_builder/integration/`: integrated ROS system model
- `src/ros_config_builder/templating/`: curated interface, templates, renderer
- `src/ros_config_builder/validation/`: acceptance and semantic-delta checks
- `configuration_templates/`: frozen templates, profiles, and expectations
- `policies/`: human-curated policy
- `tests/`: tests arranged by scope
- `experiments/`: non-production LLM, vector-store, and legacy work

Install the package for development with `python3 -m pip install -e .`, then
run `python3 -m unittest discover -v` for the complete acceptance suite.

The old `repo_code_extractor` imports remain as compatibility shims during the
transition; new code must import `ros_config_builder`.
