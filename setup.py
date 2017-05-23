#!/usr/bin/env python
from setuptools import setup, find_packages

# defusedxml is required by djangosaml2
install_requires = [
    'defusedxml==0.4.1',
    'influxdb>=4.1.0',
    'jira>=1.0.7',
    'nodeconductor>=0.138.0',
    'nodeconductor_auth_social>=0.6.0',
    'nodeconductor_auth_openid>=0.6.0',
    'nodeconductor_aws>=0.7.0',
    'nodeconductor_cost_planning>=0.3.0',
    'nodeconductor_digitalocean>=0.6.0',
    'nodeconductor_openstack>=0.26.0',
    'nodeconductor_saml2>=0.3.3',
]

test_requires = [
    'ddt>=1.0.0,<1.1.0',
    'factory_boy==2.4.1',
    'freezegun==0.3.7',
]

setup(
    name='nodeconductor-assembly-waldur',
    version='2.5.2',
    author='OpenNode Team',
    author_email='info@opennodecloud.com',
    url='http://nodeconductor.com',
    description='NodeConductor assembly for Waldur project',
    long_description=open('README.rst').read(),
    package_dir={'': 'src'},
    packages=find_packages('src', exclude=['*.tests', '*.tests.*', 'tests.*', 'tests']),
    install_requires=install_requires,
    extras_require={
        'test': test_requires,
    },
    zip_safe=False,
    entry_points={
        'nodeconductor_extensions': (
            'waldur_packages = nodeconductor_assembly_waldur.packages.extension:PackagesExtension',
            'waldur_invoices = nodeconductor_assembly_waldur.invoices.extension:InvoicesExtension',
            'waldur_support = nodeconductor_assembly_waldur.support.extension:SupportExtension',
            'waldur_analytics = nodeconductor_assembly_waldur.analytics.extension:AnalyticsExtension',
        ),
    },
    include_package_data=True,
    classifiers=[
        'Framework :: Django',
        'Intended Audience :: Developers',
        'Intended Audience :: System Administrators',
        'Operating System :: OS Independent',
        'License :: OSI Approved :: MIT License',
    ],
)
