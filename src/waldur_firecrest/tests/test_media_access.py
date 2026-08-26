"""Media access rules for SLURM batch scripts.

See :mod:`waldur_core.media.access`. Job follows BaseResource.Permissions
(project / project__customer), the same rule ResourceViewSet lists jobs with.
"""

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from rest_framework import status, test

from waldur_core.media import models as media_models
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_firecrest.models import Job

SCRIPT = b"#!/bin/bash\nsrun hostname\n"


class SlurmJobMediaAccessTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.job = Job.objects.create(
            name="job",
            project=self.fixture.project,
            service_settings=structure_factories.ServiceSettingsFactory(),
            file=SimpleUploadedFile("job.sh", SCRIPT, content_type="text/plain"),
        )
        media_file = media_models.File.objects.get(name=self.job.file.name)
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

    def test_project_member_can_download(self):
        self.assertEqual(self.get_as(self.fixture.manager), status.HTTP_200_OK)

    def test_customer_owner_can_download(self):
        self.assertEqual(self.get_as(self.fixture.owner), status.HTTP_200_OK)

    def test_staff_can_download(self):
        self.assertEqual(self.get_as(self.fixture.staff), status.HTTP_200_OK)
