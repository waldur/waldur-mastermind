import unittest

from waldur_core.core.schema_hooks import (
    add_result_count_header,
    inject_waldur_operation_ids,
    make_fields_optional,
    remove_waldur_cookie_auth,
    transform_paginated_arrays,
    validate_waldur_operation_ids,
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


def test_validate_waldur_operation_ids():
    import pytest

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
    with pytest.raises(
        ValueError, match="is a UUID but is missing 'x-waldur-operation-id'"
    ):
        validate_waldur_operation_ids(schema, None)

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

    # 3. Test success with extension
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
                            "x-waldur-operation-id": "customers_retrieve",
                        }
                    ],
                }
            }
        }
    }
    validate_waldur_operation_ids(schema, None)  # Should not raise


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
