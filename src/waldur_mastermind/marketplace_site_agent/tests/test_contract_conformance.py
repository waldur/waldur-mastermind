"""STOMP contracts describe what the producers actually send.

Two guards:

* ``ProducerCoverageTest`` scans the source for every function that publishes a
  site-agent message and requires it to be listed as a producer of a contract,
  so a new producer cannot ship without one, and a renamed one leaves no stale
  entry behind.
* ``ProducerConformanceTest`` calls each producer and validates the delivered
  payload with ``jsonschema`` against its message in contracts/asyncapi.yaml
  (required fields, types, nullability, enums), and rejects keys that neither
  the contract nor the envelope declares.
"""

import ast
import contextlib
import datetime
import json
from pathlib import Path
from unittest import mock

from django.contrib.contenttypes.models import ContentType
from django.test import SimpleTestCase
from django.utils import timezone
from jsonschema import Draft7Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT7
from rest_framework import test

import waldur_mastermind
from waldur_core.core import utils as core_utils
from waldur_core.logging.enums import ObservableObjectType
from waldur_core.logging.tests import factories as logging_factories
from waldur_core.permissions import models as permission_models
from waldur_core.structure import models as structure_models
from waldur_mastermind.marketplace import handlers as marketplace_handlers
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace import tasks as marketplace_tasks
from waldur_mastermind.marketplace import utils as marketplace_utils
from waldur_mastermind.marketplace.enums import (
    SITE_AGENT_OFFERING,
    OfferingStates,
    ResourceApiKeyActions,
    SpendingLimitPeriods,
)
from waldur_mastermind.marketplace.models import OrderStates, ResourceStates
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures
from waldur_mastermind.marketplace.tests.test_provider_project_groups import (
    ProjectGroupsTestCase,
)
from waldur_mastermind.marketplace_site_agent import handlers as site_agent_handlers
from waldur_mastermind.marketplace_site_agent import tasks as site_agent_tasks
from waldur_mastermind.marketplace_site_agent import utils as site_agent_utils
from waldur_mastermind.marketplace_site_agent.contracts import (
    CONTRACTS,
    ENVELOPE_FIELDS,
    NOT_SITE_AGENT_OBJECT_TYPES,
    load_asyncapi,
)
from waldur_mastermind.policy.tests import factories as policy_factories

RMQ = "aabb000000000000000000000000ccdd"
SOURCE_ROOT = Path(waldur_mastermind.__file__).parent
CONTRACTS_BY_NAME = {contract.name: contract for contract in CONTRACTS}

# --------------------------------------------------------------------------
# Producer coverage
# --------------------------------------------------------------------------

PROJECT_GROUP_TYPE = ObservableObjectType.SERVICE_PROVIDER_PROJECT_GROUP.value
PROJECT_GROUP_PUBLISHERS = {"publish", "publish_change"}
# project_groups.py functions that build or send project group messages for
# their callers: the callers are the producers, these are the channel.
PROJECT_GROUP_CHANNEL = PROJECT_GROUP_PUBLISHERS | {"build_messages"}
# event_dispatch.build_messages(scope_keys, payload_builder, object_type, ...)
DISPATCH_TYPE_ARG = (2, "object_type")


def _call_name(call):
    func = call.func
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)


def _type_node(call, position, keyword):
    """What a call passes for the object type parameter at position or keyword."""
    node = call.args[position] if len(call.args) > position else None
    for item in call.keywords:
        if item.arg == keyword:
            node = item.value
    return node


def _literal_type(node):
    """The ObservableObjectType value a node names, if it is a literal member."""
    if (
        isinstance(node, ast.Attribute)
        and node.attr in ObservableObjectType.__members__
    ):
        return ObservableObjectType[node.attr].value
    return None


def _dispatch_bindings(tree):
    """(module_aliases, direct_aliases) for waldur_core.logging.event_dispatch."""
    module_aliases = {"event_dispatch"}
    direct_aliases = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module == "waldur_core.logging":
                for alias in node.names:
                    if alias.name == "event_dispatch":
                        module_aliases.add(alias.asname or alias.name)
            elif node.module == "waldur_core.logging.event_dispatch":
                for alias in node.names:
                    if alias.name == "build_messages":
                        direct_aliases.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "waldur_core.logging.event_dispatch" and alias.asname:
                    module_aliases.add(alias.asname)
    return module_aliases, direct_aliases


def _is_dispatch(call, module_aliases, direct_aliases):
    """A direct call of event_dispatch.build_messages (including aliased imports)."""
    func = call.func
    if (
        isinstance(func, ast.Attribute)
        and func.attr == "build_messages"
        and isinstance(func.value, ast.Name)
        and func.value.id in module_aliases
    ):
        return True
    return isinstance(func, ast.Name) and func.id in direct_aliases


def _source_modules():
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        relative = path.relative_to(SOURCE_ROOT).as_posix()
        if "/tests/" in f"/{relative}" or "/migrations/" in f"/{relative}":
            continue
        yield relative, ast.parse(path.read_text())


def _functions(tree):
    """(qualname, node) for every function and method in a module."""

    def visit(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                yield from visit(child, f"{prefix}{child.name}.")
            elif isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                yield f"{prefix}{child.name}", child

    return visit(tree, "")


def _dispatch_helpers(modules):
    """{(module, name): (position, keyword)} for every function that passes its
    own parameter to event_dispatch.build_messages as the object type, such as
    prepare_messages. Its callers choose the type, so they are the producers."""
    helpers = {}
    for relative, tree in modules:
        module_aliases, direct_aliases = _dispatch_bindings(tree)
        for qualname, function in _functions(tree):
            # A method forwarding its parameter stays a producer of unknown
            # type, which must be declared, rather than hiding its callers.
            if "." in qualname:
                continue
            params = [arg.arg for arg in function.args.args]
            for call in ast.walk(function):
                if not isinstance(call, ast.Call) or not _is_dispatch(
                    call, module_aliases, direct_aliases
                ):
                    continue
                node = _type_node(call, *DISPATCH_TYPE_ARG)
                if isinstance(node, ast.Name) and node.id in params:
                    helpers[(relative, qualname)] = (
                        params.index(node.id),
                        node.id,
                    )
    return helpers


def _published_type(call, relative, helpers, module_aliases, direct_aliases):
    """(is a publish call, object type) for one call node."""
    name = _call_name(call)
    if _is_dispatch(call, module_aliases, direct_aliases):
        return True, _literal_type(_type_node(call, *DISPATCH_TYPE_ARG))
    for (module, helper), (position, keyword) in helpers.items():
        # A private helper is only reachable from its own module.
        if name == helper and (not helper.startswith("_") or module == relative):
            return True, _literal_type(_type_node(call, position, keyword))
    if name in PROJECT_GROUP_PUBLISHERS:
        receiver = call.func.value if isinstance(call.func, ast.Attribute) else None
        in_project_groups_module = relative == "marketplace/project_groups.py"
        if (isinstance(receiver, ast.Name) and receiver.id == "project_groups") or (
            receiver is None and in_project_groups_module
        ):
            return True, PROJECT_GROUP_TYPE
    return False, None


def find_producers():
    """{"path:qualname": {object types}} for every function that publishes.

    A function publishes when it calls event_dispatch.build_messages with a
    literal object type, a dispatch helper (see _dispatch_helpers), or a
    project group publisher.
    """
    modules = list(_source_modules())
    helpers = _dispatch_helpers(modules)
    producers = {}
    for relative, tree in modules:
        module_aliases, direct_aliases = _dispatch_bindings(tree)
        for qualname, function in _functions(tree):
            # A helper's own body is the channel; only top-level functions are
            # helpers, so a same-named method elsewhere in the module is scanned.
            if (relative, qualname) in helpers:
                continue
            if (
                relative == "marketplace/project_groups.py"
                and function.name in PROJECT_GROUP_CHANNEL
            ):
                continue
            for call in ast.walk(function):
                if not isinstance(call, ast.Call):
                    continue
                publishes, object_type = _published_type(
                    call, relative, helpers, module_aliases, direct_aliases
                )
                if publishes:
                    producers.setdefault(f"{relative}:{qualname}", set()).add(
                        object_type
                    )
    return producers


class ProducerCoverageTest(SimpleTestCase):
    def test_every_site_agent_producer_is_declared(self):
        declared = {p for contract in CONTRACTS for p in contract.producers}
        undeclared = {
            producer: types
            for producer, types in find_producers().items()
            # A producer whose only types are not for the agent needs no
            # contract; one whose type cannot be read statically does.
            if not types <= set(NOT_SITE_AGENT_OBJECT_TYPES)
            and producer not in declared
        }
        self.assertEqual(
            undeclared,
            {},
            "These functions publish site-agent messages but no contract lists them "
            "in producers. Add each to the contract describing its payload, or "
            "declare a new MessageContract for it.",
        )

    def test_every_declared_producer_exists(self):
        found = set(find_producers())
        stale = sorted(
            p for contract in CONTRACTS for p in contract.producers if p not in found
        )
        self.assertEqual(
            stale,
            [],
            "Contracts name producers that no longer publish (renamed or removed).",
        )

    def test_every_contract_names_a_producer(self):
        self.assertEqual([c.name for c in CONTRACTS if not c.producers], [])


# --------------------------------------------------------------------------
# Payload conformance
# --------------------------------------------------------------------------

# The published document, not one generated on the fly: test_contracts.py keeps
# the two equal, and this way the test checks what consumers actually read.
DOCUMENT_URI = "urn:waldur:site-agent-events"
DOCUMENT = load_asyncapi()
REGISTRY = Registry().with_resource(
    DOCUMENT_URI, Resource(contents=DOCUMENT, specification=DRAFT7)
)


def payload_validator(contract):
    """A Draft 7 validator for the contract's message payload in the document.

    The AsyncAPI Schema is a superset of JSON Schema Draft 7, and the generated
    payloads use only Draft 7 keywords, so they validate as they are.
    """
    return Draft7Validator(
        {"$ref": f"{DOCUMENT_URI}#/components/messages/{contract.name}/payload"},
        registry=REGISTRY,
    )


def conformance_errors(contract, payload):
    """Why ``payload`` does not match its contract, or an empty list.

    The document's schemas stay open to new properties, because consumers must
    tolerate optional additions. The producer side is held stricter: a key that
    neither the contract nor the envelope declares is an error here.
    """
    errors = [
        f"{'/'.join(map(str, error.absolute_path)) or 'payload'}: {error.message}"
        for error in payload_validator(contract).iter_errors(payload)
    ]
    declared = {f.name for f in [*contract.fields, *ENVELOPE_FIELDS]}
    errors += [f"undeclared field '{key}'" for key in sorted(set(payload) - declared)]
    return sorted(errors)


class ConformanceErrorsTest(SimpleTestCase):
    """The checker itself rejects each kind of drift."""

    contract = CONTRACTS_BY_NAME["AccountMessage"]

    def valid_payload(self, **changes):
        payload = {
            "account_uuid": "a" * 32,
            "account_username": "svc",
            "scope_type": "project",
            "project_uuid": "b" * 32,
            "project_name": "Project",
            "action": "create",
            "offering_uuid": "c" * 32,
            "object_type": "service_account",
            "schema_version": 1,
            "emitted_at": "2026-11-19T12:00:00+00:00",
        }
        payload.update(changes)
        return {k: v for k, v in payload.items() if v is not ...}

    def test_valid_payload_passes(self):
        self.assertEqual(conformance_errors(self.contract, self.valid_payload()), [])

    def test_value_outside_enum_fails(self):
        errors = conformance_errors(self.contract, self.valid_payload(action="rename"))
        self.assertTrue(any(e.startswith("action:") for e in errors), errors)

    def test_object_type_of_another_contract_fails(self):
        errors = conformance_errors(
            self.contract, self.valid_payload(object_type="order")
        )
        self.assertTrue(any(e.startswith("object_type:") for e in errors), errors)

    def test_wrong_type_fails(self):
        errors = conformance_errors(self.contract, self.valid_payload(project_name=7))
        self.assertTrue(any(e.startswith("project_name:") for e in errors), errors)

    def test_missing_required_field_fails(self):
        errors = conformance_errors(self.contract, self.valid_payload(account_uuid=...))
        self.assertTrue(any("account_uuid" in e for e in errors), errors)

    def test_missing_envelope_field_fails(self):
        errors = conformance_errors(self.contract, self.valid_payload(emitted_at=...))
        self.assertTrue(any("emitted_at" in e for e in errors), errors)

    def test_undeclared_field_fails(self):
        errors = conformance_errors(self.contract, self.valid_payload(extra="x"))
        self.assertIn("undeclared field 'extra'", errors)

    def test_null_in_a_non_nullable_field_fails(self):
        errors = conformance_errors(
            self.contract, self.valid_payload(project_uuid=None)
        )
        self.assertTrue(any(e.startswith("project_uuid:") for e in errors), errors)

    def test_api_key_actions_match_the_enum_in_code(self):
        [action] = [
            f
            for f in CONTRACTS_BY_NAME["ApiKeyCommandMessage"].fields
            if f.name == "action"
        ]
        self.assertEqual(sorted(action.enum), sorted(ResourceApiKeyActions.VALUES))


class ConformanceMixin:
    """Capture what a consumer queue receives and check it against a contract."""

    def make_consumer(self, *scopes, user):
        return logging_factories.EventConsumerFactory.with_scopes(
            *scopes, user=user, queue_created=True, rmq_username=RMQ
        )

    @contextlib.contextmanager
    def capture(self):
        received = []
        with mock.patch("waldur_core.logging.tasks.publish_messages.delay") as publish:
            with self.captureOnCommitCallbacks(execute=True):
                yield received
        topic = f"consumer_{self.consumer.uuid.hex}"
        for call in publish.call_args_list:
            for message in call.args[0]:
                if message.get("topic") == topic:
                    received.append(json.loads(message["payload"]))

    def assert_conforms(self, contract_name, received, **match):
        """Every received payload of the contract's types (and ``match``) conforms."""
        contract = CONTRACTS_BY_NAME[contract_name]
        payloads = [
            payload
            for payload in received
            if payload.get("object_type") in contract.object_types
            and all(payload.get(k) == v for k, v in match.items())
        ]
        self.assertTrue(payloads, f"No {contract_name} payload was delivered")
        for payload in payloads:
            self.assertEqual(
                conformance_errors(contract, payload),
                [],
                f"{contract_name} payload does not match its contract: {payload}",
            )


class ProducerConformanceTest(ConformanceMixin, test.APITestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.offering.type = SITE_AGENT_OFFERING
        self.offering.state = OfferingStates.ACTIVE
        self.offering.save()
        self.resource = self.fixture.resource
        self.resource.backend_id = "acc-1"
        self.resource.state = ResourceStates.OK
        self.resource.save()
        self.project = self.fixture.project
        self.consumer = self.make_consumer(self.offering, user=self.fixture.staff)

    # ---- orders ----

    def test_order_state_change(self):
        order = self.fixture.order
        order.state = OrderStates.EXECUTING
        with self.capture() as received:
            marketplace_handlers.send_order_state_change_to_message_queue(
                marketplace_models.Order, order
            )
        self.assert_conforms("OrderMessage", received)

    def test_pending_order_reminder(self):
        order = self.fixture.order
        marketplace_models.Order.objects.filter(pk=order.pk).update(
            state=OrderStates.PENDING_PROVIDER,
            created=timezone.now() - datetime.timedelta(hours=2),
        )
        with self.capture() as received:
            site_agent_tasks.send_messages_about_pending_orders()
        self.assert_conforms("PendingOrderMessage", received)

    def test_order_info_notifications(self):
        order = self.fixture.order
        with self.capture() as received:
            with mock.patch.object(core_utils, "broadcast_mail"):
                marketplace_tasks.notify_consumer_about_provider_info(order.uuid.hex)
                marketplace_tasks.notify_provider_about_consumer_info(order.uuid.hex)
        self.assert_conforms("OrderInfoMessage", received, action="provider_info_set")
        self.assert_conforms("OrderInfoMessage", received, action="consumer_info_set")

    # ---- resources ----

    def test_resource_flag_update(self):
        with self.capture() as received:
            site_agent_utils.push_resource_update_message(self.resource, force=True)
        self.assert_conforms("ResourceUpdateMessage", received)

    def test_resource_state_change(self):
        with self.capture() as received:
            marketplace_handlers.send_resource_state_change_to_message_queue(
                marketplace_models.Resource, self.resource, created=True
            )
        self.assert_conforms("ResourceStateMessage", received)

    # ---- offering users ----

    def make_offering_user(self):
        user = self.fixture.admin
        user.first_name = "Alice"
        user.email = "alice@example.com"
        user.save()
        return marketplace_factories.OfferingUserFactory(
            offering=self.offering, user=user, username="alice"
        )

    def test_offering_user_username_set(self):
        offering_user = self.make_offering_user()
        offering_user.username = "alice2"
        with self.capture() as received:
            site_agent_handlers.send_offering_user_username_message(
                marketplace_models.OfferingUser, offering_user
            )
        self.assert_conforms("OfferingUserMessage", received, action="username_set")

    def test_offering_user_created(self):
        offering_user = self.make_offering_user()
        with self.capture() as received:
            marketplace_handlers.send_offering_user_created_message(
                marketplace_models.OfferingUser, offering_user, created=True
            )
        self.assert_conforms("OfferingUserCreatedMessage", received, action="create")

    def test_offering_user_updated(self):
        offering_user = self.make_offering_user()
        offering_user.username = "alice2"
        with self.capture() as received:
            marketplace_handlers.send_offering_user_updated_message(
                marketplace_models.OfferingUser, offering_user
            )
        self.assert_conforms("OfferingUserUpdatedMessage", received, action="update")

    def test_offering_user_deleted(self):
        offering_user = self.make_offering_user()
        with self.capture() as received:
            marketplace_handlers.send_offering_user_deleted_message(
                marketplace_models.OfferingUser, offering_user
            )
        self.assert_conforms("OfferingUserDeletedMessage", received, action="delete")

    def test_offering_user_attribute_update(self):
        offering_user = self.make_offering_user()
        marketplace_models.OfferingUserAttributeConfig.objects.create(
            offering=self.offering, expose_full_name=True, expose_email=True
        )
        user = offering_user.user
        user.first_name = "Bob"
        with self.capture() as received:
            marketplace_handlers.send_user_attribute_update_message(
                structure_models.User, user
            )
        self.assert_conforms(
            "OfferingUserAttributeUpdateMessage", received, action="attribute_update"
        )

    def test_offering_user_account_status_update(self):
        offering_user = self.make_offering_user()
        user = offering_user.user
        user.is_active = False
        with self.capture() as received:
            marketplace_handlers.send_user_account_status_message(
                structure_models.User, user
            )
        self.assert_conforms(
            "OfferingUserAccountStatusUpdateMessage",
            received,
            action="account_status_update",
        )

    # ---- user roles ----

    def test_user_role_changed(self):
        self.fixture.admin  # grants a project role
        permission = permission_models.UserRole.objects.filter(
            content_type=ContentType.objects.get_for_model(self.project),
            object_id=self.project.id,
        ).first()
        with self.capture() as received:
            site_agent_handlers.process_role_changed(permission, granted=True)
        self.assert_conforms("UserRoleMessage", received)

    def test_user_role_sync(self):
        with self.capture() as received:
            site_agent_utils.push_user_role_sync_message(self.project)
            site_agent_utils.push_resource_user_role_sync_message(self.resource)
        payloads = [p for p in received if p["object_type"] == "user_role"]
        self.assertEqual(len(payloads), 2)
        self.assert_conforms("UserRoleSyncMessage", received)

    # ---- accounts ----

    def test_service_account(self):
        account = marketplace_factories.ProjectServiceAccountFactory(
            project=self.project
        )
        with self.capture() as received:
            site_agent_handlers.send_account_message(account, created=True)
        self.assert_conforms("AccountMessage", received)

    def test_course_account(self):
        account = marketplace_factories.CourseAccountFactory(project=self.project)
        with self.capture() as received:
            site_agent_handlers.send_account_message(account, created=False)
        self.assert_conforms("AccountMessage", received, object_type="course_account")

    # ---- SLURM periodic limits ----

    def test_periodic_limits(self):
        policy = policy_factories.SlurmPeriodicUsagePolicyFactory(scope=self.offering)
        with self.capture() as received:
            policy._send_settings_to_site_agent(self.resource, {"fairshare": 100})
        self.assert_conforms("PeriodicLimitsMessage", received)

    # ---- commands to the agent ----

    def test_backend_resource_request(self):
        request = marketplace_factories.BackendResourceRequestFactory(
            offering=self.offering
        )
        with self.capture() as received:
            marketplace_utils.publish_backend_resource_request(request)
        self.assert_conforms("BackendResourceRequestMessage", received)

    def test_offering_resources_sync(self):
        with self.capture() as received:
            marketplace_utils.publish_offering_resources_sync_request(
                self.offering, self.fixture.staff
            )
        self.assert_conforms("OfferingResourcesSyncMessage", received)

    def test_api_key_commands(self):
        api_key = marketplace_models.ResourceApiKey.objects.create(
            resource=self.resource, client_id="cid-1"
        )
        with self.capture() as received:
            for action in ResourceApiKeyActions.VALUES:
                marketplace_utils.publish_api_key_event(api_key, action)
        for action in ResourceApiKeyActions.VALUES:
            self.assert_conforms("ApiKeyCommandMessage", received, action=action)

    def test_api_key_commands_on_managed_key_with_limits(self):
        # A managed key's rotation carries its settings, and limits are set.
        self.offering.plugin_options = {"enable_api_key_provisioning": True}
        self.offering.save()
        api_key = marketplace_models.ResourceApiKey.objects.create(
            resource=self.resource,
            client_id="cid-1",
            spending_limits={"tokens": {SpendingLimitPeriods.MONTH: 100}},
        )
        with self.capture() as received:
            for action in ResourceApiKeyActions.CARRY_SETTINGS:
                marketplace_utils.publish_api_key_event(api_key, action)
        for action in ResourceApiKeyActions.CARRY_SETTINGS:
            self.assert_conforms(
                "ApiKeyCommandMessage",
                received,
                action=action,
                limits={"tokens": 100},
            )


class ProjectGroupConformanceTest(ConformanceMixin, ProjectGroupsTestCase):
    def setUp(self):
        super().setUp()
        self.consumer = self.make_consumer(self.offering, user=self.staff)

    def test_group_lifecycle_and_switch(self):
        with self.capture() as received:
            self.make_resource()
            group = self.group_of()
            group.name = f"{group.name}-renamed"
            group.save()
            group.delete()
            self.provider.account_options = {"project_groups_enabled": False}
            self.provider.save()
        for action in ("create", "update", "delete", "switch"):
            self.assert_conforms("ProjectGroupMessage", received, action=action)


# --------------------------------------------------------------------------
# Scenario coverage
# --------------------------------------------------------------------------


def checked_contract_names():
    """Contract names this module checks a delivered payload against: the first
    argument of assert_conforms(...)."""
    names = set()
    for node in ast.walk(ast.parse(Path(__file__).read_text())):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "assert_conforms"
            and node.args
            and isinstance(node.args[0], ast.Constant)
        ):
            names.add(node.args[0].value)
    return names


class ScenarioCoverageTest(SimpleTestCase):
    def test_every_contract_has_a_conformance_scenario(self):
        self.assertEqual(
            sorted({c.name for c in CONTRACTS} - checked_contract_names()),
            [],
            "These contracts have no scenario that calls their producer and checks "
            "the delivered payload. Add one to this module.",
        )

    def test_every_checked_name_is_a_contract(self):
        self.assertEqual(
            sorted(checked_contract_names() - {c.name for c in CONTRACTS}), []
        )
