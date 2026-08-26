"""Media access rules for Matrix room history exports.

See :mod:`waldur_core.media.access`. Downloads normally go through
``MatrixHistoryExportDownloadView``, which is already gated; this rule covers
the media route itself, mirroring ``MatrixHistoryExportViewSet.get_queryset``.

``export_file`` and ``media_file`` share the ``matrix_exports/`` tree and
belong to the same row, so one rule covers both.
"""

from ddt import data, ddt
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from rest_framework import status, test

from waldur_core.media import models as media_models
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.matrix_chat.tests import fixtures

ZIP = b"PK\x03\x04 export"

EXPORT_FIELDS = ("export_file", "media_file")


@ddt
class MatrixExportMediaAccessTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MatrixChatFixture()
        self.export = self.fixture.history_export

    def url_for(self, field_name):
        setattr(
            self.export,
            field_name,
            SimpleUploadedFile(
                f"{field_name}.zip", ZIP, content_type="application/zip"
            ),
        )
        self.export.save(update_fields=[field_name])
        media_file = media_models.File.objects.get(
            name=getattr(self.export, field_name).name
        )
        return reverse("media", kwargs={"uuid": media_file.uuid})

    def get_as(self, user, url):
        if user is None:
            self.client.logout()
        else:
            self.client.force_authenticate(user)
        return self.client.get(url).status_code

    @data(*EXPORT_FIELDS)
    def test_anonymous_user_cannot_download(self, field_name):
        self.assertEqual(
            self.get_as(None, self.url_for(field_name)), status.HTTP_404_NOT_FOUND
        )

    @data(*EXPORT_FIELDS)
    def test_unrelated_user_cannot_download(self, field_name):
        self.assertEqual(
            self.get_as(structure_factories.UserFactory(), self.url_for(field_name)),
            status.HTTP_404_NOT_FOUND,
        )

    @data(*EXPORT_FIELDS)
    def test_project_member_can_download(self, field_name):
        self.assertEqual(
            self.get_as(self.fixture.manager, self.url_for(field_name)),
            status.HTTP_200_OK,
        )

    @data(*EXPORT_FIELDS)
    def test_customer_owner_can_download(self, field_name):
        self.assertEqual(
            self.get_as(self.fixture.owner, self.url_for(field_name)),
            status.HTTP_200_OK,
        )

    @data(*EXPORT_FIELDS)
    def test_staff_can_download(self, field_name):
        self.assertEqual(
            self.get_as(self.fixture.staff, self.url_for(field_name)),
            status.HTTP_200_OK,
        )

    @data(*EXPORT_FIELDS)
    def test_support_can_download(self, field_name):
        self.assertEqual(
            self.get_as(self.fixture.global_support, self.url_for(field_name)),
            status.HTTP_200_OK,
        )
