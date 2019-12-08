default_app_config = 'waldur_mastermind.marketplace_openstack.apps.MarketplaceOpenStackConfig'

PACKAGE_TYPE = 'Packages.Template'
INSTANCE_TYPE = 'OpenStackTenant.Instance'
VOLUME_TYPE = 'OpenStackTenant.Volume'

RAM_TYPE = 'ram'
CORES_TYPE = 'cores'
STORAGE_TYPE = 'storage'

AVAILABLE_LIMITS = [RAM_TYPE, CORES_TYPE, STORAGE_TYPE]

STORAGE_MODE_FIXED = 'fixed'
STORAGE_MODE_DYNAMIC = 'dynamic'
