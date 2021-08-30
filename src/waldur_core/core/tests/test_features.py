from rest_framework import status, test

from waldur_core.core import models
from waldur_core.structure.tests.factories import UserFactory


class FeaturesTest(test.APITransactionTestCase):
    def test_staff_can_update_feature(self):
        user = UserFactory(is_staff=True)
        self.client.force_login(user)

        response = self.client.get('/api/configuration/')
        self.assertFalse(response.data['FEATURES']['MARKETPLACE']['FLOWS'])

        response = self.client.post(
            '/api/feature-values/', {'MARKETPLACE': {'FLOWS': True}}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        response = self.client.get('/api/configuration/')
        self.assertTrue(models.Feature.objects.get(key="MARKETPLACE.FLOWS").value)

    def test_non_staff_can_not_update_features(self):
        user = UserFactory()
        self.client.force_login(user)

        response = self.client.post(
            '/api/feature-values/', {'MARKETPLACE': {'FLOWS': True}}
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_feature_is_not_updated_if_it_is_not_defined(self):
        user = UserFactory(is_staff=True)
        self.client.force_login(user)

        response = self.client.post(
            '/api/feature-values/', {'MARKETPLACE': {'FOO': True}}
        )
        self.assertEqual(response.data, '0 features are updated.')
