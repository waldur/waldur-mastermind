import pickle  # noqa: S403


class ServiceBackendError(Exception):
    """Base exception for errors occurring during backend communication."""

    pass


class SerializableBackendError(ServiceBackendError):
    def __init__(self, *args, **kwargs):
        if not args:
            super().__init__(*args, **kwargs)

        # Some client exceptions, such as cinder_exceptions.ClientException
        # are not serializable by Celery, because they use custom arguments *args
        # and define __init__ method, but don't call Exception.__init__ method
        # http://docs.celeryproject.org/en/latest/userguide/tasks.html#creating-pickleable-exceptions
        # That's why when Celery worker tries to deserialize OpenStack client exception,
        # it uses empty invalid *args. It leads to unrecoverable error and worker dies.
        # When all workers are dead, all tasks are stuck in pending state forever.
        # In order to fix this issue we serialize exception to text type explicitly.
        args = list(args)
        for i, arg in enumerate(args):
            try:
                # pickle is used to check celery internal errors serialization,
                # it is safe from security point of view
                pickle.loads(pickle.dumps(arg))  # noqa: S301
            except (pickle.PickleError, TypeError):
                args[i] = str(arg)

        super().__init__(*args, **kwargs)


class ServiceBackendNotImplemented(NotImplementedError):
    pass
