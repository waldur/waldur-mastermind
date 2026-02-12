set -e

# Installing Python package manager
python3 -m pip install --upgrade pip
# Install uv first
python3 -m pip install uv

# Install Python dependencies for Waldur MasterMind using lock file
# Use UV_PROJECT_ENVIRONMENT to target system Python (no venv)
export UV_PROJECT_ENVIRONMENT=$(python -c "import sysconfig; print(sysconfig.get_config_var('prefix'))")
uv sync

# Install gunicorn separately after uv sync to ensure it's available
python3 -m pip install gunicorn==22.0.0

# Install the package in editable mode to ensure it's available
uv pip install --python $(which python) -e .

cp /etc/waldur/settings.py src/waldur_core/server/settings.py

# Build static assets
mkdir -p /usr/share/waldur/static
cat > tmp_settings.py << EOF
# Minimal settings required for 'collectstatic' command
INSTALLED_APPS = (
    'django.contrib.contenttypes',
    'django.contrib.admin',
    'django.contrib.staticfiles',
    'jsoneditor',
    'waldur_core.landing',
    'rest_framework',
    'django_filters',
    'drf_spectacular',
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
                'django.template.context_processors.request',
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
            ),
        },
    },
]
EOF
PYTHONPATH="${PYTHONPATH}:/usr/src/waldur" django-admin collectstatic --noinput --settings=tmp_settings
