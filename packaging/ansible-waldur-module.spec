%define __data_dir %{_datadir}/ansible-waldur
%define __playbooks_dir %{__data_dir}/waldur-apps

Name: ansible-waldur-module
Summary: Ansible module for Waldur API.
Group: Development/Libraries
Version: 0.8.9
Release: 1.el7
License: MIT
Url: https://waldur.com
Source0: %{name}-%{version}.tar.gz

Requires: python-requests >= 2.6.0
Requires: python-six >= 1.9.0

BuildArch: noarch
BuildRoot: %{_tmppath}/%{name}-%{version}-%{release}-buildroot

BuildRequires: python-setuptools

%description
This package contains collection of Ansible modules to allow provisioning and
management of infrastructure under Waldur through Ansible playbooks.

%prep
%setup -q -n %{name}-%{version}

%build
%{__python} setup.py build

%install
rm -rf %{buildroot}
%{__python} setup.py install -O1 --root=%{buildroot}

mkdir -p %{buildroot}%{__data_dir}
cp waldur_os_*.py %{buildroot}%{__data_dir}

mkdir -p %{buildroot}%{__playbooks_dir}
cp -Rv waldur-apps/jupyter_hub_management %{buildroot}%{__playbooks_dir}
cp -Rv waldur-apps/python_management %{buildroot}%{__playbooks_dir}

%clean
rm -rf %{buildroot}

%files
%defattr(-,root,root,-)
%{python_sitelib}/*
%{__data_dir}
