import logging

from waldur_mastermind.chat.schemas import UIContent

logger = logging.getLogger(__name__)


class UIComponent:
    TYPE_MAP = {
        "string": str,
        "number": (int, float),
        "array": list,
        "object": dict,
    }

    def __init__(
        self,
        key: str,
        name: str,
        description: str,
        schema: dict,
        has_loading_state: bool = False,
    ):
        self.key = key
        self.name = name
        self.description = description
        self.schema = schema
        self.has_loading_state = has_loading_state

    @property
    def properties(self) -> dict:
        return self.schema.get("properties", {})

    @property
    def required_fields(self) -> list:
        return self.schema.get("required", [])

    def process_block(self, input: dict) -> dict:
        """Filter input to only include fields defined in schema."""
        return {field: input[field] for field in self.properties if field in input}

    def validate(self, data: dict) -> tuple[bool, str | None]:
        """Validate data against schema."""
        for field in self.required_fields:  # Check required fields
            if field not in data:
                return False, f"Missing required field: {field}"

        for field, value in data.items():  # Type check fields
            prop_def = self.properties.get(field)
            if prop_def:
                expected_type = (
                    prop_def if isinstance(prop_def, str) else prop_def.get("type")
                )
                if expected_type and not self._check_type(value, expected_type):
                    return False, f"Field '{field}' must be a {expected_type}"

        return True, None

    def _check_type(self, value, expected_type: str) -> bool:
        if expected_type in self.TYPE_MAP:
            return isinstance(value, self.TYPE_MAP[expected_type])
        return True

    def get(self, key, default=None):
        # Backward compatibility for dict-like access
        if key == "has_loading_state":
            return self.has_loading_state
        if key == "schema":
            return self.schema
        if key == "key":
            return self.key
        return default

    def __getitem__(self, item):
        val = self.get(item)
        if val is None:
            raise KeyError(item)
        return val


class UIComponentRegistry:
    def __init__(self):
        self._components: dict[str, UIComponent] = {}
        self._trigger_map: dict[str, str] = {}
        self._default_code_component: str | None = None

    def register(
        self,
        key: str,
        name: str,
        description: str,
        schema: dict,
        lexical_triggers: list[str] | None = None,
        is_default_for_code: bool = False,
        has_loading_state: bool = False,
    ):
        if key in self._components:
            logger.warning(f"Component {key} already registered, overwriting")

        self._components[key] = UIComponent(
            key,
            name,
            description,
            schema,
            has_loading_state,
        )

        if lexical_triggers:
            for trigger in lexical_triggers:
                self._trigger_map[trigger.lower()] = key

        if is_default_for_code:
            self._default_code_component = key

    def get(self, key: str) -> UIComponent | None:
        return self._components.get(key)

    def get_by_trigger(self, tag: str) -> UIComponent | None:
        if tag is not None:
            key = self._trigger_map.get(tag.lower())
            if key:
                return self.get(key)

        if self._default_code_component:
            return self.get(self._default_code_component)

        return None

    def create_content(self, key: str, data: dict) -> UIContent:
        component = self.get(key)
        if not component:
            raise ValueError(f"Unknown component: {key}")

        # Filter and validate through the component
        filtered_data = component.process_block(data)
        is_valid, error = component.validate(filtered_data)
        if not is_valid:
            raise ValueError(f"Invalid data for {key}: {error}")

        return UIContent(key=key, data=filtered_data)

    def create_content_from_block(self, raw_content: dict) -> UIContent:
        tag = raw_content.get("t")
        if not tag:
            tag = self._default_code_component

        component = self.get_by_trigger(tag)
        if not component:
            raise ValueError(f"No component registered for tag: {tag}")

        return self.create_content(component.key, raw_content)


ui_registry = UIComponentRegistry()
