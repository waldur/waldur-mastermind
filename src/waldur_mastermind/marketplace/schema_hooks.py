from unittest import mock

from drf_spectacular.generators import SchemaGenerator
from drf_spectacular.openapi import AutoSchema


def add_polymorphic_attributes_schema(result, generator: SchemaGenerator, **kwargs):
    """
    Postprocessing hook to add polymorphic schema for the 'attributes' field
    in order creation endpoints based on offering types.
    """
    from waldur_mastermind.marketplace.plugins import manager

    result_schemas = result.get("components", {}).get("schemas", {})
    offering_schemas = []

    for offering_type in manager.get_offering_types():
        processor_class = manager.get_processor(
            offering_type, "create_resource_processor"
        )
        if not processor_class:
            continue
        schema = create_offering_attributes_schema(processor_class, generator)
        if not schema:
            continue

        all_registered_components = generator.registry.build(extra_components={})
        if "schemas" in all_registered_components:
            result_schemas.update(all_registered_components["schemas"])

        schema_name = f"{offering_type.replace('.', '')}CreateOrderAttributes"
        result_schemas[schema_name] = schema
        offering_schemas.append({"$ref": f"#/components/schemas/{schema_name}"})

    result_schemas["GenericOrderAttributes"] = {
        "type": "object",
        "description": "A generic JSON object for offerings without a predefined schema. Allows any key-value pairs.",
        "additionalProperties": True,
        "properties": {
            "name": {
                "type": "string",
                "description": "The name of the resource to be created. Will be displayed in the portal.",
                "maxLength": 150,
            },
            "description": {
                "type": "string",
                "description": "A free-form description for the resource.",
            },
        },
    }

    offering_schemas.append({"$ref": "#/components/schemas/GenericOrderAttributes"})

    result_schemas["OrderCreateRequest"]["properties"]["attributes"] = {
        "oneOf": offering_schemas,
        "description": (
            "Attributes structure depends on the offering type specified in the parent object. "
            "Can also be a generic object for offerings without a specific attributes schema."
        ),
    }

    return result


def create_offering_attributes_schema(processor_class, generator):
    """
    Create schema for attributes field specific to an offering type.
    This extracts the field definitions from the processor configuration.
    """
    from waldur_mastermind.marketplace.views import OrderViewSet

    if getattr(processor_class, "create_serializer_class", None):
        serializer_class = processor_class.create_serializer_class
        auto = AutoSchema()
        auto.view = OrderViewSet.as_view({"post": "create"})
        auto.view.request = mock.Mock()
        auto.view.request.query_params.getlist.return_value = []
        auto.registry = generator.registry
    else:
        viewset_class = getattr(processor_class, "viewset", None)

        if not viewset_class:
            return None

        # Get the serializer class (prefer create_serializer_class)
        serializer_class = getattr(
            viewset_class, "create_serializer_class", None
        ) or getattr(viewset_class, "serializer_class", None)

        if not serializer_class:
            return None

        auto = AutoSchema()
        auto.view = viewset_class.as_view({"post": "create"})
        auto.view.request = mock.Mock()
        auto.view.request.query_params.getlist.return_value = []
        auto.registry = generator.registry

    schema = auto._map_serializer(serializer_class, direction="request")
    fields = getattr(processor_class, "fields", ())
    if fields:
        schema["properties"] = {
            field: schema["properties"][field]
            for field in fields
            if field in schema["properties"]
        }
    return schema
