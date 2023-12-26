import logging

from waldur_smax.backend import SmaxBackend, User

from . import SupportBackend

logger = logging.getLogger(__name__)


class SmaxServiceBackend(SupportBackend):
    def __init__(self):
        self.manager = SmaxBackend()

    backend_name = 'smax'

    def create_issue(self, issue):
        """Create SMAX issue"""
        issue.begin_creating()
        issue.save()

        user = User(
            issue.caller.email, issue.caller.full_name, upn=issue.caller.uuid.hex
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
