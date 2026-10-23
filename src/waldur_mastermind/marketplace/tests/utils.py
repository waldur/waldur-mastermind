from unittest.mock import patch

from django.db import transaction

from waldur_core.structure.tests.views import TestNewInstanceViewSet
from waldur_mastermind.marketplace import processors


def _django_on_commit(func, using=None, robust=False):
    transaction.get_connection(using).on_commit(func, robust)


def real_on_commit():
    """Undo conftest's _immediate_on_commit for one block.

    That fixture runs every on_commit callback the moment it is registered,
    so a test can't otherwise see that nothing happens before the commit, or
    that a rollback discards the callback. Combined with
    captureOnCommitCallbacks(execute=True), the callbacks run once the block
    ends, after the code that registered them has finished, as on commit.
    """
    return patch.object(transaction, "on_commit", _django_on_commit)


class TestCreateProcessor(processors.BaseCreateResourceProcessor):
    __test__ = False
    viewset = TestNewInstanceViewSet
    fields = ["name"]

    def validate_order(self, request):
        pass


class TestUpdateScopedProcessor(processors.UpdateScopedResourceProcessor):
    __test__ = False

    def validate_order(self, request):
        pass

    def update_limits_process(self, user):
        return True
