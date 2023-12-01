import datetime

import factory
from rest_framework.reverse import reverse

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.proposal import models


class CallManagingOrganisationFactory(factory.DjangoModelFactory):
    class Meta:
        model = models.CallManagingOrganisation

    customer = factory.SubFactory(structure_factories.CustomerFactory)

    @classmethod
    def get_url(cls, manager=None, action=None):
        if manager is None:
            manager = CallManagingOrganisationFactory()
        url = 'http://testserver' + reverse(
            'call-managing-organisation-detail',
            kwargs={'uuid': manager.uuid.hex},
        )
        return url if action is None else url + action + '/'

    @classmethod
    def get_list_url(cls, action=None):
        url = 'http://testserver' + reverse('call-managing-organisation-list')
        return url if action is None else url + action + '/'


class CallFactory(factory.DjangoModelFactory):
    class Meta:
        model = models.Call

    name = factory.Sequence(lambda n: 'name-%s' % n)
    manager = factory.SubFactory(CallManagingOrganisationFactory)
    created_by = factory.SubFactory(structure_factories.UserFactory)
    start_time = datetime.date.today()
    end_time = datetime.date.today() + datetime.timedelta(days=30)

    @classmethod
    def get_public_url(cls, call=None, action=None):
        if call is None:
            call = CallFactory()
        url = 'http://testserver' + reverse(
            'proposal-public-call-detail',
            kwargs={'uuid': call.uuid.hex},
        )
        return url if action is None else url + action + '/'

    @classmethod
    def get_public_list_url(cls, action=None):
        url = 'http://testserver' + reverse('proposal-public-call-list')
        return url if action is None else url + action + '/'

    @classmethod
    def get_protected_url(cls, call=None, action=None):
        if call is None:
            call = CallFactory()
        url = 'http://testserver' + reverse(
            'proposal-protected-call-detail',
            kwargs={'uuid': call.uuid.hex},
        )
        return url if action is None else url + action + '/'

    @classmethod
    def get_protected_list_url(cls, action=None):
        url = 'http://testserver' + reverse('proposal-protected-call-list')
        return url if action is None else url + action + '/'


class RequestedOfferingFactory(factory.DjangoModelFactory):
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
            CallFactory.get_protected_url(call, action='offerings')
            + requested_offering.uuid.hex
            + '/'
        )

    @classmethod
    def get_list_url(cls, call):
        return CallFactory.get_protected_url(call, action='offerings')
