set -e

# Runs in the builder stage of the Dockerfile. The runtime stage copies in what
# it produces: the virtualenv in /opt/venv, the source in /usr/src/waldur (with
# its settings.py), and /usr/share/waldur. Anything else, uv included, stays in
# the builder.

# uv is installed by the Dockerfile, in a layer of its own. Nothing here may run
# python3 before `uv sync` creates /opt/venv: bash would remember
# /usr/local/bin/python3 for the rest of the script, and under QEMU emulation
# (the arm64 CI build) the venv's python is then never used, because the
# emulator hands Python that remembered path as argv[0] instead of "python3".

# Install Python dependencies for Waldur MasterMind using lock file, into the
# virtualenv the Dockerfile names in UV_PROJECT_ENVIRONMENT (/opt/venv).
# --frozen installs exactly what uv.lock records and never re-resolves, so the
# image matches the lockfile the rest of CI tests against. --no-dev keeps the
# dev group (pytest, pyright, memray, faker, ...) out of a runtime image whose
# test directories never enter the build context (.dockerignore).
# `uv sync` installs the project itself, so no separate `uv pip install -e .`
# is needed - and adding one would re-resolve against pyproject.toml, outside
# the lock. gunicorn is a locked dependency like any other.
# --no-install-package setuptools: setuptools is locked only because pbr (the
# OpenStack clients) declares it. On Python 3.13 pbr reads versions through
# importlib.metadata, and the other pkg_resources users (pytz, sentry-sdk,
# wrapt) only fall back to it, so the runtime image goes without a packaging tool.
uv sync --frozen --no-dev --no-install-package setuptools

# Download ghostty-web, the browser terminal served by `waldur web_shell`,
# pinned by URL and sha512 in waldur_core/web_shell/assets.py, so containers
# need no network access to serve it. The Dockerfile points
# WALDUR_WEB_SHELL_ASSETS_DIR at the same directory.
WALDUR_WEB_SHELL_ASSETS_DIR=/usr/share/waldur/web-shell python3 -c "from waldur_core.web_shell import assets; assets.fetch()"

cp /etc/waldur/settings.py src/waldur_core/server/settings.py

# Build static assets
mkdir -p /usr/share/waldur/static
cat > tmp_settings.py << EOF
# Minimal settings required for 'collectstatic' command
INSTALLED_APPS = (
    'django.contrib.contenttypes',
    'django.contrib.admin',
    'django.contrib.staticfiles',
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
