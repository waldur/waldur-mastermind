from decimal import Decimal

import factory
from django.contrib.contenttypes.models import ContentType
from django.db.models import signals
from django.utils import timezone
from rest_framework.reverse import reverse

from waldur_core.core import utils as core_utils
from waldur_core.core.tests.types import BaseMetaFactory
from waldur_core.permissions.fixtures import ProjectRole
from waldur_core.structure.enums import ProjectKind
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.common.mixins import UnitPriceMixin
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.enums import (
    SUPPORT_OFFERING,
    BillingTypes,
    ImpactLevel,
    OfferingStates,
    ResourceStates,
)
from waldur_pid import models as pid_models

OFFERING_OPTIONS = {
    "order": ["storage", "ram", "cpu_count"],
    "options": {
        "storage": {
            "type": "integer",
            "label": "Max storage, GB",
            "required": True,
            "help_text": "VPC storage limit in GB.",
        },
        "ram": {
            "type": "integer",
            "label": "Max RAM, GB",
            "required": True,
            "help_text": "VPC RAM limit in GB.",
        },
        "cpu_count": {
            "type": "integer",
            "label": "Max vCPU",
            "required": True,
            "help_text": "VPC CPU count limit.",
        },
    },
}


def backend_metadata_generator(number):
    return {
        "internal_ips": [f"10.40.1.{number}", f"10.40.2.{number}"],
        "external_ips": [f"193.40.1.{number}", f"193.40.2.{number}"],
    }


class ServiceProviderFactory(
    factory.django.DjangoModelFactory, metaclass=BaseMetaFactory[models.ServiceProvider]
):
    class Meta:
        model = models.ServiceProvider

    customer = factory.SubFactory(structure_factories.CustomerFactory)

    @classmethod
    def get_url(cls, service_provider=None, action=None):
        if service_provider is None:
            service_provider = ServiceProviderFactory()
        url = "http://testserver" + reverse(
            "marketplace-service-provider-detail",
            kwargs={"uuid": service_provider.uuid.hex},
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls, action=None):
        url = "http://testserver" + reverse("marketplace-service-provider-list")
        return url if action is None else url + action + "/"

    @classmethod
    def get_compliance_url(cls, service_provider, action):
        """Get service provider compliance URL."""
        url_name = f"service-provider-compliance-{action}"
        return reverse(
            url_name, kwargs={"service_provider_uuid": service_provider.uuid.hex}
        )


class CategoryFactory(
    factory.django.DjangoModelFactory, metaclass=BaseMetaFactory[models.Category]
):
    class Meta:
        model = models.Category

    title = factory.Sequence(lambda n: "category-%s" % n)

    @classmethod
    def get_url(cls, category=None, action=None):
        if category is None:
            category = CategoryFactory()
        url = "http://testserver" + reverse(
            "marketplace-category-detail", kwargs={"uuid": category.uuid.hex}
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls, action=None):
        url = "http://testserver" + reverse("marketplace-category-list")
        return url if action is None else url + action + "/"


class CategoryColumnFactory(
    factory.django.DjangoModelFactory, metaclass=BaseMetaFactory[models.CategoryColumn]
):
    class Meta:
        model = models.CategoryColumn

    category = factory.SubFactory(CategoryFactory)
    title = factory.Sequence(lambda n: "category-column-%s" % n)
    index = factory.Sequence(lambda n: n)
    attribute = factory.Sequence(lambda n: "attribute-%s" % n)

    @classmethod
    def get_url(cls, column=None, action=None):
        if column is None:
            column = CategoryColumnFactory()
        url = "http://testserver" + reverse(
            "marketplace-category-columns-detail",
            kwargs={"uuid": column.uuid.hex},
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls, action=None):
        url = "http://testserver" + reverse("marketplace-category-columns-list")
        return url if action is None else url + action + "/"


class CategoryGroupFactory(
    factory.django.DjangoModelFactory, metaclass=BaseMetaFactory[models.CategoryGroup]
):
    class Meta:
        model = models.CategoryGroup

    title = factory.Sequence(lambda n: "category-group-%s" % n)

    @classmethod
    def get_url(cls, group=None, action=None):
        if group is None:
            group = CategoryGroupFactory()
        url = "http://testserver" + reverse(
            "marketplace-category-group-detail", kwargs={"uuid": group.uuid.hex}
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls, action=None):
        url = "http://testserver" + reverse("marketplace-category-group-list")
        return url if action is None else url + action + "/"


class TagFactory(
    factory.django.DjangoModelFactory, metaclass=BaseMetaFactory[models.Tag]
):
    class Meta:
        model = models.Tag

    name = factory.Sequence(lambda n: "tag-%s" % n)

    @classmethod
    def get_url(cls, tag=None, action=None):
        if tag is None:
            tag = TagFactory()
        url = "http://testserver" + reverse(
            "marketplace-tag-detail", kwargs={"uuid": tag.uuid.hex}
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls, action=None):
        url = "http://testserver" + reverse("marketplace-tag-list")
        return url if action is None else url + action + "/"


class CategoryComponentFactory(
    factory.django.DjangoModelFactory,
    metaclass=BaseMetaFactory[models.CategoryComponent],
):
    class Meta:
        model = models.CategoryComponent

    category = factory.SubFactory(CategoryFactory)
    name = factory.Sequence(lambda n: "component-%s" % n)
    type = factory.Sequence(lambda n: "component-%s" % n)


class OfferingFactory(
    factory.django.DjangoModelFactory, metaclass=BaseMetaFactory[models.Offering]
):
    class Meta:
        model = models.Offering

    name = factory.Sequence(lambda n: "offering-%s" % n)
    slug = factory.Sequence(lambda n: "offer-%s" % n)
    options = factory.LazyAttribute(lambda _: {"order": [], "options": {}})
    category = factory.SubFactory(CategoryFactory)
    customer = factory.SubFactory(structure_factories.CustomerFactory)
    type = SUPPORT_OFFERING
    state = OfferingStates.ACTIVE

    @classmethod
    def get_url(cls, offering=None, action=None):
        if offering is None:
            offering = OfferingFactory()
        url = "http://testserver" + reverse(
            "marketplace-provider-offering-detail", kwargs={"uuid": offering.uuid.hex}
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_public_url(cls, offering=None, action=None):
        if offering is None:
            offering = OfferingFactory()
        url = "http://testserver" + reverse(
            "marketplace-public-offering-detail", kwargs={"uuid": offering.uuid.hex}
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls, action=None):
        url = "http://testserver" + reverse("marketplace-provider-offering-list")
        return url if action is None else url + action + "/"

    @classmethod
    def get_public_list_url(cls, action=None):
        url = "http://testserver" + reverse("marketplace-public-offering-list")
        return url if action is None else url + action + "/"

    @classmethod
    def get_referral_list(cls, offering):
        return (
            "http://testserver"
            + reverse("marketplace-offering-referral-list")
            + "?offering_uuid=%s" % offering.uuid.hex
        )


class ReferralFactory(
    factory.django.DjangoModelFactory,
    metaclass=BaseMetaFactory[pid_models.DataciteReferral],
):
    class Meta:
        model = pid_models.DataciteReferral
        exclude = ["scope"]

    object_id = factory.SelfAttribute("scope.id")
    content_type = factory.LazyAttribute(
        lambda o: ContentType.objects.get_for_model(o.scope)
    )

    pid = factory.Sequence(lambda n: "pid-%s" % n)
    relation_type = factory.Sequence(lambda n: "reltype-%s" % n)
    resource_type = factory.Sequence(lambda n: "restypee-%s" % n)
    creator = factory.Sequence(lambda n: "creator-%s" % n)
    publisher = factory.Sequence(lambda n: "publisher-%s" % n)
    title = factory.Sequence(lambda n: "title-%s" % n)
    published = factory.Sequence(lambda n: "published-%s" % n)
    referral_url = factory.Sequence(lambda n: "url-%s" % n)

    @classmethod
    def get_url(cls, offering_referral=None, action=None):
        if offering_referral is None:
            offering_referral = OfferingReferralFactory()
        url = "http://testserver" + reverse(
            "marketplace-offering-referral-detail",
            kwargs={"uuid": offering_referral.uuid.hex},
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls, action=None):
        url = "http://testserver" + reverse("marketplace-offering-referral-list")
        return url if action is None else url + action + "/"


class OfferingReferralFactory(ReferralFactory):
    scope = factory.SubFactory(OfferingFactory)

    class Meta:
        model = pid_models.DataciteReferral


class SectionFactory(
    factory.django.DjangoModelFactory, metaclass=BaseMetaFactory[models.Section]
):
    class Meta:
        model = models.Section

    key = factory.Sequence(lambda n: "section-key-%s" % n)
    title = factory.Sequence(lambda n: "section-title-%s" % n)
    category = factory.SubFactory(CategoryFactory)

    @classmethod
    def get_url(cls, section=None):
        if section is None:
            section = SectionFactory()
        return "http://testserver" + reverse(
            "marketplace-section-detail", kwargs={"key": section.key}
        )

    @classmethod
    def get_list_url(cls):
        return "http://testserver" + reverse("marketplace-section-list")


class AttributeFactory(
    factory.django.DjangoModelFactory, metaclass=BaseMetaFactory[models.Attribute]
):
    class Meta:
        model = models.Attribute

    key = factory.Sequence(lambda n: "attribute-%s" % n)
    section = factory.SubFactory(SectionFactory)
    type = "string"

    @classmethod
    def get_url(cls, attribute=None):
        if attribute is None:
            attribute = AttributeFactory()
        return "http://testserver" + reverse(
            "marketplace-attribute-detail", kwargs={"uuid": attribute.uuid.hex}
        )

    @classmethod
    def get_list_url(cls):
        return "http://testserver" + reverse("marketplace-attribute-list")


class AttributeOptionFactory(
    factory.django.DjangoModelFactory,
    metaclass=BaseMetaFactory[models.AttributeOption],
):
    class Meta:
        model = models.AttributeOption

    key = factory.Sequence(lambda n: "option-%s" % n)
    title = factory.Sequence(lambda n: "Option %s" % n)
    attribute = factory.SubFactory(
        AttributeFactory,
        type="choice",
    )

    @classmethod
    def get_url(cls, option=None):
        if option is None:
            option = AttributeOptionFactory()
        return "http://testserver" + reverse(
            "marketplace-attribute-option-detail", kwargs={"uuid": option.uuid.hex}
        )

    @classmethod
    def get_list_url(cls):
        return "http://testserver" + reverse("marketplace-attribute-option-list")


@factory.django.mute_signals(signals.pre_save, signals.post_save)
class ScreenshotFactory(
    factory.django.DjangoModelFactory, metaclass=BaseMetaFactory[models.Screenshot]
):
    class Meta:
        model = models.Screenshot

    name = factory.Sequence(lambda n: "screenshot-%s" % n)
    image = factory.django.ImageField()
    offering = factory.SubFactory(OfferingFactory)

    @classmethod
    def get_url(cls, screenshot=None, action=None):
        if screenshot is None:
            screenshot = ScreenshotFactory()
        url = "http://testserver" + reverse(
            "marketplace-screenshot-detail", kwargs={"uuid": screenshot.uuid.hex}
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls, action=None):
        url = "http://testserver" + reverse("marketplace-screenshot-list")
        return url if action is None else url + action + "/"


class PlanFactory(
    factory.django.DjangoModelFactory, metaclass=BaseMetaFactory[models.Plan]
):
    class Meta:
        model = models.Plan

    offering = factory.SubFactory(OfferingFactory)
    name = factory.Sequence(lambda n: "plan-%s" % n)
    unit = UnitPriceMixin.Units.QUANTITY

    @classmethod
    def get_url(cls, plan=None, action=None):
        if plan is None:
            plan = PlanFactory()
        url = "http://testserver" + reverse(
            "marketplace-plan-detail", kwargs={"uuid": plan.uuid.hex}
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_public_url(cls, plan=None):
        if plan is None:
            plan = PlanFactory()
        url = reverse(
            "marketplace-public-offering-plan-detail",
            kwargs={"uuid": plan.offering.uuid.hex, "plan_uuid": plan.uuid.hex},
        )
        return url

    @classmethod
    def get_provider_list_url(cls, action=None):
        url = "http://testserver" + reverse("marketplace-plan-list")
        return url if action is None else url + action + "/"


class OfferingComponentFactory(
    factory.django.DjangoModelFactory,
    metaclass=BaseMetaFactory[models.OfferingComponent],
):
    class Meta:
        model = models.OfferingComponent

    offering = factory.SubFactory(OfferingFactory)
    type = "cpu"
    name = "CPU"
    billing_type = BillingTypes.FIXED


class PlanComponentFactory(
    factory.django.DjangoModelFactory, metaclass=BaseMetaFactory[models.PlanComponent]
):
    class Meta:
        model = models.PlanComponent

    plan = factory.SubFactory(PlanFactory)
    component = factory.SubFactory(OfferingComponentFactory)
    price = Decimal(10)
    amount = 1

    @classmethod
    def get_list_url(cls, action=None):
        url = "http://testserver" + reverse("marketplace-plan-component-list")
        return url if action is None else url + action + "/"


class OrderFactory(
    factory.django.DjangoModelFactory, metaclass=BaseMetaFactory[models.Order]
):
    class Meta:
        model = models.Order
        rename = {"order_attributes": "attributes"}

    project = factory.SubFactory(structure_factories.ProjectFactory)
    created_by = factory.SubFactory(structure_factories.UserFactory)
    offering = factory.SubFactory(OfferingFactory)
    plan = factory.SubFactory(PlanFactory)
    slug = factory.Sequence(lambda n: "order-%s" % n)

    @factory.lazy_attribute
    def resource(self) -> models.Resource:
        attributes = getattr(self, "attributes", {})
        resource = ResourceFactory(
            project=self.project,
            offering=self.offering,
            plan=self.plan,
            attributes=attributes,
            name=attributes.get("name", "test-resource"),
        )
        resource.init_cost()
        resource.save()
        return resource

    @classmethod
    def get_url(cls, order=None, action=None):
        if order is None:
            order = OrderFactory()
        url = "http://testserver" + reverse(
            "marketplace-order-detail", kwargs={"uuid": order.uuid.hex}
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls, action=None):
        url = "http://testserver" + reverse("marketplace-order-list")
        return url if action is None else url + action + "/"


class ResourceFactory(
    factory.django.DjangoModelFactory, metaclass=BaseMetaFactory[models.Resource]
):
    class Meta:
        model = models.Resource

    offering = factory.SubFactory(OfferingFactory)
    plan = factory.LazyAttribute(lambda o: PlanFactory(offering=o.offering))
    project = factory.SubFactory(structure_factories.ProjectFactory)
    backend_metadata = factory.Sequence(backend_metadata_generator)
    name = factory.Sequence(lambda n: "resource-%s" % n)
    state = ResourceStates.CREATING
    limits = {"storage": 123}

    @classmethod
    def get_url(cls, resource=None, action=None):
        if resource is None:
            resource = ResourceFactory()
        url = reverse("marketplace-resource-detail", kwargs={"uuid": resource.uuid.hex})
        return url if action is None else url + action + "/"

    @classmethod
    def get_provider_resource_url(cls, resource=None, action=None):
        if resource is None:
            resource = ResourceFactory()
        url = reverse(
            "marketplace-provider-resource-detail", kwargs={"uuid": resource.uuid.hex}
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls, action=None):
        url = reverse("marketplace-resource-list")
        return url if action is None else url + action + "/"

    @classmethod
    def get_provider_resource_list_url(cls, action=None):
        url = reverse("marketplace-provider-resource-list")
        return url if action is None else url + action + "/"


class OfferingFileFactory(
    factory.django.DjangoModelFactory, metaclass=BaseMetaFactory[models.OfferingFile]
):
    class Meta:
        model = models.OfferingFile

    name = factory.Sequence(lambda n: "offering-file-%s" % n)
    file = factory.django.FileField()
    offering = factory.SubFactory(OfferingFactory)

    @classmethod
    def get_url(cls, offering_file=None, action=None):
        if offering_file is None:
            offering_file = OfferingFileFactory()
        url = "http://testserver" + reverse(
            "marketplace-offering-file-detail", kwargs={"uuid": offering_file.uuid.hex}
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls, action=None):
        url = "http://testserver" + reverse("marketplace-offering-file-list")
        return url if action is None else url + action + "/"


class ComponentUsageFactory(
    factory.django.DjangoModelFactory, metaclass=BaseMetaFactory[models.ComponentUsage]
):
    class Meta:
        model = models.ComponentUsage

    resource = factory.SubFactory(ResourceFactory)
    component = factory.SubFactory(OfferingComponentFactory)
    usage = 1
    date = timezone.now()
    billing_period = core_utils.month_start(timezone.now())
    plan_period = factory.LazyAttribute(
        lambda o: ResourcePlanPeriodFactory(
            resource=o.resource,
            plan=o.resource.plan,
            start=core_utils.month_start(timezone.now()),
        )
    )

    @classmethod
    def get_list_url(cls, action=None):
        url = "http://testserver" + reverse("marketplace-component-usage-list")
        return url if action is None else url + action + "/"


class ComponentUsageMonthlyFactory(
    factory.django.DjangoModelFactory,
    metaclass=BaseMetaFactory[models.ComponentUsageMonthly],
):
    class Meta:
        model = models.ComponentUsageMonthly

    component = factory.SubFactory(OfferingComponentFactory)
    billing_period = factory.LazyFunction(
        lambda: core_utils.month_start(timezone.now()).date()
    )
    total_consumed = Decimal("0")
    total_allocated = Decimal("0")


class ResourcePlanPeriodFactory(
    factory.django.DjangoModelFactory,
    metaclass=BaseMetaFactory[models.ResourcePlanPeriod],
):
    class Meta:
        model = models.ResourcePlanPeriod

    resource = factory.SubFactory(ResourceFactory)
    plan = factory.SubFactory(PlanFactory)
    start = core_utils.month_start(timezone.now())


class RobotAccountFactory(
    factory.django.DjangoModelFactory, metaclass=BaseMetaFactory[models.RobotAccount]
):
    class Meta:
        model = models.RobotAccount

    resource = factory.SubFactory(ResourceFactory)
    type = "cicd"
    username = "waldur"

    @classmethod
    def get_url(cls, account=None):
        if account is None:
            account = RobotAccountFactory()
        return "http://testserver" + reverse(
            "marketplace-robot-account-detail", kwargs={"uuid": account.uuid.hex}
        )

    @classmethod
    def get_list_url(cls):
        return "http://testserver" + reverse("marketplace-robot-account-list")


class ProjectServiceAccountFactory(
    factory.django.DjangoModelFactory,
    metaclass=BaseMetaFactory[models.ProjectServiceAccount],
):
    class Meta:
        model = models.ProjectServiceAccount

    project = factory.SubFactory(structure_factories.ProjectFactory)
    username = "waldur"

    @classmethod
    def get_url(cls, account=None):
        if account is None:
            account = ProjectServiceAccountFactory()
        return "http://testserver" + reverse(
            "marketplace-project-service-account-detail",
            kwargs={"uuid": account.uuid.hex},
        )

    @classmethod
    def get_list_url(cls):
        return "http://testserver" + reverse("marketplace-project-service-account-list")


class CustomerServiceAccountFactory(
    factory.django.DjangoModelFactory,
    metaclass=BaseMetaFactory[models.CustomerServiceAccount],
):
    customer = factory.SubFactory(structure_factories.CustomerFactory)
    username = "waldur"

    class Meta:
        model = models.CustomerServiceAccount

    @classmethod
    def get_url(cls, account=None):
        if account is None:
            account = CustomerServiceAccountFactory()
        return "http://testserver" + reverse(
            "marketplace-customer-service-account-detail",
            kwargs={"uuid": account.uuid.hex},
        )

    @classmethod
    def get_list_url(cls):
        return "http://testserver" + reverse(
            "marketplace-customer-service-account-list"
        )


class IntegrationStatusFactory(
    factory.django.DjangoModelFactory,
    metaclass=BaseMetaFactory[models.IntegrationStatus],
):
    class Meta:
        model = models.IntegrationStatus

    @classmethod
    def get_url(cls, integration_status=None, action=None):
        if integration_status is None:
            integration_status = IntegrationStatusFactory()
        url = "http://testserver" + reverse(
            "marketplace-integration-status-detail",
            kwargs={"uuid": integration_status.uuid.hex},
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls, action=None):
        url = "http://testserver" + reverse("marketplace-integration-status-list")
        return url if action is None else url + action + "/"


class OfferingUserFactory(
    factory.django.DjangoModelFactory, metaclass=BaseMetaFactory[models.OfferingUser]
):
    offering = factory.SubFactory(OfferingFactory)
    user = factory.SubFactory(structure_factories.UserFactory)
    username = factory.Sequence(lambda n: "username-%s" % n)

    class Meta:
        model = models.OfferingUser

    @classmethod
    def get_list_url(cls):
        """Get the offering user list URL."""
        return reverse("marketplace-offering-user-list")

    @classmethod
    def get_url(cls, offering_user=None, action=None):
        """Get offering user detail or action URL."""
        if offering_user is None:
            offering_user = OfferingUserFactory()

        base_name = "marketplace-offering-user"
        if action:
            # For specific actions, use the action-specific URL name
            url_name = f"{base_name}-{action}"
        else:
            url_name = f"{base_name}-detail"

        return "http://testserver" + reverse(
            url_name, kwargs={"uuid": offering_user.uuid.hex}
        )


class ComponentUserUsageLimitFactory(
    factory.django.DjangoModelFactory,
    metaclass=BaseMetaFactory[models.ComponentUserUsageLimit],
):
    resource = factory.SubFactory(ResourceFactory)
    component = factory.LazyAttribute(lambda o: o.resource.offering.components.first())
    user = factory.SubFactory(OfferingUserFactory)
    limit = 100

    class Meta:
        model = models.ComponentUserUsageLimit
        skip_postgeneration_save = True

    @classmethod
    def get_url(cls, integration_status=None, action=None):
        if integration_status is None:
            # TODO: fix typo
            integration_status = IntegrationStatusFactory()
        url = "http://testserver" + reverse(
            "component-user-usage-limit-detail",
            kwargs={"uuid": integration_status.uuid.hex},
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls, action=None):
        url = "http://testserver" + reverse("component-user-usage-limit-list")
        return url if action is None else url + action + "/"

    @factory.post_generation
    def add_user_to_project(self, create, extracted, **kwargs):
        if not create:
            return

        self.resource.project.add_user(self.user.user, ProjectRole.ADMIN)


class BackendResourceFactory(
    factory.django.DjangoModelFactory,
    metaclass=BaseMetaFactory[models.BackendResource],
):
    project = factory.SubFactory(structure_factories.ProjectFactory)
    offering = factory.SubFactory(OfferingFactory)
    name = factory.Sequence(lambda n: "importable-resource-%s" % n)
    backend_id = factory.Sequence(lambda n: "backend-id-%s" % n)
    backend_metadata = {}

    class Meta:
        model = models.BackendResource

    @classmethod
    def get_url(cls, backend_resource=None, action=None):
        if backend_resource is None:
            backend_resource = BackendResourceFactory()
        url = "http://testserver" + reverse(
            "backend-resource-detail",
            kwargs={"uuid": backend_resource.uuid.hex},
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls, action=None):
        url = "http://testserver" + reverse("backend-resource-list")
        return url if action is None else url + action + "/"


class BackendResourceRequestFactory(
    factory.django.DjangoModelFactory,
    metaclass=BaseMetaFactory[models.BackendResourceRequest],
):
    offering = factory.SubFactory(OfferingFactory)

    class Meta:
        model = models.BackendResourceRequest

    @classmethod
    def get_url(cls, backend_resource_request=None, action=None):
        if backend_resource_request is None:
            backend_resource_request = BackendResourceFactory()
        url = "http://testserver" + reverse(
            "backend-resource-request-detail",
            kwargs={"uuid": backend_resource_request.uuid.hex},
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls, action=None):
        url = "http://testserver" + reverse("backend-resource-request-list")
        return url if action is None else url + action + "/"


class MaintenanceAnnouncementFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = models.MaintenanceAnnouncement

    name = factory.Sequence(lambda n: f"Maintenance {n}")
    message = factory.Sequence(lambda n: f"Maintenance message {n}")
    scheduled_start = factory.LazyFunction(timezone.now)
    scheduled_end = factory.LazyAttribute(
        lambda o: o.scheduled_start + timezone.timedelta(hours=2)
    )
    service_provider = factory.SubFactory(ServiceProviderFactory)
    created_by = factory.SubFactory(structure_factories.UserFactory)

    @classmethod
    def get_url(cls, announcement=None, action=None):
        if announcement is None:
            announcement = MaintenanceAnnouncementFactory()
        url = "http://testserver" + reverse(
            "maintenance-announcement-detail",
            kwargs={"uuid": announcement.uuid.hex},
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls, action=None):
        url = "http://testserver" + reverse("maintenance-announcement-list")
        return url if action is None else url + action + "/"


class MaintenanceAnnouncementOfferingFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = models.MaintenanceAnnouncementOffering

    maintenance = factory.SubFactory(MaintenanceAnnouncementFactory)
    offering = factory.SubFactory(OfferingFactory)
    impact_level = ImpactLevel.DEGRADED_PERFORMANCE
    impact_description = factory.Sequence(lambda n: f"Impact description {n}")

    @classmethod
    def get_url(cls, obj=None, action=None):
        if obj is None:
            obj = MaintenanceAnnouncementOfferingFactory()
        url = "http://testserver" + reverse(
            "maintenance-announcement-offering-detail",
            kwargs={"uuid": obj.uuid.hex},
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls, action=None):
        url = "http://testserver" + reverse("maintenance-announcement-offering-list")
        return url if action is None else url + action + "/"


class MaintenanceAnnouncementTemplateFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = models.MaintenanceAnnouncementTemplate

    name = factory.Sequence(lambda n: f"Maintenance template {n}")
    message = factory.Sequence(lambda n: f"Maintenance template message {n}")
    service_provider = factory.SubFactory(ServiceProviderFactory)

    @classmethod
    def get_url(cls, template=None, action=None):
        if template is None:
            template = MaintenanceAnnouncementTemplateFactory()
        url = "http://testserver" + reverse(
            "maintenance-announcement-template-detail",
            kwargs={"uuid": template.uuid.hex},
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls, action=None):
        url = "http://testserver" + reverse("maintenance-announcement-template-list")
        return url if action is None else url + action + "/"


class MaintenanceAnnouncementOfferingTemplateFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = models.MaintenanceAnnouncementOfferingTemplate

    maintenance_template = factory.SubFactory(MaintenanceAnnouncementTemplateFactory)
    offering = factory.SubFactory(OfferingFactory)
    impact_level = ImpactLevel.DEGRADED_PERFORMANCE
    impact_description = factory.Sequence(lambda n: f"Impact template description {n}")

    @classmethod
    def get_url(cls, obj=None, action=None):
        if obj is None:
            obj = MaintenanceAnnouncementOfferingTemplateFactory()
        url = "http://testserver" + reverse(
            "maintenance-announcement-template-offering-detail",
            kwargs={"uuid": obj.uuid.hex},
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls, action=None):
        url = "http://testserver" + reverse(
            "maintenance-announcement-template-offering-list"
        )
        return url if action is None else url + action + "/"


class CourseAccountFactory(
    factory.django.DjangoModelFactory,
    metaclass=BaseMetaFactory[models.CourseAccount],
):
    class Meta:
        model = models.CourseAccount

    project = factory.SubFactory(
        structure_factories.ProjectFactory, kind=ProjectKind.COURSE
    )
    user = factory.SubFactory(structure_factories.UserFactory)
    email = factory.LazyAttribute(lambda obj: f"{obj.user.username}@example.com")
    description = factory.Sequence(lambda n: f"Course account {n}")

    @classmethod
    def get_url(cls, account=None):
        if account is None:
            account = CourseAccountFactory()
        return "http://testserver" + reverse(
            "marketplace-course-account-detail",
            kwargs={"uuid": account.uuid.hex},
        )

    @classmethod
    def get_list_url(cls):
        return "http://testserver" + reverse("marketplace-course-account-list")


class SoftwareCatalogFactory(
    factory.django.DjangoModelFactory, metaclass=BaseMetaFactory[models.SoftwareCatalog]
):
    class Meta:
        model = models.SoftwareCatalog

    name = factory.Sequence(lambda n: f"Software Catalog {n}")
    version = factory.Sequence(lambda n: f"1.{n}")
    source_url = factory.Faker("url")
    description = factory.Faker("text", max_nb_chars=200)

    @classmethod
    def get_url(cls, catalog=None, action=None):
        if catalog is None:
            catalog = SoftwareCatalogFactory()
        url = "http://testserver" + reverse(
            "marketplace-software-catalog-detail",
            kwargs={"uuid": catalog.uuid.hex},
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls, action=None):
        url = "http://testserver" + reverse("marketplace-software-catalog-list")
        return url if action is None else url + action + "/"


class SoftwarePackageFactory(
    factory.django.DjangoModelFactory, metaclass=BaseMetaFactory[models.SoftwarePackage]
):
    class Meta:
        model = models.SoftwarePackage

    catalog = factory.SubFactory(SoftwareCatalogFactory)
    name = factory.Sequence(lambda n: f"Package{n}")
    description = factory.Faker("text", max_nb_chars=200)
    homepage = factory.Faker("url")

    @factory.post_generation
    def parent_softwares(self, create, extracted, **kwargs):
        if not create:
            return
        if extracted:
            self.parent_softwares.set(extracted)

    @factory.post_generation
    def parent_software(self, create, extracted, **kwargs):
        """Backward-compatible hook: accepts a single parent and adds it."""
        if not create:
            return
        if extracted:
            self.parent_softwares.add(extracted)

    @classmethod
    def get_url(cls, package=None, action=None):
        if package is None:
            package = SoftwarePackageFactory()
        url = "http://testserver" + reverse(
            "marketplace-software-package-detail",
            kwargs={"uuid": package.uuid.hex},
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls, action=None):
        url = "http://testserver" + reverse("marketplace-software-package-list")
        return url if action is None else url + action + "/"


class SoftwareVersionFactory(
    factory.django.DjangoModelFactory, metaclass=BaseMetaFactory[models.SoftwareVersion]
):
    class Meta:
        model = models.SoftwareVersion

    package = factory.SubFactory(SoftwarePackageFactory)
    version = factory.Sequence(lambda n: f"1.{n}.0")
    release_date = None

    @classmethod
    def get_url(cls, version=None, action=None):
        if version is None:
            version = SoftwareVersionFactory()
        url = "http://testserver" + reverse(
            "marketplace-software-version-detail",
            kwargs={"uuid": version.uuid.hex},
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls, action=None):
        url = "http://testserver" + reverse("marketplace-software-version-list")
        return url if action is None else url + action + "/"


class SoftwareTargetFactory(
    factory.django.DjangoModelFactory, metaclass=BaseMetaFactory[models.SoftwareTarget]
):
    class Meta:
        model = models.SoftwareTarget

    version = factory.SubFactory(SoftwareVersionFactory)
    target_type = "cpu_architecture"
    target_name = factory.Iterator(["x86_64", "aarch64", "ppc64le"])
    target_subtype = "generic"
    location = factory.LazyAttribute(
        lambda obj: (
            f"/cvmfs/software.eessi.io/versions/2023.06/software/linux/{obj.target_name}/{obj.target_subtype}"
        )
    )
    metadata = factory.LazyAttribute(lambda obj: {"full_arch": obj.target_name})
    gpu_architectures = factory.LazyAttribute(lambda obj: [])

    @classmethod
    def get_url(cls, target=None, action=None):
        if target is None:
            target = SoftwareTargetFactory()
        url = "http://testserver" + reverse(
            "marketplace-software-target-detail",
            kwargs={"uuid": target.uuid.hex},
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls, action=None):
        url = "http://testserver" + reverse("marketplace-software-target-list")
        return url if action is None else url + action + "/"


class OfferingSoftwareCatalogFactory(
    factory.django.DjangoModelFactory,
    metaclass=BaseMetaFactory[models.OfferingSoftwareCatalog],
):
    class Meta:
        model = models.OfferingSoftwareCatalog

    offering = factory.SubFactory(OfferingFactory)
    catalog = factory.SubFactory(SoftwareCatalogFactory)
    enabled_cpu_family = factory.LazyFunction(lambda: ["x86_64", "aarch64"])
    enabled_cpu_microarchitectures = factory.LazyFunction(lambda: ["generic"])
    partition = None  # Optional - can be set when needed

    @classmethod
    def get_url(cls, offering_catalog=None, action=None):
        if offering_catalog is None:
            offering_catalog = OfferingSoftwareCatalogFactory()
        url = "http://testserver" + reverse(
            "marketplace-offering-software-catalog-detail",
            kwargs={"uuid": offering_catalog.uuid.hex},
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls, action=None):
        url = "http://testserver" + reverse(
            "marketplace-offering-software-catalog-list"
        )
        return url if action is None else url + action + "/"


class OfferingPartitionFactory(factory.django.DjangoModelFactory):
    """Factory for OfferingPartition model."""

    class Meta:
        model = models.OfferingPartition

    offering = factory.SubFactory(OfferingFactory)
    partition_name = factory.Sequence(lambda n: f"partition-{n}")

    # Architecture
    cpu_arch = ""
    gpu_arch = ""

    # CPU configuration
    cpu_bind = 1
    def_cpu_per_gpu = 2
    max_cpus_per_node = 64
    max_cpus_per_socket = 32

    # Memory configuration (in MB)
    def_mem_per_cpu = 4096
    def_mem_per_gpu = 16384
    def_mem_per_node = 262144
    max_mem_per_cpu = 8192
    max_mem_per_node = 524288

    # Time limits (in minutes)
    default_time = 60
    max_time = 1440  # 24 hours
    grace_time = 300  # 5 minutes (in seconds)

    # Node configuration
    max_nodes = 100
    min_nodes = 1

    # Resource exclusivity
    exclusive_topo = False
    exclusive_user = False

    # Scheduling configuration
    priority_tier = 50
    qos = "normal"
    req_resv = False

    @classmethod
    def get_url(cls, partition=None, action=None):
        if partition is None:
            partition = OfferingPartitionFactory()
        url = "http://testserver" + reverse(
            "marketplace-offering-partition-detail",
            kwargs={"uuid": partition.uuid.hex},
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls, action=None):
        url = "http://testserver" + reverse("marketplace-offering-partition-list")
        return url if action is None else url + action + "/"
