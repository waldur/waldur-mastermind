from ddt import data, ddt
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from rest_framework import status, test

from waldur_core.media import models as media_models
from waldur_core.media.utils import MARKDOWN_IMAGE_PREFIX, get_image_hash
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import OfferingRole, ProjectRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace.tests import fixtures

VALID_PDF = (
    b"%PDF-1.4\n1 0 obj\n<<\n/Type /Catalog\n>>\nendobj\n"
    b"xref\n0 1\n0000000000 65535 f \ntrailer\n<<\n/Size 1\n"
    b"/Root 1 0 R\n>>\nstartxref\n9\n%%EOF"
)

ORDER_ATTACHMENT_FIELDS = (
    "attachment",
    "provider_message_attachment",
    "consumer_message_attachment",
)


def _make_pdf(name="order-document.pdf"):
    return SimpleUploadedFile(name, VALID_PDF, content_type="application/pdf")


@ddt
class OrderAttachmentMediaAccessTest(test.APITestCase):
    """Ensure marketplace order PDFs require order LIST_ORDERS access via /api/media/."""

    def setUp(self):
        OfferingRole.MANAGER.add_permission(PermissionEnum.LIST_ORDERS)
        self.fixture = fixtures.MarketplaceFixture()
        self.order = self.fixture.order

    def _save_attachment(self, field_name):
        uploaded = _make_pdf(f"{field_name}.pdf")
        setattr(self.order, field_name, uploaded)
        self.order.save(update_fields=[field_name])
        media_file = media_models.File.objects.get(
            name=getattr(self.order, field_name).name
        )
        return reverse("media", kwargs={"uuid": media_file.uuid.hex})

    def _fetch(self, user, url):
        if user is None:
            self.client.logout()
        else:
            self.client.force_authenticate(user)
        return self.client.get(url)

    @data(*ORDER_ATTACHMENT_FIELDS)
    def test_anonymous_user_cannot_download_order_attachment(self, field_name):
        url = self._save_attachment(field_name)
        response = self._fetch(None, url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @data(*ORDER_ATTACHMENT_FIELDS)
    def test_unrelated_authenticated_user_cannot_download(self, field_name):
        url = self._save_attachment(field_name)
        unrelated_user = structure_factories.UserFactory()
        response = self._fetch(unrelated_user, url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @data(*ORDER_ATTACHMENT_FIELDS)
    def test_consumer_customer_owner_can_download(self, field_name):
        url = self._save_attachment(field_name)
        response = self._fetch(self.fixture.owner, url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.content, VALID_PDF)

    @data(*ORDER_ATTACHMENT_FIELDS)
    def test_consumer_project_manager_can_download(self, field_name):
        url = self._save_attachment(field_name)
        response = self._fetch(self.fixture.manager, url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.content, VALID_PDF)

    @data(*ORDER_ATTACHMENT_FIELDS)
    def test_provider_offering_customer_owner_can_download(self, field_name):
        url = self._save_attachment(field_name)
        response = self._fetch(self.fixture.offering_owner, url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.content, VALID_PDF)

    @data(*ORDER_ATTACHMENT_FIELDS)
    def test_provider_offering_manager_can_download(self, field_name):
        url = self._save_attachment(field_name)
        response = self._fetch(self.fixture.offering_manager, url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.content, VALID_PDF)

    @data(*ORDER_ATTACHMENT_FIELDS)
    def test_staff_can_download(self, field_name):
        url = self._save_attachment(field_name)
        response = self._fetch(self.fixture.staff, url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.content, VALID_PDF)

    @data(*ORDER_ATTACHMENT_FIELDS)
    def test_global_support_can_download(self, field_name):
        url = self._save_attachment(field_name)
        response = self._fetch(self.fixture.global_support, url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.content, VALID_PDF)

    @data(*ORDER_ATTACHMENT_FIELDS)
    def test_project_member_without_list_orders_cannot_download(self, field_name):
        member = structure_factories.UserFactory()
        self.fixture.project.add_user(member, ProjectRole.MEMBER)
        ProjectRole.MEMBER.permissions.filter(
            permission=PermissionEnum.LIST_ORDERS
        ).delete()

        url = self._save_attachment(field_name)
        response = self._fetch(member, url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class OrderAttachmentMediaOrphanTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()

    def test_orphan_order_prefix_file_is_not_accessible_even_to_staff(self):
        orphan_file = media_models.File.objects.create(
            name="marketplace_order_attachments/orphan.pdf",
            content=VALID_PDF,
            size=len(VALID_PDF),
            mime_type="application/pdf",
            hash=get_image_hash(VALID_PDF),
        )
        media_url = reverse("media", kwargs={"uuid": orphan_file.uuid})

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(media_url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_orphan_provider_prefix_file_is_not_accessible(self):
        orphan_file = media_models.File.objects.create(
            name="marketplace_order_provider_attachments/orphan.pdf",
            content=VALID_PDF,
            size=len(VALID_PDF),
            mime_type="application/pdf",
            hash=get_image_hash(VALID_PDF),
        )
        media_url = reverse("media", kwargs={"uuid": orphan_file.uuid})

        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(media_url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class OrderAttachmentMediaRegressionTest(test.APITestCase):
    def test_non_order_media_remains_publicly_accessible(self):
        public_file = media_models.File.objects.create(
            name=f"{MARKDOWN_IMAGE_PREFIX}public.png",
            content=b"\x89PNG\r\n",
            size=6,
            mime_type="image/png",
            hash=get_image_hash(b"\x89PNG\r\n"),
        )
        media_url = reverse("media", kwargs={"uuid": public_file.uuid})

        response = self.client.get(media_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_unregistered_prefix_is_denied(self):
        """A prefix nobody declared a rule for is served to nobody."""
        unknown_file = media_models.File.objects.create(
            name="other_files/some_file.pdf",
            content=VALID_PDF,
            size=len(VALID_PDF),
            mime_type="application/pdf",
            hash=get_image_hash(VALID_PDF),
        )
        media_url = reverse("media", kwargs={"uuid": unknown_file.uuid})

        response = self.client.get(media_url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class OrderAttachmentMediaLookupTest(test.APITestCase):
    """Verify each upload prefix resolves through the correct Order field."""

    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.order = self.fixture.order

    def _assert_field_is_protected(self, field_name):
        uploaded = _make_pdf(f"{field_name}-lookup.pdf")
        setattr(self.order, field_name, uploaded)
        self.order.save(update_fields=[field_name])

        media_file = media_models.File.objects.get(
            name=getattr(self.order, field_name).name
        )
        media_url = reverse("media", kwargs={"uuid": media_file.uuid.hex})

        self.client.logout()
        self.assertEqual(
            self.client.get(media_url).status_code, status.HTTP_404_NOT_FOUND
        )

        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(media_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_purchase_order_attachment_field(self):
        self._assert_field_is_protected("attachment")

    def test_provider_message_attachment_field(self):
        self._assert_field_is_protected("provider_message_attachment")

    def test_consumer_message_attachment_field(self):
        self._assert_field_is_protected("consumer_message_attachment")
