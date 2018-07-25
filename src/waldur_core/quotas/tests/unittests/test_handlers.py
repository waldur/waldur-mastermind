from django.test import TestCase

from waldur_core.quotas import models
from waldur_core.structure import models as structure_models
from waldur_core.structure.tests import factories as structure_factories


class GlobalQuotasHandlersTestCase(TestCase):

    def test_project_global_quota_increased_after_project_creation(self):
        quota = models.Quota.objects.get(name=structure_models.Project.GLOBAL_COUNT_QUOTA_NAME)

        structure_factories.ProjectFactory()

        reread_quota = models.Quota.objects.get(pk=quota.pk)
        self.assertEqual(reread_quota.usage, quota.usage + 1)

    def test_project_global_quota_decreased_after_project_deletion(self):
        project = structure_factories.ProjectFactory()
        quota = models.Quota.objects.get(name=structure_models.Project.GLOBAL_COUNT_QUOTA_NAME)

        project.delete()

        reread_quota = models.Quota.objects.get(pk=quota.pk)
        self.assertEqual(reread_quota.usage, quota.usage - 1)
