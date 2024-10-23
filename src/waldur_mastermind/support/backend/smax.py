import logging
import mimetypes
import os

from constance import config
from django.core.files.base import ContentFile
from django.template import Context, Template

from waldur_core.core.models import User as WaldurUser
from waldur_core.core.utils import text2html
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.support import models
from waldur_mastermind.support.backend.smax_utils import (
    Comment,
    Issue,
    SmaxBackend,
    User,
)

from . import SupportBackend, SupportBackendType, SupportedFormat

logger = logging.getLogger(__name__)


class SmaxServiceBackend(SupportBackend):
    def __init__(self):
        self.manager = SmaxBackend()

    backend_name = SupportBackendType.SMAX
    summary_max_length = 140
    message_format = SupportedFormat.HTML

    def get_or_create_support_user_by_waldur_user(
        self, waldur_user: WaldurUser
    ) -> models.SupportUser:
        support_user, _ = models.SupportUser.objects.get_or_create(
            user=waldur_user,
            backend_name=self.backend_name,
            is_active=True,
            defaults={"name": waldur_user.full_name or waldur_user.username},
        )

        if support_user.backend_id:
            return support_user

        backend_user = self.manager.get_user_by_email(waldur_user.email)

        if not backend_user:
            user = User(
                support_user.user.email,
                support_user.user.full_name,
            )
            backend_user = self.manager.add_user(user)

        support_user.backend_id = backend_user.id
        support_user.save()
        return support_user

    def create_issue(self, issue: models.Issue):
        """Create SMAX issue"""
        issue.begin_creating()
        issue.save()

        if issue.reporter:
            support_user = issue.reporter
        else:
            support_user = self.get_or_create_support_user_by_waldur_user(issue.caller)
            issue.reporter = support_user
            issue.save()

        user = User(
            support_user.user.email,
            support_user.user.full_name,
        )

        # check issue type to category mapping
        category_id = ""
        if models.RequestType.objects.filter(
            issue_type_name=issue.type, backend_name=self.backend_name
        ).exists():
            category_id = models.RequestType.objects.get(
                issue_type_name=issue.type, backend_name=self.backend_name
            ).backend_id

        if issue.resource:
            if type(issue.resource) == marketplace_models.Order:
                resource_name = issue.resource.resource.name
            else:
                resource_name = issue.resource.name
        else:
            resource_name = None

        new_smax_issue = Issue(
            summary=issue.summary,
            description=issue.description,
            organisation_name=issue.customer.name if issue.customer else None,
            project_name=issue.project.name if issue.project else None,
            resource_name=resource_name,
            category_id=category_id,
        )

        smax_issue = self.manager.add_issue(user, new_smax_issue)
        issue.backend_id = smax_issue.id
        issue.key = smax_issue.id
        issue.status = smax_issue.status
        issue.backend_name = self.backend_name
        issue.set_ok()
        issue.save()
        return smax_issue

    def update_waldur_issue_from_smax(self, issue):
        # update an issue
        backend_issue = self.manager.get_issue(issue.backend_id)
        issue.description = backend_issue.description
        issue.summary = backend_issue.summary
        issue.status = backend_issue.status
        issue.save()

        # update comments
        issue_comments = backend_issue.comments

        for backend_comment in issue_comments:
            waldur_comment = models.Comment.objects.filter(
                backend_name=self.backend_name, backend_id=backend_comment.id
            )
            if waldur_comment.exists():
                waldur_comment = waldur_comment.get()
                waldur_comment.description = backend_comment.description
                waldur_comment.is_public = backend_comment.is_public
                waldur_comment.save()
                continue

            backend_user = self.manager.get_user(backend_comment.backend_user_id)
            support_user, created = models.SupportUser.objects.get_or_create(
                backend_id=backend_user.id,
                backend_name=self.backend_name,
                defaults=dict(
                    name=backend_user.name,
                ),
            )

            if created:
                logger.info(f"Smax support user {backend_user.name} has been created.")

            models.Comment.objects.create(
                backend_name=self.backend_name,
                issue_id=issue.id,
                author=support_user,
                description=backend_comment.description,
                is_public=backend_comment.is_public,
                backend_id=backend_comment.id,
                state=models.Comment.States.OK,
            )
            logger.info(f"Smax comment {backend_comment.id} has been created.")

        issue_comments_ids = [c.id for c in issue_comments]
        count = (
            models.Comment.objects.filter(
                backend_name=self.backend_name,
                issue=issue,
            )
            .exclude(backend_id__in=issue_comments_ids)
            .delete()[0]
        )

        if count:
            logger.info(
                f"Smax comments have been deleted. Count: {count}, issue ID: {issue.id}"
            )

        # update attachments
        issue_attachments = backend_issue.attachments

        for backend_attachment in issue_attachments:
            waldur_attachment = models.Attachment.objects.filter(
                backend_name=self.backend_name, backend_id=backend_attachment.id
            )
            if waldur_attachment.exists():
                continue

            backend_user = self.manager.get_user(backend_attachment.backend_user_id)
            support_user, created = models.SupportUser.objects.get_or_create(
                backend_id=backend_user.id,
                backend_name=self.backend_name,
                defaults=dict(
                    name=backend_user.name,
                ),
            )

            if created:
                logger.info(f"Smax support user {backend_user.name} has been created.")

            if not issue.reporter:
                logger.info("Issue reporter does not exist.")
                continue

            waldur_attachment = models.Attachment.objects.create(
                issue=issue,
                backend_id=backend_attachment.id,
                backend_name=self.backend_name,
                state=models.Attachment.States.OK,
                author=support_user,
            )

            waldur_attachment.file.save(
                backend_attachment.filename,
                ContentFile(self.manager.attachment_download(backend_attachment)),
            )

        issue_attachments_ids = [a.id for a in issue_attachments]
        count = (
            models.Attachment.objects.filter(
                backend_name=self.backend_name,
                issue=issue,
            )
            .exclude(backend_id__in=issue_attachments_ids)
            .delete()[0]
        )

        if count:
            logger.info(
                f"Smax attachments have been deleted. Count: {count}, issue ID: {issue.id}"
            )

    def sync_issues(self, issue_id=None):
        issues = models.Issue.objects.filter(backend_name=self.backend_name)

        if issue_id:
            issues = issues.filter(id=issue_id)

        for issue in issues:
            self.update_waldur_issue_from_smax(issue)

    def pull_support_users(self):
        # placeholder, traversing all SMAX users might be overly costly
        pass

    def create_smax_user_for_support_user(
        self, support_user: models.SupportUser
    ) -> User:
        smax_user = User(
            email=support_user.user.email,
            name=support_user.user.full_name,
        )
        self.manager.add_user(smax_user)
        support_user.backend_id = smax_user.id
        support_user.backend_name = self.backend_name
        support_user.save()
        return smax_user

    def get_smax_user_id_for_support_user(self, support_user):
        if (
            not support_user.backend_name
            or support_user.backend_name != self.backend_name
        ) and not support_user.backend_id:
            return self.create_smax_user_for_support_user(support_user).id

        return support_user.backend_id

    def create_comment(self, comment: models.Comment):
        """Create Smax comment"""
        comment.begin_creating()
        comment.save()

        backend_user_id = self.get_smax_user_id_for_support_user(comment.author)
        smax_comment = Comment(
            description=comment.description,
            backend_user_id=backend_user_id,
            is_public=comment.is_public,
        )

        smax_comment = self.manager.add_comment(comment.issue.backend_id, smax_comment)
        comment.backend_id = smax_comment.id
        comment.backend_name = self.backend_name
        comment.set_ok()
        comment.save()
        return smax_comment

    def update_comment(self, comment: models.Comment):
        comment.schedule_updating()
        comment.begin_updating()
        comment.save()

        smax_comment = Comment(
            description=comment.description,
            backend_user_id=comment.author.backend_id,
            is_public=comment.is_public,
            id=comment.backend_id,
        )

        self.manager.update_comment(comment.issue.backend_id, smax_comment)

        comment.set_ok()
        comment.save()
        return smax_comment

    def delete_comment(self, comment: models.Comment):
        comment.schedule_deleting()
        comment.begin_deleting()
        comment.save()

        self.manager.delete_comment(comment.issue.backend_id, comment.backend_id)

        comment.set_ok()
        comment.save()
        return

    def create_attachment(self, attachment: models.Attachment):
        if (
            not attachment.issue.backend_id
            or attachment.issue.backend_name != self.backend_name
        ):
            return

        if not attachment.author:
            return

        backend_user_id = self.get_smax_user_id_for_support_user(attachment.author)

        file_name = os.path.basename(attachment.file.name)
        mime_type = attachment.mime_type

        if not mime_type:
            mime_type, _ = mimetypes.guess_type(file_name)

        file_content = attachment.file.read()

        if not file_content:
            return

        backend_attachment = self.manager.create_attachment(
            attachment.issue.backend_id,
            backend_user_id,
            file_name,
            mime_type,
            file_content,
        )
        attachment.backend_id = backend_attachment.id
        attachment.backend_name = self.backend_name
        attachment.save()
        return

    def delete_attachment(self, attachment: models.Attachment):
        if (
            not attachment.issue.backend_id
            or attachment.issue.backend_name != self.backend_name
            or not attachment.backend_id
            or attachment.backend_name != self.backend_name
        ):
            return

        self.manager.delete_attachment(
            attachment.issue.backend_id, attachment.backend_id
        )
        attachment.backend_id = ""
        attachment.backend_name = ""
        attachment.save()
        return

    def create_issue_links(self, issue, linked_issues):
        if not issue.backend_id or issue.backend_name != self.backend_name:
            return

        for linked_issue in linked_issues:
            if (
                not linked_issue.backend_id
                or linked_issue.backend_name != self.backend_name
            ):
                continue

            self.manager.create_issue_link(issue.backend_id, linked_issue.backend_id)
            logger.info(
                f"Created issue link between {issue.backend_id} and {linked_issue.backend_id}."
            )

    def create_confirmation_comment(self, issue, comment_tmpl=""):
        if not comment_tmpl:
            comment_tmpl = self.get_confirmation_comment_template(issue.type)

        if not comment_tmpl:
            return

        body = (
            Template(comment_tmpl)
            .render(Context({"issue": issue}, autoescape=False))
            .strip()
        )

        # SMAX doesn't support new lines
        body = text2html(body)

        integration_user_upn = self.manager.get_user_by_upn(config.SMAX_LOGIN)
        comment = Comment(
            description=body,
            backend_user_id=integration_user_upn.id,
            is_public=True,
            is_system=True,
        )
        return self.manager.add_comment(issue.backend_id, comment)

    def _is_issue_active(self, issue):
        return issue.resolved is None

    def comment_create_is_available(self, issue=None):
        return self._is_issue_active(issue)

    def comment_update_is_available(self, comment=None):
        return self._is_issue_active(comment.issue)

    def comment_destroy_is_available(self, comment=None):
        return self._is_issue_active(comment.issue)

    def attachment_destroy_is_available(self, attachment=None):
        return self._is_issue_active(attachment.issue)

    def attachment_create_is_available(self, issue=None):
        return self._is_issue_active(issue)
