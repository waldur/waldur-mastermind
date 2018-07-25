from django.db import IntegrityError
from django.test import TestCase

from ..factories import PlaybookFactory, PlaybookParameterFactory


class PlaybookParameterTest(TestCase):
    def setUp(self):
        self.playbook = PlaybookFactory()

    def test_cannot_create_parameters_with_same_name_for_same_playbook(self):
        param = PlaybookParameterFactory(playbook=self.playbook)
        self.assertRaises(IntegrityError, PlaybookParameterFactory, playbook=self.playbook, name=param.name)
