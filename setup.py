#!/usr/bin/env python
from setuptools import setup, find_packages

# defusedxml is required by djangosaml2
install_requires = [
    'ansible-waldur-module>=0.4.1',
    'defusedxml>=0.4.1',
    'influxdb>=4.1.0',
    'jira>=1.0.7',
    'nodeconductor>=0.150.4',
    'waldur_auth_social>=0.7.5',
    'waldur_auth_openid>=0.8.6',
    'nodeconductor_aws>=0.10.0',
    'nodeconductor_azure>=0.3.0',
    'nodeconductor_cost_planning>=0.5.4',
    'nodeconductor_digitalocean>=0.9.0',
    'waldur_openstack>=0.38.0',
    'nodeconductor_saml2>=0.8.2',
    'waldur_ansible>=0.3.2',
    'waldur_auth_valimo>=0.3.0',
    'waldur_freeipa>=0.2.3',
    'waldur_paypal>=0.6.2',
    'waldur_slurm>=0.3.2',
]

test_requires = [
    'ddt>=1.0.0,<1.1.0',
    'factory_boy==2.4.1',
    'freezegun==0.3.7',
]

setup(
    name='waldur-mastermind',
    version='2.8.2',
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
            'waldur_packages = waldur_mastermind.packages.extension:PackagesExtension',
            'waldur_invoices = waldur_mastermind.invoices.extension:InvoicesExtension',
            'waldur_support = waldur_mastermind.support.extension:SupportExtension',
            'waldur_analytics = waldur_mastermind.analytics.extension:AnalyticsExtension',
            'waldur_experts = waldur_mastermind.experts.extension:ExpertsExtension',
            'waldur_billing = waldur_mastermind.billing.extension:BillingExtension',
            'waldur_slurm_invoices = waldur_mastermind.slurm_invoices.extension:SlurmInvoicesExtension',
            'waldur_ansible_estimator = waldur_mastermind.ansible_estimator.extension:AnsibleEstimatorExtension',
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
