from celery import chain

from waldur_core.core import executors as core_executors
from waldur_core.core import tasks as core_tasks
from waldur_core.core import utils as core_utils
from waldur_core.structure import executors as structure_executors

from . import models, tasks


class IssueCreateExecutor(core_executors.CreateExecutor):
    @classmethod
    def get_task_signature(cls, issue, serialized_issue, **kwargs):
        return chain(
            core_tasks.StateTransitionTask().si(
                serialized_issue,
                state_transition='begin_creating',
                action='',
                action_details={},
            ),
            tasks.create_issue.si(serialized_issue),
            tasks.create_confirmation_comment.si(
                serialized_issue, kwargs.get('comment_tmpl')
            ),
        )


class IssueDeleteExecutor(
    core_executors.DeleteExecutorMixin, core_executors.BaseExecutor
):
    @classmethod
    def get_task_signature(cls, issue, serialized_issue, **kwargs):
        return (core_tasks.BackendMethodTask().si(serialized_issue, 'delete_issue'),)


class SupportCleanupExecutor(structure_executors.BaseCleanupExecutor):
    pre_models = (models.Offering,)

    executors = ((models.Issue, IssueDeleteExecutor),)


class OfferingIssueCreateExecutor(core_executors.BaseExecutor):
    @classmethod
    def get_task_signature(cls, offering, serialized_offering, **kwargs):
        serialized_issue = core_utils.serialize_instance(offering.issue)
        return tasks.create_issue.si(serialized_issue)


class FeedbackExecutor(core_executors.CreateExecutor):
    @classmethod
    def get_task_signature(cls, feedback, serialized_feedback, **kwargs):
        return tasks.sync_feedback.si(serialized_feedback)
