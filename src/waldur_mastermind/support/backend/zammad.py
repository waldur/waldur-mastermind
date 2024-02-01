import base64
import datetime
import functools
import logging
import mimetypes
import os

from constance import config
from django.core.files.base import ContentFile
from django.utils.timezone import now
from rest_framework.exceptions import ValidationError

from waldur_core.core.models import User as WaldurUser
from waldur_mastermind.support import models
from waldur_zammad.backend import User as ZammadUser
from waldur_zammad.backend import ZammadBackend, ZammadBackendError

from . import SupportBackend, SupportBackendType

logger = logging.getLogger(__name__)


class ZammadServiceBackendError(ZammadBackendError):
    pass


def reraise_exceptions(msg=None):
    def wrap(func):
        @functools.wraps(func)
        def wrapped(self, *args, **kwargs):
            try:
                return func(self, *args, **kwargs)
            except ZammadBackendError as e:
                raise ValidationError(e)

        return wrapped

    return wrap


class ZammadServiceBackend(SupportBackend):
    def __init__(self):
        self.manager = ZammadBackend()

    backend_name = SupportBackendType.ZAMMAD

    def comment_destroy_is_available(self, comment):
        if not comment.backend_id:
            return True

        if comment.is_public:
            return False

        if now() - comment.created < datetime.timedelta(
            minutes=config.ZAMMAD_COMMENT_COOLDOWN_DURATION
        ):
            return True

    def attachment_destroy_is_available(self, attachment):
        if not attachment.backend_id:
            return True

        if now() - attachment.created < datetime.timedelta(
            minutes=config.ZAMMAD_COMMENT_COOLDOWN_DURATION
        ):
            return True

    def comment_update_is_available(self, comment=None):
        return False

    @reraise_exceptions()
    def create_issue(self, issue):
        """Create Zammad issue"""
        issue.begin_creating()
        issue.save()

        if issue.reporter:
            support_user = issue.reporter
        else:
            support_user = self.get_or_create_support_user_by_waldur_user(issue.caller)

        zammad_issue = self.manager.add_issue(
            issue.summary,
            issue.description,
            support_user.backend_id,
            support_user.user.email,  # support user in this case is always connected to a waldur user
            tags=[config.SITE_NAME],
        )
        issue.backend_id = zammad_issue.id
        issue.key = zammad_issue.id
        issue.backend_name = self.backend_name
        issue.status = zammad_issue.status
        issue.set_ok()
        issue.save()
        return zammad_issue

    def update_waldur_issue_from_zammad(self, issue):
        zammad_issue = self.manager.get_issue(issue.backend_id)
        issue.status = zammad_issue.status
        issue.summary = zammad_issue.summary
        return issue.save()

    def update_waldur_comments_from_zammad(self, issue):
        zammad_comments = self.manager.get_comments(issue.backend_id)

        for comment in issue.comments.filter(backend_name=self.backend_name).exclude(
            backend_id__in=[c.id for c in zammad_comments]
        ):
            comment.delete()
            logger.info("Comment %s has been deleted.", comment.id)

        self.del_waldur_attachments_from_zammad(issue)

        for comment in zammad_comments:
            if comment.is_waldur_comment:
                continue

            if str(comment.id) in issue.comments.filter(
                backend_name=self.backend_name
            ).values_list("backend_id", flat=True):
                waldur_comment = models.Comment.objects.get(
                    backend_id=comment.id, backend_name=self.backend_name
                )

                if comment.is_public != waldur_comment.is_public:
                    waldur_comment.is_public = comment.is_public
                    waldur_comment.save()
                    logger.info(f"Comment {waldur_comment.uuid.hex} has been changed.")

                continue

            support_user = self.get_or_create_support_user_by_zammad_user_id(
                comment.user_id
            )

            new_comment = models.Comment.objects.create(
                issue=issue,
                created=comment.created,
                backend_id=comment.id,
                description=comment.content,
                is_public=comment.is_public,
                author=support_user,
                backend_name=self.backend_name,
            )
            logger.info("Comment %s has been created.", comment.id)
            self.add_waldur_attachments_from_zammad(new_comment)

    def add_waldur_attachments_from_zammad(self, comment):
        zammad_comment = self.manager.get_comment(comment.backend_id)

        for zammad_attachment in zammad_comment.attachments:
            waldur_attachment = models.Attachment.objects.create(
                issue=comment.issue,
                mime_type=zammad_attachment.content_type or "",
                file_size=zammad_attachment.size,
                author=comment.author,
                backend_id=zammad_attachment.id,
                backend_name=self.backend_name,
            )

            waldur_attachment.file.save(
                zammad_attachment.filename,
                ContentFile(self.manager.attachment_download(zammad_attachment)),
            )
            logger.info(
                "Attachment ID: %s, file: %s, size: %s has been created.",
                zammad_attachment.id,
                zammad_attachment.filename,
                zammad_attachment.size,
            )

    def del_waldur_attachments_from_zammad(self, issue):
        zammad_attachments = self.manager.get_ticket_attachments(issue.backend_id)

        for attachment in issue.attachments.filter(
            backend_name=self.backend_name
        ).exclude(backend_id__in=[a.id for a in zammad_attachments]):
            attachment.delete()
            logger.info("Attachment %s has been deleted.", attachment.id)

    def create_confirmation_comment(self, issue):
        pass

    @reraise_exceptions()
    def create_comment(self, comment):
        """Create Zammad comment"""
        comment.begin_creating()
        comment.save()

        self.get_or_create_zammad_user_for_support_user(comment.author)

        zammad_comment = self.manager.add_comment(
            comment.issue.backend_id,
            comment.description,
            support_user_name=comment.author.user.full_name,
            zammad_user_email=comment.author.user.email,
            # we do not set comment.is_public to True as it would disable deletion
        )
        comment.backend_id = zammad_comment.id
        comment.backend_name = self.backend_name
        comment.set_ok()
        comment.save()
        return zammad_comment

    def delete_comment(self, comment):
        try:
            self.manager.del_comment(comment.backend_id)
        except ZammadBackendError:
            logger.error(
                "Deleting a comment has failed. Zammad comment ID: %s"
                % comment.backend_id
            )

    def get_or_create_support_user_by_zammad_user_id(self, zammad_user_id):
        try:
            return models.SupportUser.objects.get(
                backend_id=zammad_user_id, backend_name=self.backend_name
            )
        except models.SupportUser.DoesNotExist:
            pass

        zammad_user = self.manager.get_user_by_id(zammad_user_id)

        support_user = models.SupportUser.objects.create(
            backend_id=zammad_user.id,
            backend_name=self.backend_name,
            name=zammad_user.name,
        )

        waldur_user = self.get_waldur_user_by_zammad_user(zammad_user)

        if waldur_user:
            support_user.user = waldur_user
            support_user.save()

        return support_user

    def get_users(self):
        return self.manager.get_users()

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

        zammad_user = self.find_zammad_user_by_waldur_user_login_and_email(waldur_user)

        if not zammad_user:
            zammad_user = self.create_zammad_user_for_support_user(support_user)

        support_user.backend_id = zammad_user.id
        support_user.save()
        return support_user

    def get_waldur_user_by_zammad_user(
        self, zammad_user: ZammadUser
    ) -> WaldurUser | None:
        try:
            return WaldurUser.objects.get(username=zammad_user.login)
        except WaldurUser.DoesNotExist:
            try:
                return WaldurUser.objects.get(email=zammad_user.email)
            except (WaldurUser.DoesNotExist, WaldurUser.MultipleObjectsReturned):
                return

    def find_zammad_user_by_waldur_user_login_and_email(
        self, waldur_user: WaldurUser
    ) -> ZammadUser | None:
        backend_user_by_login = self.manager.get_user_by_login(waldur_user.username)

        if backend_user_by_login:
            return backend_user_by_login

        backend_user_by_email = self.manager.get_user_by_email(waldur_user.email)

        if backend_user_by_email:
            return backend_user_by_email

    def create_zammad_user_for_support_user(
        self, support_user: models.SupportUser
    ) -> ZammadUser:
        zammad_user = self.manager.add_user(
            login=support_user.user.username,
            email=support_user.user.email,
            firstname=support_user.user.first_name,
            lastname=support_user.user.last_name,
        )
        support_user.backend_id = zammad_user.id
        support_user.save()
        return zammad_user

    def get_or_create_zammad_user_for_support_user(self, support_user):
        if (
            not support_user.backend_name
            or support_user.backend_name == self.backend_name
        ) and not support_user.backend_id:
            return self.create_zammad_user_for_support_user(support_user)

        if support_user.backend_name == self.backend_name and support_user.backend_id:
            return self.manager.get_user_by_id(support_user.backend_id)

    def pull_support_users(self):
        backend_users = self.get_users()

        for backend_user in backend_users:
            user, created = models.SupportUser.objects.get_or_create(
                backend_id=backend_user.id,
                backend_name=self.backend_name,
                defaults={"name": backend_user.name},
            )
            if not created and user.name != backend_user.name:
                user.name = backend_user.name
                user.save()
            if not user.is_active:
                user.is_active = True
                user.save()

            if not user.user:
                waldur_user = self.get_waldur_user_by_zammad_user(backend_user)

                if waldur_user:
                    user.user = waldur_user
                    user.save()

        models.SupportUser.objects.filter(backend_name=self.backend_name).exclude(
            backend_id__in=[u.id for u in backend_users]
        ).update(is_active=False)

    @reraise_exceptions()
    def create_attachment(self, attachment: models.Attachment):
        filename = os.path.basename(attachment.file.name)
        mime_type = attachment.mime_type

        if not mime_type:
            mime_type, _ = mimetypes.guess_type(filename)

        author_name = None

        if attachment.author and attachment.author.name:
            author_name = attachment.author.name

        waldur_user_email = ""

        if attachment.author and attachment.author.user.email:
            waldur_user_email = attachment.author.user.email

        zammad_attachment = self.manager.add_attachment(
            ticket_id=attachment.issue.backend_id,
            filename=filename,
            data_base64_encoded=base64.b64encode(attachment.file.read()),
            mime_type=mime_type,
            author_name=author_name,
            waldur_user_email=waldur_user_email,
        )
        attachment.backend_id = zammad_attachment.id
        attachment.backend_name = self.backend_name
        attachment.save()

    def delete_attachment(self, attachment):
        if (
            not attachment.backend_id
            or not attachment.issue.backend_id
            or attachment.backend_name != self.backend_name
        ):
            return

        zammad_attachments = [
            za
            for za in self.manager.get_ticket_attachments(attachment.issue.backend_id)
            if za.id == attachment.backend_id
        ]

        if zammad_attachments:
            zammad_attachment = zammad_attachments[0]
        else:
            return

        self.manager.del_comment(zammad_attachment.article_id)
        logger.info("Comment %s has been deleted.", zammad_attachment.comment.id)

    def update_issue(self, issue):
        if issue.backend_id:
            self.manager.update_issue(issue.backend_id, title=issue.summary)

    def delete_issue(self, issue):
        if issue.backend_id:
            self.manager.delete_issue(issue.backend_id)

    def pull_priorities(self):
        for priority in self.manager.pull_priorities():
            priority, created = models.Priority.objects.get_or_create(
                backend_id=priority.id,
                name=priority.name,
                backend_name=self.backend_name,
            )
            if created:
                logger.info("Priority %s has been created.", priority.name)

    def update_comment(self, comment):
        raise ZammadServiceBackendError("Updating comments is not supported.")
