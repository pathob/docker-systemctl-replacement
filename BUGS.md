# Known Bugs and Peculiar Features


## Firefox

Firefox wants to connect to the DBUS. Instead of skipping
a missing service gracefully it just stops with a reference
to "/etc/machine-id" not containt a 32-chars ID.

Firefox will start if any such number is written.

    echo "012345678901234567890123456789012" > /etc/machine-id

Theoretically it should be available through docker but that
is not always the case, somehow.


## Ansible service module

Calling Ansible "service" with "enabled: yes" will fail with

    "no service or tool found for: my.service"

Although "my.service" is mentioned it really thinks that
the tool /usr/bin/systemctl does not exist - altough it does.
The reason is an additional sanity check whether systemd is
really managing the system - and it wants to fallback to
"chkconfig" otherwise. The test (seen in Ansible 2.1) can
be defeated by

    mkdir /run/systemd/system
    # or /dev/.run/systemd or /dev/.systemd

Sadly it must be done before calling "systemctl.py" so there
is no help to make this just temporary. If you have installed
the "initscripts" package then it will work without such a
workaround - although that package is only really used by the 
old SysV style init.d service units.


