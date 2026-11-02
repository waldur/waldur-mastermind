"""Building blocks for ToolDefinition.output_schema.

Schemas describe a tool's `data` after JSON encoding with DjangoJSONEncoder,
so Decimals, dates and UUIDs are strings. Objects are closed: a key added to
a tool's data without a schema update fails the tool's tests.
"""

STRING = {"type": "string"}
INTEGER = {"type": "integer"}
NUMBER = {"type": "number"}
BOOLEAN = {"type": "boolean"}


def nullable(schema: dict) -> dict:
    return {"anyOf": [schema, {"type": "null"}]}


def array(items: dict) -> dict:
    return {"type": "array", "items": items}


def obj(properties: dict, optional: dict | None = None) -> dict:
    """A closed object: every key in ``properties`` is required."""
    return {
        "type": "object",
        "properties": {**properties, **(optional or {})},
        "required": list(properties),
        "additionalProperties": False,
    }


# A related object named by UUID, as most tools reference projects, offerings etc.
REF = obj({"uuid": STRING, "name": STRING})

# Keys list tools add when capped at MAX_LIST_RESULTS.
TRUNCATION = {"_truncated": BOOLEAN, "_total_count": INTEGER}
