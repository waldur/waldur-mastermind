"""Names, descriptions and reasons show in event messages as written."""

from django.utils import timezone
from rest_framework import status, test

from waldur_core.core.models import ImpersonatedUser
from waldur_core.logging.enums import EventType
from waldur_core.logging.models import Event
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import ProjectRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace import log, models
from waldur_mastermind.marketplace.enums import ResourceApiKeyActions
from waldur_mastermind.marketplace.tests import factories

TEXT = "AA{{BB}}CC {nosuchkey} {user_agent.__class__} {"


def last_message(event_type):
    return Event.objects.filter(event_type=event_type).latest("created").message


class HandlerMessagesTest(test.APITestCase):
    def test_plan_update(self):
        plan = factories.PlanFactory(name="plan")
        plan.name = TEXT
        plan.save()

        self.assertEqual(
            last_message(EventType.MARKETPLACE_PLAN_UPDATED),
            f"Plan {TEXT} has been updated. Details: name: plan -> {TEXT}.",
        )

    def test_maintenance_announcement_update(self):
        announcement = factories.MaintenanceAnnouncementFactory()
        announcement.message = TEXT
        announcement.save()

        self.assertIn(
            f"-> {TEXT}", last_message(EventType.MAINTENANCE_ANNOUNCEMENT_UPDATED)
        )

    def test_offering_access_subnet_update(self):
        subnet = models.OfferingAccessSubnet.objects.create(
            offering=factories.OfferingFactory(), inet="10.0.0.0/24", description="x"
        )
        subnet = models.OfferingAccessSubnet.objects.get(pk=subnet.pk)
        subnet.description = TEXT
        subnet.save()

        self.assertIn(
            f"from 'x' to '{TEXT}'",
            last_message(EventType.OFFERING_ACCESS_SUBNET_UPDATE_SUCCEEDED),
        )

    def test_offering_user_created(self):
        factories.OfferingUserFactory(offering=factories.OfferingFactory(name=TEXT))

        self.assertTrue(
            last_message(EventType.MARKETPLACE_OFFERING_USER_CREATED).endswith(
                f"in offering {TEXT} has been created."
            )
        )

    def test_offering_options_update(self):
        offering = factories.OfferingFactory(
            name=TEXT, options={"options": {}, "order": []}
        )
        offering.options = {"options": {"size{": {"type": "integer"}}, "order": []}
        offering.save()

        self.assertEqual(
            last_message(EventType.MARKETPLACE_OFFERING_OPTIONS_UPDATED),
            f"Offering {TEXT} order form options have been updated. "
            "Details: added option keys: size{.",
        )


class ResourceLogMessagesTest(test.APITestCase):
    def setUp(self):
        self.resource = factories.ResourceFactory(
            name=TEXT, end_date=timezone.now().date()
        )
        self.api_key = models.ResourceApiKey.objects.create(
            resource=self.resource, client_id="client", error_message=TEXT
        )
        self.user = structure_factories.UserFactory()

    def test_api_key_command(self):
        log.log_resource_api_key_command(
            self.api_key, ResourceApiKeyActions.ROTATE, actor=self.user, reason=TEXT
        )

        message = Event.objects.latest("created").message
        self.assertIn(f"of resource {TEXT} has been requested", message)
        self.assertTrue(message.endswith(f". {TEXT}"))

    def test_api_key_command_failed(self):
        log.log_resource_api_key_command_failed(self.api_key)

        self.assertEqual(
            last_message(EventType.MARKETPLACE_RESOURCE_API_KEY_FAILED),
            f"Rotation of API key client of resource {TEXT} has failed. {TEXT}",
        )

    def test_api_key_revealed(self):
        log.log_resource_api_key_revealed(self.api_key, self.user)

        self.assertEqual(
            last_message(EventType.MARKETPLACE_RESOURCE_API_KEY_REVEALED),
            f"API key client of resource {TEXT} has been revealed to {self.user}.",
        )

    def test_end_date_update(self):
        log.log_resource_end_date_has_been_updated(self.resource, self.user)

        self.assertIn(
            f"End date of marketplace resource {TEXT} has been updated.",
            last_message(EventType.MARKETPLACE_RESOURCE_UPDATE_END_DATE_SUCCEEDED),
        )

    def test_plan_switch(self):
        old_plan = factories.PlanFactory(name="old {")
        new_plan = factories.PlanFactory(name=TEXT)

        log.log_resource_plan_switched(
            self.resource, old_plan, new_plan, "monthly", "monthly"
        )

        self.assertEqual(
            last_message(EventType.MARKETPLACE_RESOURCE_PLAN_SWITCHED),
            f"Plan of resource {TEXT} has been switched from old {{ to {TEXT} "
            "(monthly billing).",
        )


class PlantedPlaceholderTest(test.APITestCase):
    """A project admin cannot make a later actor's request details show in a
    message the admin reads; readers who are not staff get them stripped."""

    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        ProjectRole.ADMIN.add_permission(PermissionEnum.UPDATE_RESOURCE)
        self.resource = factories.ResourceFactory(
            project=self.fixture.project,
            name="vm",
            state=models.Resource.States.OK,
        )
        self.url = factories.ResourceFactory.get_url(self.resource)

    def test_staff_request_details_stay_out_of_the_message(self):
        planted = "vm {ip_address} {user_agent} {user_impersonator_username}"
        self.client.force_authenticate(self.fixture.admin)
        response = self.client.patch(self.url, {"name": planted}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(
            self.url + "set_end_date_by_staff/",
            {"end_date": (timezone.now() + timezone.timedelta(days=30)).date()},
            format="json",
            HTTP_X_FORWARDED_FOR="203.0.113.77",
            HTTP_USER_AGENT="StaffBrowser/9.9",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        self.client.force_authenticate(self.fixture.admin)
        response = self.client.get(
            "/api/events/",
            {
                "scope": self.url,
                "event_type": EventType.MARKETPLACE_RESOURCE_UPDATE_END_DATE_SUCCEEDED,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        [event] = response.data
        self.assertIn(f"resource {planted} has been", event["message"])
        self.assertNotIn("203.0.113.77", event["message"])
        self.assertNotIn("StaffBrowser", event["message"])
        self.assertNotIn("ip_address", event["context"])


class ImpersonatedUserTest(test.APITestCase):
    def test_a_message_naming_the_user_does_not_name_the_impersonator(self):
        staff = structure_factories.UserFactory(is_staff=True)
        user = structure_factories.UserFactory()
        impersonated = ImpersonatedUser.all_objects.get(pk=user.pk)
        impersonated.impersonator = staff
        resource = factories.ResourceFactory(end_date=timezone.now().date())

        log.log_resource_end_date_has_been_updated(resource, impersonated)

        message = last_message(EventType.MARKETPLACE_RESOURCE_UPDATE_END_DATE_SUCCEEDED)
        self.assertIn(user.username, message)
        self.assertNotIn(staff.username, message)
