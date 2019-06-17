# Sane defaults for pip
export PIP_NO_CACHE_DIR=off
export PIP_DISABLE_PIP_VERSION_CHECK=on

# Install Python dependencies for Waldur MasterMind from PyPI
pip install --no-cache-dir -r docker-test/api/requirements.txt

# Compile i18n messages
cp packaging/settings.py src/waldur_core/server/settings.py
django-admin compilemessages

# Install Waldur MasterMind package
pip install .

# Build static assets
mkdir -p /usr/share/waldur/static
cat > tmp_settings.py << EOF
# Minimal settings required for 'collectstatic' command
INSTALLED_APPS = (
    'admin_tools',
    'admin_tools.dashboard',
    'admin_tools.menu',
    'admin_tools.theming',
    'fluent_dashboard',  # should go before 'django.contrib.admin'
    'django.contrib.contenttypes',
    'django.contrib.admin',
    'django.contrib.staticfiles',
    'jsoneditor',
    'waldur_core.landing',
    'rest_framework',
    'rest_framework_swagger',
    'django_filters',
)
SECRET_KEY = 'tmp'
STATIC_ROOT = '/usr/share/waldur/static'
STATIC_URL = '/static/'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': ['waldur_core/templates'],
        'OPTIONS': {
            'context_processors': (
                'django.template.context_processors.debug',
                'django.template.context_processors.request',  # required by django-admin-tools >= 0.7.0
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'django.template.context_processors.i18n',
                'django.template.context_processors.media',
                'django.template.context_processors.static',
                'django.template.context_processors.tz',
            ),
            'loaders': (
                'django.template.loaders.filesystem.Loader',
                'django.template.loaders.app_directories.Loader',
                'admin_tools.template_loaders.Loader',  # required by django-admin-tools >= 0.7.0
            ),
        },
    },
]
EOF
PYTHONPATH="${PYTHONPATH}:/usr/src/waldur" django-admin collectstatic --noinput --settings=tmp_settings

# Create user and group
useradd --home /var/lib/waldur --shell /bin/sh --system --user-group waldur

# Copy configuration files
mkdir -p /etc/waldur/
cp packaging/etc/waldur/celery.conf /etc/waldur/celery.conf
cp packaging/etc/waldur/core.ini /etc/waldur/core.ini
cp packaging/etc/waldur/uwsgi.ini /etc/waldur/uwsgi.ini

# Create logging directory
mkdir -p /var/log/waldur/
chmod 750 /var/log/waldur/
chown waldur:waldur /var/log/waldur/

# Create media assets directory
mkdir -p /var/lib/waldur/media/
chmod 750 /var/lib/waldur/
chown waldur:waldur /var/lib/waldur/

# Copy SAML2 attributes
mkdir -p /etc/waldur/saml2/
cp -r packaging/etc/waldur/saml2/attribute-maps /etc/waldur/saml2/
cp packaging/etc/waldur/saml2.conf.py.example /etc/waldur/saml2/
