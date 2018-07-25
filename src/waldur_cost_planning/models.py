from __future__ import unicode_literals

import logging

from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.utils.encoding import python_2_unicode_compatible
from django.utils.lru_cache import lru_cache
from model_utils.models import TimeStampedModel

from waldur_core.core import models as core_models
from waldur_core.structure import SupportedServices, models as structure_models


logger = logging.getLogger(__name__)


def get_content_types_query(items):
    content_types = ContentType.objects.get_for_models(*items).values()
    return {'id__in': [ct.id for ct in content_types]}


@lru_cache(maxsize=1)
def get_service_content_types():
    services = [service['service'] for service in SupportedServices.get_service_models().values()]
    return get_content_types_query(services)


@python_2_unicode_compatible
class DeploymentPlan(core_models.UuidMixin, core_models.NameMixin, TimeStampedModel):
    """
    Deployment plan contains list of plan items.
    """
    class Permissions(object):
        customer_path = 'project__customer'
        project_path = 'project'

    class Meta:
        ordering = ['-created']

    project = models.ForeignKey(structure_models.Project, related_name='+')
    certifications = models.ManyToManyField(structure_models.ServiceCertification, blank=True)

    def __str__(self):
        return self.name

    @classmethod
    def get_url_name(cls):
        return 'deployment-plan'

    def get_requirements(self):
        """ Return how many ram, cores and storage are required for plan """
        requirements = {
            'ram': 0,
            'cores': 0,
            'storage': 0,
        }
        for item in self.items.all():
            requirements['ram'] += item.preset.ram * item.quantity
            requirements['cores'] += item.preset.cores * item.quantity
            requirements['storage'] += item.preset.storage * item.quantity
        return requirements

    def get_required_certifications(self):
        return set(list(self.certifications.all()) + list(self.project.certifications.all()))


@python_2_unicode_compatible
class DeploymentPlanItem(models.Model):
    """
    Plan item specifies quantity of presets.

    For example:
    {
        "preset": <Hadoop DataNode>,
        "quantity": 10
    }
    """
    class Meta:
        ordering = 'plan', 'preset'
        unique_together = 'plan', 'preset'

    plan = models.ForeignKey(DeploymentPlan, related_name='items')
    preset = models.ForeignKey('Preset')
    quantity = models.PositiveSmallIntegerField(default=1)

    def __str__(self):
        return '%s %s' % (self.quantity, self.preset)


@python_2_unicode_compatible
class Category(core_models.NameMixin):
    class Meta(object):
        verbose_name_plural = 'Categories'

    def __str__(self):
        return self.name


@python_2_unicode_compatible
class Preset(core_models.UuidMixin, core_models.NameMixin):
    """
    Resource configuration preset.

    Example rendering of preset:
    {
        "category": "Big Data",
        "name": "Hadoop DataNode",
        "variant": "Large",
        "ram": "10240",
        "cores": "16",
        "storage": "1024000",
    }
    """
    class Meta:
        ordering = 'category', 'name', 'variant'
        unique_together = 'category', 'name', 'variant'

    SMALL = 'small'
    MEDIUM = 'medium'
    LARGE = 'large'

    VARIANTS = (
        (SMALL, 'Small'),
        (MEDIUM, 'Medium'),
        (LARGE, 'Large'),
    )

    category = models.ForeignKey(Category, related_name='presets')
    variant = models.CharField(max_length=150, choices=VARIANTS)
    ram = models.PositiveIntegerField(default=0)
    cores = models.PositiveIntegerField(default=0, help_text='Preset cores count.')
    storage = models.PositiveIntegerField(default=0)

    def __str__(self):
        return '%s %s %s' % (self.variant, self.name, self.category)

    @classmethod
    def get_url_name(cls):
        return 'deployment-preset'
