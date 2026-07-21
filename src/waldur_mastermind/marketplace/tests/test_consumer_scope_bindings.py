"""scope bindings / dispatch: scope-binding matching and delivery-time re-authorization.

A consumer is bound to a list of entities; an event matches if its scope-keys
(the offering chain + the event's project chain) intersect those bindings. The
owner is re-authorized at delivery, so revoking their role stops delivery.
"""

from rest_framework import test

from waldur_core.logging import enums as logging_enums
from waldur_core.logging import event_dispatch
from waldur_core.logging.tests import factories as logging_factories
from waldur_core.permissions import utils as permission_utils
from waldur_core.permissions.fixtures import ProjectRole
from waldur_mastermind.marketplace import enums
from waldur_mastermind.marketplace import utils as marketplace_utils
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures

RMQ = "aabb000000000000000000000000ccdd"


class ConsumerScopeBindingTest(test.APITestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.offering.type = enums.SITE_AGENT_OFFERING
        self.offering.save()
        self.order = self.fixture.order  # ties a project to this offering

    def _dispatch_order_event(self):
        payload = {
            "order_uuid": self.order.uuid.hex,
            "order_state": "pending provider",
        }
        return marketplace_utils.prepare_messages(
            self.offering, payload, logging_enums.ObservableObjectType.ORDER
        )

    def _consumer_topics(self, messages):
        return [m["topic"] for m in messages if m["topic"].startswith("consumer_")]

    def test_offering_bound_consumer_matches(self):
        consumer = logging_factories.EventConsumerFactory.with_scopes(
            self.offering,
            user=self.fixture.staff,
            queue_created=True,
            rmq_username=RMQ,
        )
        topics = self._consumer_topics(self._dispatch_order_event())
        self.assertIn(f"consumer_{consumer.uuid.hex}", topics)

    def test_project_bound_consumer_matches(self):
        """A consumer bound to the PROJECT (not the offering) still matches an
        order event on that project — that is the point of D2 bindings."""
        consumer = logging_factories.EventConsumerFactory.with_scopes(
            self.order.project,
            user=self.fixture.staff,
            queue_created=True,
            rmq_username=RMQ,
        )
        topics = self._consumer_topics(self._dispatch_order_event())
        self.assertIn(f"consumer_{consumer.uuid.hex}", topics)

    def test_multi_binding_consumer_gets_exactly_one_message(self):
        """Bound to BOTH the offering and the project: the event matches twice
        but must produce exactly one message (set intersection, not a join)."""
        consumer = logging_factories.EventConsumerFactory.with_scopes(
            self.offering,
            self.order.project,
            user=self.fixture.staff,
            queue_created=True,
            rmq_username=RMQ,
        )
        topics = self._consumer_topics(self._dispatch_order_event())
        self.assertEqual(topics.count(f"consumer_{consumer.uuid.hex}"), 1)

    def test_unrelated_binding_does_not_match(self):
        other_project = marketplace_fixtures.MarketplaceFixture().project
        consumer = logging_factories.EventConsumerFactory.with_scopes(
            other_project,
            user=self.fixture.staff,
            queue_created=True,
            rmq_username=RMQ,
        )
        topics = self._consumer_topics(self._dispatch_order_event())
        self.assertNotIn(f"consumer_{consumer.uuid.hex}", topics)

    # ---- delivery-time re-authorization (the security boundary) ----

    def test_owner_losing_role_stops_delivery(self):
        """Registration validated the binding, but authorization stays dynamic:
        revoking the owner's role must stop delivery immediately."""
        member = self.fixture.manager  # holds a role on the project
        consumer = logging_factories.EventConsumerFactory.with_scopes(
            self.order.project,
            user=member,
            queue_created=True,
            rmq_username=RMQ,
        )
        topic = f"consumer_{consumer.uuid.hex}"

        self.assertIn(topic, self._consumer_topics(self._dispatch_order_event()))

        # Revoke every active role the owner holds.
        from waldur_core.permissions import models as permission_models

        permission_models.UserRole.objects.filter(user=member).update(is_active=False)

        self.assertNotIn(topic, self._consumer_topics(self._dispatch_order_event()))

    def test_non_privileged_owner_without_role_never_receives(self):
        from waldur_core.structure.tests import factories as structure_factories

        outsider = structure_factories.UserFactory()
        consumer = logging_factories.EventConsumerFactory.with_scopes(
            self.order.project,
            user=outsider,
            queue_created=True,
            rmq_username=RMQ,
        )
        topics = self._consumer_topics(self._dispatch_order_event())
        self.assertNotIn(f"consumer_{consumer.uuid.hex}", topics)

    def test_project_role_grants_delivery(self):
        """A plain project member (no staff) bound to the project receives —
        the binding predicate mirrors filter_for_user, so the unified path is
        not narrower than the legacy one."""
        from waldur_core.structure.tests import factories as structure_factories

        user = structure_factories.UserFactory()
        self.order.project.add_user(user, ProjectRole.MEMBER)
        consumer = logging_factories.EventConsumerFactory.with_scopes(
            self.order.project,
            user=user,
            queue_created=True,
            rmq_username=RMQ,
        )
        topics = self._consumer_topics(self._dispatch_order_event())
        self.assertIn(f"consumer_{consumer.uuid.hex}", topics)

    def test_global_consumer_receives_marketplace_events(self):
        """A global (bindingless, staff/support) consumer means 'everything', so
        it receives marketplace events (orders, resources, …) through this path
        — an IdM/IGA sync account subscribes to the whole platform."""
        global_consumer = logging_factories.EventConsumerFactory(
            user=self.fixture.staff,
            queue_created=True,
            rmq_username=RMQ,
        )
        topics = self._consumer_topics(self._dispatch_order_event())
        self.assertIn(f"consumer_{global_consumer.uuid.hex}", topics)

    def test_global_consumer_excluded_from_marketplace_user_role(self):
        """USER_ROLE is the one object type both dispatchers emit. The core path
        owns it for globals (fired on the role signal); this path emits it only
        on a manual project re-sync, so it must NOT also deliver it to globals or
        they would get it twice."""
        global_consumer = logging_factories.EventConsumerFactory(
            user=self.fixture.staff,
            queue_created=True,
            rmq_username=RMQ,
        )
        messages = marketplace_utils.prepare_messages(
            self.offering,
            {"user_uuid": self.fixture.staff.uuid.hex},
            logging_enums.ObservableObjectType.USER_ROLE,
        )
        self.assertNotIn(
            f"consumer_{global_consumer.uuid.hex}", self._consumer_topics(messages)
        )

    def test_filtered_out_type_does_not_suppress_the_legacy_path(self):
        """A consumer that filters this event type out must not be reported as
        covered — otherwise the legacy subscription is suppressed too and the
        event is delivered NOWHERE, with no error and no log.
        """
        consumer = logging_factories.EventConsumerFactory.with_scopes(
            self.offering,
            user=self.fixture.staff,
            queue_created=True,
            rmq_username=RMQ,
            object_types=[logging_enums.ObservableObjectType.RESOURCE.value],
        )
        result = event_dispatch.build_messages(
            set(permission_utils.scope_keys_for(self.offering)),
            lambda: {"order_uuid": self.order.uuid.hex},
            logging_enums.ObservableObjectType.ORDER,
        )
        self.assertEqual(result.messages, [])
        self.assertNotIn(consumer.user_id, result.user_ids)

    # --- Coverage for the remaining object types the site agent subscribes to
    # that flow through this dispatch path (importable resources, periodic
    # limits). They are non-USER_ROLE marketplace events, so both a scoped and a
    # global consumer must receive them — pins the type-agnostic dispatch for the
    # agent-facing contract. ---

    def test_no_consumers_short_circuits_cheaply(self):
        """With nothing listening on either path, prepare_messages must return
        early after two cheap EXISTS rather than running the several-query
        scope-key resolution — it fires on essentially every marketplace signal."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as ctx:
            result = marketplace_utils.prepare_messages(
                self.offering,
                {"order_uuid": self.order.uuid.hex},
                logging_enums.ObservableObjectType.ORDER,
            )
        self.assertEqual(result, [])
        self.assertLessEqual(
            len(ctx.captured_queries),
            3,
            f"expected the short-circuit, got {len(ctx.captured_queries)} queries",
        )

    def test_importable_resources_dispatch(self):
        """IMPORTABLE_RESOURCES (offering-scoped) reaches an offering-bound
        consumer and a global consumer."""
        scoped = logging_factories.EventConsumerFactory.with_scopes(
            self.offering, user=self.fixture.staff, queue_created=True, rmq_username=RMQ
        )
        global_consumer = logging_factories.EventConsumerFactory(
            user=self.fixture.staff,
            queue_created=True,
            rmq_username="bbcc000000000000000000000000eeff",
        )
        messages = marketplace_utils.prepare_messages(
            self.offering,
            {"backend_resource_request_uuid": self.order.uuid.hex},
            logging_enums.ObservableObjectType.IMPORTABLE_RESOURCES,
        )
        topics = self._consumer_topics(messages)
        self.assertIn(f"consumer_{scoped.uuid.hex}", topics)
        self.assertIn(f"consumer_{global_consumer.uuid.hex}", topics)

    def test_resource_periodic_limits_dispatch(self):
        """RESOURCE_PERIODIC_LIMITS (resource-scoped via resource_uuid) reaches a
        consumer bound to the resource's project and a global consumer."""
        resource = self.fixture.resource
        scoped = logging_factories.EventConsumerFactory.with_scopes(
            resource.project,
            user=self.fixture.staff,
            queue_created=True,
            rmq_username=RMQ,
        )
        global_consumer = logging_factories.EventConsumerFactory(
            user=self.fixture.staff,
            queue_created=True,
            rmq_username="ccdd000000000000000000000000aa11",
        )
        messages = marketplace_utils.prepare_messages(
            self.offering,
            {"resource_uuid": resource.uuid.hex},
            logging_enums.ObservableObjectType.RESOURCE_PERIODIC_LIMITS,
        )
        topics = self._consumer_topics(messages)
        self.assertIn(f"consumer_{scoped.uuid.hex}", topics)
        self.assertIn(f"consumer_{global_consumer.uuid.hex}", topics)
