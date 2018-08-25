## sparsebak

Efficient, disk image-centric backups for Qubes OS.

### Status

Alpha stage, still in testing and development. Can only do full or incremental
backups to local dom0 location. Do NOT rely on this program as your primary backup system!

### Operation

sparsebak looks in '/baktest/set01/sparsebak.conf' for a list of volume names to
be monitored and backed up. The volumes should all be from qubes_dom0/pool00,
unless you have changed the `vgname` and `poolname` variables.

The resulting backup data is also saved to '/baktest/set01/' for now. Backup to
remote is still not implemented.

Currently the default mode (with no command options) is a monitor-only session
that collects volume change metadata. This only takes a few seconds and is good
to do on a frequent, regular basis - i.e. several times an hour.

Command options:
  -s, --send    Perform a backup. By default, only incremental backups will be attempted.
  -f, --full    Do initial, full backups when no prior backup session exists for a volume.

### Restoring

Currently this has to be done manually but is quite manageable with regular Linux
commands. The general process is:

1. Get the backup set onto a local volume, then cd to the 'vm*' subdir.

2. Create a 'zero' file of `bkchunksize` using `dd` and create a new dir called 'full':
```
dd if=/dev/zero of=zero bs=1024 count=256 # caution, size may change in future!
mkdir full
```

3. Hardlink the most recent session into 'full' with `ln S_00001122-334455/* full`.
Repeat for other session dirs, working backwards in time until the oldest is linked.
Note: `ln` will say it can't link some files because destination already exists --
this is intended.

4. Convert all the zero-length files in 'full' to point to 'zero' from step 2
with `find full -size 0 -type f -exec ln -f ./zero '{}' \;`

5. Combine files into a volume:
```
cat full/* | sudo dd of=/dev/mapper/vm-test123-volume bs=4096 conv=sparse
```

### Todo

* Basic functions: Volume selection, Send, Restore, Delete

* Encryption

* Pool-based Deduplication

* Additional sanity checks

* Btrfs support
