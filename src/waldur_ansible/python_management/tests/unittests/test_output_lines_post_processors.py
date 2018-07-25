from django.test import TestCase
from waldur_ansible.python_management.backend import output_lines_post_processors


class PythonManagementServiceTest(TestCase):

    def test_extracts_installed_libs(self):
        output_lines_post_processor = output_lines_post_processors.InstalledLibrariesOutputLinesPostProcessor()
        output = 'ok: [remote_ip] => {"stdout_lines": ["pkg-resources==0.0","tensorflow==1.6rc0", "numpy==1.3"]}'

        output_lines_post_processor.post_process_line(output_lines_post_processors.InstalledLibrariesOutputLinesPostProcessor.INSTALLED_LIBRARIES_AFTER_MODIFICATIONS_TASK)
        output_lines_post_processor.post_process_line(output)

        self.assertIn(output_lines_post_processors.LibraryDs('tensorflow', '1.6rc0'), output_lines_post_processor.installed_libraries_after_modifications)
        self.assertIn(output_lines_post_processors.LibraryDs('numpy', '1.3'), output_lines_post_processor.installed_libraries_after_modifications)

    def test_extracts_installed_virtual_envs(self):
        output_lines_post_processor = output_lines_post_processors.InstalledVirtualEnvironmentsOutputLinesPostProcessor()
        output = 'ok: [remote_ip] => {"stdout_lines": ["first-virt-env","second-virt-env"]}'

        output_lines_post_processor.post_process_line(output_lines_post_processors.InstalledVirtualEnvironmentsOutputLinesPostProcessor.INSTALLED_VIRTUAL_ENVIRONMENTS_TASK)
        output_lines_post_processor.post_process_line(output)

        self.assertIn('first-virt-env', output_lines_post_processor.installed_virtual_environments)
        self.assertIn('second-virt-env', output_lines_post_processor.installed_virtual_environments)
