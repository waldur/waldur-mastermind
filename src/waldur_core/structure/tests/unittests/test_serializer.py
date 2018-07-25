
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import resolve
from rest_framework.test import APIRequestFactory
from six.moves.urllib.parse import urlparse

from .. import factories as structure_factories
from ...serializers import BasicUserSerializer

User = get_user_model()


class UUIDSerializerTest(TestCase):
    def setUp(self):
        factory = APIRequestFactory()
        request = factory.get('/users/')
        context = {'request': request}
        user = structure_factories.UserFactory()
        serializer = BasicUserSerializer(instance=user, context=context)
        self.data = serializer.data

    def test_url_and_uuid_do_not_contain_hyphenation(self):
        path = urlparse(self.data['url']).path
        match = resolve(path)
        self.assertEqual(match.url_name, 'user-detail')

        value = match.kwargs.get('uuid')
        self.assertEqual(value, self.data['uuid'])
        self.assertTrue('-' not in value)
