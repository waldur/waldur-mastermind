import unittest
from unittest import mock

from waldur_core.core.openapi_hooks import (
    add_result_count_header,
    make_fields_optional,
    postprocess_drop_inherited_descriptions,
    remove_waldur_cookie_auth,
    transform_paginated_arrays,
)


def test_drop_inherited_docstrings():
    class Parent:
        """Parent docstring."""

        pass

    class Child(Parent):
        pass

    class ChildWithDoc(Parent):
        """Child docstring."""

        pass

    result = {
        "components": {
            "schemas": {
                "Parent": {"description": "Parent docstring."},
                "Child": {"description": "Parent docstring."},
                "ChildWithDoc": {"description": "Child docstring."},
            }
        }
    }

    mock_generator = mock.Mock()
    mock_registry = mock.Mock()
    mock_generator.registry = mock_registry

    mock_registry._components = {
        ("Parent", "schemas"): mock.Mock(object=Parent()),
        ("Child", "schemas"): mock.Mock(object=Child()),
        ("ChildWithDoc", "schemas"): mock.Mock(object=ChildWithDoc()),
    }

    postprocess_drop_inherited_descriptions(result, mock_generator)

    assert (
        result["components"]["schemas"]["Parent"]["description"] == "Parent docstring."
    )
    assert "description" not in result["components"]["schemas"]["Child"]
    assert (
        result["components"]["schemas"]["ChildWithDoc"]["description"]
        == "Child docstring."
    )


def test_drop_blacklisted_docstrings():
    result = {
        "components": {
            "schemas": {
                "TestComponent": {
                    "description": "This mixin allows to specify list of fields to be rendered by serializer. It expects that request is available in serializer's context."
                },
                "OtherComponent": {"description": "Valid description."},
            }
        }
    }
    mock_generator = mock.Mock()
    mock_generator.registry._components = {}

    postprocess_drop_inherited_descriptions(result, mock_generator)

    assert "description" not in result["components"]["schemas"]["TestComponent"]
    assert (
        result["components"]["schemas"]["OtherComponent"]["description"]
        == "Valid description."
    )


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
