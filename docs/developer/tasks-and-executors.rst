Tasks and executors
===================

Scheduling Celery task from signal handler
------------------------------------------

Please use transaction.on_commit wrapper if you need to schedule Celery task from signal handler.
Otherwise, Celery task is scheduled too early and executed even if object is not yet saved to the database.
See also `django docs <https://docs.djangoproject.com/en/1.11/topics/db/transactions/#performing-actions-after-commit>`_

Executors
---------
Waldur performs logical operations using executors that combine several tasks.

Executor represents a logical operation on a backend, like VM creation or resize.
It executes one or more background tasks and takes care of resource state updates
and exception handling.

Tasks
-----

There are 3 types of task queues: regular (used by default), heavy and background.

Task registration
-----------------

For class based tasks use old Task base class for compatibility:

.. code-block:: python

    from celery import Task

For functions use decorator shared_task:

.. code-block:: python

    from celery import shared_task


    @shared_task
    def add(x, y):
        return x + y

Regular tasks
^^^^^^^^^^^^^

Each regular task corresponds to a particular granular action - like state transition,
object deletion or backend method execution. They are supposed to be combined and
called in executors. It is not allowed to schedule tasks directly from
views or serializer.

Heavy tasks
^^^^^^^^^^^

If task takes too long to complete, you should try to break it down into smaller regular tasks
in order to avoid flooding general queue. Only if backend does not allow to do so,
you should mark such tasks as heavy so that they use separate queue.

.. code-block:: python

    @shared_task(is_heavy_task=True)
    def heavy(uuid=0):
        print '** Heavy %s' % uuid

Throttle tasks
^^^^^^^^^^^^^^

Some backends don't allow to execute several operations concurrently within the same scope.
For example, one OpenStack settings does not support provisioning of more than 4 instances together.
In this case task throttling should be used.

Background tasks
^^^^^^^^^^^^^^^^

Tasks that are executed by celerybeat should be marked as "background".
To mark task as background you need to inherit it from core.BackgroundTask:

.. code-block:: python

    from waldur_core.core import tasks as core_tasks
    class MyTask(core_tasks.BackgroundTask):
        def run(self):
            print '** background task'

Explore BackgroundTask to discover background tasks features.
