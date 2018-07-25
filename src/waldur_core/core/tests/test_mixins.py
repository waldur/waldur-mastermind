from django.contrib import auth
from rest_framework import serializers, test
from six.moves import mock

from .. import exceptions, mixins

User = auth.get_user_model()


class TestUserSerializer(serializers.HyperlinkedModelSerializer):
    class Meta(object):
        model = User
        fields = ('username', 'full_name')


class ExecutorMixinTest(test.APITransactionTestCase):
    def setUp(self):
        self.executor = mixins.CreateExecutorMixin()
        self.executor.create_executor = mock.Mock()

        self.serializer = TestUserSerializer(data={
            'username': 'alice2017',
            'full_name': 'Alice Lebowski',
        })
        self.serializer.is_valid()

    def test_if_executor_succeeds_database_object_is_saved(self):
        self.executor.perform_create(self.serializer)
        self.assertTrue(User.objects.filter(username='alice2017').exists())

    def test_if_executor_fails_and_atomic_transaction_is_not_used_database_object_is_saved(self):
        self.executor.use_atomic_transaction = False
        self.executor.create_executor.execute.side_effect = exceptions.IncorrectStateException()
        self.assertRaises(exceptions.IncorrectStateException, self.executor.perform_create, self.serializer)
        self.assertTrue(User.objects.filter(username='alice2017').exists())

    def test_if_executor_fails_and_atomic_transaction_is_used_database_object_is_not_saved(self):
        self.executor.use_atomic_transaction = True
        self.executor.create_executor.execute.side_effect = exceptions.IncorrectStateException()
        self.assertRaises(exceptions.IncorrectStateException, self.executor.perform_create, self.serializer)
        self.assertFalse(User.objects.filter(username='alice2017').exists())
