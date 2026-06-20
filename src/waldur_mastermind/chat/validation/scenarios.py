import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)


@dataclass
class EvaluationCriteria:
    """Defines what to check in an LLM response."""

    type: str  # "tool_usage", "pattern", "length", "code_block", "language"
    config: dict = field(default_factory=dict)

    def __post_init__(self):
        """Validate evaluation criteria type."""
        valid_types = {
            "tool_usage",
            "pattern",
            "length",
            "code_block",
            "language",
            "data_match",
            "tool_arguments",
        }
        if self.type not in valid_types:
            raise ValueError(
                f"Invalid evaluation type '{self.type}'. "
                f"Must be one of: {', '.join(valid_types)}"
            )


@dataclass
class Scenario:
    """A test scenario for LLM validation."""

    name: str
    category: str
    description: str
    inputs: list[str]
    evaluations: list[EvaluationCriteria | dict] = field(default_factory=list)

    def __post_init__(self):
        """Convert evaluation dicts to EvaluationCriteria objects."""
        converted_evaluations: list[EvaluationCriteria] = []
        for ev in self.evaluations:
            if isinstance(ev, dict):
                converted_evaluations.append(
                    EvaluationCriteria(
                        type=ev["type"],
                        config=ev.get("config", {}),
                    )
                )
            else:
                converted_evaluations.append(ev)
        self.evaluations = converted_evaluations  # type: ignore[assignment]


@dataclass
class EvaluationResult:
    """Result of a single evaluation check."""

    passed: bool
    score: float  # 0.0 to 1.0
    message: str
    details: dict | None = None


@dataclass
class ScenarioResult:
    """Result of running a complete scenario."""

    scenario_name: str
    input_text: str
    response_text: str
    tool_called: str | None
    evaluations: list[EvaluationResult]
    total_score: float
    max_score: float
    passed: bool
    duration_ms: int


def load_scenarios_from_yaml(yaml_path: Path) -> list[Scenario]:
    """
    Load test scenarios from a YAML file.

    Args:
        yaml_path: Path to YAML file containing scenarios

    Returns:
        List of Scenario objects

    Raises:
        FileNotFoundError: If YAML file doesn't exist
        yaml.YAMLError: If YAML file is malformed
    """
    if not yaml_path.exists():
        raise FileNotFoundError(f"Scenario file not found: {yaml_path}")

    try:
        with open(yaml_path) as f:
            data = yaml.safe_load(f)

        if not data:
            logger.warning(f"Empty scenario file: {yaml_path}")
            return []

        scenarios = []
        for scenario_data in data:
            try:
                # Extract category from filename (e.g., "tool_usage.yaml" -> "tool_usage")
                category = yaml_path.stem

                scenario = Scenario(
                    name=scenario_data["name"],
                    category=category,
                    description=scenario_data.get("description", ""),
                    inputs=scenario_data.get("inputs", []),
                    evaluations=scenario_data.get("evaluations", []),
                )
                scenarios.append(scenario)

            except (KeyError, TypeError) as e:
                logger.error(
                    f"Failed to parse scenario in {yaml_path}: {e}",
                    exc_info=True,
                )
                continue

        return scenarios

    except yaml.YAMLError as e:
        logger.error(f"Failed to parse YAML file {yaml_path}: {e}")
        raise


def load_all_scenarios(scenarios_dir: Path) -> dict[str, list[Scenario]]:
    """
    Load all scenarios from a directory, organized by category.

    Args:
        scenarios_dir: Directory containing YAML scenario files

    Returns:
        Dictionary mapping category name to list of scenarios
    """
    if not scenarios_dir.exists():
        raise FileNotFoundError(f"Scenarios directory not found: {scenarios_dir}")

    scenarios_by_category = {}

    for yaml_file in sorted(scenarios_dir.glob("*.yaml")):
        try:
            scenarios = load_scenarios_from_yaml(yaml_file)
            category = yaml_file.stem

            if scenarios:
                scenarios_by_category[category] = scenarios
                logger.info(
                    f"Loaded {len(scenarios)} scenarios from category '{category}'"
                )

        except Exception as e:
            logger.error(f"Failed to load scenarios from {yaml_file}: {e}")
            continue

    return scenarios_by_category
