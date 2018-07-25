# This file mainly exists to allow python setup.py test to work.
import os
import sys

from setuptools.command.test import test as TestCommand

os.environ['DJANGO_SETTINGS_MODULE'] = 'waldur_core.server.test_settings'
test_dir = os.path.abspath(os.path.join(os.path.dirname(os.path.dirname(__file__)), os.path.pardir))
sys.path.insert(0, test_dir)


class Test(TestCommand):
    user_options = TestCommand.user_options + [('parallel', None, "Runs tests in separate parallel processes.")]

    def initialize_options(self):
        TestCommand.initialize_options(self)
        self.parallel = 0

    def run_tests(self):
        import django
        from django.conf import settings
        from django.test.runner import default_test_processes
        from django.test.utils import get_runner

        django.setup()
        test_runner_class = get_runner(settings)
        try:
            import xmlrunner

            class XMLTestRunner(test_runner_class):
                def run_suite(self, suite, **kwargs):
                    verbosity = getattr(settings, 'TEST_OUTPUT_VERBOSE', 1)
                    if isinstance(verbosity, bool):
                        verbosity = (1, 2)[verbosity]
                    descriptions = getattr(settings, 'TEST_OUTPUT_DESCRIPTIONS', False)
                    output = getattr(settings, 'TEST_OUTPUT_DIR', '.')

                    return xmlrunner.XMLTestRunner(
                        verbosity=verbosity,
                        descriptions=descriptions,
                        output=output
                    ).run(suite)
            test_runner_class = XMLTestRunner
        except ImportError:
            print("Not generating XML reports, run 'pip install unittest-xml-reporting' to enable XML report generation")

        if self.parallel:
            parallel = default_test_processes()
        else:
            parallel = 0

        test_runner = test_runner_class(verbosity=2, interactive=True, parallel=parallel)

        failures = test_runner.run_tests([])
        sys.exit(bool(failures))
