from decimal import Decimal

import factory
from django.contrib.contenttypes.models import ContentType
from django.db.models import signals
from django.utils import timezone
from rest_framework.reverse import reverse

from waldur_core.core import utils as core_utils
from waldur_core.core.types import BaseMetaFactory
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.common.mixins import UnitPriceMixin
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace_support import PLUGIN_NAME
from waldur_pid import models as pid_models

OFFERING_OPTIONS = {
    'order': ['storage', 'ram', 'cpu_count'],
    'options': {
        'storage': {
            'type': 'integer',
            'label': 'Max storage, GB',
            'required': True,
            'help_text': 'VPC storage limit in GB.',
        },
        'ram': {
            'type': 'integer',
            'label': 'Max RAM, GB',
            'required': True,
            'help_text': 'VPC RAM limit in GB.',
        },
        'cpu_count': {
            'type': 'integer',
            'label': 'Max vCPU',
            'required': True,
            'help_text': 'VPC CPU count limit.',
        },
    },
}


def backend_metadata_generator(number):
    return {
        'internal_ips': [f'10.40.1.{number}', f'10.40.2.{number}'],
        'external_ips': [f'193.40.1.{number}', f'193.40.2.{number}'],
    }


class ServiceProviderFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = models.ServiceProvider

    customer = factory.SubFactory(structure_factories.CustomerFactory)

    @classmethod
    def get_url(cls, service_provider=None, action=None):
        if service_provider is None:
            service_provider = ServiceProviderFactory()
        url = 'http://testserver' + reverse(
            'marketplace-service-provider-detail',
            kwargs={'uuid': service_provider.uuid.hex},
        )
        return url if action is None else url + action + '/'

    @classmethod
    def get_list_url(cls, action=None):
        url = 'http://testserver' + reverse('marketplace-service-provider-list')
        return url if action is None else url + action + '/'


class CategoryFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = models.Category

    title = factory.Sequence(lambda n: 'category-%s' % n)

    @classmethod
    def get_url(cls, category=None, action=None):
        if category is None:
            category = CategoryFactory()
        url = 'http://testserver' + reverse(
            'marketplace-category-detail', kwargs={'uuid': category.uuid.hex}
        )
        return url if action is None else url + action + '/'

    @classmethod
    def get_list_url(cls, action=None):
        url = 'http://testserver' + reverse('marketplace-category-list')
        return url if action is None else url + action + '/'


class CategoryGroupFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = models.CategoryGroup

    title = factory.Sequence(lambda n: 'category-group-%s' % n)

    @classmethod
    def get_url(cls, group=None, action=None):
        if group is None:
            group = CategoryGroupFactory()
        url = 'http://testserver' + reverse(
            'marketplace-category-group-detail', kwargs={'uuid': group.uuid.hex}
        )
        return url if action is None else url + action + '/'

    @classmethod
    def get_list_url(cls, action=None):
        url = 'http://testserver' + reverse('marketplace-category-group-list')
        return url if action is None else url + action + '/'


class CategoryComponentFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = models.CategoryComponent

    category = factory.SubFactory(CategoryFactory)
    name = factory.Sequence(lambda n: 'component-%s' % n)
    type = factory.Sequence(lambda n: 'component-%s' % n)


class OfferingFactory(
    factory.django.DjangoModelFactory, metaclass=BaseMetaFactory[models.Offering]
):
    class Meta:
        model = models.Offering

    name = factory.Sequence(lambda n: 'offering-%s' % n)
    category = factory.SubFactory(CategoryFactory)
    customer = factory.SubFactory(structure_factories.CustomerFactory)
    type = PLUGIN_NAME
    state = models.Offering.States.ACTIVE

    @classmethod
    def get_url(cls, offering=None, action=None):
        if offering is None:
            offering = OfferingFactory()
        url = 'http://testserver' + reverse(
            'marketplace-provider-offering-detail', kwargs={'uuid': offering.uuid.hex}
        )
        return url if action is None else url + action + '/'

    @classmethod
    def get_public_url(cls, offering=None, action=None):
        if offering is None:
            offering = OfferingFactory()
        url = 'http://testserver' + reverse(
            'marketplace-public-offering-detail', kwargs={'uuid': offering.uuid.hex}
        )
        return url if action is None else url + action + '/'

    @classmethod
    def get_list_url(cls, action=None):
        url = 'http://testserver' + reverse('marketplace-provider-offering-list')
        return url if action is None else url + action + '/'

    @classmethod
    def get_public_list_url(cls, action=None):
        url = 'http://testserver' + reverse('marketplace-public-offering-list')
        return url if action is None else url + action + '/'

    @classmethod
    def get_referral_list(cls, offering):
        return (
            'http://testserver'
            + reverse('marketplace-offering-referral-list')
            + '?offering_uuid=%s' % offering.uuid.hex
        )


class ReferralFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = pid_models.DataciteReferral
        exclude = ['scope']

    object_id = factory.SelfAttribute('scope.id')
    content_type = factory.LazyAttribute(
        lambda o: ContentType.objects.get_for_model(o.scope)
    )

    pid = factory.Sequence(lambda n: 'pid-%s' % n)
    relation_type = factory.Sequence(lambda n: 'reltype-%s' % n)
    resource_type = factory.Sequence(lambda n: 'restypee-%s' % n)
    creator = factory.Sequence(lambda n: 'creator-%s' % n)
    publisher = factory.Sequence(lambda n: 'publisher-%s' % n)
    title = factory.Sequence(lambda n: 'title-%s' % n)
    published = factory.Sequence(lambda n: 'published-%s' % n)
    referral_url = factory.Sequence(lambda n: 'url-%s' % n)

    @classmethod
    def get_url(cls, offering_referral=None, action=None):
        if offering_referral is None:
            offering_referral = OfferingReferralFactory()
        url = 'http://testserver' + reverse(
            'marketplace-offering-referral-detail',
            kwargs={'uuid': offering_referral.uuid.hex},
        )
        return url if action is None else url + action + '/'

    @classmethod
    def get_list_url(cls, action=None):
        url = 'http://testserver' + reverse('marketplace-offering-referral-list')
        return url if action is None else url + action + '/'


class OfferingReferralFactory(ReferralFactory):
    scope = factory.SubFactory(OfferingFactory)

    class Meta:
        model = pid_models.DataciteReferral


class SectionFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = models.Section

    key = factory.Sequence(lambda n: 'section-%s' % n)
    category = factory.SubFactory(CategoryFactory)


class AttributeFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = models.Attribute

    key = factory.Sequence(lambda n: 'attribute-%s' % n)
    section = factory.SubFactory(SectionFactory)


@factory.django.mute_signals(signals.pre_save, signals.post_save)
class ScreenshotFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = models.Screenshot

    name = factory.Sequence(lambda n: 'screenshot-%s' % n)
    image = factory.django.ImageField()
    offering = factory.SubFactory(OfferingFactory)

    @classmethod
    def get_url(cls, screenshot=None, action=None):
        if screenshot is None:
            screenshot = ScreenshotFactory()
        url = 'http://testserver' + reverse(
            'marketplace-screenshot-detail', kwargs={'uuid': screenshot.uuid.hex}
        )
        return url if action is None else url + action + '/'

    @classmethod
    def get_list_url(cls, action=None):
        url = 'http://testserver' + reverse('marketplace-screenshot-list')
        return url if action is None else url + action + '/'


class PlanFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = models.Plan

    offering = factory.SubFactory(OfferingFactory)
    name = factory.Sequence(lambda n: 'plan-%s' % n)
    unit = UnitPriceMixin.Units.QUANTITY

    @classmethod
    def get_url(cls, plan=None, action=None):
        if plan is None:
            plan = PlanFactory()
        url = 'http://testserver' + reverse(
            'marketplace-plan-detail', kwargs={'uuid': plan.uuid.hex}
        )
        return url if action is None else url + action + '/'

    @classmethod
    def get_public_url(cls, plan=None):
        if plan is None:
            plan = PlanFactory()
        url = reverse(
            'marketplace-public-offering-plan-detail',
            kwargs={'uuid': plan.offering.uuid.hex, 'plan_uuid': plan.uuid.hex},
        )
        return url

    @classmethod
    def get_list_url(cls, action=None):
        url = 'http://testserver' + reverse('marketplace-plan-list')
        return url if action is None else url + action + '/'


class OfferingComponentFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = models.OfferingComponent

    offering = factory.SubFactory(OfferingFactory)
    type = 'cpu'
    name = 'CPU'
    billing_type = models.OfferingComponent.BillingTypes.FIXED


class PlanComponentFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = models.PlanComponent

    plan = factory.SubFactory(PlanFactory)
    component = factory.SubFactory(OfferingComponentFactory)
    price = Decimal(10)
    amount = 1

    @classmethod
    def get_list_url(cls, action=None):
        url = 'http://testserver' + reverse('marketplace-plan-component-list')
        return url if action is None else url + action + '/'


class OrderFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = models.Order
        rename = {'order_attributes': 'attributes'}

    project = factory.SubFactory(structure_factories.ProjectFactory)
    created_by = factory.SubFactory(structure_factories.UserFactory)
    offering = factory.SubFactory(OfferingFactory)
    plan = factory.SubFactory(PlanFactory)

    @factory.lazy_attribute
    def resource(self) -> models.Resource:
        attributes = getattr(self, 'attributes', {})
        resource = ResourceFactory(
            project=self.project,
            offering=self.offering,
            plan=self.plan,
            attributes=attributes,
            name=attributes.get('name', 'test-resource'),
        )
        resource.init_cost()
        resource.save()
        return resource

    @classmethod
    def get_url(cls, order=None, action=None):
        if order is None:
            order = OrderFactory()
        url = 'http://testserver' + reverse(
            'marketplace-order-detail', kwargs={'uuid': order.uuid.hex}
        )
        return url if action is None else url + action + '/'

    @classmethod
    def get_list_url(cls, action=None):
        url = 'http://testserver' + reverse('marketplace-order-list')
        return url if action is None else url + action + '/'


class CartItemFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = models.CartItem

    offering = factory.SubFactory(OfferingFactory)
    user = factory.SubFactory(structure_factories.UserFactory)
    project = factory.SubFactory(structure_factories.ProjectFactory)

    @classmethod
    def get_url(cls, item=None):
        if item is None:
            item = CartItemFactory()
        return reverse('marketplace-cart-item-detail', kwargs={'uuid': item.uuid.hex})

    @classmethod
    def get_list_url(cls, action=None):
        url = reverse('marketplace-cart-item-list')
        return url if action is None else url + action + '/'


class ResourceFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = models.Resource

    offering = factory.SubFactory(OfferingFactory)
    project = factory.SubFactory(structure_factories.ProjectFactory)
    backend_metadata = factory.Sequence(backend_metadata_generator)
    name = factory.Sequence(lambda n: 'resource-%s' % n)
    state = models.Resource.States.CREATING

    @classmethod
    def get_url(cls, resource=None, action=None):
        if resource is None:
            resource = ResourceFactory()
        url = reverse('marketplace-resource-detail', kwargs={'uuid': resource.uuid.hex})
        return url if action is None else url + action + '/'

    @classmethod
    def get_list_url(cls, action=None):
        url = reverse('marketplace-resource-list')
        return url if action is None else url + action + '/'


class OfferingFileFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = models.OfferingFile

    name = factory.Sequence(lambda n: 'offering-file-%s' % n)
    file = factory.django.FileField()
    offering = factory.SubFactory(OfferingFactory)

    @classmethod
    def get_url(cls, offering_file=None, action=None):
        if offering_file is None:
            offering_file = OfferingFileFactory()
        url = 'http://testserver' + reverse(
            'marketplace-offering-file-detail', kwargs={'uuid': offering_file.uuid.hex}
        )
        return url if action is None else url + action + '/'

    @classmethod
    def get_list_url(cls, action=None):
        url = 'http://testserver' + reverse('marketplace-offering-file-list')
        return url if action is None else url + action + '/'


class ComponentUsageFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = models.ComponentUsage

    resource = factory.SubFactory(ResourceFactory)
    component = factory.SubFactory(OfferingComponentFactory)
    usage = 1
    date = timezone.now()
    billing_period = core_utils.month_start(timezone.now())


class ResourcePlanPeriodFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = models.ResourcePlanPeriod

    resource = factory.SubFactory(ResourceFactory)
    plan = factory.SubFactory(PlanFactory)
    start = core_utils.month_start(timezone.now())


class RobotAccountFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = models.RobotAccount

    resource = factory.SubFactory(ResourceFactory)
    type = 'cicd'
    username = 'waldur'

    @classmethod
    def get_url(cls, account=None):
        if account is None:
            account = RobotAccountFactory()
        return 'http://testserver' + reverse(
            'marketplace-robot-account-detail', kwargs={'uuid': account.uuid.hex}
        )

    @classmethod
    def get_list_url(cls):
        return 'http://testserver' + reverse('marketplace-robot-account-list')


class SectionFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = models.Section

    key = factory.Sequence(lambda n: 'section-key-%s' % n)
    title = factory.Sequence(lambda n: 'section-title-%s' % n)
    category = factory.SubFactory(CategoryFactory)

    @classmethod
    def get_url(cls, section=None):
        if section is None:
            section = SectionFactory()
        return 'http://testserver' + reverse(
            'marketplace-section-detail', kwargs={'key': section.key}
        )

    @classmethod
    def get_list_url(cls):
        return 'http://testserver' + reverse('marketplace-section-list')
