from datetime import timedelta
from urllib.parse import urlencode

import factory.fuzzy
from django.utils import timezone
from rest_framework.authtoken import models as authtoken_models
from rest_framework.reverse import reverse

from waldur_core.core import models as core_models
from waldur_core.core.enums import CoreStates, ReviewStates
from waldur_core.core.tests.types import BaseMetaFactory
from waldur_core.core.utils import normalize_unicode
from waldur_core.structure import models

from . import models as test_models
from .apps import TestConfig


class UserFactory(
    factory.django.DjangoModelFactory, metaclass=BaseMetaFactory[core_models.User]
):
    class Meta:
        model = core_models.User
        skip_postgeneration_save = True

    username = factory.Sequence(lambda n: "john%s" % n)
    civil_number = factory.Sequence(lambda n: "%08d" % n)
    email = factory.LazyAttribute(lambda o: "%s@example.org" % o.username)
    first_name = factory.Sequence(lambda n: "John%03d" % n)
    last_name = factory.Sequence(lambda n: "Doe%03d" % n)
    native_name = factory.Sequence(lambda n: "Jöhn Dõe%03d" % n)
    organization = factory.Sequence(lambda n: "Organization %s" % n)
    phone_number = factory.Sequence(lambda n: "555-555-%s-2" % n)
    description = factory.Sequence(lambda n: "Description %s" % n)
    job_title = factory.Sequence(lambda n: "Job %s" % n)
    is_staff = False
    is_active = True

    @factory.post_generation
    def customers(self, create, extracted, **kwargs):
        if not create:
            return

        if extracted:
            for customer in extracted:
                self.customers.add(customer)

    @factory.post_generation
    def query_field(self, create, extracted, **kwargs):
        self.query_field = normalize_unicode(self.first_name + " " + self.last_name)
        if create:
            self.save(update_fields=["query_field"])

    @classmethod
    def get_url(cls, user=None, action=None):
        if user is None:
            user = UserFactory()
        url = "http://testserver" + reverse(
            "user-detail", kwargs={"uuid": user.uuid.hex}
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_password_url(self, user):
        return (
            "http://testserver"
            + reverse("user-detail", kwargs={"uuid": user.uuid.hex})
            + "password/"
        )

    @classmethod
    def get_list_url(cls, action=None):
        url = "http://testserver" + reverse("user-list")
        return url if action is None else url + action + "/"


class SshPublicKeyFactory(
    factory.django.DjangoModelFactory,
    metaclass=BaseMetaFactory[core_models.SshPublicKey],
):
    class Meta:
        model = core_models.SshPublicKey

    user = factory.SubFactory(UserFactory)
    name = factory.Sequence(lambda n: "eduteams_%s" % n)
    public_key = factory.Sequence(
        lambda n: (
            "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQDDURXDP5YhOQUYoDuTxJ84DuzqMJYJqJ8+SZT28"
            "TtLm5yBDRLKAERqtlbH2gkrQ3US58gd2r8H9jAmQOydfvgwauxuJUE4eDpaMWupqquMYsYLB5f+vVGhdZbbzfc6DTQ2rY"
            "dknWoMoArlG7MvRMA/xQ0ye1muTv+mYMipnd7Z+WH0uVArYI9QBpqC/gpZRRIouQ4VIQIVWGoT6M4Kat5ZBXEa9yP+9du"
            "D2C05GX3gumoSAVyAcDHn/xgej9pYRXGha4l+LKkFdGwAoXdV1z79EG1+9ns7wXuqMJFHM2KDpxAizV0GkZcojISvDwuh"
            "vEAFdOJcqjyyH4%010d test" % n
        )
    )

    @classmethod
    def get_url(cls, key=None):
        if key is None:
            key = SshPublicKeyFactory()
        return "http://testserver" + reverse(
            "sshpublickey-detail", kwargs={"uuid": str(key.uuid)}
        )

    @classmethod
    def get_list_url(cls):
        return "http://testserver" + reverse("sshpublickey-list")


class CustomerFactory(
    factory.django.DjangoModelFactory, metaclass=BaseMetaFactory[models.Customer]
):
    class Meta:
        model = models.Customer

    name = factory.Sequence(lambda n: "Customer%s" % n)
    slug = factory.Sequence(lambda n: "cust-%s" % n)
    abbreviation = factory.Sequence(lambda n: "Cust%s" % n)
    contact_details = factory.Sequence(lambda n: "contacts %s" % n)
    max_service_accounts = 2

    @classmethod
    def get_url(cls, customer=None, action=None):
        if customer is None:
            customer = CustomerFactory()
        url = "http://testserver" + reverse(
            "customer-detail", kwargs={"uuid": customer.uuid.hex}
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls, fields=None):
        url = "http://testserver" + reverse("customer-list")
        if fields is not None:
            query_string = urlencode({"field": fields}, doseq=True)
            url += f"?{query_string}"
        return url


class ProjectFactory(
    factory.django.DjangoModelFactory, metaclass=BaseMetaFactory[models.Project]
):
    class Meta:
        model = models.Project

    max_service_accounts = 2
    name = factory.Sequence(lambda n: "Proj%s" % n)
    slug = factory.Sequence(lambda n: "proj-%s" % n)
    customer = factory.SubFactory(CustomerFactory)

    @classmethod
    def get_url(cls, project=None, action=None):
        if project is None:
            project = ProjectFactory()
        url = "http://testserver" + reverse(
            "project-detail", kwargs={"uuid": project.uuid.hex}
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls):
        return "http://testserver" + reverse("project-list")


class ServiceSettingsFactory(
    factory.django.DjangoModelFactory, metaclass=BaseMetaFactory[models.ServiceSettings]
):
    class Meta:
        model = models.ServiceSettings

    name = factory.Sequence(lambda n: "Settings %s" % n)
    state = CoreStates.OK
    shared = False
    type = TestConfig.service_name

    @classmethod
    def get_url(cls, settings=None, action=None):
        if settings is None:
            settings = ServiceSettingsFactory()
        url = "http://testserver" + reverse(
            "servicesettings-detail", kwargs={"uuid": settings.uuid.hex}
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls):
        return "http://testserver" + reverse("servicesettings-list")


class TestNewInstanceFactory(
    factory.django.DjangoModelFactory,
    metaclass=BaseMetaFactory[test_models.TestNewInstance],
):
    __test__ = False

    class Meta:
        model = test_models.TestNewInstance

    name = factory.Sequence(lambda n: "instance%s" % n)
    service_settings = factory.SubFactory(ServiceSettingsFactory)
    project = factory.SubFactory(ProjectFactory)

    @classmethod
    def get_url(cls, instance=None, action=None):
        if instance is None:
            instance = TestNewInstanceFactory()
        url = "http://testserver" + reverse(
            "test-new-instances-detail", kwargs={"uuid": instance.uuid.hex}
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls):
        return "http://testserver" + reverse("test-new-instances-list")


class TestSubResourceFactory(
    factory.django.DjangoModelFactory,
    metaclass=BaseMetaFactory[test_models.TestSubResource],
):
    class Meta:
        model = test_models.TestSubResource


class TestVolumeFactory(
    factory.django.DjangoModelFactory, metaclass=BaseMetaFactory[test_models.TestVolume]
):
    size = factory.fuzzy.FuzzyInteger(1024, 102400, step=1024)
    service_settings = factory.SubFactory(ServiceSettingsFactory)
    project = factory.SubFactory(ProjectFactory)

    class Meta:
        model = test_models.TestVolume


class TestSnapshotFactory(
    factory.django.DjangoModelFactory,
    metaclass=BaseMetaFactory[test_models.TestSnapshot],
):
    size = factory.fuzzy.FuzzyInteger(1024, 102400, step=1024)
    service_settings = factory.SubFactory(ServiceSettingsFactory)
    project = factory.SubFactory(ProjectFactory)

    class Meta:
        model = test_models.TestSnapshot


class OrganizationGroupFactory(
    factory.django.DjangoModelFactory,
    metaclass=BaseMetaFactory[models.OrganizationGroup],
):
    class Meta:
        model = models.OrganizationGroup

    name = factory.Sequence(lambda n: "OrganizationGroup_%s" % n)

    @classmethod
    def get_url(cls, organization_group=None, action=None):
        if organization_group is None:
            organization_group = OrganizationGroupFactory()
        url = "http://testserver" + reverse(
            "organization-group-detail", kwargs={"uuid": organization_group.uuid.hex}
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls):
        return "http://testserver" + reverse("organization-group-list")


class AffiliatedOrganizationFactory(
    factory.django.DjangoModelFactory,
    metaclass=BaseMetaFactory[models.AffiliatedOrganization],
):
    class Meta:
        model = models.AffiliatedOrganization

    name = factory.Sequence(lambda n: "AffiliatedOrganization_%s" % n)
    code = factory.Sequence(lambda n: "AO%s" % n)
    abbreviation = factory.Sequence(lambda n: "AO%s" % n)

    @classmethod
    def get_url(cls, affiliated_organization=None, action=None):
        if affiliated_organization is None:
            affiliated_organization = AffiliatedOrganizationFactory()
        url = "http://testserver" + reverse(
            "affiliated-organization-detail",
            kwargs={"uuid": affiliated_organization.uuid.hex},
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls):
        return "http://testserver" + reverse("affiliated-organization-list")


class NotificationTemplateFactory(
    factory.django.DjangoModelFactory,
    metaclass=BaseMetaFactory[core_models.NotificationTemplate],
):
    name = factory.Sequence(lambda n: "NotificationTemplate_%s" % n)
    path = factory.Sequence(lambda n: "NotificationTemplate_%s" % n)

    class Meta:
        model = core_models.NotificationTemplate

    @classmethod
    def get_url(cls, notification_template=None, action=None):
        if notification_template is None:
            notification_template = NotificationTemplateFactory()
        url = "http://testserver" + reverse(
            "notification-messages-templates-detail",
            kwargs={"uuid": notification_template.uuid.hex},
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls):
        return "http://testserver" + reverse("notification-messages-templates-list")


class NotificationFactory(
    factory.django.DjangoModelFactory,
    metaclass=BaseMetaFactory[core_models.Notification],
):
    key = factory.Sequence(lambda n: "Notification_%s" % n)
    enabled = True

    class Meta:
        model = core_models.Notification
        skip_postgeneration_save = True

    @classmethod
    def get_url(cls, notification=None, action=None):
        if notification is None:
            notification = NotificationFactory()
        url = "http://testserver" + reverse(
            "notification-messages-detail", kwargs={"uuid": notification.uuid.hex}
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls):
        return "http://testserver" + reverse("notification-messages-list")

    @factory.post_generation
    def templates(self, create, extracted, **kwargs):
        if not create:
            return

        module, event_type = self.key.split(".")
        self.templates.create(path=f"{module}/{event_type}_subject.txt")
        self.templates.create(path=f"{module}/{event_type}_message.txt")
        self.templates.create(path=f"{module}/{event_type}_message.html")


class AuthTokenFactory(
    factory.django.DjangoModelFactory, metaclass=BaseMetaFactory[authtoken_models.Token]
):
    key = factory.Sequence(lambda n: "key_%s" % n)
    user = factory.SubFactory(UserFactory)

    class Meta:
        model = authtoken_models.Token

    @classmethod
    def get_url(cls, token=None, action=None):
        if token is None:
            token = AuthTokenFactory()
        url = "http://testserver" + reverse(
            "auth-tokens-detail",
            kwargs={"user_id": token.user_id},
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls):
        return "http://testserver" + reverse("auth-tokens-list")


class AccessSubnetFactory(
    factory.django.DjangoModelFactory, metaclass=BaseMetaFactory[models.AccessSubnet]
):
    class Meta:
        model = models.AccessSubnet

    customer = factory.SubFactory(CustomerFactory)
    inet = factory.Sequence(lambda n: "192.168.%s.0/24" % n)

    @classmethod
    def get_url(cls, access_subnet=None, action=None):
        if access_subnet is None:
            access_subnet = AccessSubnetFactory()
        url = "http://testserver" + reverse(
            "access-subnets-detail", kwargs={"uuid": access_subnet.uuid.hex}
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls):
        return "http://testserver" + reverse("access-subnets-list")


class ExternalLinkFactory(
    factory.django.DjangoModelFactory, metaclass=BaseMetaFactory[models.ExternalLink]
):
    class Meta:
        model = models.ExternalLink

    name = factory.Sequence(lambda n: "External Link %s" % n)
    link = factory.Sequence(lambda n: "https://example.com/%s" % n)

    @classmethod
    def get_url(cls, external_link=None):
        if external_link is None:
            external_link = ExternalLinkFactory()
        url = "http://testserver" + reverse(
            "external-links-detail", kwargs={"uuid": external_link.uuid.hex}
        )
        return url

    @classmethod
    def get_list_url(cls):
        return "http://testserver" + reverse("external-links-list")


class ProjectPermissionReviewFactory(
    factory.django.DjangoModelFactory,
    metaclass=BaseMetaFactory[models.ProjectPermissionReview],
):
    class Meta:
        model = models.ProjectPermissionReview

    project = factory.SubFactory(ProjectFactory)
    reviewer = factory.SubFactory(UserFactory)
    is_pending = True

    @classmethod
    def get_url(cls, review=None, action=None):
        if review is None:
            review = ProjectPermissionReviewFactory()
        url = "http://testserver" + reverse(
            "project-permissions-review-detail", kwargs={"uuid": review.uuid.hex}
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls):
        return "http://testserver" + reverse("project-permissions-review-list")


class ProjectEndDateChangeRequestFactory(
    factory.django.DjangoModelFactory,
    metaclass=BaseMetaFactory[models.ProjectEndDateChangeRequest],
):
    class Meta:
        model = models.ProjectEndDateChangeRequest

    project = factory.SubFactory(ProjectFactory)
    created_by = factory.SubFactory(UserFactory)
    requested_end_date = factory.LazyFunction(
        lambda: (timezone.now() + timedelta(days=30)).date()
    )
    state = ReviewStates.PENDING

    @classmethod
    def get_url(cls, request=None, action=None):
        if request is None:
            request = ProjectEndDateChangeRequestFactory()
        url = "http://testserver" + reverse(
            "project-end-date-change-request-detail",
            kwargs={"uuid": request.uuid.hex},
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls):
        return "http://testserver" + reverse("project-end-date-change-request-list")


class UserAgreementFactory(
    factory.django.DjangoModelFactory,
    metaclass=BaseMetaFactory[models.UserAgreement],
):
    class Meta:
        model = models.UserAgreement

    content = factory.Sequence(lambda n: "<p>Agreement content %s</p>" % n)
    agreement_type = models.UserAgreement.UserAgreements.TOS
    language = ""

    @classmethod
    def get_url(cls, agreement=None):
        if agreement is None:
            agreement = UserAgreementFactory()
        return "http://testserver" + reverse(
            "user-agreements-detail", kwargs={"uuid": agreement.uuid.hex}
        )

    @classmethod
    def get_list_url(cls, query_params=None):
        url = "http://testserver" + reverse("user-agreements-list")
        if query_params:
            url += "?" + urlencode(query_params)
        return url
