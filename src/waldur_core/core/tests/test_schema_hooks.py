import unittest

from waldur_core.core.schema_hooks import (
    add_result_count_header,
    make_fields_optional,
    remove_waldur_cookie_auth,
    transform_paginated_arrays,
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
