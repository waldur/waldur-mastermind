%define __data_dir %{_datadir}/ansible-waldur

Name: python-waldur-client
Summary: Python bindings to the Waldur API.
Group: Development/Libraries
Version: 0.0.1
Release: 1.el7
License: MIT
Url: https://waldur.com
Source0: %{name}-%{version}.tar.gz

Requires: python-requests >= 2.6.0

BuildArch: noarch
BuildRoot: %{_tmppath}/%{name}-%{version}-%{release}-buildroot

BuildRequires: python-setuptools

%description
Python bindings to the Waldur API.

%prep
%setup -q -n %{name}-%{version}

%build
%{__python} setup.py build

%install
rm -rf %{buildroot}
%{__python} setup.py install -O1 --root=%{buildroot}
mkdir -p %{buildroot}%{__data_dir}
cp waldur_os_*.py %{buildroot}%{__data_dir}

%clean
rm -rf %{buildroot}

%files
%defattr(-,root,root,-)
%{python_sitelib}/*
%{__data_dir}
