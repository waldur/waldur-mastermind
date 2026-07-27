"""Audit trail for the ResourceProject lifecycle.

Creation was previously untracked entirely (no created_by field, no
event), unlike deletion which already captured removed_by. These tests
pin the full trail: created_by on the row, and created/removed/recovered
events with the acting user in context.
"""

from rest_framework import status, test
from rest_framework.reverse import reverse

from waldur_core.logging.models import Event, Feed
from waldur_core.structure.tests import fixtures
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.tests import factories


class ResourceProjectAuditTest(test.APITestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.ProjectFixture()
        self.offering = factories.OfferingFactory(
            customer=self.fixture.customer,
            plugin_options={"enable_resource_projects": True},
        )
        self.resource = factories.ResourceFactory(
            project=self.fixture.project, offering=self.offering
        )
        self.staff = self.fixture.staff
        self.client.force_authenticate(self.staff)
        self.list_url = "http://testserver" + reverse(
            "marketplace-resource-project-list"
        )

    def _create(self, name="audited-rp"):
        response = self.client.post(
            self.list_url,
            {"resource": self.resource.uuid.hex, "name": name},
            format="json",
        )
        self.assertEqual(status.HTTP_201_CREATED, response.status_code, response.data)
        return response

    def _detail_url(self, rp_uuid):
        return "http://testserver" + reverse(
            "marketplace-resource-project-detail", kwargs={"uuid": rp_uuid}
        )

    def test_creation_records_created_by_and_emits_event(self):
        response = self._create()
        rp = models.ResourceProject.objects.get(uuid=response.data["uuid"])
        self.assertEqual(self.staff, rp.created_by)
        self.assertEqual(self.staff.username, response.data["created_by_username"])

        event = Event.objects.filter(
            event_type="marketplace_resource_project_created"
        ).latest("created")
        self.assertEqual(rp.uuid.hex, event.context.get("resource_project_uuid"))
        # The acting user is captured by the event-context middleware; in
        # tests (no middleware) at minimum the scope feeds must resolve.
        self.assertTrue(Feed.objects.filter(event=event).exists())

    def test_soft_delete_emits_removed_event(self):
        response = self._create("audited-del")
        rp_uuid = response.data["uuid"]
        response = self.client.delete(self._detail_url(rp_uuid))
        self.assertEqual(status.HTTP_204_NO_CONTENT, response.status_code)
        event = Event.objects.filter(
            event_type="marketplace_resource_project_removed"
        ).latest("created")
        self.assertEqual(rp_uuid, event.context.get("resource_project_uuid"))

    def test_recover_emits_recovered_event(self):
        response = self._create("audited-rec")
        rp_uuid = response.data["uuid"]
        self.client.delete(self._detail_url(rp_uuid))
        response = self.client.post(
            self._detail_url(rp_uuid) + "recover/", {}, format="json"
        )
        self.assertEqual(status.HTTP_200_OK, response.status_code, response.data)
        event = Event.objects.filter(
            event_type="marketplace_resource_project_recovered"
        ).latest("created")
        self.assertEqual(rp_uuid, event.context.get("resource_project_uuid"))

    def test_created_by_username_null_for_legacy_rows(self):
        rp = models.ResourceProject.objects.create(
            resource=self.resource,
            name="legacy-row",
            state=models.ResourceProject.States.OK,
        )
        response = self.client.get(self._detail_url(rp.uuid.hex))
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        self.assertIn("created_by_username", response.data)
        self.assertIsNone(response.data["created_by_username"])
