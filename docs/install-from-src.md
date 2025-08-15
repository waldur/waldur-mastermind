# Installation Guide

## Installation via Dev Containers

If you use VS Code or GitHub Codespaces, you can quickly set up a development environment using Dev Containers. This method provides a consistent, pre-configured environment with all necessary dependencies.

Prerequisites for Dev Containers are:

- [VS Code](https://code.visualstudio.com/) with the [Dev Containers extension](https://marketplace.visualstudio.com/items?itemName=ms-vscode-remote.remote-containers) installed
- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (for local development)
- Git

After cloning repository, when prompted "Reopen in Container", click on it. Alternatively, you can press Ctrl+Shift+P, type "Dev Containers: Reopen in Container" and press Enter.

VS Code will build the dev container and set up the environment automatically. This process includes:

- Installing all system dependencies
- Setting up Python with the correct version
- Installing VS Code extensions
- Installing uv and project dependencies
- Installing PostgreSQL
- Configuring pre-commit hooks

Once the container is built and running, you'll have a fully configured development environment ready to use.

## Installation from source

### Prerequisites

- Linux OS. If you use Windows, you should install Linux either via
    [Virtualbox](https://www.freecodecamp.org/news/how-to-install-ubuntu-with-oracle-virtualbox/)
    or [Windows Subsystem for Linux](https://docs.microsoft.com/en-us/windows/wsl/install).
- `git`
- `virtualenv`
- `C` compiler and development libraries needed to build dependencies

#### Package installation by OS

- Debian or Ubuntu:
    `sudo apt install git python3-pip python3-venv python3-dev gcc libffi-dev libsasl2-dev libssl-dev libpq-dev libjpeg8-dev zlib1g-dev xmlsec1 libldap2-dev liblzma-dev libxslt1-dev libxml2-dev libbz2-dev libreadline-dev libsqlite3-dev`

- OS X:
    `brew install openssl; export CFLAGS="-I$(brew --prefix openssl)/include $CFLAGS"; export LDFLAGS="-L$(brew --prefix openssl)/lib $LDFLAGS"`

### Installation steps

#### Install uv

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

#### Install pyenv

```bash
curl https://pyenv.run | bash
pyenv install 3.11.9
pyenv global 3.11.9
```

#### Get the code

```bash
git clone https://github.com/waldur/waldur-mastermind.git
cd waldur-mastermind
```

#### Install Waldur in development mode

```bash
uv sync --dev
uv pip install -e .
uv run pre-commit install
```

**NB**: If you use a machine with Apple M1 CPU, run this before:

```bash
export optflags="-Wno-error=implicit-function-declaration"
export LDFLAGS="-L/opt/homebrew/opt/libffi/lib"
export CPPFLAGS="-I/opt/homebrew/opt/libffi/include"
export PKG_CONFIG_PATH="/opt/homebrew/opt/libffi/lib/pkgconfig"
```

Create and edit settings file

```bash
cp src/waldur_core/server/settings.py.example src/waldur_core/server/settings.py
vi src/waldur_core/server/settings.py
```

#### Database setup

Initialize PostgreSQL database:

```bash
sudo -u postgres -i
createdb waldur
createuser waldur
```

Add a password *waldur* for this user:

```bash
psql
ALTER USER waldur PASSWORD 'waldur';
ALTER DATABASE waldur OWNER TO waldur;
```

#### Final Setup Steps

Run migrations:

```bash
uv run waldur migrate --noinput
```

Collect static files:

```bash
uv run waldur collectstatic --noinput
```

Start Waldur:

```bash
uv run waldur runserver
```

### Additional configuration

For detailed configuration instructions, visit <https://docs.waldur.com/latest/admin-guide/mastermind-configuration/general/>
