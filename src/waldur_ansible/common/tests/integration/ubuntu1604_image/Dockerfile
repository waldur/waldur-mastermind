FROM xenial-server-cloudimg

# setting up systemd
# the solution is taken from here https://github.com/solita/docker-systemd
ENV container docker

RUN find /etc/systemd/system \
    /lib/systemd/system \
    -path '*.wants/*' \
    -not -name '*journald*' \
    -not -name '*systemd-tmpfiles*' \
    -not -name '*systemd-user-sessions*' \
    -exec rm \{} \;

RUN apt-get update && \
    apt-get install -y \
    dbus && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

RUN systemctl set-default multi-user.target

COPY setup /sbin/
RUN ["chmod", "+x", "/sbin/setup"]

STOPSIGNAL SIGRTMIN+3


# configure SSHD
RUN mkdir -p /var/run/sshd
RUN echo 'root:screencast' | chpasswd
RUN sed -i 's/PermitRootLogin prohibit-password/PermitRootLogin yes/' /etc/ssh/sshd_config

ENV LC_ALL=C
ENV DEBIAN_FRONTEND=noninteractive
RUN dpkg-reconfigure openssh-server

# SSH login fix. Otherwise user is kicked off after login
RUN sed 's@session\s*required\s*pam_loginuid.so@session optional pam_loginuid.so@g' -i /etc/pam.d/sshd

ENV NOTVISIBLE "in users profile"
RUN echo "export VISIBLE=now" >> /etc/profile

#add ubuntu and add it to sudoers
RUN useradd -ms /bin/bash ubuntu
RUN usermod -a -G sudo ubuntu
RUN echo '%sudo ALL=(ALL) NOPASSWD:ALL' >> /etc/sudoers
ADD --chown=ubuntu waldur_integration_test_ssh_key.pub /home/ubuntu/.ssh/authorized_keys
# allow PAM authentication through SSH
RUN sed -i 's/#UsePAM yes/UsePAM yes/g' /etc/ssh/sshd_config

# these commands are executed when container runs
CMD ["/bin/bash", "-c", "service ssh restart && exec /sbin/init --log-target=journal 3>&1"]

# Clean up APT when done.
RUN apt-get clean && rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*
