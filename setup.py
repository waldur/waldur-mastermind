#!/usr/bin/env python
from setuptools import setup, find_packages

# defusedxml is required by djangosaml2
install_requires = [
    'defusedxml==0.4.1',
    'influxdb>=4.1.0',
    'jira>=1.0.7',
    'nodeconductor>=0.146.4',
    'nodeconductor_auth_social>=0.7.3',
    'nodeconductor_auth_openid>=0.8.4',
    'nodeconductor_aws>=0.10.0',
    'nodeconductor_azure>=0.3.0',
    'nodeconductor_cost_planning>=0.5.0',
    'nodeconductor_digitalocean>=0.8.4',
    'nodeconductor_openstack>=0.35.0',
    'nodeconductor_saml2>=0.7.3',
    'ansible-waldur-module>=0.3.3',
    'waldur_ansible>=0.2.0',
    'waldur_freeipa>=0.2.2',
    'waldur_paypal>=0.6.0',
    'waldur_slurm>=0.1.3',
]

test_requires = [
    'ddt>=1.0.0,<1.1.0',
    'factory_boy==2.4.1',
    'freezegun==0.3.7',
]

setup(
    name='nodeconductor-assembly-waldur',
    version='2.7.0',
    author='OpenNode Team',
    author_email='info@opennodecloud.com',
    url='http://waldur.com',
    description='Waldur MasterMind is a hybrid cloud orchestrator.',
    license='MIT',
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
            'waldur_experts = nodeconductor_assembly_waldur.experts.extension:ExpertsExtension',
            'waldur_billing = nodeconductor_assembly_waldur.billing.extension:BillingExtension',
            'waldur_slurm_invoices = nodeconductor_assembly_waldur.slurm_invoices.extension:SlurmInvoicesExtension',
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
