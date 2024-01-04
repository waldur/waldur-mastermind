import logging

from waldur_core.core.models import User as WaldurUser
from waldur_mastermind.support import models
from waldur_smax.backend import Comment, SmaxBackend, User

from . import SupportBackend

logger = logging.getLogger(__name__)


class SmaxServiceBackend(SupportBackend):
    def __init__(self):
        self.manager = SmaxBackend()

    backend_name = 'smax'

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

    def create_issue(self, issue):
        """Create SMAX issue"""
        issue.begin_creating()
        issue.save()

        if issue.reporter:
            support_user = issue.reporter
        else:
            support_user = self.get_or_create_support_user_by_waldur_user(issue.caller)

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
        issue.backend_name = self.backend_name
        issue.set_ok()
        issue.save()
        return smax_issue

    def create_confirmation_comment(self, *args, **kwargs):
        pass

    def periodic_task(self):
        issues = models.Issue.objects.filter(backend_name=self.backend_name)

        for issue in issues:
            # update an issue
            backend_issue = self.manager.get_issue(issue.backend_id)
            issue.description = backend_issue.description
            issue.summary = backend_issue.summary
            issue.status = backend_issue.status
            issue.save()

            # update comments
            for backend_comment in self.manager.get_comments(issue.backend_id):
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
                    logger.info(
                        f'Smax support user {backend_user.name} has been created.'
                    )

                models.Comment.objects.create(
                    backend_name=self.backend_name,
                    issue_id=issue.id,
                    author=support_user,
                    description=backend_comment.description,
                    is_public=backend_comment.is_public,
                    backend_id=backend_comment.id,
                    state=models.Comment.States.OK,
                )
                logger.info(f'Smax comment {backend_comment.id} has been created.')

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

    def get_or_create_smax_user_for_support_user(self, support_user):
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

        backend_user_id = self.get_or_create_smax_user_for_support_user(comment.author)
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
