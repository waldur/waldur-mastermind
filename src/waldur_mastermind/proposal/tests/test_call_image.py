import io
import os

from ddt import data, ddt
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from PIL import Image
from rest_framework import status, test

from waldur_core.media import models as media_models
from waldur_core.media.utils import dummy_image, dummy_svg
from waldur_core.permissions.fixtures import CallRole
from waldur_mastermind.proposal import (
    call_transfer,
    models,
    utils,
    workflow_versions,
)
from waldur_mastermind.proposal.enums import CallStates
from waldur_mastermind.proposal.tests import fixtures

from . import factories


def oversized_png():
    # Noise does not compress, so the PNG stays above the 2 MB cap.
    image = Image.frombytes("RGB", (1000, 1000), os.urandom(1000 * 1000 * 3))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return SimpleUploadedFile("big.png", buffer.getvalue(), content_type="image/png")


def media_url(file):
    return reverse("media", kwargs={"uuid": file.uuid.hex})


def set_logo(call):
    call.image.save("logo.gif", dummy_image(), save=True)


@ddt
class CallImageUpdateTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call

    def upload(self, user, payload, format="multipart"):
        if isinstance(user, str):
            user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.CallFactory.get_protected_url(self.call)
        response = self.client.patch(url, payload, format=format)
        self.call.refresh_from_db()
        return response

    @data("staff", "call_manager", "call_organizer_user")
    def test_call_writer_can_upload_logo(self, user):
        response = self.upload(user, {"image": dummy_image()})

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertTrue(self.call.image)
        self.assertTrue(self.call.image.name.startswith(f"call/{self.call.uuid.hex}"))
        self.assertIn("/api/media/", response.data["image"])

    @data("owner", "reviewer_1")
    def test_call_reader_can_not_upload_logo(self, user):
        response = self.upload(user, {"image": dummy_image()})

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(self.call.image)

    @data("user", "customer_support")
    def test_outsider_can_not_upload_logo(self, user):
        response = self.upload(user, {"image": dummy_image()})

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertFalse(self.call.image)

    def test_empty_multipart_value_removes_logo(self):
        set_logo(self.call)

        response = self.upload("call_manager", {"image": ""})

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertFalse(self.call.image)
        self.assertIsNone(response.data["image"])

    def test_null_removes_logo(self):
        set_logo(self.call)

        response = self.upload("call_manager", {"image": None}, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertFalse(self.call.image)

    def test_other_field_update_keeps_logo(self):
        set_logo(self.call)
        name = self.call.image.name

        response = self.upload(
            "call_manager", {"description": "new description"}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.call.image.name, name)

    def test_svg_is_rejected(self):
        response = self.upload("call_manager", {"image": dummy_svg()})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("image", response.data)
        self.assertFalse(self.call.image)

    def test_non_image_is_rejected(self):
        document = SimpleUploadedFile(
            "notes.txt", b"not an image", content_type="text/plain"
        )

        response = self.upload("call_manager", {"image": document})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("image", response.data)

    def test_oversized_image_is_rejected(self):
        response = self.upload("call_manager", {"image": oversized_png()})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("image", response.data)
        self.assertFalse(self.call.image)

    def test_archived_call_logo_can_not_be_changed(self):
        self.call.state = CallStates.ARCHIVED
        self.call.save()

        response = self.upload("call_manager", {"image": dummy_image()})

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertFalse(self.call.image)

    def test_call_with_logo_records_a_workflow_version(self):
        # Every write to a call snapshots its exported settings into a JSON
        # field; a stored file in that snapshot would fail to serialise.
        set_logo(self.call)

        version = workflow_versions.record(self.call, self.fixture.staff)

        version.refresh_from_db()
        self.assertNotIn("image", version.document["call"])


class PublicCallImageTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call

    def test_public_call_exposes_logo(self):
        set_logo(self.call)

        response = self.client.get(factories.CallFactory.get_public_url(self.call))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("/api/media/", response.data["image"])

    def test_public_call_without_logo_has_null_image(self):
        response = self.client.get(factories.CallFactory.get_public_url(self.call))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNone(response.data["image"])

    def test_logo_is_served_anonymously(self):
        set_logo(self.call)
        url = self.client.get(factories.CallFactory.get_public_url(self.call)).data[
            "image"
        ]

        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response["Content-Type"], "image/gif")

    def test_draft_call_logo_is_served_anonymously(self):
        # The manager's pages load it through <img src>, without the token.
        self.call.state = CallStates.DRAFT
        self.call.save()
        set_logo(self.call)
        file = media_models.File.objects.get(name=self.call.image.name)

        response = self.client.get(media_url(file))

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_replaced_logo_is_no_longer_served(self):
        set_logo(self.call)
        old = media_models.File.objects.get(name=self.call.image.name)
        set_logo(self.call)
        self.assertNotEqual(self.call.image.name, old.name)

        self.assertEqual(
            self.client.get(media_url(old)).status_code, status.HTTP_404_NOT_FOUND
        )
        new = media_models.File.objects.get(name=self.call.image.name)
        self.assertEqual(
            self.client.get(media_url(new)).status_code, status.HTTP_200_OK
        )

    def test_removed_logo_is_no_longer_served(self):
        set_logo(self.call)
        old = media_models.File.objects.get(name=self.call.image.name)
        self.call.image = None
        self.call.save()

        self.assertEqual(
            self.client.get(media_url(old)).status_code, status.HTTP_404_NOT_FOUND
        )

    def test_public_serializer_does_not_accept_logo(self):
        # The public endpoint is read-only; the field must not be writable
        # through any serializer it uses.
        self.client.force_authenticate(self.fixture.staff)

        response = self.client.patch(
            factories.CallFactory.get_public_url(self.call),
            {"image": dummy_image()},
            format="multipart",
        )

        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)


class CallImageCopyTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.call.add_user(self.fixture.call_manager, CallRole.MANAGER)
        set_logo(self.call)

    def test_duplicate_keeps_logo(self):
        new_call = utils.duplicate_call(
            self.call, "Copy of call", created_by=self.fixture.staff
        )

        new_call.refresh_from_db()
        self.assertEqual(new_call.image.name, self.call.image.name)

    def test_duplicate_without_logo_has_none(self):
        self.call.image = None
        self.call.save()

        new_call = utils.duplicate_call(
            self.call, "Copy of call", created_by=self.fixture.staff
        )

        new_call.refresh_from_db()
        self.assertFalse(new_call.image)

    def test_export_leaves_logo_out(self):
        document, _ = call_transfer.export_call(self.call)

        self.assertNotIn("image", document["call"])

    def test_import_ignores_logo_path(self):
        # A document must not be able to point a call at a stored file.
        document, _ = call_transfer.export_call(self.call)
        document["call"]["image"] = self.call.image.name

        new_call, _, warnings = call_transfer.import_call(
            document, self.call.manager, self.fixture.staff
        )

        self.assertFalse(models.Call.objects.get(pk=new_call.pk).image)
        self.assertTrue(any("'image'" in warning for warning in warnings))
