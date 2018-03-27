#!/bin/bash
# $1 - directory with Python virtual environments managed by the cloud broker
# $2 - concrete virtual environment directory
source $1/$2/bin/activate
jupyter kernelspec uninstall $2
