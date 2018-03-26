#!/bin/bash
source $1/$2/bin/activate
jupyter kernelspec uninstall $2
