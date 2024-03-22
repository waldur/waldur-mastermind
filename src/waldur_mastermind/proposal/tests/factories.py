import datetime

import factory
from rest_framework.reverse import reverse

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.proposal import models


class CallManagingOrganisationFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = models.CallManagingOrganisation

    customer = factory.SubFactory(structure_factories.CustomerFactory)

    @classmethod
    def get_url(cls, manager=None, action=None):
        if manager is None:
            manager = CallManagingOrganisationFactory()
        url = "http://testserver" + reverse(
            "call-managing-organisation-detail",
            kwargs={"uuid": manager.uuid.hex},
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls, action=None):
        url = "http://testserver" + reverse("call-managing-organisation-list")
        return url if action is None else url + action + "/"


class CallFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = models.Call

    name = factory.Sequence(lambda n: "name-%s" % n)
    manager = factory.SubFactory(CallManagingOrganisationFactory)
    created_by = factory.SubFactory(structure_factories.UserFactory)

    @classmethod
    def get_public_url(cls, call=None, action=None):
        if call is None:
            call = CallFactory()
        url = "http://testserver" + reverse(
            "proposal-public-call-detail",
            kwargs={"uuid": call.uuid.hex},
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_public_list_url(cls, action=None):
        url = "http://testserver" + reverse("proposal-public-call-list")
        return url if action is None else url + action + "/"

    @classmethod
    def get_protected_url(cls, call=None, action=None):
        if call is None:
            call = CallFactory()
        url = "http://testserver" + reverse(
            "proposal-protected-call-detail",
            kwargs={"uuid": call.uuid.hex},
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_protected_list_url(cls, action=None):
        url = "http://testserver" + reverse("proposal-protected-call-list")
        return url if action is None else url + action + "/"


class RequestedOfferingFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = models.RequestedOffering

    call = factory.SubFactory(CallFactory)
    created_by = factory.SubFactory(structure_factories.UserFactory)
    offering = factory.SubFactory(marketplace_factories.OfferingFactory)

    @classmethod
    def get_url(cls, call=None, requested_offering=None):
        if requested_offering is None:
            requested_offering = RequestedOfferingFactory()
        return (
            CallFactory.get_protected_url(call, action="offerings")
            + requested_offering.uuid.hex
            + "/"
        )

    @classmethod
    def get_list_url(cls, call):
        return CallFactory.get_protected_url(call, action="offerings")

    @classmethod
    def get_provider_list_url(cls):
        url = "http://testserver" + reverse("proposal-requested-offering-list")
        return url

    @classmethod
    def get_provider_url(cls, requested_offering=None, action=None):
        if requested_offering is None:
            requested_offering = RequestedOfferingFactory()
        url = "http://testserver" + reverse(
            "proposal-requested-offering-detail",
            kwargs={"uuid": requested_offering.uuid.hex},
        )
        return url if action is None else url + action + "/"


class RoundFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = models.Round

    call = factory.SubFactory(CallFactory)
    start_time = datetime.date.today() + datetime.timedelta(days=5)
    cutoff_time = datetime.date.today() + datetime.timedelta(days=10)

    @classmethod
    def get_url(cls, call=None, call_round=None):
        if call_round is None:
            call_round = RoundFactory()
        return (
            CallFactory.get_protected_url(call, action="rounds")
            + call_round.uuid.hex
            + "/"
        )

    @classmethod
    def get_list_url(cls, call):
        return CallFactory.get_protected_url(call, action="rounds")


class ProposalFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = models.Proposal

    round = factory.SubFactory(RoundFactory)
    duration_in_days = 10
    created_by = factory.SubFactory(structure_factories.UserFactory)
    project = factory.SubFactory(structure_factories.ProjectFactory)

    @classmethod
    def get_url(cls, proposal=None, action=None):
        if proposal is None:
            proposal = ProposalFactory()
        url = "http://testserver" + reverse(
            "proposal-proposal-detail",
            kwargs={"uuid": proposal.uuid.hex},
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls, action=None):
        url = "http://testserver" + reverse("proposal-proposal-list")
        return url if action is None else url + action + "/"


class RequestedResourceFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = models.RequestedResource

    proposal = factory.SubFactory(ProposalFactory)
    created_by = factory.SubFactory(structure_factories.UserFactory)
    resource = factory.SubFactory(marketplace_factories.ResourceFactory)
    requested_offering = factory.SubFactory(RequestedOfferingFactory)

    @classmethod
    def get_url(cls, proposal, requested_resource=None):
        if requested_resource is None:
            requested_resource = RequestedResourceFactory()
        return (
            ProposalFactory.get_url(proposal, action="resources")
            + requested_resource.uuid.hex
            + "/"
        )

    @classmethod
    def get_list_url(cls, proposal):
        return ProposalFactory.get_url(proposal, action="resources")

    @classmethod
    def get_provider_list_url(cls):
        url = "http://testserver" + reverse("proposal-requested-resource-list")
        return url

    @classmethod
    def get_provider_url(cls, requested_resource=None, action=None):
        if requested_resource is None:
            requested_resource = RequestedResourceFactory()
        url = "http://testserver" + reverse(
            "proposal-requested-resource-detail",
            kwargs={"uuid": requested_resource.uuid.hex},
        )
        return url if action is None else url + action + "/"


class ReviewFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = models.Review

    proposal = factory.SubFactory(ProposalFactory)
    reviewer = factory.SubFactory(structure_factories.UserFactory)

    @classmethod
    def get_url(cls, review=None, action=None):
        if review is None:
            review = ReviewFactory()
        url = "http://testserver" + reverse(
            "proposal-review-detail",
            kwargs={"uuid": review.uuid.hex},
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls, action=None):
        url = "http://testserver" + reverse("proposal-review-list")
        return url if action is None else url + action + "/"
