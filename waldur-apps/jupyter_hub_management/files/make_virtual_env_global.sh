#!/bin/bash
# $1 - directory with Python virtual environments managed by the cloud broker
# $2 - concrete virtual environment directory
source $1/$2/bin/activate
python3 -m ipykernel install --name $2 --display-name "$2"
