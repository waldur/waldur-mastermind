import factory.fuzzy
from rest_framework.reverse import reverse

from waldur_core.core.types import BaseMetaFactory
from waldur_core.permissions import fixtures as permission_fixtures
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.users import models


class InvitationBaseFactory(factory.django.DjangoModelFactory):
    email = factory.Sequence(lambda n: "test%s@invitation.com" % n)

    @classmethod
    def get_list_url(cls, action=None):
        url = "http://testserver" + reverse("user-invitation-list")
        return url if action is None else url + action + "/"

    @classmethod
    def get_url(cls, invitation=None, action=None):
        if invitation is None:
            invitation = cls()
        url = "http://testserver" + reverse(
            "user-invitation-detail", kwargs={"uuid": invitation.uuid.hex}
        )
        return url if action is None else url + action + "/"


class ProjectInvitationFactory(
    InvitationBaseFactory, metaclass=BaseMetaFactory[models.Invitation]
):
    class Meta:
        model = models.Invitation

    customer = factory.SelfAttribute("scope.customer")
    scope = factory.SubFactory(structure_factories.ProjectFactory)
    role = factory.LazyAttribute(lambda _: permission_fixtures.ProjectRole.MANAGER)


class CustomerInvitationFactory(
    InvitationBaseFactory, metaclass=BaseMetaFactory[models.Invitation]
):
    class Meta:
        model = models.Invitation

    customer = factory.SelfAttribute("scope")
    scope = factory.SubFactory(structure_factories.CustomerFactory)
    role = factory.LazyAttribute(lambda _: permission_fixtures.CustomerRole.OWNER)


class GroupInvitationBaseFactory(factory.django.DjangoModelFactory):
    @classmethod
    def get_list_url(cls, action=None):
        url = "http://testserver" + reverse("user-group-invitation-list")
        return url if action is None else url + action + "/"

    @classmethod
    def get_url(cls, invitation=None, action=None):
        if invitation is None:
            invitation = cls()
        url = "http://testserver" + reverse(
            "user-group-invitation-detail", kwargs={"uuid": invitation.uuid.hex}
        )
        return url if action is None else url + action + "/"


class ProjectGroupInvitationFactory(
    GroupInvitationBaseFactory, metaclass=BaseMetaFactory[models.GroupInvitation]
):
    class Meta:
        model = models.GroupInvitation

    customer = factory.SelfAttribute("scope.customer")
    scope = factory.SubFactory(structure_factories.ProjectFactory)
    role = factory.LazyAttribute(lambda _: permission_fixtures.ProjectRole.MANAGER)


class CustomerGroupInvitationFactory(
    GroupInvitationBaseFactory, metaclass=BaseMetaFactory[models.GroupInvitation]
):
    class Meta:
        model = models.GroupInvitation

    customer = factory.SelfAttribute("scope")
    scope = factory.SubFactory(structure_factories.CustomerFactory)
    role = factory.LazyAttribute(lambda _: permission_fixtures.CustomerRole.OWNER)


class PermissionRequestFactory(
    factory.django.DjangoModelFactory,
    metaclass=BaseMetaFactory[models.PermissionRequest],
):
    class Meta:
        model = models.PermissionRequest

    invitation = factory.SubFactory(CustomerGroupInvitationFactory)
    created_by = factory.SubFactory(structure_factories.UserFactory)
    state = models.PermissionRequest.States.PENDING

    @classmethod
    def get_list_url(cls, action=None):
        url = "http://testserver" + reverse("user-permission-request-list")
        return url if action is None else url + action + "/"

    @classmethod
    def get_url(cls, request=None, action=None):
        if request is None:
            request = cls()
        url = "http://testserver" + reverse(
            "user-permission-request-detail", kwargs={"uuid": request.uuid.hex}
        )
        return url if action is None else url + action + "/"
