import factory.fuzzy
from rest_framework.reverse import reverse

from waldur_core.structure import models as structure_models
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.users import models


class InvitationBaseFactory(factory.DjangoModelFactory):
    email = factory.Sequence(lambda n: 'test%s@invitation.com' % n)

    @classmethod
    def get_list_url(cls, action=None):
        url = 'http://testserver' + reverse('user-invitation-list')
        return url if action is None else url + action + '/'

    @classmethod
    def get_url(cls, invitation=None, action=None):
        if invitation is None:
            invitation = cls()
        url = 'http://testserver' + reverse(
            'user-invitation-detail', kwargs={'uuid': invitation.uuid.hex}
        )
        return url if action is None else url + action + '/'


class ProjectInvitationFactory(InvitationBaseFactory):
    class Meta:
        model = models.Invitation

    customer = factory.SelfAttribute('project.customer')
    project = factory.SubFactory(structure_factories.ProjectFactory)
    project_role = structure_models.ProjectRole.MANAGER


class CustomerInvitationFactory(InvitationBaseFactory):
    class Meta:
        model = models.Invitation

    customer = factory.SubFactory(structure_factories.CustomerFactory)
    customer_role = structure_models.CustomerRole.OWNER


class GroupInvitationBaseFactory(factory.DjangoModelFactory):
    @classmethod
    def get_list_url(cls, action=None):
        url = 'http://testserver' + reverse('user-group-invitation-list')
        return url if action is None else url + action + '/'

    @classmethod
    def get_url(cls, invitation=None, action=None):
        if invitation is None:
            invitation = cls()
        url = 'http://testserver' + reverse(
            'user-group-invitation-detail', kwargs={'uuid': invitation.uuid.hex}
        )
        return url if action is None else url + action + '/'


class ProjectGroupInvitationFactory(GroupInvitationBaseFactory):
    class Meta:
        model = models.GroupInvitation

    customer = factory.SelfAttribute('project.customer')
    project = factory.SubFactory(structure_factories.ProjectFactory)
    project_role = structure_models.ProjectRole.MANAGER


class CustomerGroupInvitationFactory(GroupInvitationBaseFactory):
    class Meta:
        model = models.GroupInvitation

    customer = factory.SubFactory(structure_factories.CustomerFactory)
    customer_role = structure_models.CustomerRole.OWNER
