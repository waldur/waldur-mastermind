import json
from collections import namedtuple

LibraryDs = namedtuple('LibraryDs', ['name', 'version'])


class InstalledLibrariesOutputLinesPostProcessor(object):
    INSTALLED_LIBRARIES_AFTER_MODIFICATIONS_TASK = 'Final list of all installed libraries in the venv'

    def __init__(self):
        self.stop_line_processing = False
        self.next_line_contains_installed_libraries_after_modifications = False
        self.installed_libraries_after_modifications = []
        self.installed_libraries_before_modifications = []

    def post_process_line(self, output_line):
        if not self.stop_line_processing:
            if not self.next_line_contains_installed_libraries_after_modifications:
                if InstalledLibrariesOutputLinesPostProcessor.INSTALLED_LIBRARIES_AFTER_MODIFICATIONS_TASK in output_line:
                    self.next_line_contains_installed_libraries_after_modifications = True
            else:
                ip_and_command_info_parts = output_line.split(' => ')
                command_info = json.loads(ip_and_command_info_parts[1])
                installed_libraries_with_versions = command_info['stdout_lines']

                for installed_library_with_version in installed_libraries_with_versions:
                    name_and_version_parts = installed_library_with_version.split('==')
                    if name_and_version_parts[0] != 'pkg-resources':
                        self.installed_libraries_after_modifications.append(
                            LibraryDs(name=name_and_version_parts[0], version=name_and_version_parts[1]))
                self.stop_line_processing = True


class InstalledVirtualEnvironmentsOutputLinesPostProcessor(object):
    INSTALLED_VIRTUAL_ENVIRONMENTS_TASK = 'list all installed virtual environments'

    def __init__(self):
        self.stop_line_processing = False
        self.next_line_contains_installed_virtual_environments = False
        self.installed_virtual_environments = []

    def post_process_line(self, output_line):
        if not self.stop_line_processing:
            if not self.next_line_contains_installed_virtual_environments:
                if InstalledVirtualEnvironmentsOutputLinesPostProcessor.INSTALLED_VIRTUAL_ENVIRONMENTS_TASK in output_line:
                    self.next_line_contains_installed_virtual_environments = True
            else:
                ip_and_command_info_parts = output_line.split(' => ')
                command_info = json.loads(ip_and_command_info_parts[1])
                installed_virtual_envs = command_info['stdout_lines']

                for installed_virtual_env in installed_virtual_envs:
                    self.installed_virtual_environments.append(installed_virtual_env)
                self.stop_line_processing = True


class InitializationOutputLinesPostProcessor(object):
    PYTHON_VERSION_IDENTIFYING_TASK = 'Identify installed python version'

    def __init__(self):
        self.stop_line_processing = False
        self.next_line_contains_python_version = False
        self.python_version = []

    def post_process_line(self, output_line):
        if not self.stop_line_processing:
            if not self.next_line_contains_python_version:
                if InitializationOutputLinesPostProcessor.PYTHON_VERSION_IDENTIFYING_TASK in output_line:
                    self.next_line_contains_python_version = True
            else:
                ip_and_command_info_parts = output_line.split(' => ')
                command_info = json.loads(ip_and_command_info_parts[1])
                self.python_version = command_info['stdout_lines'][0].replace('Python', '')
                self.stop_line_processing = True


class NullOutputLinesPostProcessor(object):
    def post_process_line(self, output_line):
        pass
