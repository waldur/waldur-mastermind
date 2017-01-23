from rest_framework import status

from . import base, factories
from .. import models


class BaseOfferingTest(base.BaseTest):
    def setUp(self, **kwargs):
        super(BaseOfferingTest, self).setUp()
        self.client.force_authenticate(self.fixture.staff)


class OfferingPermissionsTest(base.BaseTest):

    def setUp(self):
        super(OfferingPermissionsTest, self).setUp()
        self.offering = self.fixture.offering

    def test_user_can_see_list_of_offerings(self):
        self.client.force_authenticate(self.fixture.user)
        url = factories.OfferingRequestFactory.get_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_user_cannot_complete_offering(self):
        self.client.force_authenticate(self.fixture.user)
        url = factories.OfferingRequestFactory.get_url(offering=self.offering, action='complete')
        request_data = {'price': 10}
        response = self.client.post(url, request_data)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.offering.refresh_from_db()
        self.assertEqual(self.offering.state, models.Offering.States.REQUESTED)

    def test_staff_can_complete_offering(self):
        self.client.force_authenticate(self.fixture.staff)
        url = factories.OfferingRequestFactory.get_url(offering=self.offering, action='complete')
        request_data = {'price': 10}
        response = self.client.post(url, request_data)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.offering.refresh_from_db()
        self.assertEqual(self.offering.state, models.Offering.States.OK)


class OfferingCompleteTest(BaseOfferingTest):

    def test_offering_is_in_ok_state_when_complete_is_called(self):
        offering = factories.OfferingRequestFactory()
        self.assertEqual(offering.state, models.Offering.States.REQUESTED)

        url = factories.OfferingRequestFactory.get_url(offering=offering, action='complete')
        request_data = {'price': 10}
        response = self.client.post(url, request_data)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        offering.refresh_from_db()
        self.assertEqual(offering.state, models.Offering.States.OK)

    def test_offering_cannot_be_completed_if_it_is_terminated(self):
        offering = factories.OfferingRequestFactory(state=models.Offering.States.TERMINATED)
        url = factories.OfferingRequestFactory.get_url(offering=offering, action='complete')
        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_offering_cannot_be_completed_without_price(self):
        offering = factories.OfferingRequestFactory()
        url = factories.OfferingRequestFactory.get_url(offering=offering, action='complete')
        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        offering.refresh_from_db()
        self.assertEqual(offering.state, models.Offering.States.REQUESTED)


class OfferingTerminateTest(BaseOfferingTest):

    def test_offering_is_in_terminated_state_when_terminate_is_called(self):
        offering = factories.OfferingRequestFactory()
        self.assertEqual(offering.state, models.Offering.States.REQUESTED)

        url = factories.OfferingRequestFactory.get_url(offering=offering, action='terminate')
        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        offering.refresh_from_db()
        self.assertEqual(offering.state, models.Offering.States.TERMINATED)
