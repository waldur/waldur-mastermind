import factory
import factory.fuzzy
from rest_framework.reverse import reverse

from waldur_core.structure import models as structure_models
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.users import models


class InvitationBaseFactory(factory.DjangoModelFactory):
    @classmethod
    def get_list_url(cls):
        return 'http://testserver' + reverse('user-invitation-list')

    @classmethod
    def get_url(cls, invitation, action=None):
        url = 'http://testserver' + reverse('user-invitation-detail', kwargs={'uuid': invitation.uuid})
        return url if action is None else url + action + '/'


class ProjectInvitationFactory(InvitationBaseFactory):
    class Meta(object):
        model = models.Invitation

    customer = factory.SelfAttribute('project.customer')
    project = factory.SubFactory(structure_factories.ProjectFactory)
    project_role = structure_models.ProjectRole.MANAGER
    link_template = factory.Sequence(lambda n: 'http://testinvitation%1.com/project/{uuid}' % n)
    email = factory.Sequence(lambda n: 'test%s@invitation.com' % n)

    @classmethod
    def get_url(cls, invitation=None, action=None):
        if invitation is None:
            invitation = ProjectInvitationFactory()
        return super(ProjectInvitationFactory, cls).get_url(invitation, action)


class CustomerInvitationFactory(InvitationBaseFactory):
    class Meta(object):
        model = models.Invitation

    customer = factory.SubFactory(structure_factories.CustomerFactory)
    customer_role = structure_models.CustomerRole.OWNER
    link_template = factory.Sequence(lambda n: 'http://testinvitation%1.com/customer/{uuid}' % n)
    email = factory.Sequence(lambda n: 'test%s@invitation.com' % n)

    @classmethod
    def get_url(cls, invitation=None, action=None):
        if invitation is None:
            invitation = CustomerInvitationFactory()
        return super(CustomerInvitationFactory, cls).get_url(invitation, action)
