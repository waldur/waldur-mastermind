from waldur_mastermind.chat.ui_registry import ui_registry

ui_registry.register(
    key="markdown",
    name="Markdown Content",
    description="Standard markdown text",
    schema={
        "type": "object",
        "required": ["c"],
        "properties": {"c": "string"},
    },
    # No triggers, handled by parser as default text
)

ui_registry.register(
    key="code",
    name="Code Block",
    description="Syntax highlighted code",
    schema={
        "type": "object",
        "required": ["c", "t"],
        "properties": {"c": "string", "t": "string"},
    },
    lexical_triggers=[""],
    is_default_for_code=True,  # Handles unknown tags like ```foobar
    has_loading_state=True,
)


ui_registry.register(
    key="mermaid",
    name="Mermaid Diagram",
    description="Mermaid diagrams",
    schema={
        "type": "object",
        "required": ["c"],
        "properties": {"c": "string"},
    },
    lexical_triggers=["mermaid"],
    has_loading_state=True,
)


ui_registry.register(
    key="load",
    name="Loading Indicator",
    description="Indicates that a block is currently generating",
    schema={
        "type": "object",
        "required": ["t"],
        "properties": {"t": "string"},
    },
)


ui_registry.register(
    key="table",
    name="Data Table",
    description="Interactive data table with columns and rows",
    schema={
        "type": "object",
        "required": ["h", "r"],
        "properties": {
            "h": {"type": "array"},  # headers: ["Name", "Category ", "State", ...]
            "r": {"type": "array"},  # rows: [["VM1", "Storage", "OK"], ...]
            "n": "number",  # total count (optional)
        },
    },
    has_loading_state=True,
)
