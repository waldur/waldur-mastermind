import re
import unittest
from unittest import mock

import pytest
from drf_spectacular.drainage import reset_generator_stats
from drf_spectacular.generators import SchemaGenerator
from rest_framework import serializers, viewsets

from waldur_core.core.schema_hooks import (
    _to_pascal_case,
    add_polymorphic_attributes_schema,
    add_result_count_header,
    create_offering_attributes_schema,
    inject_waldur_operation_ids,
    make_fields_optional,
    preprocess_request_bodies,
    remove_waldur_cookie_auth,
    transform_paginated_arrays,
    validate_go_sdk_naming_collisions,
    validate_python_sdk_naming_collisions,
    validate_waldur_operation_ids,
)
from waldur_core.server.openapi_settings import SPECTACULAR_SETTINGS


def test_remove_waldur_cookie_auth():
    # Test input schema
    schema = {
        "paths": {
            "/api/path1": {
                "get": {"security": [{"waldurCookieAuth": []}, {"tokenAuth": []}]}
            },
            "/api/path2": {
                "post": {
                    "security": [
                        {"waldurCookieAuth": []},
                    ]
                }
            },
            "/api/path3": {"put": {"security": [{"tokenAuth": []}]}},
        }
    }

    # Run the function
    result = remove_waldur_cookie_auth(schema, None)

    # Verify waldurCookieAuth was removed but other auth remains
    assert result["paths"]["/api/path1"]["get"]["security"] == [{"tokenAuth": []}]

    # Verify security field is removed when empty after waldurCookieAuth removal
    assert "security" not in result["paths"]["/api/path2"]["post"]

    # Verify paths without waldurCookieAuth are unchanged
    assert result["paths"]["/api/path3"]["put"]["security"] == [{"tokenAuth": []}]


def test_remove_waldur_cookie_auth_no_security():
    # Test input with no security fields
    schema = {"paths": {"/api/path1": {"get": {"description": "test endpoint"}}}}

    # Run the function
    result = remove_waldur_cookie_auth(schema, None)

    # Verify schema is unchanged
    assert result == schema


def test_make_fields_optional():
    schema = {
        "paths": {
            "/api/resource": {
                "get": {
                    "parameters": [
                        {"name": "field", "in": "query", "schema": {"type": "string"}}
                    ],
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Resource"}
                                }
                            }
                        }
                    },
                }
            }
        },
        "components": {
            "schemas": {
                "Resource": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "description": {"type": "string"},
                    },
                    "required": ["name", "description"],
                },
            }
        },
    }

    expected_schema = {
        "paths": {
            "/api/resource": {
                "get": {
                    "parameters": [
                        {"name": "field", "in": "query", "schema": {"type": "string"}}
                    ],
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Resource"}
                                }
                            }
                        }
                    },
                }
            }
        },
        "components": {
            "schemas": {
                "Resource": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "description": {"type": "string"},
                    },
                    "required": [],
                },
            }
        },
    }

    result = make_fields_optional(schema.copy(), None)
    assert result == expected_schema


def test_transform_paginated_arrays():
    schema = {
        "components": {
            "schemas": {
                "PaginatedResourcesList": {
                    "type": "array",
                    "items": {"$ref": "#/components/schemas/Resource"},
                },
                "Resource": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "description": {"type": "string"},
                    },
                },
            }
        },
        "paths": {
            "/api/resources": {
                "get": {
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "$ref": "#/components/schemas/PaginatedResourcesList"
                                    }
                                }
                            }
                        }
                    }
                }
            },
        },
    }

    expected_schema = {
        "components": {
            "schemas": {
                "Resource": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "description": {"type": "string"},
                    },
                },
            }
        },
        "paths": {
            "/api/resources": {
                "get": {
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "array",
                                        "items": {
                                            "$ref": "#/components/schemas/Resource"
                                        },
                                    }
                                }
                            }
                        }
                    }
                }
            },
        },
    }

    result = transform_paginated_arrays(schema.copy(), None)
    assert result == expected_schema


class TestAddResultCountHeader(unittest.TestCase):
    def test_adds_header_to_list_operation_response(self):
        """Test header reference added to list operation responses"""
        result = {
            "paths": {
                "/items/": {
                    "get": {
                        "operationId": "item_list",
                        "responses": {
                            "200": {
                                "content": {
                                    "application/json": {"schema": {"type": "array"}}
                                }
                            }
                        },
                    }
                }
            }
        }
        modified = add_result_count_header(result, None)

        response = modified["paths"]["/items/"]["get"]["responses"]["200"]
        self.assertIn("headers", response)
        self.assertEqual(
            response["headers"]["x-result-count"],
            {"$ref": "#/components/headers/XResultCount"},
        )

    def test_skips_non_list_operations(self):
        """Test header not added to non-list operations"""
        result = {
            "paths": {
                "/items/": {
                    "get": {
                        "operationId": "item_detail",
                        "responses": {
                            "200": {
                                "content": {
                                    "application/json": {"schema": {"type": "object"}}
                                }
                            }
                        },
                    }
                }
            }
        }
        modified = add_result_count_header(result, None)

        response = modified["paths"]["/items/"]["get"]["responses"]["200"]
        self.assertNotIn("headers", response)


def test_validate_waldur_operation_ids(capsys):
    reset_generator_stats()

    # 1. Test failure
    schema = {
        "paths": {
            "/api/test": {
                "get": {
                    "operationId": "test_list",
                    "parameters": [
                        {
                            "name": "customer_uuid",
                            "in": "query",
                            "schema": {"type": "string", "format": "uuid"},
                        }
                    ],
                }
            }
        }
    }
    validate_waldur_operation_ids(schema, None)
    captured = capsys.readouterr()
    assert "is a UUID but is missing 'x-waldur-operation-id'" in captured.err

    # 2. Test whitelist
    for field in ["uuid", "scope_uuid", "scope", "parent_uuid"]:
        schema = {
            "paths": {
                "/api/test": {
                    "get": {
                        "operationId": "test_list",
                        "parameters": [
                            {
                                "name": field,
                                "in": "query",
                                "schema": {"type": "string", "format": "uuid"},
                            }
                        ],
                    }
                }
            }
        }
        validate_waldur_operation_ids(schema, None)  # Should not raise

    # 3. Test success with extension (referenced operationId must exist)
    schema = {
        "paths": {
            "/api/customers/{uuid}/": {
                "get": {
                    "operationId": "customers_retrieve",
                }
            },
            "/api/test": {
                "get": {
                    "operationId": "test_list",
                    "parameters": [
                        {
                            "name": "customer_uuid",
                            "in": "query",
                            "schema": {"type": "string", "format": "uuid"},
                            "x-waldur-operation-id": "customers_retrieve",
                        }
                    ],
                }
            },
        }
    }
    validate_waldur_operation_ids(schema, None)  # Should not raise

    # 4. Test failure when x-waldur-operation-id references a non-existent operationId
    schema = {
        "paths": {
            "/api/test": {
                "get": {
                    "operationId": "test_list",
                    "parameters": [
                        {
                            "name": "customer_uuid",
                            "in": "query",
                            "schema": {"type": "string", "format": "uuid"},
                            "x-waldur-operation-id": "nonexistent_operation",
                        }
                    ],
                }
            }
        }
    }
    validate_waldur_operation_ids(schema, None)
    captured = capsys.readouterr()
    assert "does not match any operationId in the schema" in captured.err


def test_inject_waldur_operation_ids():
    from unittest import mock

    from waldur_core.core import filters as core_filters

    # Mock generator and its endpoints
    # path, path_regex, method, view
    mock_view = mock.Mock()
    mock_filter = mock.Mock(spec=core_filters.URLFilter)
    mock_filter.view_name = "customers-detail"
    mock_view.cls.filterset_class.base_filters = {"customer": mock_filter}

    generator = mock.Mock()
    mock_view_no_cls = mock.Mock()
    mock_view_no_cls.cls = None
    generator.endpoints = [
        ("/api/customers/", None, "GET", mock_view_no_cls),
        ("/api/test/", None, "GET", mock_view),
    ]

    result = {
        "paths": {
            "/api/customers/": {"get": {"operationId": "customers_list"}},
            "/api/test/": {
                "get": {
                    "operationId": "test_list",
                    "parameters": [{"name": "customer", "in": "query"}],
                }
            },
        }
    }

    with mock.patch("waldur_core.core.schema_hooks.resolve") as mock_resolve:

        def side_effect(path):
            m = mock.Mock()
            if "customers" in path:
                m.view_name = "customers-list"
            else:
                m.view_name = "test-list"
            return m

        mock_resolve.side_effect = side_effect

        inject_waldur_operation_ids(result, generator)

    param = result["paths"]["/api/test/"]["get"]["parameters"][0]
    assert param["x-waldur-operation-id"] == "customers_list"


def test_to_pascal_case():
    assert _to_pascal_case("identity_bridge") == "IdentityBridge"
    assert _to_pascal_case("identity_bridge_create") == "IdentityBridgeCreate"
    assert _to_pascal_case("customers_list") == "CustomersList"
    assert _to_pascal_case("simple") == "Simple"


def test_validate_go_sdk_naming_collisions_detects_collision(capsys):
    """Schema component 'IdentityBridgeResponse' collides with wrapper for operation 'identity_bridge'."""
    reset_generator_stats()

    schema = {
        "paths": {
            "/api/identity-bridge/": {
                "post": {
                    "operationId": "identity_bridge",
                }
            }
        },
        "components": {
            "schemas": {
                "IdentityBridgeResponse": {
                    "type": "object",
                    "properties": {"uuid": {"type": "string"}},
                }
            }
        },
    }
    validate_go_sdk_naming_collisions(schema, None)
    captured = capsys.readouterr()
    assert "IdentityBridgeResponse" in captured.err


def test_validate_go_sdk_naming_collisions_passes_for_safe_names():
    """Schema component 'IdentityBridgeResult' does NOT collide with wrapper for 'identity_bridge'."""
    schema = {
        "paths": {
            "/api/identity-bridge/": {
                "post": {
                    "operationId": "identity_bridge",
                }
            }
        },
        "components": {
            "schemas": {
                "IdentityBridgeResult": {
                    "type": "object",
                    "properties": {"uuid": {"type": "string"}},
                }
            }
        },
    }
    # Should not raise
    result = validate_go_sdk_naming_collisions(schema, None)
    assert result is schema


def test_validate_go_sdk_naming_collisions_empty_schema():
    """Empty schemas and paths should pass without error."""
    schema = {"paths": {}, "components": {"schemas": {}}}
    result = validate_go_sdk_naming_collisions(schema, None)
    assert result is schema


def test_validate_go_sdk_naming_collisions_multiple_collisions(capsys):
    """Multiple collisions should all be reported in a single error."""
    reset_generator_stats()

    schema = {
        "paths": {
            "/api/identity-bridge/": {"post": {"operationId": "identity_bridge"}},
            "/api/identity-bridge/remove/": {
                "post": {"operationId": "identity_bridge_remove"}
            },
        },
        "components": {
            "schemas": {
                "IdentityBridgeResponse": {"type": "object"},
                "IdentityBridgeRemoveResponse": {"type": "object"},
            }
        },
    }
    validate_go_sdk_naming_collisions(schema, None)
    captured = capsys.readouterr()

    # Both collisions should be in the error message
    assert "IdentityBridgeResponse" in captured.err
    assert "IdentityBridgeRemoveResponse" in captured.err


def _obj(**properties):
    return {"type": "object", "properties": properties}


_A_FIELD = {"a": {"type": "string"}}

# Each case: the components, and the class names the hook must report. Every
# case was run through openapi-python-client itself: for each reported name it
# removed a schema ("Unable to process schema"; inside a union it only says
# "Invalid property in union"), and for the cases with none it removed nothing.
PYTHON_SDK_NAMING_CASES = [
    pytest.param(
        {
            "Resource": _obj(
                spending_limits={
                    "type": "object",
                    "additionalProperties": {"type": "object"},
                }
            ),
            "ResourceSpendingLimits": _obj(spending_limits={"type": "object"}),
        },
        ["ResourceSpendingLimits"],
        id="inline-object-vs-component",
    ),
    pytest.param(
        {
            "Resource": _obj(
                spending_limits={"type": "object"},
                offering={"$ref": "#/components/schemas/Offering"},
            ),
            "Offering": {"type": "object"},
            "SetResourceSpendingLimits": _obj(spending_limits={"type": "object"}),
        },
        [],
        id="renamed-body-does-not-clash",
    ),
    pytest.param(
        {
            "Resource": _obj(
                spending_limits={**_obj(**_A_FIELD), "nullable": True},
            ),
            "ResourceSpendingLimitsType0": _obj(**_A_FIELD),
        },
        ["ResourceSpendingLimitsType0"],
        id="nullable-inline-object-is-named-type0",
    ),
    pytest.param(
        {
            "Resource": _obj(
                spending_limits={**_obj(**_A_FIELD), "nullable": True},
            ),
            "ResourceSpendingLimits": _obj(**_A_FIELD),
        },
        [],
        id="nullable-inline-object-does-not-take-the-plain-name",
    ),
    pytest.param(
        {
            "Resource": _obj(
                limits={"oneOf": [_obj(**_A_FIELD), {"type": "string"}]},
            ),
            "ResourceLimitsType0": _obj(**_A_FIELD),
        },
        ["ResourceLimitsType0"],
        id="one-of-member",
    ),
    pytest.param(
        {
            "FooBar": _obj(baz=_obj(**_A_FIELD)),
            "Foo": _obj(bar_baz=_obj(**_A_FIELD)),
        },
        ["FooBarBaz"],
        id="two-inline-objects",
    ),
    pytest.param(
        {
            "Resource": _obj(tags={"type": "array", "items": _obj(**_A_FIELD)}),
            "ResourceTagsItem": _obj(**_A_FIELD),
        },
        ["ResourceTagsItem"],
        id="array-item",
    ),
    pytest.param(
        {
            "Resource": {"type": "object", "additionalProperties": _obj(**_A_FIELD)},
            "ResourceAdditionalProperty": _obj(**_A_FIELD),
        },
        ["ResourceAdditionalProperty"],
        id="additional-properties",
    ),
    pytest.param(
        {
            "Resource": _obj(state={"type": "string", "enum": ["ok", "erred"]}),
            "ResourceState": _obj(**_A_FIELD),
        },
        ["ResourceState"],
        id="inline-enum-vs-component",
    ),
    pytest.param(
        {
            "FooBar": _obj(baz={"type": "string", "enum": ["x", "y"]}),
            "Foo": _obj(bar_baz={"type": "string", "enum": ["x", "y"]}),
        },
        [],
        id="enums-with-equal-values-are-shared",
    ),
    pytest.param(
        {
            "FooBar": _obj(baz={"type": "string", "enum": ["x", "y"]}),
            "Foo": _obj(bar_baz={"type": "string", "enum": ["x", "z"]}),
        },
        ["FooBarBaz"],
        id="enums-with-different-values",
    ),
]


def _python_sdk_collisions(schemas, capsys):
    reset_generator_stats()
    schema = {"components": {"schemas": schemas}}
    assert validate_python_sdk_naming_collisions(schema, None) is schema
    return [
        line
        for line in capsys.readouterr().err.splitlines()
        if "Python SDK naming collision" in line
    ]


@pytest.mark.parametrize("schemas, clashing_classes", PYTHON_SDK_NAMING_CASES)
def test_validate_python_sdk_naming_collisions(schemas, clashing_classes, capsys):
    collisions = _python_sdk_collisions(schemas, capsys)
    assert [re.search(r"class '(\w+)'", line).group(1) for line in collisions] == (
        clashing_classes
    )


def test_validate_python_sdk_naming_collisions_names_both_schemas(capsys):
    """Resource.spending_limits was generated as ResourceSpendingLimits, which was taken."""
    (collision,) = _python_sdk_collisions(PYTHON_SDK_NAMING_CASES[0].values[0], capsys)
    assert "inline schema 'Resource.spending_limits'" in collision
    assert "component 'ResourceSpendingLimits'" in collision


def test_validate_python_sdk_naming_collisions_empty_schema(capsys):
    assert _python_sdk_collisions({}, capsys) == []


def test_validate_python_sdk_naming_collisions_runs_after_hooks_that_add_components():
    hooks = SPECTACULAR_SETTINGS["POSTPROCESSING_HOOKS"]
    python_hook = hooks.index(
        "waldur_core.core.schema_hooks.validate_python_sdk_naming_collisions"
    )
    assert python_hook == len(hooks) - 1
    assert python_hook > hooks.index(
        "waldur_core.core.schema_hooks.extract_query_enums"
    )


class _DummySerializer(serializers.Serializer):
    name = serializers.CharField()
    flavor = serializers.CharField()
    image = serializers.CharField()


class _DummyViewSet(viewsets.ViewSet):
    serializer_class = _DummySerializer


def _make_generator():
    from drf_spectacular.plumbing import ComponentRegistry

    generator = mock.Mock(spec=SchemaGenerator)
    generator.registry = ComponentRegistry()
    return generator


class TestCreateOfferingAttributesSchema:
    """Tests for create_offering_attributes_schema to prevent missing return value regression."""

    def test_returns_schema_for_processor_with_viewset(self):
        """create_offering_attributes_schema must return a dict, not None."""

        class FakeProcessor:
            viewset = _DummyViewSet
            fields = ("name", "flavor", "image")

        generator = _make_generator()
        result = create_offering_attributes_schema(FakeProcessor, generator)

        assert result is not None, (
            "create_offering_attributes_schema returned None — "
            "likely missing 'return schema' statement"
        )
        assert isinstance(result, dict)
        assert "properties" in result
        assert "flavor" in result["properties"]
        assert "image" in result["properties"]

    def test_returns_schema_for_processor_with_create_serializer_class(self):
        class FakeProcessor:
            create_serializer_class = _DummySerializer

        generator = _make_generator()
        result = create_offering_attributes_schema(FakeProcessor, generator)

        assert result is not None
        assert isinstance(result, dict)
        assert "properties" in result

    def test_filters_fields_when_specified(self):
        class FakeProcessor:
            viewset = _DummyViewSet
            fields = ("flavor",)

        generator = _make_generator()
        result = create_offering_attributes_schema(FakeProcessor, generator)

        assert result is not None
        assert "flavor" in result["properties"]
        assert "image" not in result["properties"]

    def test_returns_none_for_processor_without_viewset(self):
        class FakeProcessor:
            pass

        generator = _make_generator()
        result = create_offering_attributes_schema(FakeProcessor, generator)
        assert result is None


@pytest.mark.django_db
def test_polymorphic_schema_includes_offering_specific_attributes():
    """
    Integration test: add_polymorphic_attributes_schema must produce
    at least one offering-specific schema in the oneOf array, not just
    the generic fallback. Catches regression where
    create_offering_attributes_schema silently returns None.
    """
    generator = _make_generator()

    result = {
        "components": {
            "schemas": {
                "OrderCreateRequest": {
                    "type": "object",
                    "properties": {
                        "attributes": {"type": "object"},
                    },
                },
            }
        }
    }

    add_polymorphic_attributes_schema(result, generator)

    schemas = result["components"]["schemas"]
    attributes = schemas["OrderCreateRequest"]["properties"]["attributes"]
    one_of = attributes["oneOf"]

    # Must have more than just GenericOrderAttributes
    non_generic = [
        ref
        for ref in one_of
        if ref.get("$ref", "").split("/")[-1] != "GenericOrderAttributes"
    ]
    assert len(non_generic) > 0, (
        "No offering-specific attribute schemas in oneOf — "
        "create_offering_attributes_schema likely returns None"
    )

    # Verify at least one offering-type schema was actually added to components
    offering_schema_names = [
        name
        for name in schemas
        if name.endswith("CreateOrderAttributes") and name != "GenericOrderAttributes"
    ]
    assert len(offering_schema_names) > 0, (
        "No offering-type schemas in components/schemas"
    )


class TestPreprocessRequestBodies(unittest.TestCase):
    def spec(self, documents):
        return {
            "paths": {
                "/api/notes/": {
                    "post": {
                        "requestBody": {
                            "content": {
                                "multipart/form-data": {
                                    "schema": {"$ref": "#/components/schemas/Note"}
                                }
                            }
                        }
                    }
                }
            },
            "components": {
                "schemas": {
                    "Note": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "documents": documents,
                        },
                    }
                }
            },
        }

    def content(self, documents):
        result = preprocess_request_bodies(self.spec(documents))
        return result["paths"]["/api/notes/"]["post"]["requestBody"]["content"]

    def test_a_list_of_files_keeps_the_multipart_body(self):
        documents = {"type": "array", "items": {"type": "string", "format": "binary"}}
        self.assertIn("multipart/form-data", self.content(documents))

    def test_multipart_without_files_is_pruned(self):
        documents = {"type": "array", "items": {"type": "string"}}
        self.assertNotIn("multipart/form-data", self.content(documents))
