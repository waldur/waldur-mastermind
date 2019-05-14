#!/bin/bash

/waldur-mastermind/venv/bin/waldur migrate --noinput && venv/bin/waldur createstaffuser -u staff -p qwerty || echo 'User Already Exists, Skipping' && /waldur-mastermind/venv/bin/waldur runserver 0.0.0.0:8000

