import factory

from django.core.urlresolvers import reverse

from nodeconductor.structure.tests import factories as structure_factories

from .. import models


class IssueFactory(factory.DjangoModelFactory):
    class Meta(object):
        model = models.Issue

    key = factory.Sequence(lambda n: 'TST-%s' % n)
    project = factory.SubFactory(structure_factories.ProjectFactory)
    creator = factory.SubFactory(structure_factories.UserFactory)
    reporter = factory.SubFactory(structure_factories.UserFactory)

    @classmethod
    def get_url(cls, issue=None, action=None):
        if issue is None:
            issue = IssueFactory()
        url = 'http://testserver' + reverse('support-issue-detail', kwargs={'uuid': issue.uuid.hex})
        return url if action is None else url + action + '/'

    @classmethod
    def get_list_url(cls):
        return 'http://testserver' + reverse('support-issue-list')
