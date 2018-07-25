from novaclient import exceptions as nova_exceptions

from waldur_core.structure.models import Project
from waldur_core.structure.executors import ProjectCleanupExecutor

from waldur_openstack.openstack.tests.unittests import test_backend

from . import fixtures
from .. import models


class PackageCleanupTest(test_backend.BaseBackendTestCase):
    def setUp(self):
        super(PackageCleanupTest, self).setUp()
        fixture = fixtures.OpenStackTenantFixture()
        self.staff = fixture.staff
        self.project = fixture.project
        self.instance = fixture.instance

    def test_when_project_is_cleaned_all_resources_are_deleted(self):
        self.mocked_nova().servers.get.side_effect = nova_exceptions.NotFound(code=404)

        ProjectCleanupExecutor.execute(self.project, async=False)
        self.assertFalse(Project.objects.filter(id=self.project.id).exists())
        self.assertFalse(models.Instance.objects.filter(id=self.instance.id).exists())
