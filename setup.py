#!/usr/bin/env python

from setuptools import setup

install_requires = [
    'requests>=2.6.0,!=2.12.2,!=2.13.0',
]


setup(
    name='python-waldur-client',
    version='0.0.1',
    author='OpenNode Team',
    author_email='info@opennodecloud.com',
    description='Waldur Client for OpenStack infrastructure management.',
    long_description=open('README.rst').read(),
    py_modules=['waldur_client'],
    install_requires=install_requires,
    classifiers=[
        'Intended Audience :: Developers',
        'Intended Audience :: System Administrators',
        'License :: OSI Approved :: MIT License',
        'Operating System :: OS Independent',
    ],
)
