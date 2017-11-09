#!/usr/bin/env bash
WALDUR_COMPONENTS="waldur-core
waldur-auth-social
waldur-auth-openid
waldur-auth-saml2
waldur-aws
waldur-azure
waldur-digitalocean
waldur-openstack
waldur-cost-planning
ansible-waldur-module
waldur-ansible
python-freeipa
waldur-freeipa
waldur-slurm
waldur-paypal
waldur-mastermind"
for component in $WALDUR_COMPONENTS
do
  if [ ! -d "$component" ]
    then git clone git@github.com:opennode/${component}.git
  fi

  cd $component
  git checkout develop
  git pull
  python setup.py develop
  cd ../
done
