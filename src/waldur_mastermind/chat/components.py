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


ui_registry.register(
    key="vm_order",
    name="VM Order Result",
    description="Structured display of VM creation order result or configuration form",
    schema={
        "type": "object",
        "required": [
            "name",
            "status",
        ],  # order_id not required (preview has no order yet)
        "properties": {
            "order_id": {"type": "string"},  # Order UUID (only in success/error)
            "name": {"type": "string"},  # VM name
            "flavor": {
                "type": "string"
            },  # Flavor display (e.g., "m1.small (2 vCPU, 4GB RAM)")
            "image": {"type": "string"},  # Image name
            "status": {"type": "string"},  # Order state (form/preview/success/error)
            "message": {"type": "string"},  # Success message (optional)
            "error": {"type": "string"},  # Error message (optional)
            "content": {
                "type": "string"
            },  # Preview intro text or form instructions (optional)
            "project": {"type": "string"},  # Project name (optional)
            "organization": {"type": "string"},  # Organization/customer name (optional)
            "project_uuid": {"type": "string"},  # Project UUID (optional)
            "flavors": {"type": "array"},  # Available flavors list (form mode only)
            "images": {"type": "array"},  # Available images list (form mode only)
            "projects": {
                "type": "array"
            },  # Available projects list (project_form mode only)
        },
    },
)
