# Installation from source

## Prerequisites

- Linux OS: Ubuntu or CentOS. If you use Windows, you should
    install Linux either via
    [Virtualbox](https://www.freecodecamp.org/news/how-to-install-ubuntu-with-oracle-virtualbox/)
    or [Windows Subsystem for Linux](https://docs.microsoft.com/en-us/windows/wsl/install).
- `git`
- `redis` and `hiredis` library
- `virtualenv`
- `C` compiler and development libraries needed to build dependencies
  - CentOS:
  `gcc libffi-devel openssl-devel postgresql-devel libjpeg-devel zlib-devel python-devel xmlsec1 xz-devel`
  - Ubuntu:
  `sudo apt install git python3-pip python3-venv python3-dev gcc libffi-dev libsasl2-dev libssl-dev libpq-dev libjpeg8-dev zlib1g-dev xmlsec1 libldap2-dev liblzma-dev libxslt1-dev libxml2-dev`
  - OS X:
  `brew install openssl; export CFLAGS="-I$(brew --prefix openssl)/include $CFLAGS"; export LDFLAGS="-L$(brew --prefix openssl)/lib $LDFLAGS"`

## Waldur MasterMind installation

### Install poetry

``` bash
pip3 install poetry
```

### Get the code

``` bash
git clone https://github.com/waldur/waldur-mastermind.git
cd waldur-mastermind
```

### Install Waldur in development mode along with dependencies

``` bash
poetry install
poetry run pre-commit install
```

**NB**: If you use a machine with Apple M1 CPU, run this before:

``` bash
export optflags="-Wno-error=implicit-function-declaration"
export LDFLAGS="-L/opt/homebrew/opt/libffi/lib"
export CPPFLAGS="-I/opt/homebrew/opt/libffi/include"
export PKG_CONFIG_PATH="/opt/homebrew/opt/libffi/lib/pkgconfig"
```

### Create and edit settings file (see 'Configuration' section for details)

``` bash
cp src/waldur_core/server/settings.py.example src/waldur_core/server/settings.py
vi src/waldur_core/server/settings.py
```

### Initialise PostgreSQL database

``` bash
sudo -u postgres -i
createdb waldur
createuser waldur
```

### Add a password *waldur* for this user

``` bash
psql
ALTER USER waldur PASSWORD 'waldur';
```

### Then run poetry

``` bash
poetry run waldur migrate --noinput
```

### Collect static data \-- static files will be copied to `./static/` in the same directory

``` bash
poetry run waldur collectstatic --noinput
```

- Start Waldur:

``` bash
poetry run waldur runserver
```

## Configuration

Instructions are here: <https://docs.waldur.com/admin-guide/mastermind-configuration/general/>
