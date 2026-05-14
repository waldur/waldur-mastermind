import pickle  # noqa: S403


class ServiceBackendError(Exception):
    """Base exception for errors occurring during backend communication."""

    pass


class SerializableBackendError(ServiceBackendError):
    def __init__(self, *args, **kwargs):
        # Two failure modes the wrapping must survive:
        #
        # 1. Celery worker dying on deserialization. Some openstack client
        #    exceptions don't call Exception.__init__, so pickle round-trips
        #    them with invalid *args and the worker crashes. The pickle
        #    round-trip test below catches that.
        # 2. Celery JSON result backend losing the inner exception's message.
        #    ensure_serializable() falls back to safe_repr() when an arg
        #    can't be JSON-encoded, but only if the *outer* exception args
        #    aren't already exception instances — otherwise the inner
        #    exception is dropped or stringified inconsistently and the
        #    order's error_message ends up as bare "NotFound()". Eagerly
        #    stringifying any wrapped BaseException avoids both paths.
        args = list(args)
        for i, arg in enumerate(args):
            if isinstance(arg, BaseException):
                args[i] = str(arg).strip() or repr(arg) or type(arg).__name__
                continue
            try:
                pickle.loads(pickle.dumps(arg))  # noqa: S301
            except (pickle.PickleError, TypeError):
                args[i] = str(arg)

        super().__init__(*args, **kwargs)


class ServiceBackendNotImplemented(NotImplementedError):
    pass
