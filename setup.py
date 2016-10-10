#!/usr/bin/env python
from setuptools import setup, find_packages

install_requires = [
    'nodeconductor>=0.108.1',
]

tests_requires = [
    'ddt>=1.0.0,<1.1.0',
    'factory_boy==2.4.1',
]

setup(
    name='nodeconductor-assembly-waldur',
    version='0.1.0.dev0',
    author='OpenNode Team',
    author_email='info@opennodecloud.com',
    url='http://nodeconductor.com',
    description='NodeConductor assembly for Waldur project',
    long_description=open('README.rst').read(),
    package_dir={'': 'src'},
    packages=find_packages('src', exclude=['*.tests', '*.tests.*', 'tests.*', 'tests']),
    install_requires=install_requires,
    extras_require={
        'tests': tests_requires,
    },
    zip_safe=False,
    entry_points={
        'nodeconductor_extensions': (
            'waldur_packages = nodeconductor_assembly_waldur.packages.extension:PackagesExtension',
        ),
    },
    tests_require=tests_requires,
    test_suite='nodeconductor.server.test_runner.run_tests',
    include_package_data=True,
    classifiers=[
        'Framework :: Django',
        'Intended Audience :: Developers',
        'Intended Audience :: System Administrators',
        'Operating System :: OS Independent',
        'License :: OSI Approved :: MIT License',
    ],
)
