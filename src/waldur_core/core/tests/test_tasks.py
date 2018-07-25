import mock
from celery.app.task import Context
from celery.backends.base import Backend
from django.test import testcases


class ExecutorTest(testcases.TestCase):
    def setUp(self):
        app = mock.Mock(**{
            'conf.result_serializer': 'json',
            'conf.accept_content': None
        })
        self.backend = Backend(app)
        errback = {"chord_size": None,
                   "task": "waldur_core.core.tasks.ErrorStateTransitionTask",
                   "args": ["waldur.obj:1"],
                   "immutable": False,
                   "subtask_type": None,
                   "kwargs": {},
                   "options": {}
                   }
        self.request = Context(errbacks=[errback], id='task_id', root_id='root_id')

    @mock.patch('waldur_core.core.tasks.group')
    def test_use_old_signature_in_task_error(self, mock_group):
        self.backend._call_task_errbacks(self.request, Exception('test'), '')
        self.assertEqual(mock_group.call_count, 1)
