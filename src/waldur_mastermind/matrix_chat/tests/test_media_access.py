"""Media access rules for Matrix room history exports.

See :mod:`waldur_core.media.access`. Downloads normally go through
``MatrixHistoryExportDownloadView``, which is already gated; this rule covers
the media route itself, with the same policy as the API.

``export_file`` and ``media_file`` share the ``matrix_exports/`` tree and
belong to the same row, so one rule covers both.
"""

from constance.test import override_config
from ddt import data, ddt
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from rest_framework import status, test

from waldur_core.core.models import Token
from waldur_core.media import models as media_models
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.tests.test_pat_list_filtering import (
    _auth_header,
    _create_pat,
)
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
    def test_room_member_cannot_download(self, field_name):
        self.assertEqual(
            self.get_as(self.fixture.matrix_room_member.user, self.url_for(field_name)),
            status.HTTP_404_NOT_FOUND,
        )

    @data(*EXPORT_FIELDS)
    def test_project_manager_cannot_download(self, field_name):
        self.assertEqual(
            self.get_as(self.fixture.manager, self.url_for(field_name)),
            status.HTTP_404_NOT_FOUND,
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

    def get_with_token(self, user, url, scopes, bindings=()):
        user.can_use_personal_access_tokens = True
        user.save(update_fields=["can_use_personal_access_tokens"])
        Token.objects.get_or_create(user=user)
        pat = _create_pat(user, scopes=scopes, bindings=list(bindings))
        self.client.credentials(HTTP_AUTHORIZATION=_auth_header(pat))
        return self.client.get(url).status_code

    @data(*EXPORT_FIELDS)
    @override_config(PAT_ENABLED=True)
    def test_support_token_without_support_scope_cannot_download(self, field_name):
        self.assertEqual(
            self.get_with_token(
                self.fixture.global_support,
                self.url_for(field_name),
                [PermissionEnum.LIST_PROJECTS.value],
            ),
            status.HTTP_404_NOT_FOUND,
        )

    @data(*EXPORT_FIELDS)
    @override_config(PAT_ENABLED=True)
    def test_support_token_with_support_scope_can_download(self, field_name):
        self.assertEqual(
            self.get_with_token(
                self.fixture.global_support,
                self.url_for(field_name),
                [PermissionEnum.SUPPORT_ACCESS.value],
            ),
            status.HTTP_200_OK,
        )

    @data(*EXPORT_FIELDS)
    @override_config(PAT_ENABLED=True)
    def test_owner_token_bound_to_another_project_cannot_download(self, field_name):
        other = structure_factories.ProjectFactory(customer=self.fixture.customer)
        self.assertEqual(
            self.get_with_token(
                self.fixture.owner,
                self.url_for(field_name),
                [PermissionEnum.CREATE_MATRIX_ROOM.value],
                [other],
            ),
            status.HTTP_404_NOT_FOUND,
        )
