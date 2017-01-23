import factory
from factory import fuzzy

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
    caller = factory.SubFactory(structure_factories.UserFactory)
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


class CommentFactory(factory.DjangoModelFactory):
    class Meta(object):
        model = models.Comment

    issue = factory.SubFactory(IssueFactory)
    author = factory.SubFactory(SupportUserFactory)
    backend_id = factory.Sequence(lambda n: 'key_%s' % n)
    description = factory.Sequence(lambda n: 'Comment-description-%s' % n)

    @classmethod
    def get_url(cls, comment=None, action=None):
        if comment is None:
            comment = IssueFactory()
        url = 'http://testserver' + reverse('support-comment-detail', kwargs={'uuid': comment.uuid.hex})
        return url if action is None else url + action + '/'

    @classmethod
    def get_list_url(cls):
        return 'http://testserver' + reverse('support-comment-list')


class OfferingFactory(factory.DjangoModelFactory):

    class Meta:
        model = models.Offering

    issue = factory.SubFactory(IssueFactory)
    price = fuzzy.FuzzyInteger(0, 10)

    @classmethod
    def get_url(cls, offering=None, action=None):
        if offering is None:
            offering = OfferingFactory()
        url = 'http://testserver' + reverse('support-offering-detail', kwargs={'uuid': offering.uuid.hex})
        return url if action is None else url + action + '/'

    @classmethod
    def get_list_url(cls):
        return 'http://testserver' + reverse('support-offering-list')

    @classmethod
    def get_list_action_url(cls):
        return 'http://testserver' + reverse('support-offering-list') + 'configured/'
