#!/usr/bin/env python

from setuptools import setup

install_requires = [
    'requests>=2.6.0',
]

tests_requires = [
    'responses>=0.5.0',
]


setup(
    name='ansible-waldur-module',
    version='0.3.2',
    author='OpenNode Team',
    author_email='info@opennodecloud.com',
    url='https://waldur.com',
    license='MIT',
    description='Ansible module for the Waldur API.',
    long_description=open('README.rst').read(),
    py_modules=['waldur_client'],
    install_requires=install_requires,
    tests_require=tests_requires,
    test_suite='test_waldur_client',
    classifiers=[
        'Intended Audience :: Developers',
        'Intended Audience :: System Administrators',
        'License :: OSI Approved :: MIT License',
        'Operating System :: OS Independent',
    ],
)
