#!/usr/bin/env python

from setuptools import setup

install_requires = [
    'requests>=2.6.0',
    'six>=1.9.0',
]

tests_requires = [
    'mock>=1.0.1',
    'responses>=0.5.0',
]


setup(
    name='ansible-waldur-module',
    version='1.0.8',
    author='OpenNode Team',
    author_email='info@opennodecloud.com',
    url='https://waldur.com',
    license='MIT',
    description='Ansible module for the Waldur API.',
    long_description=open('README.rst').read(),
    py_modules=[
        'waldur_client',
        'waldur_marketplace',
        'waldur_marketplace_os_get_instance',
        'waldur_marketplace_os_instance',
        'waldur_marketplace_os_volume',
        'waldur_os_floating_ip',
        'waldur_os_instance_volume',
        'waldur_os_security_group',
        'waldur_os_security_group_gather_facts',
        'waldur_os_snapshot',
        'waldur_batch_allocation',
        'waldur_batch_offering',
    ],
    install_requires=install_requires,
    tests_require=tests_requires,
    classifiers=[
        'Intended Audience :: Developers',
        'Intended Audience :: System Administrators',
        'License :: OSI Approved :: MIT License',
        'Operating System :: OS Independent',
    ],
)
