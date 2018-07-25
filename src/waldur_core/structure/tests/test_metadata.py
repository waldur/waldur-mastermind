from rest_framework import status, test
from rest_framework.reverse import reverse

from waldur_core.structure.tests import factories


class ServiceMetadataTest(test.APITransactionTestCase):
    def test_any_user_can_get_service_metadata(self):
        self.client.force_authenticate(factories.UserFactory())
        response = self.client.get(reverse('service_metadata-list'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
