from ddt import data, ddt
from rest_framework import status
from rest_framework.test import APITransactionTestCase

from waldur_mastermind.marketplace.tests import (
    factories as marketplace_factories,
)
from waldur_mastermind.marketplace.tests import (
    fixtures as marketplace_fixtures,
)
from waldur_mastermind.marketplace_remote.tests import (
    factories as marketplace_remote_factories,
)


@ddt
class BaseRemoteSynchronisationTest(APITransactionTestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.remote_synchronisation = (
            marketplace_remote_factories.RemoteSynchronisationFactory(
                local_service_provider=self.fixture.service_provider
            )
        )
        self.url = marketplace_remote_factories.RemoteSynchronisationFactory.get_url(
            self.remote_synchronisation
        )
        self.list_url = (
            marketplace_remote_factories.RemoteSynchronisationFactory.get_list_url()
        )


@ddt
class RemoteSynchronisationCreateTest(BaseRemoteSynchronisationTest):
    def _get_payload(self):
        return {
            "api_url": "http://127.0.0.77/api/",
            "token": "token",
            "remote_organization_uuid": "97cd78cb553f4e129b780a72dabb8cd6",
            "remote_organization_name": "organization",
            "local_service_provider": marketplace_factories.ServiceProviderFactory.get_url(
                self.fixture.service_provider
            ),
            "remotelocalcategory_set": [
                {
                    "local_category": marketplace_factories.CategoryFactory.get_url(),
                    "remote_category": "ffc2a795-589b-4dc6-bcc1-71fc0b0a0a78",
                }
            ],
        }

    @data("staff")
    def test_user_can_create_permissions(self, user):
        self.client.force_authenticate(user=getattr(self.fixture, user))
        response = self.client.post(self.list_url, self._get_payload())
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    @data("provider_owner", "user")
    def test_user_can_not_create_permissions(self, user):
        self.client.force_authenticate(user=getattr(self.fixture, user))
        response = self.client.post(self.list_url, self._get_payload())
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_synchronization_cannot_reference_the_same_waldur(self):
        payload = self._get_payload()
        payload["remote_organization_uuid"] = self.fixture.customer.uuid.hex

        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.post(self.list_url, payload)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.data["non_field_errors"][0],
            "Synchronization cannot reference the same Waldur instance.",
        )


@ddt
class RemoteSynchronisationReadTest(BaseRemoteSynchronisationTest):
    @data("staff")
    def test_user_can_read_permissions(self, user):
        self.client.force_authenticate(user=getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data("provider_owner", "user")
    def test_user_can_not_read_permissions(self, user):
        self.client.force_authenticate(user=getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


@ddt
class RemoteSynchronisationUpdateTest(BaseRemoteSynchronisationTest):
    def _get_update_payload(self):
        return {
            "api_url": "http://127.0.0.1/api/v2/",
            "token": "new_token",
            "remote_organization_uuid": "97cd78cb553f4e129b780a72dabb8cd6",
            "remote_organization_name": "updated_organization",
            "local_service_provider": marketplace_factories.ServiceProviderFactory.get_url(
                self.fixture.service_provider
            ),
            "remotelocalcategory_set": [
                {
                    "local_category": marketplace_factories.CategoryFactory.get_url(),
                    "remote_category": "ffc2a795-589b-4dc6-bcc1-71fc0b0a0a78",
                }
            ],
        }

    @data("staff")
    def test_user_can_update_permissions(self, user):
        self.client.force_authenticate(user=getattr(self.fixture, user))
        response = self.client.put(self.url, self._get_update_payload())
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data("provider_owner", "user")
    def test_user_can_not_update_permissions(self, user):
        self.client.force_authenticate(user=getattr(self.fixture, user))
        response = self.client.put(self.url, self._get_update_payload())
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


@ddt
class RemoteSynchronisationDeleteTest(BaseRemoteSynchronisationTest):
    @data("staff")
    def test_user_can_delete_permissions(self, user):
        self.client.force_authenticate(user=getattr(self.fixture, user))
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    @data("provider_owner", "user")
    def test_user_can_not_delete_permissions(self, user):
        self.client.force_authenticate(user=getattr(self.fixture, user))
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
