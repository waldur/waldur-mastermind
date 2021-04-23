import factory
from django.urls import reverse

from waldur_core.structure.tests import factories as structure_factories

from .. import models


class SupportUserFactory(factory.DjangoModelFactory):
    class Meta:
        model = models.SupportUser

    name = factory.Sequence(lambda n: 'user-%s' % n)
    user = factory.SubFactory(structure_factories.UserFactory)

    @classmethod
    def get_list_url(cls):
        return 'http://testserver' + reverse('support-user-list')


class IssueFactory(factory.DjangoModelFactory):
    class Meta:
        model = models.Issue

    backend_id = factory.Sequence(lambda n: 'TST-%s' % n)
    key = factory.Sequence(lambda n: 'TST-%s' % n)
    project = factory.SubFactory(structure_factories.ProjectFactory)
    caller = factory.SubFactory(structure_factories.UserFactory)
    reporter = factory.SubFactory(SupportUserFactory)

    @classmethod
    def get_url(cls, issue=None, action=None):
        if issue is None:
            issue = IssueFactory()
        url = 'http://testserver' + reverse(
            'support-issue-detail', kwargs={'uuid': issue.uuid.hex}
        )
        return url if action is None else url + action + '/'

    @classmethod
    def get_list_url(cls):
        return 'http://testserver' + reverse('support-issue-list')


class CommentFactory(factory.DjangoModelFactory):
    class Meta:
        model = models.Comment

    issue = factory.SubFactory(IssueFactory)
    author = factory.SubFactory(SupportUserFactory)
    backend_id = factory.Sequence(lambda n: 'key_%s' % n)
    description = factory.Sequence(lambda n: 'Comment-description-%s' % n)
    is_public = False

    @classmethod
    def get_url(cls, comment=None, action=None):
        if comment is None:
            comment = IssueFactory()
        url = 'http://testserver' + reverse(
            'support-comment-detail', kwargs={'uuid': comment.uuid.hex}
        )
        return url if action is None else url + action + '/'

    @classmethod
    def get_list_url(cls):
        return 'http://testserver' + reverse('support-comment-list')


class AttachmentFactory(factory.DjangoModelFactory):
    class Meta:
        model = models.Attachment

    backend_id = factory.Sequence(lambda n: 'key_%s' % n)
    issue = factory.SubFactory(IssueFactory)
    file = factory.django.FileField(filename='the_file.txt')

    @classmethod
    def get_url(cls, attachment=None, action=None):
        if attachment is None:
            attachment = AttachmentFactory()
        url = 'http://testserver' + reverse(
            'support-attachment-detail', kwargs={'uuid': attachment.uuid.hex}
        )
        return url if action is None else url + action + '/'

    @classmethod
    def get_list_url(cls):
        return 'http://testserver' + reverse('support-attachment-list')


class TemplateFactory(factory.DjangoModelFactory):
    class Meta:
        model = models.Template

    name = factory.Sequence(lambda n: 'template_%s' % n)
    description = factory.Sequence(lambda n: 'template_description_%s' % n)

    @classmethod
    def get_url(cls, template=None, action=None):
        if template is None:
            template = TemplateFactory()
        url = 'http://testserver' + reverse(
            'support-template-detail', kwargs={'uuid': template.uuid.hex}
        )
        return url if action is None else url + action + '/'

    @classmethod
    def get_list_url(cls):
        return 'http://testserver' + reverse('support-template-list')


class IgnoredIssueStatusFactory(factory.DjangoModelFactory):
    class Meta:
        model = models.IgnoredIssueStatus

    name = factory.Sequence(lambda n: 'status_%s' % n)


class TemplateStatusNotificationFactory(factory.DjangoModelFactory):
    class Meta:
        model = models.TemplateStatusNotification

    status = factory.Sequence(lambda n: 'status_%s' % n)
    html = 'Test template {{issue.summary}}'
    text = 'Test template {{issue.summary}}'
    subject = 'Test template {{issue.summary}}'


class PriorityFactory(factory.DjangoModelFactory):
    class Meta:
        model = models.Priority

    backend_id = factory.Sequence(lambda n: n)
    name = factory.Sequence(lambda n: 'priority-%s' % n)

    @classmethod
    def get_url(cls, priority=None):
        if priority is None:
            priority = PriorityFactory()
        return 'http://testserver' + reverse(
            'support-priority-detail', kwargs={'uuid': priority.uuid.hex}
        )

    @classmethod
    def get_list_url(cls):
        return 'http://testserver' + reverse('support-priority-list')


class RequestTypeFactory(factory.DjangoModelFactory):
    class Meta:
        model = models.RequestType

    backend_id = factory.Sequence(lambda n: n)
    name = factory.Sequence(lambda n: 'request_type_%s' % n)
    issue_type_name = factory.Sequence(lambda n: 'issue_type_%s' % n)


class SupportCustomerFactory(factory.DjangoModelFactory):
    class Meta:
        model = models.SupportCustomer

    user = factory.SubFactory(structure_factories.UserFactory)
    backend_id = factory.Sequence(lambda n: 'qm:%s' % n)


class IssueStatusFactory(factory.DjangoModelFactory):
    class Meta:
        model = models.IssueStatus


class TemplateConfirmationCommentFactory(factory.DjangoModelFactory):
    class Meta:
        model = models.TemplateConfirmationComment


class FeedbackFactory(factory.DjangoModelFactory):
    issue = factory.SubFactory(IssueFactory)
    evaluation = models.Feedback.Evaluation.TEN

    class Meta:
        model = models.Feedback

    @classmethod
    def get_url(cls, feedback=None):
        if feedback is None:
            feedback = FeedbackFactory()
        return 'http://testserver' + reverse(
            'support-feedback-detail', kwargs={'uuid': feedback.uuid.hex}
        )

    @classmethod
    def get_list_url(cls):
        return 'http://testserver' + reverse('support-feedback-list')
