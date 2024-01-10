from ddt import data, ddt
from rest_framework import status, test

from waldur_core.media.utils import dummy_image
from waldur_core.structure.tests import fixtures
from waldur_mastermind.marketplace import models

from . import factories


@ddt
class OfferingFileGetTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.offering_file = factories.OfferingFileFactory()

    @data("staff", "owner", "user", "customer_support", "admin", "manager")
    def test_offering_file_should_be_visible_to_all_authenticated_users(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.OfferingFileFactory.get_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 1)

    def test_offering_file_should_be_invisible_to_unauthenticated_users(self):
        url = factories.OfferingFileFactory.get_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    @data("staff", "owner", "user", "customer_support", "admin", "manager")
    def test_offering_file_of_offering_should_be_visible_to_all_authenticated_users(
        self, user
    ):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        offering = self.offering_file.offering
        url = factories.OfferingFileFactory.get_list_url()
        response = self.client.get(url, {"offering_uuid": offering.uuid.hex})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 1)


@ddt
class OfferingFileCreateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.customer = self.fixture.customer

    @data("staff", "owner")
    def test_staff_and_owner_can_create_offering_file(self, user):
        response = self.create_offering_file(user)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(
            models.OfferingFile.objects.filter(
                offering__customer=self.customer
            ).exists()
        )

    @data("user", "customer_support", "admin", "manager")
    def test_other_users_can_not_create_offering_file(self, user):
        response = self.create_offering_file(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def create_offering_file(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.OfferingFileFactory.get_list_url()
        self.provider = factories.ServiceProviderFactory(customer=self.customer)
        self.offering = factories.OfferingFactory(customer=self.customer)

        payload = {
            "name": "offering_file_1",
            "offering": factories.OfferingFactory.get_url(offering=self.offering),
            "file": dummy_image(),
        }

        return self.client.post(url, payload, format="multipart")


@ddt
class OfferingFileDeleteTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.customer = self.fixture.customer
        self.provider = factories.ServiceProviderFactory(customer=self.customer)
        self.offering = factories.OfferingFactory(customer=self.customer)
        self.offering_file = factories.OfferingFileFactory(offering=self.offering)

    @data("staff", "owner")
    def test_staff_and_owner_can_delete_offering_file(self, user):
        response = self.delete_offering_file(user)
        self.assertEqual(
            response.status_code, status.HTTP_204_NO_CONTENT, response.data
        )
        self.assertFalse(
            models.OfferingFile.objects.filter(
                offering__customer=self.customer
            ).exists()
        )

    @data("user", "customer_support", "admin", "manager")
    def test_other_users_can_not_delete_offering_file(self, user):
        response = self.delete_offering_file(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(
            models.OfferingFile.objects.filter(
                offering__customer=self.customer
            ).exists()
        )

    def delete_offering_file(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.OfferingFileFactory.get_url(self.offering_file)
        response = self.client.delete(url)
        return response
