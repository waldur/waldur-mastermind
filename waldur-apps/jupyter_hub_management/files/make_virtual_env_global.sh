#!/bin/bash
source $1/$2/bin/activate
python3 -m ipykernel install --name $2 --display-name "$2"
