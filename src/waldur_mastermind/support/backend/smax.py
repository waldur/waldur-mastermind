import logging
import mimetypes
import os

from django.core.files.base import ContentFile
from django.template import Context, Template

from waldur_core.core.models import User as WaldurUser
from waldur_mastermind.support import models
from waldur_smax.backend import Comment, SmaxBackend, User

from . import SupportBackend

logger = logging.getLogger(__name__)


class SmaxServiceBackend(SupportBackend):
    def __init__(self):
        self.manager = SmaxBackend()

    backend_name = "smax"

    def get_or_create_support_user_by_waldur_user(
        self, waldur_user: WaldurUser
    ) -> models.SupportUser:
        support_user, _ = models.SupportUser.objects.get_or_create(
            user=waldur_user,
            backend_name=self.backend_name,
            defaults={"name": waldur_user.full_name or waldur_user.username},
        )

        if support_user.backend_id:
            return support_user

        backend_user = self.manager.search_user_by_email(waldur_user.email)

        if not backend_user:
            user = User(
                support_user.user.email,
                support_user.user.full_name,
                upn=support_user.uuid.hex,
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
            upn=support_user.uuid.hex,
        )

        smax_issue = self.manager.add_issue(
            issue.summary,
            user,
            issue.description,
        )
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
                mime_type=backend_attachment.content_type or "",
                file_size=backend_attachment.size,
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

    def sync_issues(self):
        issues = models.Issue.objects.filter(backend_name=self.backend_name)

        for issue in issues:
            self.update_waldur_issue_from_smax(issue)

    def create_smax_user_for_support_user(
        self, support_user: models.SupportUser
    ) -> User:
        smax_user = User(
            email=support_user.user.email,
            name=support_user.user.full_name,
            upn=support_user.uuid.hex,
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

    def attachment_destroy_is_available(self, *args, **kwargs):
        return True

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
        comment = Comment(description=body, backend_user_id=issue.reporter.backend_id)
        return self.manager.add_comment(issue.backend_id, comment)
