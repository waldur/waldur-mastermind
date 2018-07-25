from django.test.utils import override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework import test

from waldur_core.core.tests.helpers import override_waldur_core_settings
from waldur_core.structure.images import dummy_image
from waldur_core.structure.models import CustomerRole
from waldur_core.structure.tests.factories import UserFactory, CustomerFactory


@override_settings(MEDIA_URL='/media/')
class ImageUploadTest(test.APITransactionTestCase):
    def setUp(self):
        self.staff = UserFactory(is_staff=True)
        self.owner = UserFactory()
        self.user = UserFactory()
        self.customer = CustomerFactory()
        self.customer.add_user(self.owner, CustomerRole.OWNER)
        self.url = reverse('customer_image', kwargs={'uuid': self.customer.uuid.hex})

    def test_default_customer_logo(self):
        self.client.force_authenticate(user=self.staff)
        self.assert_default_logo()

        with dummy_image() as image:
            self.assert_can_upload_image(image)
            self.assert_can_delete_image()

        self.assert_default_logo()

    # Positive
    def test_staff_can_upload_and_delete_customer_logo(self):
        self.client.force_authenticate(user=self.staff)

        with dummy_image() as image:
            self.assert_can_upload_image(image)
            self.assert_can_delete_image()

    def test_customer_owner_can_upload_and_delete_customer_logo(self):
        self.client.force_authenticate(user=self.owner)

        with dummy_image() as image:
            self.assert_can_upload_image(image)
            self.assert_can_delete_image()

    # Negative
    def test_user_cannot_upload_logo_for_customer_he_is_not_owner_of(self):
        self.client.force_authenticate(user=self.user)

        with dummy_image() as image:
            response = self.upload_image(self.url, image)
            self.assertEqual(status.HTTP_403_FORBIDDEN, response.status_code)

    # Helpers
    def assert_can_upload_image(self, image):
        response = self.upload_image(self.url, image)
        self.assertEqual(status.HTTP_200_OK, response.status_code, response.data)
        self.assertIn('image', response.data)
        self.assertIsNotNone(response.data['image'])

    def assert_can_delete_image(self):
        response = self.client.delete(self.url)
        self.assertEqual(status.HTTP_204_NO_CONTENT, response.status_code, response.data)

    @override_waldur_core_settings(DEFAULT_CUSTOMER_LOGO='default_logo.jpg')
    def assert_default_logo(self):
        url = reverse('customer-detail', kwargs={'uuid': self.customer.uuid.hex})
        response = self.client.get(url)

        self.assertEqual(status.HTTP_200_OK, response.status_code)
        self.assertEqual('default_logo.jpg', response.data['image'])

    def upload_image(self, url, image):
        return self.client.put(self.url, {'image': image}, format='multipart')
