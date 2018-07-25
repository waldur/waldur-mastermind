from __future__ import unicode_literals

import factory
from django.urls import reverse

from waldur_core.structure.tests import factories as structure_factories

from .. import models


class DeploymentPlanFactory(factory.DjangoModelFactory):
    class Meta(object):
        model = models.DeploymentPlan

    name = factory.Sequence(lambda n: 'plan%s' % n)
    project = factory.SubFactory(structure_factories.ProjectFactory)

    @classmethod
    def get_list_url(cls):
        return 'http://testserver' + reverse('deployment-plan-list')

    @classmethod
    def get_url(cls, obj, action=None):
        url = 'http://testserver' + reverse('deployment-plan-detail', kwargs={'uuid': obj.uuid.hex})
        return url if not action else url + action + '/'


class CategoryFactory(factory.DjangoModelFactory):
    class Meta(object):
        model = models.Category

    name = factory.Sequence(lambda n: 'category%s' % n)


class PresetFactory(factory.DjangoModelFactory):
    class Meta(object):
        model = models.Preset

    name = factory.Sequence(lambda n: 'preset%s' % n)
    category = factory.SubFactory(CategoryFactory)

    @classmethod
    def get_url(cls, obj=None):
        if obj is None:
            obj = PresetFactory()
        return 'http://testserver' + reverse('deployment-preset-detail', kwargs={'uuid': obj.uuid.hex})


class DeploymentPlanItemFactory(factory.DjangoModelFactory):
    class Meta(object):
        model = models.DeploymentPlanItem

    plan = factory.SubFactory(DeploymentPlanFactory)
    preset = factory.SubFactory(PresetFactory)
    quantity = 1
