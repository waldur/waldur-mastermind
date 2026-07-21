import factory
from django.urls import reverse

from waldur_core.core.tests.types import BaseMetaFactory
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace.tests import factories as marketplace_factories

from .. import models


class SupportUserFactory(
    factory.django.DjangoModelFactory, metaclass=BaseMetaFactory[models.SupportUser]
):
    class Meta:
        model = models.SupportUser

    name = factory.Sequence(lambda n: "user-%s" % n)
    user = factory.SubFactory(structure_factories.UserFactory)
    backend_id = factory.Sequence(lambda n: "TST-%s" % n)

    @classmethod
    def get_list_url(cls):
        return "http://testserver" + reverse("support-user-list")


class IssueFactory(
    factory.django.DjangoModelFactory, metaclass=BaseMetaFactory[models.Issue]
):
    class Meta:
        model = models.Issue

    backend_id = factory.Sequence(lambda n: "TST-%s" % n)
    key = factory.Sequence(lambda n: "TST-%s" % n)
    project = factory.SubFactory(structure_factories.ProjectFactory)
    caller = factory.SubFactory(structure_factories.UserFactory)
    reporter = factory.SubFactory(SupportUserFactory)

    @classmethod
    def get_url(cls, issue=None, action=None):
        if issue is None:
            issue = IssueFactory()
        url = "http://testserver" + reverse(
            "support-issue-detail", kwargs={"uuid": issue.uuid.hex}
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls):
        return "http://testserver" + reverse("support-issue-list")


class CommentFactory(
    factory.django.DjangoModelFactory, metaclass=BaseMetaFactory[models.Comment]
):
    class Meta:
        model = models.Comment

    issue = factory.SubFactory(IssueFactory)
    author = factory.SubFactory(SupportUserFactory)
    backend_id = factory.Sequence(lambda n: "key_%s" % n)
    description = factory.Sequence(lambda n: "Comment-description-%s" % n)
    is_public = False

    @classmethod
    def get_url(cls, comment=None, action=None):
        if comment is None:
            comment = IssueFactory()
        url = "http://testserver" + reverse(
            "support-comment-detail", kwargs={"uuid": comment.uuid.hex}
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls):
        return "http://testserver" + reverse("support-comment-list")


class AttachmentFactory(
    factory.django.DjangoModelFactory, metaclass=BaseMetaFactory[models.Attachment]
):
    class Meta:
        model = models.Attachment

    backend_id = factory.Sequence(lambda n: "key_%s" % n)
    issue = factory.SubFactory(IssueFactory)
    file = factory.django.FileField(filename="the_file.txt")

    @classmethod
    def get_url(cls, attachment=None, action=None):
        if attachment is None:
            attachment = AttachmentFactory()
        url = "http://testserver" + reverse(
            "support-attachment-detail", kwargs={"uuid": attachment.uuid.hex}
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls):
        return "http://testserver" + reverse("support-attachment-list")


class TemplateFactory(
    factory.django.DjangoModelFactory, metaclass=BaseMetaFactory[models.Template]
):
    class Meta:
        model = models.Template

    name = factory.Sequence(lambda n: "template_%s" % n)
    description = factory.Sequence(lambda n: "template_description_%s" % n)

    @classmethod
    def get_url(cls, template=None, action=None):
        if template is None:
            template = TemplateFactory()
        url = "http://testserver" + reverse(
            "support-template-detail", kwargs={"uuid": template.uuid.hex}
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls):
        return "http://testserver" + reverse("support-template-list")


class IgnoredIssueStatusFactory(
    factory.django.DjangoModelFactory,
    metaclass=BaseMetaFactory[models.IgnoredIssueStatus],
):
    class Meta:
        model = models.IgnoredIssueStatus

    name = factory.Sequence(lambda n: "status_%s" % n)


class TemplateStatusNotificationFactory(
    factory.django.DjangoModelFactory,
    metaclass=BaseMetaFactory[models.TemplateStatusNotification],
):
    class Meta:
        model = models.TemplateStatusNotification

    status = factory.Sequence(lambda n: "status_%s" % n)
    html = "Test template {{issue.summary}}"
    text = "Test template {{issue.summary}}"
    subject = "Test template {{issue.summary}}"


class PriorityFactory(
    factory.django.DjangoModelFactory, metaclass=BaseMetaFactory[models.Priority]
):
    class Meta:
        model = models.Priority

    backend_id = factory.Sequence(lambda n: n)
    name = factory.Sequence(lambda n: "priority-%s" % n)

    @classmethod
    def get_url(cls, priority=None):
        if priority is None:
            priority = PriorityFactory()
        return "http://testserver" + reverse(
            "support-priority-detail", kwargs={"uuid": priority.uuid.hex}
        )

    @classmethod
    def get_list_url(cls):
        return "http://testserver" + reverse("support-priority-list")


class RequestTypeFactory(
    factory.django.DjangoModelFactory, metaclass=BaseMetaFactory[models.RequestType]
):
    class Meta:
        model = models.RequestType

    backend_id = (
        None  # Null by default (manual type), set explicitly to create synced type
    )
    name = factory.Sequence(lambda n: "request_type_%s" % n)
    issue_type_name = factory.Sequence(lambda n: "issue_type_%s" % n)
    is_active = True
    order = factory.Sequence(lambda n: n)

    @classmethod
    def get_url(cls, request_type=None, action=None):
        if request_type is None:
            request_type = RequestTypeFactory()
        url = "http://testserver" + reverse(
            "support-request-type-admin-detail", kwargs={"uuid": request_type.uuid.hex}
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls):
        return "http://testserver" + reverse("support-request-type-admin-list")


class SupportCustomerFactory(
    factory.django.DjangoModelFactory, metaclass=BaseMetaFactory[models.SupportCustomer]
):
    class Meta:
        model = models.SupportCustomer

    user = factory.SubFactory(structure_factories.UserFactory)
    backend_id = factory.Sequence(lambda n: "qm:%s" % n)


class IssueStatusFactory(
    factory.django.DjangoModelFactory, metaclass=BaseMetaFactory[models.IssueStatus]
):
    class Meta:
        model = models.IssueStatus


class TemplateConfirmationCommentFactory(
    factory.django.DjangoModelFactory,
    metaclass=BaseMetaFactory[models.TemplateConfirmationComment],
):
    class Meta:
        model = models.TemplateConfirmationComment


class FeedbackFactory(
    factory.django.DjangoModelFactory, metaclass=BaseMetaFactory[models.Feedback]
):
    issue = factory.SubFactory(IssueFactory)
    evaluation = 10

    class Meta:
        model = models.Feedback

    @classmethod
    def get_url(cls, feedback=None):
        if feedback is None:
            feedback = FeedbackFactory()
        return "http://testserver" + reverse(
            "support-feedback-detail", kwargs={"uuid": feedback.uuid.hex}
        )

    @classmethod
    def get_list_url(cls):
        return "http://testserver" + reverse("support-feedback-list")


class IssueStatusTransitionFactory(
    factory.django.DjangoModelFactory,
    metaclass=BaseMetaFactory[models.IssueStatusTransition],
):
    class Meta:
        model = models.IssueStatusTransition

    from_status = factory.Sequence(lambda n: "from_status_%s" % n)
    to_status = factory.Sequence(lambda n: "to_status_%s" % n)


class ProviderHelpdeskFactory(
    factory.django.DjangoModelFactory,
    metaclass=BaseMetaFactory[models.ProviderHelpdesk],
):
    class Meta:
        model = models.ProviderHelpdesk

    service_provider = factory.SubFactory(marketplace_factories.ServiceProviderFactory)
    backend_type = models.ProviderHelpdesk.BackendTypes.BASIC
    is_active = True

    @classmethod
    def get_url(cls, provider_helpdesk=None, action=None):
        if provider_helpdesk is None:
            provider_helpdesk = ProviderHelpdeskFactory()
        url = "http://testserver" + reverse(
            "provider-helpdesk-detail", kwargs={"uuid": provider_helpdesk.uuid.hex}
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls):
        return "http://testserver" + reverse("provider-helpdesk-list")


class ProviderSupportUserFactory(
    factory.django.DjangoModelFactory,
    metaclass=BaseMetaFactory[models.ProviderSupportUser],
):
    class Meta:
        model = models.ProviderSupportUser

    provider_helpdesk = factory.SubFactory(ProviderHelpdeskFactory)
    user = factory.SubFactory(structure_factories.UserFactory)
    role = models.ProviderSupportUser.Roles.AGENT
    is_active = True
    max_open_tickets = 20

    @classmethod
    def get_url(cls, provider_support_user=None, action=None):
        if provider_support_user is None:
            provider_support_user = ProviderSupportUserFactory()
        url = "http://testserver" + reverse(
            "provider-support-user-detail",
            kwargs={"uuid": provider_support_user.uuid.hex},
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls, action=None):
        url = "http://testserver" + reverse("provider-support-user-list")
        return url if action is None else url + action + "/"


class ProviderCannedResponseFactory(
    factory.django.DjangoModelFactory,
    metaclass=BaseMetaFactory[models.ProviderCannedResponse],
):
    class Meta:
        model = models.ProviderCannedResponse

    provider_helpdesk = factory.SubFactory(ProviderHelpdeskFactory)
    name = factory.Sequence(lambda n: "canned_response_%s" % n)
    text = "Hello {{ customer_name }}, your ticket is being processed."
    category = "general"

    @classmethod
    def get_url(cls, canned_response=None, action=None):
        if canned_response is None:
            canned_response = ProviderCannedResponseFactory()
        url = "http://testserver" + reverse(
            "provider-canned-response-detail",
            kwargs={"uuid": canned_response.uuid.hex},
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls):
        return "http://testserver" + reverse("provider-canned-response-list")


class IssueTagFactory(
    factory.django.DjangoModelFactory,
    metaclass=BaseMetaFactory[models.IssueTag],
):
    class Meta:
        model = models.IssueTag

    name = factory.Sequence(lambda n: "tag_%s" % n)
    color = "#ff0000"

    @classmethod
    def get_url(cls, tag=None, action=None):
        if tag is None:
            tag = IssueTagFactory()
        url = "http://testserver" + reverse(
            "support-issue-tag-detail", kwargs={"uuid": tag.uuid.hex}
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls):
        return "http://testserver" + reverse("support-issue-tag-list")


class IssueLinkFactory(
    factory.django.DjangoModelFactory,
    metaclass=BaseMetaFactory[models.IssueLink],
):
    class Meta:
        model = models.IssueLink

    source = factory.SubFactory(IssueFactory)
    target = factory.SubFactory(IssueFactory)
    link_type = models.IssueLink.LinkTypes.RELATED

    @classmethod
    def get_url(cls, link=None, action=None):
        if link is None:
            link = IssueLinkFactory()
        url = "http://testserver" + reverse(
            "support-issue-link-detail", kwargs={"uuid": link.uuid.hex}
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls):
        return "http://testserver" + reverse("support-issue-link-list")


class SavedFilterFactory(
    factory.django.DjangoModelFactory,
    metaclass=BaseMetaFactory[models.SavedFilter],
):
    class Meta:
        model = models.SavedFilter

    name = factory.Sequence(lambda n: "filter_%s" % n)
    user = factory.SubFactory(structure_factories.UserFactory)
    filter_params = {"status": "open"}
    is_shared = False

    @classmethod
    def get_url(cls, saved_filter=None, action=None):
        if saved_filter is None:
            saved_filter = SavedFilterFactory()
        url = "http://testserver" + reverse(
            "support-saved-filter-detail", kwargs={"uuid": saved_filter.uuid.hex}
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls):
        return "http://testserver" + reverse("support-saved-filter-list")


class CannedResponseFactory(
    factory.django.DjangoModelFactory,
    metaclass=BaseMetaFactory[models.CannedResponse],
):
    class Meta:
        model = models.CannedResponse

    name = factory.Sequence(lambda n: "canned_response_%s" % n)
    text = "Thank you for contacting support."
    category = "general"
    is_active = True
    created_by = factory.SubFactory(structure_factories.UserFactory)

    @classmethod
    def get_url(cls, canned_response=None, action=None):
        if canned_response is None:
            canned_response = CannedResponseFactory()
        url = "http://testserver" + reverse(
            "support-canned-response-detail",
            kwargs={"uuid": canned_response.uuid.hex},
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls):
        return "http://testserver" + reverse("support-canned-response-list")
