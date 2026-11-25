import copy

import jsonschema
from django.test import SimpleTestCase, TestCase

from waldur_core.logging.enums import ObservableObjectType
from waldur_mastermind.marketplace_site_agent.contracts import (
    ASYNCAPI_PATH,
    CONTRACTS,
    DOCS_PATH,
    NOT_SITE_AGENT_OBJECT_TYPES,
    MessageField,
    _field_to_json_schema,
    asyncapi_to_schema,
    check_compatibility,
    compare_schemas,
    contracts_to_asyncapi,
    contracts_to_markdown,
    contracts_to_schema,
    dump_asyncapi,
    load_asyncapi,
)

UPDATE_COMMAND = (
    "uv run python -m waldur_mastermind.marketplace_site_agent.contracts --update"
)


class ContractSnapshotTest(SimpleTestCase):
    def test_snapshot_exists(self):
        self.assertTrue(
            ASYNCAPI_PATH.exists(),
            f"AsyncAPI document not found at {ASYNCAPI_PATH}. Run: {UPDATE_COMMAND}",
        )

    def test_no_breaking_changes(self):
        old = asyncapi_to_schema(load_asyncapi())
        new = contracts_to_schema(CONTRACTS)
        changes = compare_schemas(old, new)
        breaking = [c for c in changes if c["breaking"]]
        self.assertEqual(
            breaking,
            [],
            "Breaking STOMP contract changes detected:\n"
            + "\n".join(f"  - {c['description']}" for c in breaking)
            + f"\n\nIf they are intended, regenerate the document: {UPDATE_COMMAND}",
        )

    def test_all_contracts_in_snapshot(self):
        snapshot_names = set(load_asyncapi()["components"]["messages"])
        contract_names = {c.name for c in CONTRACTS}
        missing = contract_names - snapshot_names
        self.assertEqual(
            missing,
            set(),
            f"New message types not in the AsyncAPI document: {missing}. "
            f"Run: {UPDATE_COMMAND}",
        )

    def test_document_is_current(self):
        self.assertEqual(
            ASYNCAPI_PATH.read_text(),
            dump_asyncapi(contracts_to_asyncapi(CONTRACTS)),
            f"contracts/asyncapi.yaml is out of date. Run: {UPDATE_COMMAND}",
        )

    def test_reference_page_is_current(self):
        self.assertEqual(
            DOCS_PATH.read_text(),
            contracts_to_markdown(CONTRACTS),
            f"docs/site-agent-events.md is out of date. Run: {UPDATE_COMMAND}",
        )

    def test_reference_page_lists_every_message(self):
        page = contracts_to_markdown(CONTRACTS)
        for contract in CONTRACTS:
            self.assertIn(f"#### {contract.name}", page)

    def test_document_round_trips_to_the_contract_schema(self):
        """What the breaking-change check reads back equals what it was written from."""
        read_back = asyncapi_to_schema(contracts_to_asyncapi(CONTRACTS))
        written = contracts_to_schema(CONTRACTS)
        self.assertEqual(
            read_back["messages"],
            {
                name: {
                    "required": message["required"],
                    "properties": message["properties"],
                    "object_types": message["object_types"],
                }
                for name, message in written["messages"].items()
            },
        )
        self.assertEqual(
            read_back["envelope"],
            {
                "required": written["envelope"]["required"],
                "properties": written["envelope"]["properties"],
            },
        )


class ObservableObjectTypeCoverageTest(SimpleTestCase):
    """Every event type is either declared for the site agent or excluded on purpose."""

    all_types = {member.value for member in ObservableObjectType}

    def contracted_types(self):
        return {t for contract in CONTRACTS for t in contract.object_types}

    def test_every_object_type_is_contracted_or_excluded(self):
        uncovered = (
            self.all_types - self.contracted_types() - set(NOT_SITE_AGENT_OBJECT_TYPES)
        )
        self.assertEqual(
            uncovered,
            set(),
            f"Observable object types without a STOMP contract: {sorted(uncovered)}. "
            "Declare the message the site agent receives for each in contracts.py "
            "(MessageContract with object_types), or add the type to "
            "NOT_SITE_AGENT_OBJECT_TYPES with the reason it is not for the agent.",
        )

    def test_no_object_type_is_both_contracted_and_excluded(self):
        overlap = self.contracted_types() & set(NOT_SITE_AGENT_OBJECT_TYPES)
        self.assertEqual(overlap, set())

    def test_every_contract_names_its_object_types(self):
        unnamed = [contract.name for contract in CONTRACTS if not contract.object_types]
        self.assertEqual(unnamed, [])

    def test_contracts_and_exclusions_name_existing_object_types(self):
        unknown = (self.contracted_types() | set(NOT_SITE_AGENT_OBJECT_TYPES)) - (
            self.all_types
        )
        self.assertEqual(unknown, set())


class AgentCompatibilityTest(TestCase):
    def setUp(self):
        from waldur_mastermind.marketplace.tests import (
            factories as marketplace_factories,
        )
        from waldur_mastermind.marketplace_site_agent.models import AgentIdentity

        self.offering = marketplace_factories.OfferingFactory()
        self.old_agent = AgentIdentity.objects.create(
            name="Old SLURM Agent",
            offering=self.offering,
            version="0.9.0",
        )
        self.current_agent = AgentIdentity.objects.create(
            name="Current SLURM Agent",
            offering=self.offering,
            version="1.0.4",
        )

    def test_detects_incompatible_agent(self):
        from waldur_mastermind.marketplace_site_agent.contracts import (
            check_agent_compatibility,
        )

        warnings = check_agent_compatibility()
        by_status = {w["agent"]: w["status"] for w in warnings}
        self.assertEqual(by_status["Old SLURM Agent"], "incompatible")
        self.assertEqual(by_status["Current SLURM Agent"], "compatible")

    def test_incompatible_agent_has_minimum_version(self):
        from waldur_mastermind.marketplace_site_agent.contracts import (
            MINIMUM_SITE_AGENT_VERSION,
            check_agent_compatibility,
        )

        warnings = check_agent_compatibility()
        old_warning = next(w for w in warnings if w["agent"] == "Old SLURM Agent")
        self.assertEqual(old_warning["minimum_required"], MINIMUM_SITE_AGENT_VERSION)


class ContractComparisonTest(TestCase):
    def test_detects_field_removal(self):
        old = {
            "messages": {
                "Msg": {
                    "required": ["a", "b"],
                    "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
                }
            }
        }
        new = {
            "messages": {
                "Msg": {"required": ["a"], "properties": {"a": {"type": "string"}}}
            }
        }
        changes = compare_schemas(old, new)
        breaking = [c for c in changes if c["breaking"]]
        self.assertEqual(len(breaking), 1)
        self.assertEqual(breaking[0]["type"], "field_removed")

    def test_required_field_addition_is_not_breaking(self):
        # Mastermind sends the message: a new field is data an agent ignores.
        old = {
            "messages": {
                "Msg": {"required": ["a"], "properties": {"a": {"type": "string"}}}
            }
        }
        new = {
            "messages": {
                "Msg": {
                    "required": ["a", "b"],
                    "properties": {"a": {"type": "string"}, "b": {"type": "integer"}},
                }
            }
        }
        changes = compare_schemas(old, new)
        self.assertEqual(
            [(c["type"], c["breaking"]) for c in changes], [("field_added", False)]
        )

    def test_field_becoming_required_is_not_breaking(self):
        old = self.message([], a={"type": "string"})
        new = self.message(["a"], a={"type": "string"})
        changes = compare_schemas(old, new)
        self.assertEqual(
            [(c["type"], c["breaking"]) for c in changes],
            [("field_became_required", False)],
        )

    def test_detects_type_change(self):
        old = {
            "messages": {
                "Msg": {"required": ["a"], "properties": {"a": {"type": "string"}}}
            }
        }
        new = {
            "messages": {
                "Msg": {"required": ["a"], "properties": {"a": {"type": "integer"}}}
            }
        }
        changes = compare_schemas(old, new)
        breaking = [c for c in changes if c["breaking"]]
        self.assertEqual(len(breaking), 1)
        self.assertEqual(breaking[0]["type"], "field_type_changed")

    def test_optional_field_not_breaking(self):
        old = {
            "messages": {
                "Msg": {"required": ["a"], "properties": {"a": {"type": "string"}}}
            }
        }
        new = {
            "messages": {
                "Msg": {
                    "required": ["a"],
                    "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
                }
            }
        }
        changes = compare_schemas(old, new)
        breaking = [c for c in changes if c["breaking"]]
        self.assertEqual(len(breaking), 0)

    def test_detects_message_removal(self):
        old = {"messages": {"Msg": {"required": [], "properties": {}}}}
        new = {"messages": {}}
        changes = compare_schemas(old, new)
        breaking = [c for c in changes if c["breaking"]]
        self.assertEqual(len(breaking), 1)
        self.assertEqual(breaking[0]["type"], "message_removed")

    def test_detects_object_type_removal(self):
        old = {
            "messages": {
                "Msg": {
                    "required": [],
                    "properties": {},
                    "object_types": ["order", "resource"],
                }
            }
        }
        new = {
            "messages": {
                "Msg": {"required": [], "properties": {}, "object_types": ["order"]}
            }
        }
        changes = compare_schemas(old, new)
        breaking = [c for c in changes if c["breaking"]]
        self.assertEqual(len(breaking), 1)
        self.assertEqual(breaking[0]["type"], "object_type_removed")
        self.assertEqual(breaking[0]["object_type"], "resource")

    def test_detects_message_moved_to_another_object_type(self):
        old = {
            "messages": {
                "Msg": {"required": [], "properties": {}, "object_types": ["order"]}
            }
        }
        new = {
            "messages": {
                "Msg": {"required": [], "properties": {}, "object_types": ["resource"]}
            }
        }
        changes = compare_schemas(old, new)
        self.assertEqual(
            sorted((c["type"], c["breaking"]) for c in changes),
            [("object_type_added", False), ("object_type_removed", True)],
        )

    def test_object_type_addition_not_breaking(self):
        old = {
            "messages": {
                "Msg": {"required": [], "properties": {}, "object_types": ["order"]}
            }
        }
        new = {
            "messages": {
                "Msg": {
                    "required": [],
                    "properties": {},
                    "object_types": ["order", "resource"],
                }
            }
        }
        changes = compare_schemas(old, new)
        self.assertEqual([c["breaking"] for c in changes], [False])

    def test_detects_envelope_field_removal(self):
        old = {
            "envelope": {
                "required": ["emitted_at"],
                "properties": {"emitted_at": {"type": "string"}},
            },
            "messages": {},
        }
        new = {"envelope": {"required": [], "properties": {}}, "messages": {}}
        changes = compare_schemas(old, new)
        breaking = [c for c in changes if c["breaking"]]
        self.assertEqual(len(breaking), 1)
        self.assertEqual(breaking[0]["type"], "field_removed")
        self.assertEqual(breaking[0]["message"], "envelope")

    def message(self, required, **properties):
        return {"messages": {"Msg": {"required": required, "properties": properties}}}

    def breaking_types(self, old, new):
        return [c["type"] for c in compare_schemas(old, new) if c["breaking"]]

    def test_field_becoming_optional_is_breaking(self):
        old = self.message(["a"], a={"type": "string"})
        new = self.message([], a={"type": "string"})
        self.assertEqual(self.breaking_types(old, new), ["field_became_optional"])

    def test_field_becoming_nullable_is_breaking(self):
        old = self.message(["a"], a={"type": "string"})
        new = self.message(["a"], a={"type": "string", "nullable": True})
        self.assertEqual(self.breaking_types(old, new), ["field_became_nullable"])

    def test_added_enum_value_is_breaking(self):
        old = self.message(["a"], a={"type": "string", "enum": ["x"]})
        new = self.message(["a"], a={"type": "string", "enum": ["x", "y"]})
        self.assertEqual(self.breaking_types(old, new), ["enum_value_added"])

    def test_removed_enum_value_is_not_breaking(self):
        old = self.message(["a"], a={"type": "string", "enum": ["x", "y"]})
        new = self.message(["a"], a={"type": "string", "enum": ["x"]})
        changes = compare_schemas(old, new)
        self.assertEqual(
            [(c["type"], c["breaking"]) for c in changes],
            [("enum_value_removed", False)],
        )

    def test_declaring_values_is_not_breaking(self):
        old = self.message(["a"], a={"type": "string"})
        new = self.message(["a"], a={"type": "string", "enum": ["x"]})
        changes = compare_schemas(old, new)
        self.assertEqual(
            [(c["type"], c["breaking"]) for c in changes], [("enum_added", False)]
        )

    def test_nullable_enum_field_admits_null(self):
        schema = _field_to_json_schema(
            MessageField("state", "string", nullable=True, enum=["a", "b"])
        )
        jsonschema.validate(None, schema)
        jsonschema.validate("a", schema)
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate("c", schema)

    def test_dropping_declared_values_is_breaking(self):
        old = self.message(["a"], a={"type": "string", "enum": ["x"]})
        new = self.message(["a"], a={"type": "string"})
        self.assertEqual(self.breaking_types(old, new), ["enum_removed"])

    def test_array_items_type_change_is_breaking(self):
        old = self.message(["a"], a={"type": "array", "items": {"type": "string"}})
        new = self.message(["a"], a={"type": "array", "items": {"type": "integer"}})
        self.assertEqual(self.breaking_types(old, new), ["array_items_type_changed"])

    def test_array_items_removed_is_breaking(self):
        old = self.message(["a"], a={"type": "array", "items": {"type": "string"}})
        new = self.message(["a"], a={"type": "array"})
        self.assertEqual(self.breaking_types(old, new), ["array_items_untyped"])

    def test_array_items_added_is_not_breaking(self):
        old = self.message(["a"], a={"type": "array"})
        new = self.message(["a"], a={"type": "array", "items": {"type": "string"}})
        changes = compare_schemas(old, new)
        self.assertEqual(
            [(c["type"], c["breaking"]) for c in changes],
            [("array_items_typed", False)],
        )


class ContractCompatibilityTest(SimpleTestCase):
    """The check CI runs against the merge request's target branch."""

    def setUp(self):
        self.base = contracts_to_asyncapi(CONTRACTS)

    def head(self, version=None):
        head = copy.deepcopy(self.base)
        body = head["components"]["messages"]["ApiKeyCommandMessage"]["payload"][
            "allOf"
        ][1]
        body["properties"].pop("client_id")
        body["required"].remove("client_id")
        if version:
            head["info"]["version"] = version
        return head

    def test_unchanged_document_is_compatible(self):
        result = check_compatibility(self.base, copy.deepcopy(self.base))
        self.assertTrue(result["compatible"])
        self.assertEqual(result["changes"], [])

    def test_breaking_change_without_major_bump_is_rejected(self):
        result = check_compatibility(self.base, self.head(version="1.1.0"))
        self.assertFalse(result["compatible"])
        self.assertIn("CONTRACTS_VERSION", result["summary"])

    def test_breaking_change_with_major_bump_is_accepted(self):
        result = check_compatibility(self.base, self.head(version="2.0.0"))
        self.assertTrue(result["compatible"])

    def test_non_breaking_change_needs_no_bump(self):
        head = copy.deepcopy(self.base)
        body = head["components"]["messages"]["ApiKeyCommandMessage"]["payload"][
            "allOf"
        ][1]
        body["properties"]["note"] = {"type": "string", "description": ""}
        result = check_compatibility(self.base, head)
        self.assertTrue(result["compatible"])
        self.assertEqual(len(result["changes"]), 1)

    def test_message_moved_to_another_object_type_is_rejected(self):
        head = copy.deepcopy(self.base)
        message = head["components"]["messages"]["ApiKeyCommandMessage"]
        message["payload"]["allOf"][1]["properties"]["object_type"]["enum"] = [
            "resource"
        ]
        message["x-waldur-object-types"] = ["resource"]
        result = check_compatibility(self.base, head)
        self.assertFalse(result["compatible"])
        self.assertIn(
            ("object_type_removed", "resource_api_key_rotation"),
            [(c["type"], c.get("object_type")) for c in result["changes"]],
        )

    def test_envelope_field_removal_is_rejected(self):
        head = copy.deepcopy(self.base)
        envelope = head["components"]["schemas"]["Envelope"]
        envelope["properties"].pop("emitted_at")
        envelope["required"].remove("emitted_at")
        result = check_compatibility(self.base, head)
        self.assertFalse(result["compatible"])
        self.assertEqual(
            [(c["type"], c["message"]) for c in result["changes"]],
            [("field_removed", "envelope")],
        )
