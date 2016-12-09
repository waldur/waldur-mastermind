import factory

from django.core.urlresolvers import reverse

from nodeconductor.structure.tests import factories as structure_factories

from .. import models


class SupportUserFactory(factory.DjangoModelFactory):
    class Meta(object):
        model = models.SupportUser

    name = factory.Sequence(lambda n: 'user-%s' % n)
    user = factory.SubFactory(structure_factories.UserFactory)


class IssueFactory(factory.DjangoModelFactory):
    class Meta(object):
        model = models.Issue

    key = factory.Sequence(lambda n: 'TST-%s' % n)
    project = factory.SubFactory(structure_factories.ProjectFactory)
    caller = factory.SubFactory(SupportUserFactory)
    reporter = factory.SubFactory(SupportUserFactory)

    @classmethod
    def get_url(cls, issue=None, action=None):
        if issue is None:
            issue = IssueFactory()
        url = 'http://testserver' + reverse('support-issue-detail', kwargs={'uuid': issue.uuid.hex})
        return url if action is None else url + action + '/'

    @classmethod
    def get_list_url(cls):
        return 'http://testserver' + reverse('support-issue-list')
