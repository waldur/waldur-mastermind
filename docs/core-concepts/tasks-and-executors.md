# Tasks and executors

## Overview

Waldur performs logical operations using executors that combine several tasks. This document explains the executor pattern, its implementation in Waldur, and provides examples of real-world usage.

### Executor Pattern

Executor represents a logical operation on a backend, like VM creation or resize. It executes one or more background tasks and takes care of resource state updates and exception handling. The pattern provides several benefits:

- **Abstraction**: Hides complex backend interactions behind a simple interface
- **Consistency**: Ensures consistent state management across operations
- **Modularity**: Allows reusing common tasks across different operations
- **Task Coordination**: Simplifies orchestration of multiple related tasks

### Basic Executor Flow

1. **Pre-apply phase**: Prepare the resource by handling initial state transition
2. **Task generation**: Create Celery task signature or chain of tasks
3. **Success/failure handlers**: Define how to handle task completion or errors
4. **Execution**: Process tasks either asynchronously or synchronously

## Types of Executors

Waldur implements several specialized executors that inherit from the `BaseExecutor` class:

- **CreateExecutor**: For creating resources (sets state to OK on success)
- **UpdateExecutor**: For updating resources (schedules updating before actual update)
- **DeleteExecutor**: For deleting resources (schedules deleting before actual deletion)
- **ActionExecutor**: For executing specific actions on resources (custom operations)

## Scheduling Celery task from signal handler

Please use transaction.on_commit wrapper if you need to schedule Celery task from signal handler.
Otherwise, Celery task is scheduled too early and executed even if object is not yet saved to the database.
See also [django docs](https://docs.djangoproject.com/en/4.2/topics/db/transactions/#performing-actions-after-commit)

## Task Types

There are 3 types of task queues: regular (used by default), heavy and background.

### Regular tasks

Each regular task corresponds to a particular granular action - like state transition,
object deletion or backend method execution. They are supposed to be combined and
called in executors. It is not allowed to schedule tasks directly from
views or serializer.

### Heavy tasks

If task takes too long to complete, you should try to break it down into smaller regular tasks
in order to avoid flooding general queue. Only if backend does not allow to do so,
you should mark such tasks as heavy so that they use separate queue.

```python
  @shared_task(is_heavy_task=True)
  def heavy(uuid=0):
    print('** Heavy %s' % uuid)
```

### Throttle tasks

Some backends don't allow to execute several operations concurrently within the same scope.
For example, one OpenStack settings does not support provisioning of more than 4 instances together.
In this case task throttling should be used.

### Background tasks

Tasks that are executed by celerybeat should be marked as "background".
To mark task as background you need to inherit it from core.BackgroundTask:

```python
  from waldur_core.core import tasks as core_tasks
  class MyTask(core_tasks.BackgroundTask):
    def run(self):
      print('** Background task')
```

Explore BackgroundTask to discover background tasks features.

## Task registration

For class based tasks use old Task base class for compatibility:

```python
  from celery import Task
```

For functions use decorator shared_task:

```python
  from celery import shared_task


  @shared_task
  def add(x, y):
      return x + y
```

## Real-world Example: OpenStack Instance Creation

The OpenStack plugin's `InstanceCreateExecutor` demonstrates a complex real-world implementation of the executor pattern. It orchestrates multiple tasks:

1. Creates all volumes for the instance
2. Creates necessary network ports
3. Creates the instance itself on the OpenStack backend
4. Attaches volumes to the instance
5. Updates security groups
6. Creates and attaches floating IPs
7. Pulls the final state of the instance and related resources

Each step is carefully orchestrated with appropriate state transitions, error handling, and checks to ensure the operation completes successfully.

```python
class InstanceCreateExecutor(core_executors.CreateExecutor):
    @classmethod
    def get_task_signature(cls, instance, serialized_instance, ssh_key=None, flavor=None, server_group=None):
        serialized_volumes = [
            core_utils.serialize_instance(volume) for volume in instance.volumes.all()
        ]
        _tasks = [
            tasks.ThrottleProvisionStateTask().si(
                serialized_instance, state_transition="begin_creating"
            )
        ]
        _tasks += cls.create_volumes(serialized_volumes)
        _tasks += cls.create_ports(serialized_instance)
        _tasks += cls.create_instance(serialized_instance, flavor, ssh_key, server_group)
        _tasks += cls.pull_volumes(serialized_volumes)
        _tasks += cls.pull_security_groups(serialized_instance)
        _tasks += cls.create_floating_ips(instance, serialized_instance)
        _tasks += cls.pull_server_group(serialized_instance)
        _tasks += cls.pull_instance(serialized_instance)
        return chain(*_tasks)

    # ... additional methods for each step ...
```

## Common Task Types in Executors

Executors typically use the following task types:

1. **BackendMethodTask**: Executes a method on the backend resource

   ```python
   core_tasks.BackendMethodTask().si(serialized_resource, "create_resource")
   ```

2. **StateTransitionTask**: Changes the state of a resource

   ```python
   core_tasks.StateTransitionTask().si(serialized_resource, state_transition="set_ok")
   ```

3. **PollRuntimeStateTask**: Polls the backend until a resource reaches a desired state

   ```python
   core_tasks.PollRuntimeStateTask().si(
       serialized_resource,
       backend_pull_method="pull_runtime_state",
       success_state="running",
       erred_state="error"
   )
   ```

4. **PollBackendCheckTask**: Checks if a backend operation has completed

   ```python
   core_tasks.PollBackendCheckTask().si(serialized_resource, "is_resource_deleted")
   ```

## Executor-Task Relationship

Executors construct and manage task chains, providing a higher-level interface for complex operations.

## Best Practices

1. **Use appropriate executor type** based on operation (create, update, delete, action)
2. **Implement pre_apply** for necessary state transitions
3. **Handle both success and failure cases** with appropriate signatures
4. **Use transaction.on_commit** when scheduling from signal handlers
5. **Break down long-running tasks** into smaller chunks
6. **Use throttling** when backend has concurrency limitations
