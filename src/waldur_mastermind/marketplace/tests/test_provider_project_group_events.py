"""Project group changes are announced to the provider's offerings over STOMP."""

import json
from functools import partial
from unittest import mock

from django.db import connection

from waldur_core.core.middleware import set_skip_side_effects
from waldur_core.logging.tests import factories as logging_factories
from waldur_core.permissions.fixtures import OfferingRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models, project_groups
from waldur_mastermind.marketplace.enums import BASIC_OFFERING
from waldur_mastermind.marketplace.tests import factories
from waldur_mastermind.marketplace.tests.test_provider_project_groups import (
    LIST_URL,
    ProjectGroupsTestCase,
)

RMQ = "aabb000000000000000000000000ccdd"
OBJECT_TYPE = "service_provider_project_group"


@mock.patch("waldur_core.logging.tasks.publish_messages.delay")
class ProjectGroupEventTest(ProjectGroupsTestCase):
    def setUp(self):
        super().setUp()
        # The site agent: an offering manager whose queue is bound to its
        # offering, as register_queue binds it.
        self.agent_user = structure_factories.UserFactory()
        self.offering.add_user(self.agent_user, OfferingRole.MANAGER)
        self.agent = self.consumer(self.offering, user=self.agent_user)

    def consumer(self, *scopes, user=None):
        return logging_factories.EventConsumerFactory.with_scopes(
            *scopes,
            user=user or self.staff,
            queue_created=True,
            rmq_username=RMQ,
        )

    def events(self, mock_publish, consumer=None):
        """Payloads of the project group events delivered to ``consumer``."""
        topic = f"consumer_{(consumer or self.agent).uuid.hex}"
        payloads = []
        for call in mock_publish.call_args_list:
            for message in call.args[0]:
                payload = json.loads(message["payload"])
                if message["topic"] == topic and payload["object_type"] == OBJECT_TYPE:
                    payloads.append(payload)
        return payloads

    def test_first_approved_resource_announces_the_numbered_group(self, mock_publish):
        self.make_resource()
        group = self.group_of()

        [event] = self.events(mock_publish)
        self.assertEqual(event["action"], "create")
        self.assertEqual(event["gid"], 20001)
        self.assertEqual(event["name"], group.name)
        self.assertEqual(event["project_group_uuid"], group.uuid.hex)
        self.assertEqual(event["project_uuid"], self.project.uuid.hex)
        self.assertEqual(event["service_provider_uuid"], self.provider.uuid.hex)
        self.assertEqual(event["customer_uuid"], self.provider.customer.uuid.hex)

    def test_event_is_sent_only_once_the_transaction_commits(self, mock_publish):
        self.make_resource()
        group = self.group_of()
        mock_publish.reset_mock()
        deferred = []
        with mock.patch.object(
            project_groups.transaction, "on_commit", side_effect=deferred.append
        ):
            group.gid = 20100
            group.save()

        self.assertFalse(mock_publish.called)
        [callback] = deferred
        callback()
        [event] = self.events(mock_publish)
        self.assertEqual(event["gid"], 20100)

    def test_more_resources_in_the_project_announce_nothing_more(self, mock_publish):
        self.make_resource()
        self.make_resource(offering=self.other_offering)
        self.assertEqual(len(self.events(mock_publish)), 1)

    def test_group_without_a_gid_is_not_announced(self, mock_publish):
        self.pool.delete()
        self.make_resource()
        self.assertIsNone(self.group_of().gid)
        self.assertFalse(self.events(mock_publish))

    def test_numbering_a_group_later_announces_it(self, mock_publish):
        self.pool.delete()
        self.make_resource()
        factories.PosixIdPoolFactory(
            service_provider=self.provider,
            min_gid=30000,
            max_gid=30999,
            next_gid=30000,
            min_group_gid=20001,
            max_group_gid=20200,
            next_group_gid=20001,
        )
        project_groups.backfill(self.provider)

        [event] = self.events(mock_publish)
        self.assertEqual(event["action"], "create")
        self.assertEqual(event["gid"], 20001)

    def test_set_gid_announces_the_new_gid(self, mock_publish):
        self.make_resource()
        mock_publish.reset_mock()

        self.set_gid(self.group_of(), gid=20150)

        [event] = self.events(mock_publish)
        self.assertEqual(event["action"], "update")
        self.assertEqual(event["gid"], 20150)
        self.assertEqual(event["changed_fields"], ["gid"])

    def test_adopt_announces_the_group(self, mock_publish):
        self.adopt(gid=20042)
        [event] = self.events(mock_publish)
        self.assertEqual(event["action"], "create")
        self.assertEqual(event["gid"], 20042)

    def test_import_announces_every_group(self, mock_publish):
        projects = [structure_factories.ProjectFactory() for _ in range(3)]
        self.client.force_authenticate(self.staff)
        payload = {
            "service_provider": self.provider.uuid.hex,
            "groups": [
                {"project": project.uuid.hex, "gid": 20001 + index}
                for index, project in enumerate(projects)
            ],
        }
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(LIST_URL + "import_groups/", payload, format="json")

        self.assertEqual(
            sorted(event["gid"] for event in self.events(mock_publish)),
            [20001, 20002, 20003],
        )

    def test_import_rolled_back_part_way_announces_nothing(self, mock_publish):
        # The second entry is refused only when its GID is pinned, after the
        # first group has been written: the rollback must take its event along.
        self.make_resource()
        held = self.group_of().gid
        mock_publish.reset_mock()
        projects = [structure_factories.ProjectFactory() for _ in range(2)]
        self.client.force_authenticate(self.staff)
        payload = {
            "service_provider": self.provider.uuid.hex,
            "groups": [
                {"project": projects[0].uuid.hex, "gid": 20010},
                {"project": projects[1].uuid.hex, "gid": held},
            ],
        }
        # The suite's conftest runs on_commit callbacks at once; give the
        # publisher Django's own, so a rolled-back savepoint discards them.
        with (
            mock.patch.object(
                project_groups.transaction,
                "on_commit",
                side_effect=lambda func, using=None, **kw: connection.on_commit(func),
            ),
            self.captureOnCommitCallbacks(execute=False) as callbacks,
        ):
            response = self.client.post(
                LIST_URL + "import_groups/", payload, format="json"
            )

        self.assertNotEqual(response.status_code, 200, response.data)
        self.assertFalse(
            models.ServiceProviderProjectGroup.objects.filter(
                project__in=projects
            ).exists()
        )
        published = [c for c in callbacks if isinstance(c, partial)]
        self.assertFalse(published, "an event survived a rolled-back import")
        self.assertFalse(self.events(mock_publish))

    def test_import_that_commits_queues_its_events_on_commit(self, mock_publish):
        # Positive control for the test above: the same harness does see the
        # events of an import that is not rolled back.
        project = structure_factories.ProjectFactory()
        self.client.force_authenticate(self.staff)
        payload = {
            "service_provider": self.provider.uuid.hex,
            "groups": [{"project": project.uuid.hex, "gid": 20010}],
        }
        with (
            mock.patch.object(
                project_groups.transaction,
                "on_commit",
                side_effect=lambda func, using=None, **kw: connection.on_commit(func),
            ),
            self.captureOnCommitCallbacks(execute=False) as callbacks,
        ):
            response = self.client.post(
                LIST_URL + "import_groups/", payload, format="json"
            )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue([c for c in callbacks if isinstance(c, partial)])

    def test_deleting_a_group_without_a_gid_announces_nothing(self, mock_publish):
        self.pool.delete()
        self.make_resource()
        mock_publish.reset_mock()
        self.group_of().delete()
        self.assertFalse(self.events(mock_publish))

    def test_clearing_a_gid_is_announced(self, mock_publish):
        self.make_resource()
        group = self.group_of()
        mock_publish.reset_mock()
        group.gid = None
        group.save()
        [event] = self.events(mock_publish)
        self.assertEqual(event["action"], "update")
        self.assertIsNone(event["gid"])

    def test_deleting_a_group_announces_it(self, mock_publish):
        self.make_resource()
        group = self.group_of()
        mock_publish.reset_mock()

        with self.captureOnCommitCallbacks(execute=True):
            group.delete()

        [event] = self.events(mock_publish)
        self.assertEqual(event["action"], "delete")
        self.assertEqual(event["gid"], 20001)

    def test_deleting_the_provider_announces_no_group(self, mock_publish):
        self.make_resource()
        mock_publish.reset_mock()

        with self.captureOnCommitCallbacks(execute=True):
            self.provider.delete()

        self.assertFalse(
            [e for e in self.events(mock_publish) if e["action"] == "delete"]
        )

    def test_switching_project_groups_on_and_off_is_announced(self, mock_publish):
        provider = factories.ServiceProviderFactory()
        offering = factories.OfferingFactory(
            customer=provider.customer, type=BASIC_OFFERING
        )
        consumer = self.consumer(offering)

        with self.captureOnCommitCallbacks(execute=True):
            provider.account_options = {"project_groups_enabled": True}
            provider.save()
        with self.captureOnCommitCallbacks(execute=True):
            provider.account_options = {"project_groups_enabled": False}
            provider.save()

        self.assertEqual(
            [
                (event["action"], event["project_groups_enabled"])
                for event in self.events(mock_publish, consumer)
            ],
            [("switch", True), ("switch", False)],
        )

    def test_saving_the_provider_without_a_switch_announces_nothing(self, mock_publish):
        with self.captureOnCommitCallbacks(execute=True):
            self.provider.account_options = {
                **self.provider.account_options,
                "homedir_prefix": "/home/",
            }
            self.provider.save()
        self.assertFalse(self.events(mock_publish))

    def test_every_offering_of_the_provider_hears_it(self, mock_publish):
        # Non-staff owners, so delivery re-authorisation is exercised too.
        other_manager = structure_factories.UserFactory()
        self.other_offering.add_user(other_manager, OfferingRole.MANAGER)
        other_agent = self.consumer(self.other_offering, user=other_manager)
        customer_bound = self.consumer(self.provider.customer, user=self.owner)
        self.make_resource()

        self.assertEqual(len(self.events(mock_publish, other_agent)), 1)
        self.assertEqual(len(self.events(mock_publish, customer_bound)), 1)

    def test_another_providers_offering_does_not_hear_it(self, mock_publish):
        stranger_offering = factories.OfferingFactory(type=BASIC_OFFERING)
        stranger_manager = structure_factories.UserFactory()
        stranger_offering.add_user(stranger_manager, OfferingRole.MANAGER)
        stranger = self.consumer(stranger_offering, user=stranger_manager)
        self.make_resource()
        self.assertFalse(self.events(mock_publish, stranger))

    def test_archived_offering_does_not_hear_it(self, mock_publish):
        archived = self.make_offering(state=models.Offering.States.ARCHIVED)
        consumer = self.consumer(archived)
        self.make_resource()
        self.assertFalse(self.events(mock_publish, consumer))

    def test_nothing_is_announced_when_side_effects_are_skipped(self, mock_publish):
        set_skip_side_effects(True)
        try:
            self.make_resource()
        finally:
            set_skip_side_effects(False)
        self.assertFalse(self.events(mock_publish))
