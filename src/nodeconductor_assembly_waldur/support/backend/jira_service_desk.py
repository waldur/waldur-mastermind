from jira import Comment as JiraComment
from jira.utils import json_loads
import json

from nodeconductor_assembly_waldur.support.backend.base import AtlassianBackend


class JiraServiceDesk(AtlassianBackend):

    def create_comment(self, comment, is_internal=False):
        backend_comment = self._add_comment(
            comment.issue.backend_id,
            self._prepare_comment_message(comment),
            is_internal=is_internal,
        )
        comment.backend_id = backend_comment.id
        comment.is_public = not is_internal
        comment.save(update_fields=['backend_id', 'is_public'])

    def _add_comment(self, issue, body, is_internal):
        data = {
            'body': body,
            "properties": [{"key": "sd.public.comment", "value": {"internal": is_internal}},]
        }

        url = self.manager._get_url('issue/' + str(issue) + '/comment')
        r = self.manager._session.post(
            url, data=json.dumps(data))

        comment = JiraComment(self._options, self._session, raw=json_loads(r))
        return comment

