import datetime

import reversion
from ddt import data, ddt
from rest_framework import status, test

from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.enums import OrderTypes, ResourceStates
from waldur_mastermind.marketplace.tests import factories, fixtures


@ddt
class AdjustResourceDatesTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.resource = self.fixture.resource
        self.resource.state = ResourceStates.OK
        self.resource.save()

        self.fixture.offering_component.is_prepaid = True
        self.fixture.offering_component.save()

        # Touch the cached_property so a CREATE order exists for the resource.
        self.fixture.order

        self.start_date = datetime.date.today() + datetime.timedelta(days=7)
        self.end_date = datetime.date.today() + datetime.timedelta(days=37)
        self.url = factories.ResourceFactory.get_url(self.resource, "adjust_dates")

    def _payload(self, **overrides):
        payload = {
            "start_date": self.start_date.isoformat(),
            "end_date": self.end_date.isoformat(),
        }
        payload.update(overrides)
        return payload

    def _post(self, payload, user="staff"):
        self.client.force_authenticate(getattr(self.fixture, user))
        return self.client.post(self.url, payload)

    def test_staff_can_adjust_dates(self):
        response = self._post(self._payload(comment="helpdesk shift"))
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        self.resource.refresh_from_db()
        self.assertEqual(self.resource.end_date, self.end_date)
        self.assertEqual(self.resource.end_date_requested_by, self.fixture.staff)

        order = models.Order.objects.get(resource=self.resource, type=OrderTypes.CREATE)
        self.assertEqual(order.start_date, self.start_date)

    def test_revision_is_recorded_with_comment(self):
        response = self._post(self._payload(comment="helpdesk shift"))
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        self.resource.refresh_from_db()
        versions = reversion.models.Version.objects.get_for_object(self.resource)
        self.assertGreater(versions.count(), 0)
        self.assertEqual(versions.first().revision.comment, "helpdesk shift")

    def test_default_revision_comment_when_omitted(self):
        response = self._post(self._payload())
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        self.resource.refresh_from_db()
        versions = reversion.models.Version.objects.get_for_object(self.resource)
        self.assertIn("Staff adjusted dates", versions.first().revision.comment)

    @data("offering_owner", "service_manager")
    def test_provider_users_cannot_adjust_dates(self, role):
        response = self._post(self._payload(), user=role)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @data("owner", "admin", "manager")
    def test_consumer_users_cannot_adjust_dates(self, role):
        response = self._post(self._payload(), user=role)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_non_prepaid_resource_returns_400(self):
        self.fixture.offering_component.is_prepaid = False
        self.fixture.offering_component.save()
        response = self._post(self._payload())
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_end_before_start_returns_400(self):
        response = self._post(
            self._payload(
                start_date=self.end_date.isoformat(),
                end_date=self.start_date.isoformat(),
            )
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_end_in_past_returns_400(self):
        past_start = datetime.date.today() - datetime.timedelta(days=10)
        past_end = datetime.date.today() - datetime.timedelta(days=5)
        response = self._post(
            self._payload(
                start_date=past_start.isoformat(),
                end_date=past_end.isoformat(),
            )
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @data(ResourceStates.TERMINATING, ResourceStates.TERMINATED)
    def test_blocked_states_return_400(self, state):
        self.resource.state = state
        self.resource.save()
        response = self._post(self._payload())
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @data(
        ResourceStates.CREATING,
        ResourceStates.UPDATING,
        ResourceStates.OK,
        ResourceStates.ERRED,
    )
    def test_allowed_states_succeed(self, state):
        self.resource.state = state
        self.resource.save()
        response = self._post(self._payload())
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_resource_without_create_order_only_updates_end_date(self):
        models.Order.objects.filter(
            resource=self.resource, type=OrderTypes.CREATE
        ).delete()
        response = self._post(self._payload())
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.end_date, self.end_date)
