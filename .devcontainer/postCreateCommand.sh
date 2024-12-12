#!/usr/bin/env bash

set -ex
cp pytest.ini.example pytest.ini
cp src/waldur_core/server/dev_settings.py src/waldur_core/server/settings.py
poetry config virtualenvs.create false
poetry install
git config --global --add safe.directory /workspaces/waldur-mastermind
pre-commit install
