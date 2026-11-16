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

# Download the documentation corpus searched by the AI assistant's search_docs
# tool (waldur/waldur-docs#37); initdb loads it with `waldur load_docs_corpus`.
# Optional for branch and MR builds: on any failure the image is built without
# it, and the tool is left out of every tool set. A release build sets
# DOCS_CORPUS_REQUIRED: it waits up to DOCS_CORPUS_WAIT seconds (default 900)
# for the docs pipeline of the same tag to publish the corpus, then fails, so a
# release never ships without documentation search unnoticed. The file is only
# kept if it is a complete gzip stream.
# The package registry is private. A CI job token, mounted as the
# `docs_corpus_token` build secret, is sent as JOB-TOKEN; the job's project must
# be on waldur-docs' job token allowlist.
DOCS_CORPUS_VERSION=${DOCS_CORPUS_VERSION:-latest}
DOCS_CORPUS_URL=${DOCS_CORPUS_URL:-https://code.opennodecloud.com/api/v4/projects/waldur%2Fwaldur-docs/packages/generic/docs-corpus/${DOCS_CORPUS_VERSION}/docs-corpus.jsonl.gz}
DOCS_CORPUS_TOKEN_FILE=/run/secrets/docs_corpus_token
mkdir -p /usr/share/waldur/docs-corpus
download_docs_corpus() {
python3 - "$DOCS_CORPUS_URL" /usr/share/waldur/docs-corpus/docs-corpus.jsonl.gz "$DOCS_CORPUS_TOKEN_FILE" << 'EOF'
import gzip
import os
import shutil
import sys
import urllib.error
import urllib.request

url, target, token_file = sys.argv[1], sys.argv[2], sys.argv[3]
partial = target + ".part"
request = urllib.request.Request(url)
if os.path.isfile(token_file):
    with open(token_file) as stream:
        token = stream.read().strip()
    if token:
        # Not passed on to a redirect: GitLab may answer with a presigned
        # object-storage URL, which needs no token and must not receive it.
        request.add_unredirected_header("JOB-TOKEN", token)
try:
    with urllib.request.urlopen(request, timeout=60) as response, open(partial, "wb") as out:
        shutil.copyfileobj(response, out)
except urllib.error.HTTPError as exc:
    print(f"HTTP {exc.code} {exc.reason}", file=sys.stderr)
    # 401/403: the token is missing or not allowed; waiting will not help.
    sys.exit(3 if exc.code in (401, 403) else 2)
with gzip.open(partial, "rb") as stream:
    while stream.read(1 << 20):
        pass
shutil.move(partial, target)
EOF
}
docs_corpus_attempts=1
if [ -n "${DOCS_CORPUS_REQUIRED:-}" ]; then
    docs_corpus_attempts=$(( ${DOCS_CORPUS_WAIT:-900} / 60 + 1 ))
fi
docs_corpus_ok=""
docs_corpus_denied=""
for attempt in $(seq 1 "$docs_corpus_attempts"); do
    docs_corpus_status=0
    download_docs_corpus || docs_corpus_status=$?
    if [ "$docs_corpus_status" = 0 ]; then
        docs_corpus_ok=1
        break
    fi
    if [ "$docs_corpus_status" = 3 ]; then
        docs_corpus_denied=1
        break
    fi
    rm -f /usr/share/waldur/docs-corpus/docs-corpus.jsonl.gz.part
    if [ "$attempt" -lt "$docs_corpus_attempts" ]; then
        echo "Docs corpus ${DOCS_CORPUS_VERSION} not available yet (attempt ${attempt}/${docs_corpus_attempts}); retrying in 60 seconds."
        sleep 60
    fi
done
if [ -n "$docs_corpus_ok" ]; then
    echo "Docs corpus ${DOCS_CORPUS_VERSION} downloaded."
elif [ -n "${DOCS_CORPUS_REQUIRED:-}" ] && [ -n "$docs_corpus_denied" ]; then
    echo "ERROR: access to the docs corpus at ${DOCS_CORPUS_URL} was refused. Check that the build gets the docs_corpus_token secret and that this project is on waldur-docs' CI/CD job token allowlist."
    exit 1
elif [ -n "${DOCS_CORPUS_REQUIRED:-}" ]; then
    echo "ERROR: docs corpus ${DOCS_CORPUS_VERSION} not downloaded from ${DOCS_CORPUS_URL}. A release image needs the corpus of its own tag: check the waldur-docs pipeline for ${DOCS_CORPUS_VERSION} (job \"Publish docs corpus\"), then re-run this job."
    exit 1
else
    echo "WARNING: docs corpus not downloaded from ${DOCS_CORPUS_URL}; the AI assistant will have no documentation search."
fi

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
