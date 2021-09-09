# OpenStack instances

When a VM instance is created through Waldur, it is created
using Cinder service with 2 volumes:

- **root volume** containing OS root image, bootable;
- **data volume** an empty volume for data.

VM resize (flavor). To change memory or CPU number, a flavor should be
changed. Please note, that the disk size is not affected. Change can
happen only for a stopped VM.

VM resize (disk). Increasing a disk size means extension of the **data
volume** attached to the instance. The process includes detaching of a
data volume, extending it and re-attaching to a VM. Disk can be
increased only for a stopped VM.
