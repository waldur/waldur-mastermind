from django.test import TestCase

from waldur_core.structure.tests import factories


class ServiceProjectLinkTest(TestCase):

    def setUp(self):
        self.link = factories.TestServiceProjectLinkFactory()

    def test_link_validation_state_is_ERRED_if_service_does_not_satisfy_project_certifications(self):
        certification = factories.ServiceCertificationFactory()
        self.assertEqual(self.link.States.OK, self.link.validation_state)

        self.link.project.certifications.add(certification)

        self.assertEqual(self.link.States.ERRED, self.link.validation_state)

    def test_link_validation_state_is_OK_if_project_certifications_is_a_subset_of_service_certifications(self):
        certifications = factories.ServiceCertificationFactory.create_batch(2)
        self.link.project.certifications.add(*certifications)
        certifications.append(factories.ServiceCertificationFactory())

        self.link.service.settings.certifications.add(*certifications)

        self.assertEqual(self.link.States.OK, self.link.validation_state)
