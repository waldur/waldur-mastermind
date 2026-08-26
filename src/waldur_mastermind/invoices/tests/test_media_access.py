"""Media access rules for payment proofs.

See :mod:`waldur_core.media.access`. Payment.Permissions routes through
``profile__organization``, which is what PaymentViewSet's GenericRoleFilter
applies, so a proof is readable by users with a role on the paying customer.
"""

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from rest_framework import status, test

from waldur_core.media import models as media_models
from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.invoices.tests import factories

PDF = b"%PDF-1.4 proof"


class PaymentProofMediaAccessTest(test.APITestCase):
    def setUp(self):
        self.customer = structure_factories.CustomerFactory()
        profile = factories.PaymentProfileFactory(organization=self.customer)
        self.payment = factories.PaymentFactory(profile=profile)
        self.payment.proof = SimpleUploadedFile(
            "proof.pdf", PDF, content_type="application/pdf"
        )
        self.payment.save(update_fields=["proof"])

        media_file = media_models.File.objects.get(name=self.payment.proof.name)
        self.url = reverse("media", kwargs={"uuid": media_file.uuid})

    def get_as(self, user):
        if user is None:
            self.client.logout()
        else:
            self.client.force_authenticate(user)
        return self.client.get(self.url).status_code

    def test_anonymous_user_cannot_download(self):
        self.assertEqual(self.get_as(None), status.HTTP_404_NOT_FOUND)

    def test_unrelated_user_cannot_download(self):
        self.assertEqual(
            self.get_as(structure_factories.UserFactory()), status.HTTP_404_NOT_FOUND
        )

    def test_owner_of_the_paying_customer_can_download(self):
        owner = structure_factories.UserFactory()
        self.customer.add_user(owner, CustomerRole.OWNER)
        self.assertEqual(self.get_as(owner), status.HTTP_200_OK)

    def test_owner_of_another_customer_cannot_download(self):
        other_owner = structure_factories.UserFactory()
        structure_factories.CustomerFactory().add_user(other_owner, CustomerRole.OWNER)
        self.assertEqual(self.get_as(other_owner), status.HTTP_404_NOT_FOUND)

    def test_staff_can_download(self):
        self.assertEqual(
            self.get_as(structure_factories.UserFactory(is_staff=True)),
            status.HTTP_200_OK,
        )

    def test_support_can_download(self):
        self.assertEqual(
            self.get_as(structure_factories.UserFactory(is_support=True)),
            status.HTTP_200_OK,
        )
