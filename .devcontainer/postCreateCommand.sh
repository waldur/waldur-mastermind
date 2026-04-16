#!/usr/bin/env bash

set -ex
cp pytest.ini.example pytest.ini
cp src/waldur_core/server/dev_settings.py src/waldur_core/server/settings.py
pip install uv
uv sync --dev
uv pip install -e .
git config --global --add safe.directory /workspaces/waldur-mastermind
uv tool install prek
