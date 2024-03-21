import django.contrib.auth
import factory.fuzzy
from rest_framework.authtoken import models as authtoken_models
from rest_framework.reverse import reverse

from waldur_core.core import models as core_models
from waldur_core.core.types import BaseMetaFactory
from waldur_core.core.utils import normalize_unicode
from waldur_core.structure import models

from . import models as test_models
from .apps import TestConfig


class UserFactory(
    factory.django.DjangoModelFactory, metaclass=BaseMetaFactory[core_models.User]
):
    class Meta:
        model = django.contrib.auth.get_user_model()

    username = factory.Sequence(lambda n: "john%s" % n)
    civil_number = factory.Sequence(lambda n: "%08d" % n)
    email = factory.LazyAttribute(lambda o: "%s@example.org" % o.username)
    first_name = factory.Sequence(lambda n: "John%s" % n)
    last_name = factory.Sequence(lambda n: "Doe%s" % n)
    native_name = factory.Sequence(lambda n: "Jöhn Dõe%s" % n)
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
        lambda n: "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQDDURXDP5YhOQUYoDuTxJ84DuzqMJYJqJ8+SZT28"
        "TtLm5yBDRLKAERqtlbH2gkrQ3US58gd2r8H9jAmQOydfvgwauxuJUE4eDpaMWupqquMYsYLB5f+vVGhdZbbzfc6DTQ2rY"
        "dknWoMoArlG7MvRMA/xQ0ye1muTv+mYMipnd7Z+WH0uVArYI9QBpqC/gpZRRIouQ4VIQIVWGoT6M4Kat5ZBXEa9yP+9du"
        "D2C05GX3gumoSAVyAcDHn/xgej9pYRXGha4l+LKkFdGwAoXdV1z79EG1+9ns7wXuqMJFHM2KDpxAizV0GkZcojISvDwuh"
        "vEAFdOJcqjyyH4%010d test" % n
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
    abbreviation = factory.Sequence(lambda n: "Cust%s" % n)
    contact_details = factory.Sequence(lambda n: "contacts %s" % n)

    @classmethod
    def get_url(cls, customer=None, action=None):
        if customer is None:
            customer = CustomerFactory()
        url = "http://testserver" + reverse(
            "customer-detail", kwargs={"uuid": customer.uuid.hex}
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls):
        return "http://testserver" + reverse("customer-list")


class ProjectFactory(
    factory.django.DjangoModelFactory, metaclass=BaseMetaFactory[models.Project]
):
    class Meta:
        model = models.Project

    name = factory.Sequence(lambda n: "Proj%s" % n)
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
    state = core_models.StateMixin.States.OK
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


class OrganizationGroupTypeFactory(
    factory.django.DjangoModelFactory,
    metaclass=BaseMetaFactory[models.OrganizationGroupType],
):
    class Meta:
        model = models.OrganizationGroupType

    name = factory.Sequence(lambda n: "OrganizationGroupType_%s" % n)

    @classmethod
    def get_url(cls, organization_group_type=None, action=None):
        if organization_group_type is None:
            organization_group_type = OrganizationGroupTypeFactory()
        url = "http://testserver" + reverse(
            "organization-group-type-detail",
            kwargs={"uuid": organization_group_type.uuid.hex},
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls):
        return "http://testserver" + reverse("organization-group-type-list")


class OrganizationGroupFactory(
    factory.django.DjangoModelFactory,
    metaclass=BaseMetaFactory[models.OrganizationGroup],
):
    class Meta:
        model = models.OrganizationGroup

    name = factory.Sequence(lambda n: "OrganizationGroup_%s" % n)
    type = factory.SubFactory(OrganizationGroupTypeFactory)

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

    class Meta:
        model = core_models.Notification

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
