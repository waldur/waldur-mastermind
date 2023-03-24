from __future__ import annotations

import datetime
import logging

from django.conf import settings
from django.utils.timezone import now

from waldur_core.core.models import User as WaldurUser
from waldur_mastermind.support import models
from waldur_zammad.backend import User as ZammadUser
from waldur_zammad.backend import ZammadBackend, ZammadBackendError

from . import SupportBackend

logger = logging.getLogger(__name__)


class ZammadServiceBackend(SupportBackend):
    def __init__(self):
        self.manager = ZammadBackend()

    backend_name = 'zammad'

    def comment_destroy_is_available(self, comment):
        if now() - comment.created < datetime.timedelta(
            minutes=settings.WALDUR_ZAMMAD['COMMENT_COOLDOWN_DURATION']
        ):
            return True

    def comment_update_is_available(self, comment=None):
        return False

    def create_issue(self, issue):
        """Create Zammad issue"""
        try:
            issue.begin_creating()
            issue.save()

            if issue.reporter:
                support_user = issue.reporter
            else:
                support_user = self.get_or_create_support_user_by_waldur_user(
                    issue.caller
                )

            zammad_issue = self.manager.add_issue(
                issue.summary, issue.description, support_user.backend_id
            )
            issue.backend_id = zammad_issue.id
            issue.backend_name = self.backend_name
            issue.status = zammad_issue.status
            issue.set_ok()
            issue.save()
            return zammad_issue
        except ZammadBackendError as e:
            issue.set_erred()
            issue.error_message = e
            issue.save()

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
            logger.info('Comment %s has been deleted.', comment.id)

        for comment in zammad_comments:
            if str(comment.id) in issue.comments.filter(
                backend_name=self.backend_name
            ).values_list('backend_id', flat=True):
                continue

            if comment.is_waldur_comment:
                continue

            support_user = self.get_or_create_support_user_by_zammad_user_id(
                comment.user_id
            )

            models.Comment.objects.create(
                issue=issue,
                created=comment.created,
                backend_id=comment.id,
                description=comment.content,
                is_public=comment.is_public,
                author=support_user,
                backend_name=self.backend_name,
            )
            logger.info('Comment %s has been created.', comment.id)

    def create_confirmation_comment(self, issue):
        pass

    def create_comment(self, comment):
        """Create Zammad comment"""
        try:
            comment.begin_creating()
            comment.save()

            # The comment will be created from an authorized user.
            # It is not possible to create a comment from another.
            zammad_comment = self.manager.add_comment(
                comment.issue.backend_id, comment.description
            )
            comment.backend_id = zammad_comment.id
            comment.backend_name = self.backend_name
            comment.set_ok()
            comment.save()
            return zammad_comment
        except ZammadBackendError as e:
            comment.set_erred()
            comment.error_message = e
            comment.save()

    def delete_comment(self, comment):
        try:
            self.manager.del_comment(comment.backend_id)
        except ZammadBackendError:
            logger.error(
                'Deleting a comment has failed. Zammad comment ID: %s'
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
            defaults={'name': waldur_user.full_name or waldur_user.username},
        )

        if support_user.backend_id:
            return support_user

        zammad_user = self.get_zammad_user_by_waldur_user(waldur_user)

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

    def get_zammad_user_by_waldur_user(
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
        return self.manager.add_user(
            login=support_user.user.username,
            email=support_user.user.email,
            firstname=support_user.user.first_name,
            lastname=support_user.user.last_name,
        )

    def pull_support_users(self):
        backend_users = self.get_users()

        for backend_user in backend_users:
            user, created = models.SupportUser.objects.get_or_create(
                backend_id=backend_user.id,
                backend_name=self.backend_name,
                defaults={'name': backend_user.name},
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
