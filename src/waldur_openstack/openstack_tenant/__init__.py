from collections import OrderedDict

from waldur_core import _get_version

__version__ = _get_version()

default_app_config = 'waldur_openstack.openstack_tenant.apps.OpenStackTenantConfig'


class PriceItemTypes:
    FLAVOR = 'flavor'
    STORAGE = 'storage'
    LICENSE_APPLICATION = 'license-application'
    LICENSE_OS = 'license-os'
    SUPPORT = 'support'

    CHOICES = (
        (FLAVOR, 'flavor'),
        (STORAGE, 'storage'),
        (LICENSE_APPLICATION, 'license-application'),
        (LICENSE_OS, 'license-os'),
        (SUPPORT, 'support'),
    )


class OsTypes:
    CENTOS6 = 'centos6'
    CENTOS7 = 'centos7'
    UBUNTU = 'ubuntu'
    RHEL6 = 'rhel6'
    RHEL7 = 'rhel7'
    FREEBSD = 'freebsd'
    WINDOWS = 'windows'
    OTHER = 'other'

    CHOICES = (
        (CENTOS6, 'Centos 6'),
        (CENTOS7, 'Centos 7'),
        (UBUNTU, 'Ubuntu'),
        (RHEL6, 'RedHat 6'),
        (RHEL7, 'RedHat 7'),
        (FREEBSD, 'FreeBSD'),
        (WINDOWS, 'Windows'),
        (OTHER, 'Other'),
    )

    CATEGORIES = OrderedDict([
        ('Linux', (CENTOS6, CENTOS7, UBUNTU, RHEL6, RHEL7)),
        ('Windows', (WINDOWS,)),
        ('Other', (FREEBSD, OTHER)),
    ])


class ApplicationTypes:
    WORDPRESS = 'wordpress'
    POSTGRESQL = 'postgresql'
    ZIMBRA = 'zimbra'
    ZABBIX = 'zabbix'
    SUGAR = 'sugar'

    CHOICES = (
        (WORDPRESS, 'WordPress'),
        (POSTGRESQL, 'PostgreSQL'),
        (ZIMBRA, 'Zimbra'),
        (ZABBIX, 'Zabbix'),
        (SUGAR, 'Sugar'),
    )


class SupportTypes:
    BASIC = 'basic'
    PREMIUM = 'premium'
    ADVANCED = 'advanced'

    CHOICES = (
        (BASIC, 'Basic'),
        (PREMIUM, 'Premium'),
        (ADVANCED, 'Advanced'),
    )


class Types:
    PriceItems = PriceItemTypes
    Applications = ApplicationTypes
    Support = SupportTypes
    Os = OsTypes
