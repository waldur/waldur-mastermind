from nodeconductor.core import tasks, executors


class IssueCreateExecutor(executors.BaseExecutor):

    @classmethod
    def get_task_signature(cls, issue, serialized_issue, **kwargs):
        return tasks.BackendMethodTask().si(serialized_issue, 'create_issue')


class IssueUpdateExecutor(executors.BaseExecutor):

    @classmethod
    def get_task_signature(cls, issue, serialized_issue, **kwargs):
        return tasks.BackendMethodTask().si(serialized_issue, 'update_issue')


class IssueDeleteExecutor(executors.BaseExecutor):

    @classmethod
    def get_task_signature(cls, issue, serialized_issue, **kwargs):
        if issue.key:
            return tasks.BackendMethodTask().si(serialized_issue, 'delete_issue')
