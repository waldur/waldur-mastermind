import factory
from django.urls import reverse

from waldur_keycloak import models
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.tests import factories as marketplace_factories


class OfferingUserRoleFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = marketplace_models.OfferingUserRole

    name = factory.Sequence(lambda n: "role-%s" % n)
    offering = factory.SubFactory(marketplace_factories.OfferingFactory)

    @classmethod
    def get_url(cls, role=None):
        if role is None:
            role = OfferingUserRoleFactory()
        return "http://testserver" + reverse(
            "marketplace-offering-user-role-detail",
            kwargs={"uuid": role.uuid.hex},
        )


class OfferingKeycloakGroupFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = models.OfferingKeycloakGroup

    name = factory.Sequence(lambda n: "keycloak-group-%s" % n)
    offering = factory.SubFactory(marketplace_factories.OfferingFactory)
    role = factory.LazyAttribute(lambda o: OfferingUserRoleFactory(offering=o.offering))
    backend_id = factory.Sequence(lambda n: "backend-group-id-%s" % n)

    @classmethod
    def get_url(cls, group=None, action=None):
        if group is None:
            group = OfferingKeycloakGroupFactory()
        url = "http://testserver" + reverse(
            "offering-keycloak-group-detail",
            kwargs={"uuid": group.uuid.hex},
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls, action=None):
        url = "http://testserver" + reverse("offering-keycloak-group-list")
        return url if action is None else url + action + "/"


class OfferingKeycloakMembershipFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = models.OfferingKeycloakMembership

    username = factory.Sequence(lambda n: "keycloak-user-%s" % n)
    email = factory.LazyAttribute(lambda o: "%s@example.com" % o.username)
    group = factory.SubFactory(OfferingKeycloakGroupFactory)

    @classmethod
    def get_url(cls, membership=None, action=None):
        if membership is None:
            membership = OfferingKeycloakMembershipFactory()
        url = "http://testserver" + reverse(
            "offering-keycloak-membership-detail",
            kwargs={"uuid": membership.uuid.hex},
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls, action=None):
        url = "http://testserver" + reverse("offering-keycloak-membership-list")
        return url if action is None else url + action + "/"
