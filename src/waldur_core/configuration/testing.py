import inspect
from functools import wraps

from django.test import SimpleTestCase

from waldur_core.configuration import overrides, registry
from waldur_core.configuration.signals import setting_updated


class override_config:
    """Override settings in tests, without writing to the database.

    Works as a context manager, as a decorator of test functions (sync or
    async), and as a decorator of ``SimpleTestCase`` subclasses, where the
    override covers ``setUp``, the test and ``tearDown``.
    One instance may be reused for any number of those.
    """

    def __init__(self, **values):
        for key in values:
            if not registry.has(key):
                raise AttributeError(key)
        self.values = values
        self._layers = []

    def enable(self):
        self._layers.append(overrides.push(self.values))
        self._announce()

    def disable(self):
        overrides.pop(self._layers.pop())
        self._announce()

    def _announce(self):
        # Receivers drop what they derived from settings, such as the cached
        # public configuration, as they do on a write.
        for key in self.values:
            setting_updated.send(
                sender=override_config, key=key, old_value=None, new_value=None
            )

    def __enter__(self):
        self.enable()

    def __exit__(self, exc_type, exc_value, traceback):
        self.disable()

    def __call__(self, test_func):
        if isinstance(test_func, type):
            if not issubclass(test_func, SimpleTestCase):
                raise TypeError(
                    "Only SimpleTestCase subclasses can be decorated with "
                    "override_config"
                )
            return self._decorate_test_case(test_func)

        if inspect.iscoroutinefunction(test_func):

            @wraps(test_func)
            async def inner_async(*args, **kwargs):
                with self:
                    return await test_func(*args, **kwargs)

            return inner_async

        @wraps(test_func)
        def inner(*args, **kwargs):
            with self:
                return test_func(*args, **kwargs)

        return inner

    def _decorate_test_case(self, test_case):
        original_pre_setup = test_case._pre_setup.__func__
        original_post_teardown = test_case._post_teardown

        override = self

        @classmethod
        def _pre_setup(cls):
            override.enable()
            try:
                original_pre_setup(cls)
            except BaseException:
                # _post_teardown does not run when set-up fails.
                override.disable()
                raise

        def _post_teardown(self):
            try:
                original_post_teardown(self)
            finally:
                override.disable()

        test_case._pre_setup = _pre_setup
        test_case._post_teardown = _post_teardown
        return test_case
