from __future__ import unicode_literals

from zipfile import ZipFile

from django.core.files.base import ContentFile
from django.test import TestCase
from rest_framework.serializers import ValidationError
from waldur_ansible.playbook_jobs import serializers


class PlaybookSerializerTest(TestCase):
    def test_valid_playbook_should_succeed(self):
        data = self._get_data()
        serializer = serializers.PlaybookSerializer(data=data)
        try:
            serializer.is_valid(raise_exception=True)
        except ValidationError as e:
            self.fail(e)

    def test_playbook_with_invalid_file_extension_should_fail(self):
        data = self._get_data(filename='playbook.invalid')
        serializer = serializers.PlaybookSerializer(data=data)

        with self.assertRaises(ValidationError) as e:
            serializer.is_valid(raise_exception=True)
        self.assertEqual(e.exception.detail['archive'], ["File must have '.zip' extension."])

    def test_playbook_with_invalid_file_type_should_fail(self):
        data = self._get_data()
        data['archive'] = ContentFile('content', name='playbook.zip')
        serializer = serializers.PlaybookSerializer(data=data)

        with self.assertRaises(ValidationError) as e:
            serializer.is_valid(raise_exception=True)
        self.assertEqual(e.exception.detail['archive'], ['ZIP file must be uploaded.'])

    def test_playbook_with_invalid_entrypoint_should_fail(self):
        data = self._get_data()
        data['entrypoint'] = 'invalid'
        serializer = serializers.PlaybookSerializer(data=data)

        with self.assertRaises(ValidationError) as e:
            serializer.is_valid(raise_exception=True)
        self.assertEqual(e.exception.detail['non_field_errors'],
                         ['Failed to find entrypoint %s in archive %s.' % (data['entrypoint'], data['archive'].name)])

    def _get_data(self, filename='playbook.zip'):
        temp_file = ContentFile('file content', name=filename)
        zip_file = ZipFile(temp_file, 'w')
        zip_file.writestr('main.yml', 'test'.encode('utf8'))
        zip_file.close()
        temp_file.seek(0)

        return {
            'name': 'test playbook',
            'archive': temp_file,
            'entrypoint': 'main.yml',
            'parameters': [
                {
                    'name': 'parameter1',
                },
                {
                    'name': 'parameter2',
                },
            ]
        }


class PlaybookParameterSerializerTest(TestCase):
    def test_parameter_with_invalid_name_format_should_fail(self):
        data = {
            'name': 'parameter name with spaces'
        }
        serializer = serializers.PlaybookParameterSerializer(data=data)
        self.assertRaises(ValidationError, serializer.is_valid, raise_exception=True)

    def test_parameter_with_valid_name_format_should_succeed(self):
        data = {
            'name': 'parameter1'
        }
        serializer = serializers.PlaybookParameterSerializer(data=data)
        try:
            serializer.is_valid(raise_exception=True)
        except ValidationError as e:
            self.fail(e)
