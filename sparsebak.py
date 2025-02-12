#!/usr/bin/env python3


###  sparsebak
###  Copyright Christopher Laprise 2018-2019 / tasket@github.com
###  Licensed under GNU General Public License v3. See file 'LICENSE'.


import sys, os, stat, shutil, subprocess, time, datetime
import re, mmap, zlib, gzip, tarfile, io, fcntl, tempfile
import xml.etree.ElementTree
import argparse, configparser, hashlib, uuid
# For deduplication tests:
import ctypes, sqlite3, resource
from array import array


# ArchiveSet manages configuration and configured volume info

class ArchiveSet:
    def __init__(self, name, top, init=False):
        self.name        = name
        self.path        = pjoin(top,name)
        self.confpath    = pjoin(self.path,"archive.ini")
        self.hashindex   = {}
        self.vols        = {}
        self.allsessions = []
        # persisted:
        self.chunksize   = bkchunksize
        self.compression = "zlib"
        self.compr_level = "4"
        self.hashtype    = "sha256"
        self.vgname      = None
        self.poolname    = None
        self.destsys     = None
        self.destdir     = "."
        self.destmountpoint = None
        self.uuid        = None

        self.conf = cp   = configparser.ConfigParser()
        cp.optionxform   = lambda option: option
        cp["var"]        = {}
        cp["volumes"]    = {}
        if not os.path.exists(self.confpath):
            self.uuid = str(uuid.uuid4())
            return
        cp.read(self.confpath)
        self.a_ints      = {"chunksize"}
        for name in cp["var"].keys():
            setattr(self, name, int(cp["var"][name]) \
                                if name in self.a_ints else cp["var"][name])

        # Convert old path to new
        if os.path.exists(top+"/"+self.vgname+"%"+self.poolname):
            os.rename(top+"/"+self.vgname+"%"+self.poolname, top+"/"+self.name)
        if not self.uuid: self.uuid = str(uuid.uuid4())
        if "destvm" in cp["var"].keys(): ##
            self.destsys = self.destvm   ##
            del cp["var"]["destvm"]
        self.save_conf()

        dedup = options.dedup > 0
        for key in cp["volumes"]:
            if cp["volumes"][key] != "disable" and \
            (len(options.volumes)==0 or key in options.volumes or dedup):
                os.makedirs(pjoin(self.path,key), exist_ok=True)
                self.vols[key] = self.Volume(self, key, pjoin(self.path,key),
                                             self.vgname)
                self.vols[key].enabled = True
                self.allsessions += self.vols[key].sessions.values()

        # Create master session list sorted by date-time
        self.allsessions.sort(key=lambda x: x.localtime)


    def save_conf(self):
        c = self.conf['var']
        c['chunksize']   = str(self.chunksize)
        c['compression'] = self.compression
        c['compr_level'] = self.compr_level
        c['hashtype']    = self.hashtype
        c['vgname']      = self.vgname
        c['poolname']    = self.poolname
        c['destsys']     = self.destsys
        c['destdir']     = self.destdir
        c['destmountpoint'] = self.destmountpoint
        c['uuid']        = self.uuid
        os.makedirs(os.path.dirname(self.confpath), exist_ok=True)
        with open(self.confpath, "w") as f:
            self.conf.write(f)

    def add_volume(self, datavol):
        if datavol in self.conf["volumes"].keys():
            x_it(1, datavol+" is already configured.")

        volname_check = re.compile("^[a-zA-Z0-9\+\._-]+$")
        if volname_check.match(datavol) is None:
            x_it(1, "Only characters A-Z 0-9 . + _ - are allowed"
                " in volume names.")

        if len(datavol) > 112:
            x_it(1, "Volume name must be 112 characters or less.")

        #self.vols[datavol] = self.Volume(datavol, pjoin(self.path,datavol),
        #                                 self.vgname)

        self.conf["volumes"][datavol] = "enable"
        self.save_conf()

    def delete_volume(self, datavol):
        if datavol in self.conf["volumes"].keys():
            del(self.conf["volumes"][datavol])
            self.save_conf()

        for ext in {".tick",".tock"}:
            if lv_exists(self.vgname, datavol+ext):
                do_exec([["lvremove", "-f", self.vgname+"/"+datavol+ext]])
                print("Removed snapshot", self.vgname+"/"+datavol+ext)

        if os.path.exists(pjoin(self.path,datavol)):
            shutil.rmtree(pjoin(self.path,datavol))


    class Volume:
        def __init__(self, archive, name, path, vgname):
            self.name    = name
            self.archive = archive
            self.path    = path
            self.vgname  = vgname
            self.present = lv_exists(vgname, name)
            self.enabled = False
            self.error   = False
            self.volsize = None
            self.mapfile = path+"/deltamap"
            # persisted:
            self.format_ver = "0"
            self.uuid    = None
            self.first   = None
            self.last    = None
            self.que_meta_update = "false"

            # load volume info
            if os.path.exists(pjoin(path,"volinfo")):
                with open(pjoin(path,"volinfo"), "r") as f:
                    for ln in f:
                        vname, value = ln.strip().split(" = ")
                        setattr(self, vname, value)

            # load sessions
            self.sessions ={e.name: self.Ses(self,e.name,pjoin(path,e.name)) \
                for e in os.scandir(path) if e.name[:2]=="S_" \
                    and e.name[-3:]!="tmp"} ##if self.present else {}

            no_manifest = [ses.name for ses in self.sessions.values()
                           if not ses.present]
            if len(no_manifest):
                x_it(1, "ERROR: Some manifests do not exist for "+name
                        +".\n(Alpha format?)")
            mapfile = pjoin(path, "deltamap")
            self.mapped = os.path.exists(mapfile)

            if int(self.format_ver) > format_version:
                raise ValueError("Archive format ver = "+self.format_ver+
                                 ". Expected = "+format_version)

            # build ordered, linked list of names
            sesnames = []
            sname = self.last
            for i in range(len(self.sessions)):
                sesnames.insert(0, sname)
                if sname == "none":
                    break
                sname = self.sessions[sname].previous
            self.sesnames = sesnames

            # check for continuity between sessions
            for sname, s in self.sessions.items():
                if s.previous == "none" and self.first != sname:
                    print("**** PREVIOUS MISMATCH",sname, self.first)
                elif s.previous not in sesnames+["none"]:
                    print("**** PREVIOUS NOT FOUND",sname, s.previous)

            # use latest volsize
            self.volsize = self.sessions[self.last].volsize \
                            if self.sessions else 0


        def map_exists(self):
            return os.path.exists(self.mapfile)

        # Based on last session size unless volume_size is specified.
        def mapsize(self, volume_size=None):
            if not volume_size:
                volume_size = self.volsize
            return (volume_size // self.archive.chunksize // 8) + 1

        def save_volinfo(self, fname="volinfo"):
            with open(pjoin(self.path,fname), "w") as f:
                print("format_ver =", format_version, file=f)
                print("uuid =", self.uuid if self.uuid else str(uuid.uuid4()),
                      file=f)
                print("first =", self.first, file=f)
                print("last =", self.last, file=f)
                print("que_meta_update =", self.que_meta_update, file=f)

        def new_session(self, sname):
            ns = self.Ses(self, sname)
            ns.path = pjoin(self.path, sname)
            if self.first is None:
                ns.sequence = 0
                self.first = sname
            else:
                ns.previous = self.last
                ns.sequence = self.sessions[self.last].sequence + 1

            self.last = sname
            self.sesnames.append(sname)
            self.sessions[sname] = ns
            self.archive.allsessions.append(ns)
            return ns

        def delete_session(self, sname):
            ses = self.sessions[sname]
            if sname == self.last:
                raise NotImplementedError("Cannot delete last session")
            index = self.sesnames.index(sname)
            affected = self.sesnames[index+1]
            self.sessions[affected].previous = ses.previous
            if index == 0:
                self.first = self.sesnames[1]
            del self.sesnames[index]
            del self.sessions[sname]
            index = self.archive.allsessions.index(ses)
            del self.archive.allsessions[index]

            shutil.rmtree(pjoin(self.path, sname))
            return affected


        class Ses:
            def __init__(self, volume, name, path=""):
                self.name     = name
                self.path     = path
                self.present  = os.path.exists(pjoin(path,"manifest"))
                self.volume   = volume
                # persisted:
                self.localtime = None
                self.volsize  = None
                self.format   = None
                self.sequence = None
                self.previous = "none"
                attr_str      = {"localtime","format","previous"}
                attr_int      = {"volsize","sequence"}

                if path:
                    with open(pjoin(path,"info"), "r") as sf:
                        for ln in sf:
                            vname, value = ln.strip().split(" = ")
                            setattr(self, vname, 
                                int(value) if vname in attr_int else value)
                    if self.localtime is None or self.localtime == "None":
                        self.localtime = self.name[2:]

            def save_info(self):
                if not self.path:
                    raise ValueError("Path not set for save_info")
                self.volume.volsize = self.volsize
                with open(pjoin(self.path,"info"), "w") as f:
                    print("localtime =", self.localtime, file=f)
                    print("volsize =",   self.volsize, file=f)
                    print("format =",    self.format, file=f)
                    print("sequence =",  self.sequence, file=f)
                    print("previous =",  self.previous, file=f)


class Lvm_VolGroup:
    def __init__(self, name):
        self.name = name
        self.lvs  = {}

class Lvm_Volume:
    colnames  = ["vg_name","lv_name","lv_attr","lv_size","lv_time",
                 "pool_lv","thin_id","lv_path"]
    attr_ints = ["lv_size"]

    def __init__(self, members):
        for attr in self.colnames:
            val = members[self.colnames.index(attr)]
            setattr(self, attr, int(re.sub("[^0-9]", "", val)) if attr \
                in self.attr_ints else val)


# Retrieves survey of all LVs as vgs[].lvs[] dicts

def get_lvm_vgs():

    do_exec([["lvs", "--units=b", "--noheadings", "--separator=::",
                "--options=" + ",".join(Lvm_Volume.colnames)]],
            out=tmpdir+"/volumes.lst")

    vgs = {}
    with open(tmpdir+"/volumes.lst", "r") as vlistf:
        for ln in vlistf:
            members = ln.strip().split("::")
            vgname = members[0] # Fix: use colname index
            lvname = members[1]
            if vgname not in vgs.keys():
                vgs[vgname] = Lvm_VolGroup(vgname)
            vgs[vgname].lvs[lvname] = Lvm_Volume(members)

    return vgs


# Initialize a new ArchiveSet:

def arch_init(aset):
    if not options.source or not options.dest:
        x_it(1,"--source and --dest are required.")

    vgname, poolname = options.source.split("/")
    if not vg_exists(vgname):
        print("Warning: Volume group '%s' does not exist." % vgname)
    if not lv_exists(vgname, poolname):
        print("Warning: LV pool '%s' does not exist." % poolname)
    aset.vgname   = vgname
    aset.poolname = poolname

    dest    = options.dest
    dtypes  = ["qubes://","ssh://","qubes-ssh://","internal:"]
    destsys = delim = mountpoint = ""
    for i in dtypes:
        if dest.startswith(i):
            destsys, delim, mountpoint = dest.replace(i,"",1).partition("/")
            break
    if (not mountpoint and not delim) or \
       (i != "internal:" and not destsys) or \
       (i == "qubes-ssh://" and ("" in destsys.split("|") or len(destsys.split("|"))<2)):
        x_it(1,"Error: Malformed --dest specification.")

    aset.destsys        = i+destsys
    aset.destmountpoint = delim+mountpoint

    if options.subdir:
        if options.subdir.strip()[0] == "/":
            x_it(1,"Subdir cannot be absolute path.")
        aset.destdir    = options.subdir.strip()

    if options.compression:
        if ":" in options.compression:
            compression, compr_level = options.compression.strip().split(":")
            aset.compr_level = compr_level
        if compression not in {"zlib"}:
            x_it(1, "Invalid compression spec.")
        aset.compression = compression

    if options.chfactor:
        aset.chunksize = int(options.chfactor) * bkchunksize
        if aset.chunksize > 16 * 1024 * 1024:
            x_it(1, "Requested chunk size not supported.")
        if aset.chunksize > 256 * 1024:
            print("Large chunk size set:", aset.chunksize)

    aset.save_conf()


# Get global configuration settings:

def get_configs():

    # Convert old paths to new
    if os.path.exists(topdir) and not os.path.exists(metadir+topdir):
        if os.stat(topdir).st_dev == os.stat(metadir).st_dev:
            os.rename(topdir, metadir+topdir)
        else:
            shutil.copytree(topdir, metadir+topdir)
            os.rename(topdir, topdir+"-old")
    if os.path.exists(metadir+topdir+"/default.ini"):
        os.rename(metadir+topdir+"/default.ini", metadir+topdir+"/default/archive.ini")

    aset = ArchiveSet("default", metadir+topdir)
    if options.action == "arch-init" and not aset.destmountpoint:
        aset = arch_init(aset)
        x_it(0, "Done.")
    elif options.action == "arch-init":
        x_it(1, "Archive already initialized for "+aset.name)

    dvs = []
    for vn,v in aset.vols.items():
        if v.enabled:
            dvs.append(v.name)

    return aset, dvs


# Detect features of internal and destination environments:

def detect_internal_state():

    destsys = aset.destsys
    if os.path.exists("/etc/qubes-release") and destsys[:8] == "qubes://":
        desttype = "qubes" # Qubes OS guest VM
        destsys = destsys[8:]
    elif destsys[:6] == "ssh://":
        desttype = "ssh"
        destsys = destsys[6:]
    elif destsys[:12] == "qubes-ssh://":
        desttype = "qubes-ssh"
        destsys = destsys[12:]
    elif destsys[:11] == "internal:":
        desttype = "internal" # local shell environment
    else:
        raise ValueError("'destsys' not an accepted type.")

    for prg in {"thin_delta","lvs","lvdisplay","lvcreate","blkdiscard",
                "truncate","ssh" if desttype=="ssh" else "sh"}:
        if not shutil.which(prg):
            raise RuntimeError("Required command not found: "+prg)

    p = subprocess.check_output(["thin_delta", "-V"])
    ver = p[:5].decode("UTF-8").strip()
    target_ver = "0.7.4"
    if ver < target_ver:
        print("Note: Thin provisioning tools version", target_ver,
              "or later is recommended for stabilty."
              " Installed version =", ver+".")


    #####>  Begin helper program  <#####

    dest_program = \
    '''import os, sys, shutil
cmd = sys.argv[1]
with open("''' + tmpdir + '''/rpc/dest.lst", "r") as lstf:
    if cmd == "receive":
        for line in lstf:
            fname = line.strip()
            fsize = os.path.getsize(fname) if os.path.exists(fname) else 0
            i = sys.stdout.buffer.write(fsize.to_bytes(4,"big"))
            if fsize:
                with open(fname,"rb") as dataf:
                    i = sys.stdout.buffer.write(dataf.read(fsize))
    elif cmd == "merge":
        merge_target, target = lstf.readline().strip().split()
        src_list = []
        while True:
            ln = lstf.readline().strip()
            if ln == "###":
                break
            src_list.append(ln)
        subdirs = set()
        for src in src_list:
            for i in os.scandir(src):
                if i.is_dir():
                    subdirs.add(i.name)
        for sdir in subdirs:
            os.makedirs(merge_target+"/"+sdir, exist_ok=True)
        for line in lstf:
            ln = line.strip().split()
            if ln[0] == "rename" and os.path.exists(ln[1]):
                os.replace(ln[1], ln[2])
            elif ln[0] == "rm" and os.path.exists(ln[1]):
                os.remove(ln[1])
        for dir in src_list:
            shutil.rmtree(dir)
        os.replace(merge_target, target)
    elif cmd == "dedup":
        ddcount = 0
        for line in lstf:
            source, dest = line.strip().split()
            if os.stat(source).st_ino != os.stat(dest).st_ino:
                os.link(source, dest+"-lnk")
                os.replace(dest+"-lnk", dest)
                ddcount += 1
        print(ddcount, "reduced.")
    '''
    with open(tmpdir +"/rpc/dest_helper.py", "wb") as progf:
        progf.write(bytes(dest_program, encoding="UTF=8"))

    #####>  End helper program  <#####

    return destsys, desttype


def detect_dest_state(destsys):

    if options.action not in {"index-test","monitor","list","version","add"} \
                            and destsys is not None:

        if desttype == "qubes-ssh":
            dargs = dest_run_map["qubes"][:-1] + [destsys.split("|")[0]]

            cmd = dargs + [shell_prefix \
                  +"rm -rf "+tmpdir+"-old"
                  +" && { if [ -d "+tmpdir+" ]; then mv "+tmpdir
                  +" "+tmpdir+"-old; fi }"
                  +"  && mkdir -p "+tmpdir+"/rpc"
                  ]
            do_exec([cmd])

        # Fix: get OSTYPE env variable
        try:
            cmd = ["mountpoint -q '"+aset.destmountpoint
                    +"' && mkdir -p '"+aset.destmountpoint+"/"+aset.destdir+topdir
                    +"' && cd '"+aset.destmountpoint+"/"+aset.destdir+topdir+"'"

                    +"  && { if [ -d "+aset.vgname+"%"+aset.poolname+" ]; then"
                    +"  mv "+aset.vgname+"%"+aset.poolname+" "+aset.name+"; fi }"

                    +"  && touch archive.dat"
                    +(" && ln -f archive.dat .hardlink" if options.dedup else "")
                    ]
            dest_run(cmd)

            # send helper program to remote
            if desttype != "internal":
                cmd = ["rm -rf "+tmpdir
                      +" && mkdir -p "+tmpdir+"/rpc"
                      +" && cat >"+tmpdir+"/rpc/dest_helper.py"
                      ]
                dest_run(cmd, infile=tmpdir+"/rpc/dest_helper.py")
        except subprocess.CalledProcessError:
            x_it(1, "Destination not ready to receive commands.")


# Run system commands with pipes, without shell:
# 'commands' is a list of lists, each element a command line.
# If multiple command lines, then they are piped together.
# 'out' redirects the last command output to a file; append mode can be
# selected by beginning 'out' path with '>>'.

def do_exec(commands, cwd=None, check=True, out="", infile=""):
    if out[:2] == ">>":
        out = out[2:].strip()
        outmode = "ab"
    else:
        outmode = "wb"
    if cwd and out and out[0] != "/":
        out = pjoin(cwd,out)
    if cwd and infile and infile[0] != "/":
        infile = pjoin(cwd,infile)

    procs = []
    outf = open(out, outmode) if out else subprocess.DEVNULL
    inf  = open(infile, "rb") if infile else subprocess.DEVNULL
    for i, clist in enumerate(commands):
        p = subprocess.Popen(clist, stdin=inf if i==0 else procs[i-1].stdout,
                             stdout=outf if i==len(commands)-1 else subprocess.PIPE,
                             stderr=subprocess.DEVNULL, cwd=cwd) #, env=env)
        procs.append(p)

    err = None
    for p in procs:
        if p.wait() != 0 and check:
            #print("Error:", p.args)
            err = p
            break
    for f in [inf, outf]:
        if type(f) is not int: f.close()
    if err and check:
        raise subprocess.CalledProcessError(err.returncode, err.args)


# Run system commands on destination

def dest_run(commands, dest_type=None, infile=""):
    if dest_type is None:
        dest_type = desttype

    cmd = dest_run_args(dest_type, commands)
    do_exec([cmd], infile=infile)

    #else:
    #    p = subprocess.check_output(cmd, **kwargs)


def dest_run_args(dest_type, commands):

    # shunt commands to tmp file
    with tempfile.NamedTemporaryFile(dir=tmpdir, delete=False) as tmpf:
        cmd = bytes(shell_prefix + " ".join(commands) + "\n", encoding="UTF-8")
        tmpf.write(cmd)
        remotetmp = os.path.basename(tmpf.name)

    if dest_type in {"qubes","qubes-ssh"}:
        do_exec([["qvm-run", "-p",
                  (destsys if dest_type == "qubes" else destsys.split("|")[0]),
                  "mkdir -p "+pjoin(tmpdir,"rpc")
                  +" && cat >"+pjoin(tmpdir,"rpc",remotetmp)
                ]], infile=pjoin(tmpdir,remotetmp))
        if dest_type == "qubes":
            add_cmd = ["sh "+pjoin(tmpdir,"rpc",remotetmp)]
        elif dest_type == "qubes-ssh":
            add_cmd = ["ssh "+destsys.split("|")[1]
                      +' "$(cat '+pjoin(tmpdir,"rpc",remotetmp)+')"']

    elif dest_type == "ssh":
        #add_cmd = [' "$(cat '+pjoin(tmpdir,remotetmp)+')"']
        add_cmd = [cmd]

    elif dest_type == "internal":
        add_cmd = [pjoin(tmpdir,remotetmp)]

    ret = dest_run_map[dest_type] + add_cmd
    #print("CMD",ret)
    return ret


# Prepare snapshots and check consistency with metadata.
# Must run get_lvm_vgs() again after this.

def prepare_snapshots(datavols):

    ''' Normal precondition will have a snap1vol already in existence in addition
    to the source datavol. Here we create a fresh snap2vol so we can compare
    it to the older snap1vol. Then, depending on monitor or backup mode, we'll
    accumulate delta info and possibly use snap2vol as source for a
    backup session.
    '''

    print("Preparing snapshots...")
    dvs    = []
    nvs    = []
    vgname = aset.vgname
    for datavol in datavols:
        vol      = aset.vols[datavol]
        sessions = vol.sesnames
        snap1vol = datavol + ".tick"
        snap2vol = datavol + ".tock"
        mapfile  = vol.mapfile

        if not lv_exists(vgname, datavol):
            print("Warning:", datavol, "does not exist!")
            continue

        # Remove stale snap2vol
        if lv_exists(vgname, snap2vol):
            p = subprocess.check_output(["lvremove", "-f",vgname+"/"+snap2vol],
                                        stderr=subprocess.STDOUT)

        # Future: Expand recovery to start send-resume
        if os.path.exists(mapfile+"-tmp"):
            #print("  Delta map not finalized for", datavol, "...recovering.")
            os.rename(mapfile+"-tmp", mapfile)

        # Make initial snapshot if necessary:
        if not os.path.exists(mapfile):
            if len(sessions) > 0:
                raise RuntimeError("ERROR: Sessions exist but no map for "+datavol)
            if not monitor_only and not lv_exists(vgname, snap1vol):
                p = subprocess.check_output(["lvcreate", "-pr", "-kn",
                    "-ay", "-s", vgname+"/"+datavol, "-n", snap1vol],
                    stderr=subprocess.STDOUT)
                volgroups[vgname].lvs[snap1vol] = "placeholder"
                print("  Initial snapshot created for", datavol)
            nvs.append(datavol)

        if os.path.exists(mapfile) and not lv_exists(vgname, snap1vol):
            raise RuntimeError("ERROR: Map and snapshots in inconsistent state, "
                            +snap1vol+" is missing!")

        # Make current snapshot
        p = subprocess.check_output( ["lvcreate", "-pr", "-kn", "-ay",
            "-s", vgname+"/"+datavol, "-n",snap2vol], stderr=subprocess.STDOUT)
        #print("  Current snapshot created:", snap2vol)

        if datavol not in nvs:
            dvs.append(datavol)

    return dvs, nvs


def lv_exists(vgname, lvname):
    return vgname in volgroups.keys() \
            and lvname in volgroups[vgname].lvs.keys()


def vg_exists(vgname):
    try:
        do_exec([["vgdisplay", vgname]])
    except subprocess.CalledProcessError:
        return False
    else:
        return True


# Get raw lvm deltas between snapshots

def get_lvm_deltas(datavols):
    vgname   = aset.vgname
    poolname = aset.poolname
    print("Acquiring deltas.")

    do_exec([["dmsetup","message", vgname+"-"+poolname+"-tpool",
              "0", "release_metadata_snap"]], check=False)
    do_exec([["dmsetup","message", vgname+"-"+poolname+"-tpool",
              "0", "reserve_metadata_snap"]])
    err = False
    for datavol in datavols:
        snap1vol = datavol + ".tick"
        snap2vol = datavol + ".tock"
        try:
            do_exec([["thin_delta", "-m",
                      "--thin1=" + l_vols[snap1vol].thin_id,
                      "--thin2=" + l_vols[snap2vol].thin_id,
                      "/dev/mapper/"+vgname+"-"+poolname+"_tmeta"],
                     ["grep", "-v", "<same .*\/>$"]
                    ],  out=tmpdir+"/delta."+datavol
                   )
        except:
            err = True
            break

    do_exec([["dmsetup","message", vgname+"-"+poolname+"-tpool",
              "0", "release_metadata_snap"]], check=not err)

    if err:
        x_it(1, "ERROR running thin_delta process.")


# update_delta_digest: Translates raw lvm delta information
# into a bitmap (actually chunk map) that repeatedly accumulates change status
# for volume block ranges until a send command is successfully performed and
# the mapfile is reinitialzed with zeros.

def update_delta_digest(datavol):

    if monitor_only:
        print("Updating block change map. ", end="")

    vol         = aset.vols[datavol]
    if len(vol.sessions) == 0:
        return False
    snap2vol    = vol.name + ".tock"
    snap2size   = l_vols[snap2vol].lv_size
    chunksize   = aset.chunksize
    os.rename(vol.mapfile, vol.mapfile+"-tmp")
    dtree       = xml.etree.ElementTree.parse(tmpdir+"/delta."+datavol).getroot()
    dblocksize  = int(dtree.get("data_block_size"))
    bmap_byte   = 0
    lastindex   = 0
    dnewblocks  = 0
    dfreedblocks = 0

    with open(vol.mapfile+"-tmp", "r+b") as bmapf:
        os.ftruncate(bmapf.fileno(), vol.mapsize(snap2size))
        bmap_mm = mmap.mmap(bmapf.fileno(), 0)

        for delta in dtree.find("diff"):
            blockbegin = int(delta.get("begin")) * dblocksize
            blocklen   = int(delta.get("length")) * dblocksize
            if delta.tag in {"different", "right_only"}:
                dnewblocks += blocklen
            elif delta.tag == "left_only":
                dfreedblocks += blocklen
            else: # superfluous tag
                continue

            # blockpos iterates over disk blocks, with
            # thin LVM tools constant of 512 bytes/block.
            # dblocksize (source) and chunksize (dest) may be
            # somewhat independant of each other.
            for blockpos in range(blockbegin, blockbegin + blocklen):
                volsegment = blockpos // (chunksize // bs)
                bmap_pos = volsegment // 8
                if bmap_pos != lastindex:
                    bmap_mm[lastindex] |= bmap_byte
                    bmap_byte = 0
                bmap_byte |= 1 << (volsegment % 8)
                lastindex = bmap_pos

        bmap_mm[lastindex] |= bmap_byte

    if monitor_only and dnewblocks+dfreedblocks > 0:
        print(dnewblocks * bs, "changed,",
              dfreedblocks * bs, "discarded.")
    elif monitor_only:
        print("No changes.")

    return dnewblocks+dfreedblocks > 0


def last_chunk_addr(volsize, chunksize):
    return (volsize-1) - ((volsize-1) % chunksize)


# Send volume to destination:

def send_volume(datavol, localtime):

    vol         = aset.vols[datavol]
    snap2vol    = vol.name + ".tock"
    snap2size   = l_vols[snap2vol].lv_size
    allsessions = aset.allsessions
    chunksize   = aset.chunksize
    bmap_size   = vol.mapsize(snap2size)
    chdigits    = max_address.bit_length() // 4
    chformat    = "%0"+str(chdigits)+"x"
    bksession   = "S_"+localtime
    sdir        = pjoin(datavol, bksession)
    send_all    = len(vol.sessions) == 0

    # testing four deduplication types:
    dedup_idx     = dedup_db = None
    dedup         = options.dedup
    if dedup == 3: # sql
        dedup_db  = aset.hashindex
        cursor    = dedup_db.cursor()
        c_uint64  = ctypes.c_uint64
        c_int64   = ctypes.c_int64
    elif dedup == 4: # array tree
        hashtree, ht_ksize, hashdigits, hash_w, hash0len, \
        dataf, chtree, chdigits, ch_w, ses_w \
                  = aset.hashindex
        ht_ksize  = ht_ksize//2
        hsegs     = hash_w//hash0len
        chtree_max= 2**(chtree[0].itemsize*8)
        idxcount  = dataf.tell() // (ch_w+ses_w)
    elif dedup == 5: # bytearray tree
        hashtree, ht_ksize, hashdigits, hash_w, \
        dataf, chtree, chdigits, ch_w, ses_w \
                  = aset.hashindex
        chtree_max= 2**(chtree[0].itemsize*8)
        ht_ksize  = ht_ksize//2
        idxcount  = dataf.tell() // (ch_w+ses_w)

    ses = vol.new_session(bksession)
    ses.localtime = localtime
    ses.volsize   = snap2size
    ses.format    = "tar" if options.tarfile else "folders"
    ses.path      = vol.path+"/"+bksession+"-tmp"
    ses_index     = allsessions.index(ses)

    # Set current dir and make new session folder
    os.chdir(metadir+bkdir)
    os.makedirs(sdir+"-tmp")

    zeros     = bytes(chunksize)
    bcount    = ddbytes = 0
    addrsplit = -address_split[1]
    lchunk_addr = last_chunk_addr(snap2size, chunksize)

    if send_all:
        # sends all from this address forward
        sendall_addr = 0
    else:
        # beyond range; send all is off
        sendall_addr = snap2size + 1

    # Check volume size vs prior backup session
    if not send_all:
        prior_size       = vol.volsize
        next_chunk_addr  = last_chunk_addr(prior_size, chunksize) + chunksize
        if prior_size > snap2size:
            print("  Volume size has shrunk.")
        elif snap2size-1 >= next_chunk_addr:
            print("  Volume size has increased.")
            sendall_addr = next_chunk_addr

    if aset.compression=="zlib":
        compress = zlib.compress
    # add zstd here.
    compresslevel = int(aset.compr_level)

    # Use tar to stream files to destination
    stream_started = False
    untar_cmd = destcd \
                +" && mkdir -p ."+bkdir+"/"+sdir+"-tmp" \
                +" && "+destcd + bkdir                  \
                +" && rm -f .set"
    if options.tarfile:
        # don't untar at destination
        untar_cmd = [ untar_cmd
                    +" && cat >"+pjoin(sdir+"-tmp",bksession+".tar")]
    else:
        untar_cmd = [ untar_cmd
                    +" && tar -xmf - && sync -f "+datavol]

    # Open source volume and its delta bitmap as r, session manifest as w.
    with open(pjoin("/dev",aset.vgname,snap2vol),"rb") as vf, \
         open(sdir+"-tmp/manifest", "w") as hashf,             \
         open("/dev/zero" if send_all else vol.mapfile+"-tmp","r+b") as bmapf:

        bmap_mm = bytes(1) if send_all else mmap.mmap(bmapf.fileno(), 0)
        vf_seek = vf.seek; vf_read = vf.read
        sha256 = hashlib.sha256; BytesIO = io.BytesIO

        # Show progress in increments determined by 1000/checkpt_pct
        # where '200' results in five updates i.e. in unattended mode.
        checkpt = checkpt_pct = 335 if options.unattended else 1
        percent = 0

        # Cycle over range of addresses in volume.
        for addr in range(0, snap2size, chunksize):

            # Calculate corresponding position in bitmap.
            chunk = addr // chunksize
            bmap_pos = chunk // 8
            b = chunk % 8

            # Send chunk if its above the send-all line
            # or its bit is on in the deltamap.
            if addr >= sendall_addr or bmap_mm[bmap_pos] & (1 << b):

                vf_seek(addr)
                buf = vf_read(chunksize)
                destfile = "x"+chformat % addr

                # Start tar stream
                if not stream_started:
                    untar = subprocess.Popen(dest_run_args(desttype, untar_cmd),
                            stdin =subprocess.PIPE,    stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
                    tarf = tarfile.open(mode="w|", fileobj=untar.stdin)
                    tarf_addfile = tarf.addfile; TarInfo = tarfile.TarInfo
                    LNKTYPE = tarfile.LNKTYPE
                    stream_started = True

                # Show progress.
                percent = int(bmap_pos/bmap_size*1000)
                if percent >= checkpt:
                    print("  %.1f%%   %dMB " % (percent/10, bcount//1000000),
                          end="\x0d", flush=True)
                    checkpt += checkpt_pct

                # Compress & write only non-empty and last chunks
                if buf == zeros and addr < lchunk_addr:
                    print("0", destfile, file=hashf)
                    continue

                # Performance fix: move compression into separate processes
                buf      = compress(buf, compresslevel)
                bhash    = sha256(buf)
                # Add buffer to stream
                tar_info = TarInfo("%s-tmp/%s/%s" % 
                                (sdir, destfile[1:addrsplit], destfile))
                print(bhash.hexdigest(), destfile, file=hashf)

                # If chunk already in archive, link to it
                if not dedup:
                    pass

                elif dedup == 3:
                    bhashb = bhash.digest()
                    row    = cursor.execute("SELECT chunk,ses_id FROM hashindex "
                            "WHERE id = ?", (bhashb,)).fetchone()
                    if row:
                        ddch, ddses_i = row
                        ddses = allsessions[ddses_i]
                        ddchx = chformat % (c_uint64(ddch).value)
                        tar_info.type = LNKTYPE
                    else:
                        # perf fix: use execute_many + index of waiting inserts
                        cursor.execute("INSERT INTO hashindex(id,chunk,ses_id)"
                            " VALUES(?,?,?)", 
                            (bhashb, c_int64(addr).value, ses_index))

                elif dedup == 4:
                    bhashb = bhash.digest()
                    i      = int.from_bytes(bhashb[:ht_ksize], "big")
                    ht     = hashtree[i]; ct = chtree[i]
                    while True:
                        try:
                            pos = ht.index(int.from_bytes(bhashb[:hash0len],
                                                        "little"))
                        except ValueError:
                            if idxcount < chtree_max:
                                hashtree[i].frombytes(bhashb)
                                chtree[i].append(idxcount)
                                dataf.write(ses_index.to_bytes(ses_w,"big"))
                                dataf.write(addr.to_bytes(ch_w,"big"))
                                idxcount += 1
                                break # while

                        if pos % hsegs == 0 and \
                            ht[pos+1:pos+hsegs].tobytes() == bhashb[hash0len:]:
                            # First hash segment matched; test remaining segments.
                            data_i = ct[pos//hsegs]
                            dataf.seek(data_i*(ses_w+ch_w))
                            ddses  = allsessions[int.from_bytes(
                                     dataf.read(ses_w),"big")]
                            ddchx  = dataf.read(ch_w).hex().zfill(chdigits)
                            dataf.seek(0,2)
                            tar_info.type = LNKTYPE
                            break # while

                        pos += hsegs - (pos % hsegs)
                        ht = ht[pos:]; ct = ct[pos//hsegs:]

                elif dedup == 5:
                    bhashb = bhash.digest()
                    i      = int.from_bytes(bhashb[:ht_ksize], "big")

                    pos = hashtree[i].find(bhashb)
                    if pos % hash_w == 0:
                        data_i = chtree[i][pos//hash_w]
                        dataf.seek(data_i*(ses_w+ch_w))
                        ddses = allsessions[int.from_bytes(
                                dataf.read(ses_w),"big")]
                        ddchx = dataf.read(ch_w).hex().zfill(chdigits)
                        dataf.seek(0,2)
                        tar_info.type = LNKTYPE
                    elif idxcount < chtree_max:
                        hashtree[i].extend(bhashb)
                        chtree[i].append(idxcount)
                        dataf.write(ses_index.to_bytes(ses_w,"big"))
                        dataf.write(addr.to_bytes(ch_w,"big"))
                        idxcount += 1

                if tar_info.type == LNKTYPE:
                    tar_info.linkname = "%s/%s/%s/x%s" % \
                        (ddses.volume.name,
                            ddses.name+"-tmp" if ddses==ses else ddses.name,
                            ddchx[:addrsplit],
                            ddchx)
                    ddbytes += len(buf)
                    tarf_addfile(tarinfo=tar_info)
                else:
                    tar_info.size = len(buf)
                    tarf_addfile(tarinfo=tar_info, fileobj=BytesIO(buf))
                    bcount += len(buf)

    # Send session info, end stream and cleanup
    if stream_started:
        print("  100%  ", ("%.1f" % (bcount/1000000)) +"MB",
              ("  ( dd: %0.1fMB reduced. )" % (ddbytes/1000000)) 
              if ddbytes else "")

        # Save session info
        ses.save_info()
        for session in vol.sessions.values() \
                        if vol.que_meta_update == "true" else [ses]:
            tarf.add(pjoin(vol.name, os.path.basename(session.path)))
        vol.que_meta_update = "false"
        vol.save_volinfo("volinfo-tmp")
        tarf.add(datavol+"/volinfo-tmp")
        tarf.add(os.path.basename(aset.confpath))

        #print("Ending tar process ", end="")
        tarf.close()
        untar.stdin.close()
        for i in range(30):
            if untar.poll() is not None:
                break
            time.sleep(1)
        if untar.poll() is None:
            time.sleep(5)
            if untar.poll() is None:
                untar.terminate()
                print("terminated untar process!")

        # Cleanup on VM/remote
        dest_run([ destcd + bkdir
            +" && touch .set"
            +" && mv "+sdir+"-tmp "+sdir
            +" && mv "+datavol+"/volinfo-tmp "+datavol+"/volinfo"
            +" && sync -f "+datavol+"/volinfo"])
        # Local cleanup, remove -tmp suffixes
        os.replace(ses.path, ses.path[:-4])
        ses.path = ses.path[:-4]
        os.replace(vol.path+"/volinfo-tmp", vol.path+"/volinfo")

    else:
        shutil.rmtree(metadir+bkdir+"/"+sdir+"-tmp")

    if bcount == 0:
        print("  No changes.")

    if dedup:
        show_mem_stats() ####

    return stream_started


# Build deduplication hash index and list

def init_dedup_index3(listfile=""):

    addrsplit = -address_split[1]
    sessions  = aset.allsessions
    c_int64   = ctypes.c_int64
    chdigits  = max_address.bit_length() // 4
    chformat  = "%0"+str(chdigits)+"x"
    ctime     = time.time()

    db     = sqlite3.connect(tmpdir+"/hashindex.db")
    #db    = sqlite3.connect(":memory:")
    cursor = db.cursor()
    cursor.execute('''
        CREATE TABLE hashindex(id BLOB PRIMARY KEY ON CONFLICT IGNORE,
        chunk INTEGER, ses_id INTEGER
        )''')
    insert_phrase = 'INSERT INTO hashindex(id, chunk, ses_id) VALUES(?,?,?)'
    cursor.execute('PRAGMA cache_size = 10000')
    #cursor.execute('PRAGMA synchronous = OFF')
    #cursor.execute('PRAGMA journal_mode = OFF')

    if listfile:
        dedupf = open(tmpdir+"/"+listfile, "w")

    inserts = []; rows = 0
    for sesnum, ses in enumerate(sessions):
        volname = ses.volume.name; sesname = ses.name
        with open(pjoin(ses.path,"manifest"),"r") as manf:
            for ln in manf:
                line = ln.strip().split()
                if line[0] == "0":
                    continue
                bhash = bytes().fromhex(line[0])
                uint  = int(line[1][1:],16)
                addr  = c_int64(uint)

                inserts.append((bhash, addr.value, sesnum))
                # Insert only 1 at a time when generating a listfile.
                if listfile or not len(inserts) % 2000:
                    cursor.executemany(insert_phrase, inserts)
                    rows += cursor.rowcount
                    inserts.clear()

                    if listfile and cursor.rowcount < 1:
                        row = cursor.execute("SELECT chunk,ses_id FROM hashindex "
                                "WHERE id = ?", (bhash,)).fetchone()
                        if row:
                            ddch, ddses_i = row
                            ddses = sessions[ddses_i]
                            ddchx = chformat % ddch
                            print("%s/%s/%s/x%s %s/%s/%s/%s" % \
                                (ddses.volume.name, ddses.name, ddchx[:addrsplit], ddchx,
                                volname, sesname, line[1][1:addrsplit], line[1]),
                                file=dedupf)

    if len(inserts):
        cursor.executemany(insert_phrase, inserts)
        rows += cursor.rowcount
    db.commit()
    aset.hashindex = db

    if listfile:
        dedupf.close()

    ####
    print("\nIndexed in %.1f seconds." % int(time.time()-ctime))
    vsz, rss = map(int, os.popen("ps -up"+str(os.getpid())).readlines()[-1].split()[4:6])
    print("\nMemory use: Max %dMB, index count: %d" %
        (resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * resource.getpagesize() // 1024//1024,
        rows)
        )
    print("Current: vsize %d, rsize %d" % (vsz/1000,rss/1000))


def init_dedup_index4(listfile=""):

    ctime     = time.time()
    # Define arrays and element widths
    hashdigits = 256 // 4 # 4bits per hex digit
    hash_w     = hashdigits // 2
    hash0len   = 8        # "Q" ulonglong = 8bytes
    hsegs      = hash_w//hash0len
    ht_ksize   = 4 # hex digits for tree key
    hashtree   = [array("Q") for x in range(2**(ht_ksize*4))]
    chtree     = [array("I") for x in range(2**(ht_ksize*4))]
    chtree_max = 2**(chtree[0].itemsize*8) # "I" has 32bit range
    chdigits   = max_address.bit_length() // 4 # 4bits per digit
    ses_w = 2; ch_w = chdigits //2
    # limit number of sessions to ses_w + room for vol set:
    sessions   = aset.allsessions[:2**(ses_w*8)-(len(aset.vols))-1]
    addrsplit  = -address_split[1]

    dataf = open(tmpdir+"/hashindex.dat","w+b")
    if listfile:
        dedupf = open(tmpdir+"/"+listfile, "w")

    count = match = 0
    for sesnum, ses in enumerate(sessions):
        volname = ses.volume.name; sesname = ses.name
        with open(pjoin(ses.path,"manifest"),"r") as manf:
            for ln in manf:
                ln1, ln2 = ln.strip().split()
                if ln1 == "0":
                    continue
                bhashb = bytes().fromhex(ln1)
                #bhash = int(ln1[:hash0len*2], 16)
                i      = int(ln1[:ht_ksize], 16)

                ht = hashtree[i]; ct = chtree[i]
                while True:
                    try:
                        pos = ht.index(int.from_bytes(bhashb[:hash0len],
                                                      "little"))
                    except ValueError:
                        if count < chtree_max:
                            hashtree[i].frombytes(bhashb)
                            chtree[i].append(count)
                            dataf.write(sesnum.to_bytes(ses_w,"big"))
                            dataf.write(bytes().fromhex(ln2[1:]))
                            count += 1
                            break # while

                    if pos % hsegs == 0 and \
                       ht[pos+1:pos+hsegs].tobytes() == bhashb[hash0len:]:
                        #First hash segment matched; test remaining segments.
                        if listfile:
                            data_i = ct[pos//hsegs]
                            dataf.seek(data_i*(ses_w+ch_w))
                            ddses  = sessions[int.from_bytes(
                                     dataf.read(ses_w),"big")]
                            ddchx  = dataf.read(ch_w).hex().zfill(chdigits)
                            print("%s/%s/%s/x%s %s/%s/%s/%s" % \
                                (ddses.volume.name, ddses.name, ddchx[:addrsplit], ddchx,
                                volname, sesname, ln2[1:addrsplit], ln2),
                                file=dedupf)
                            dataf.seek(0,2)
                        match += 1
                        break # while

                    pos += hsegs - (pos % hsegs)
                    ht = ht[pos:]; ct = ct[pos//hsegs:]

    if listfile:
        dedupf.close()
        dataf.close()

    aset.hashindex = (hashtree, ht_ksize, hashdigits, hash_w, hash0len,
                      dataf, chtree, chdigits, ch_w, ses_w)

    print("\n %d matches in %.1f seconds." % (match, int(time.time()-ctime)))
    vsz, rss = map(int, os.popen("ps -up"+str(os.getpid())).readlines()[-1].split()[4:6])
    print("\nMemory use: Max %dMB, index count: %d" %
        (resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * resource.getpagesize() // 1024//1024,
         count)
        )
    print("Current: vsize %d, rsize %d" % (vsz/1000,rss/1000))


def init_dedup_index5(listfile=""):

    ctime = time.time()
    # Define arrays and element widths
    hashdigits = 256 // 4  # sha256 @4bits per hex digit
    hash_w     = hashdigits // 2
    ht_ksize   = 4 # hex digits for tree key
    hashtree   = [bytearray() for x in range(2**(ht_ksize*4))]
    chtree     = [array("I") for x in range(2**(ht_ksize*4))]
    chtree_max = 2**(chtree[0].itemsize*8) # "I" has 32bit range
    chdigits   = max_address.bit_length() // 4 # 4bits per digit
    ses_w = 2; ch_w = chdigits //2
    # limit number of sessions to ses_w range:
    sessions   = aset.allsessions[:2**(ses_w*8)-(len(aset.vols))-1]
    addrsplit  = -address_split[1]

    dataf  = open(tmpdir+"/hashindex.dat","w+b")
    if listfile:
        dedupf = open(tmpdir+"/"+listfile, "w")

    count = match = 0
    for sesnum, ses in enumerate(sessions):
        volname = ses.volume.name; sesname = ses.name
        with open(pjoin(ses.path,"manifest"),"r") as manf:
            for ln in manf:
                ln1, ln2 = ln.strip().split()
                if ln1 == "0":
                    continue
                bhashb = bytes().fromhex(ln1)
                i      = int(ln1[:ht_ksize], 16)
                pos    = hashtree[i].find(bhashb)
                if pos % hash_w == 0:
                    match += 1
                    if listfile:
                        data_i = chtree[i][pos//hash_w]
                        dataf.seek(data_i*(ses_w+ch_w))
                        ddses  = sessions[int.from_bytes(
                                 dataf.read(ses_w),"big")]
                        ddchx  = dataf.read(ch_w).hex().zfill(chdigits)
                        print("%s/%s/%s/x%s %s/%s/%s/%s" % \
                            (ddses.volume.name, ddses.name, ddchx[:addrsplit], ddchx,
                            volname, sesname, ln2[1:addrsplit], ln2),
                            file=dedupf)
                        dataf.seek(0,2)
                elif count < chtree_max:
                    hashtree[i].extend(bhashb)
                    chtree[i].append(count)
                    dataf.write(sesnum.to_bytes(ses_w,"big"))
                    dataf.write(bytes().fromhex(ln2[1:]))
                    count += 1

    if listfile:
        dedupf.close()
        dataf.close()

    aset.hashindex = (hashtree, ht_ksize, hashdigits, hash_w,
                      dataf, chtree, chdigits, ch_w, ses_w)

    print("\nIndexed in %.1f seconds." % int(time.time()-ctime))
    vsz, rss = map(int, os.popen("ps -up"+str(os.getpid())).readlines()[-1].split()[4:6])
    print("\nMemory use: Max %dMB, index count: %d, matches: %d" %
        (resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * resource.getpagesize() // 1024//1024,
         count, match)
        )
    print("Current: vsize %d, rsize %d" % (vsz/1000,rss/1000))
    #print("idx size: %d" % sys.getsizeof(idx))


# Deduplicate data already in archive

def dedup_existing():

    print("Building deduplication index...")
    init_dedup_index("dedup.lst")

    print("Linking...")
    do_exec([dest_run_args(desttype, [destcd + bkdir
               +" && cat >"+tmpdir+"/rpc/dest.lst"
               +" && python3 "+tmpdir+"/rpc/dest_helper.py dedup"
               ])
            ], infile=tmpdir+"/dedup.lst")


# Controls flow of monitor and send_volume procedures:

def monitor_send(datavols, selected=[], monitor_only=True):

    global volgroups, l_vols

    localtime = time.strftime("%Y%m%d-%H%M%S")

    datavols, newvols \
        = prepare_snapshots(selected if len(selected) >0 else datavols)

    volgroups = get_lvm_vgs()
    if aset.vgname not in volgroups.keys():
        raise ValueError("Volume group "+aset.vgname+" not present.")
    l_vols = volgroups[aset.vgname].lvs

    if monitor_only:
        newvols = []
        volumes = []

    if len(datavols)+len(newvols) == 0:
        x_it(0, "No new data.")

    if len(datavols) > 0:
        get_lvm_deltas(datavols)

    if options.dedup:
        init_dedup_index()

    if not monitor_only:
        print("\nSending backup session", localtime,
              "to", (desttype+"://"+destsys) if \
                  destsys != "internal:" else aset.destmountpoint)

    for datavol in datavols+newvols:
        print("\nVolume :", datavol)
        vol = aset.vols[datavol]

        map_updated \
                = update_delta_digest(datavol)

        if not monitor_only:
            sent \
                = send_volume(datavol, localtime)
            finalize_bk_session(vol, sent)
        else:
            finalize_monitor_session(vol, map_updated)


def init_deltamap(bmfile, bmsize):
    if os.path.exists(bmfile):
        os.remove(bmfile)
    if os.path.exists(bmfile+"-tmp"):
        os.remove(bmfile+"-tmp")
    with open(bmfile, "wb") as bmapf:
        os.ftruncate(bmapf.fileno(), bmsize)


def rotate_snapshots(vol, rotate=True):
    snap1vol = vol.name+".tick"
    snap2vol = vol.name+".tock"
    if rotate:
        #print("Rotating snapshots for", datavol)
        # Review: this should be atomic
        do_exec([["lvremove","--force", aset.vgname+"/"+snap1vol]])
        do_exec([["lvrename",aset.vgname+"/"+snap2vol, snap1vol]])
        l_vols[snap2vol].lv_name = l_vols[snap1vol].lv_name
        l_vols[snap2vol].lv_path = l_vols[snap1vol].lv_path
        l_vols[snap1vol] = l_vols[snap2vol]
        del l_vols[snap2vol]

    else:
        do_exec([["lvremove","--force",aset.vgname+"/"+snap2vol]])
        del l_vols[snap2vol]


def finalize_monitor_session(vol, map_updated):
    rotate_snapshots(vol, rotate=map_updated)
    os.rename(vol.mapfile+"-tmp", vol.mapfile)
    os.sync()


def finalize_bk_session(vol, sent):
    rotate_snapshots(vol, rotate=sent)
    init_deltamap(vol.mapfile, vol.mapsize())
    os.sync()


# Prune backup sessions from an archive. Basis is a non-overwriting dir tree
# merge starting with newest dirs and working backwards. Target of merge is
# timewise the next session dir after the pruned dirs.
# Specify data volume and one or two member list with start [end] date-time
# in YYYYMMDD-HHMMSS format.

def prune_sessions(datavol, times):

    # Validate date-time params
    for dt in times:
        datetime.datetime.strptime(dt, "%Y%m%d-%H%M%S")

    # t1 alone should be a specific session date-time,
    # t1 and t2 together are a date-time range.
    t1 = "S_"+times[0].strip()
    if len(times) > 1:
        t2 = "S_"+times[1].strip()
        if t2 <= t1:
            x_it(1, "Error: second date-time must be later than first.")
    else:
        t2 = ""

    print("\nPruning Volume :", datavol)

    volume = aset.vols[datavol]
    sessions = volume.sesnames
    if len(sessions) < 2:
        print("  No extra sessions to prune.")
        return
    if t1 >= sessions[-1] or t2 >= sessions[-1]:
        print("  Cannot prune most recent session; Skipping.")
        return

    # Find specific sessions to prune;
    # Use contiguous ranges.
    to_prune = []
    if options.allbefore:
        for ses in sessions:
            if ses >= t1:
                break
            to_prune.append(ses)

    elif t2 == "":
        if t1 in sessions:
            to_prune.append(t1)

    else:
        if t1 in sessions:
            start = sessions.index(t1)
        else:
            for ses in sessions:
                if ses > t1:
                    start = sessions.index(ses)
                    break
        end = 0
        if t2 in sessions:
            end = sessions.index(t2)+1
        else:
            for ses in reversed(sessions):
                if ses < t2:
                    end = sessions.index(ses)+1
                    break
        to_prune = sessions[start:end]

    if len(to_prune) == 0:
        print("  No sessions in this date-time range.")
        return

    # Determine target session where data will be merged.
    target_s = sessions[sessions.index(to_prune[-1]) + 1]

    if not options.unattended and len(to_prune)>1:
        print("This will remove multiple sessions:\n"," ".join(to_prune))
        ans = input("Are you sure? [y/N]: ")
        if ans.lower() not in {"y","yes"}:
            x_it(0,"")

    merge_sessions(datavol, to_prune, target_s,
                   clear_sources=True)


# Merge sessions together. Starting from first session results in a target
# that contains an updated, complete volume. Other starting points can
# form the basis for a pruning operation.
# Specify the data volume (datavol), source sessions (sources), and
# target. Caution: clear_sources is destructive.

def merge_sessions(datavol, sources, target, clear_sources=False):

    volume = aset.vols[datavol]
    for ses in sources + [target]:
        if volume.sessions[ses].format == "tar":
            x_it(1, "Cannot merge range containing tarfile session.")

    # Get volume size
    chdigits   = max_address.bit_length() // 4 # 4bits per digit
    chformat   = "x%0"+str(chdigits)+"x"
    volsize    = volume.sessions[target].volsize
    vol_shrank = volsize < volume.sessions[sources[0]].volsize
    last_chunk = chformat % last_chunk_addr(volsize, aset.chunksize)
    lc_filter  = '"'+last_chunk+'"'

    # Prepare manifests for efficient merge using fs mv/replace. The target is
    # included as a source, and oldest source is our target for mv. At the end
    # the merge_target will be renamed to the specified target. This avoids
    # processing the full range of volume chunks in the likely case that
    # the oldest (full) session is being pruned.
    merge_sources = ([target] + list(reversed(sources)))[:-1]
    merge_target  = sources[0]

    with open(pjoin(tmpdir,"sources.lst"), "w") as srcf:
        print(merge_target, target, file=srcf)

        # Get manifests, append session name to eol, print session names to srcf.
        print("  Reading manifests")
        manifests = []
        for ses in merge_sources:
            if clear_sources:
                print(ses, file=srcf)
                manifests.append("man."+ses)
            do_exec([["sed", "-E", "s|$| "+ses+"|",
                      pjoin(metadir+bkdir,datavol,ses+"/manifest")
                    ]], cwd=tmpdir, out="man."+ses)
        print("###", file=srcf)

    # Unique-merge filenames: one for rename, one for new full manifest.
    do_exec([["sort", "-umd", "-k2,2"] + manifests],
             out="manifest.tmp", cwd=tmpdir)
    do_exec([["sort", "-umd", "-k2,2", "manifest.tmp",
              pjoin(metadir+bkdir,datavol,merge_target+"/manifest")
            ]], out="manifest.new", cwd=tmpdir)

    # Output manifest filenames in the sftp-friendly form:
    # 'rename src_session/subdir/xaddress target/subdir/xaddress'
    # then pipe to destination and run dest_helper.py.
    print("  Merging to", target)

    os.chdir(pjoin(metadir+bkdir,datavol))
    do_exec([
            ["awk", "$2<="+lc_filter, tmpdir+"/manifest.tmp"],
            ["sed", "-E",

             "s|^0 x(\S{" + str(address_split[0]) + "})(\S+)\s+(S_\S+)|"
             "rm "+merge_target+"/\\1/x\\1\\2|; t; "

             "s|^\S+\s+x(\S{" + str(address_split[0]) + "})(\S+)\s+(S_\S+)|"
             "rename \\3/\\1/x\\1\\2 "+merge_target+"/\\1/x\\1\\2|"
            ],
            ["cat", tmpdir+"/sources.lst", "-"],
            dest_run_args(desttype, [destcd + bkdir+"/"+datavol
                +" && cat >"+tmpdir+"/rpc/dest.lst"
                +" && python3 "+tmpdir+"/rpc/dest_helper.py merge"
                ])
            ])

    # Update info records and trim to target size
    if clear_sources:
        for ses in sources:
            affected = volume.delete_session(ses)
        volume.sessions[target].save_info()
        volume.save_volinfo()
        print("  Removed", " ".join(sources))

    do_exec([["awk", "$2<="+lc_filter+" {print $1, $2}", tmpdir+"/manifest.new"]],
            out=target+"/manifest") 

    if vol_shrank:
        # If volume size shrank in this period then make trim list.
        do_exec([["awk", "$2>"+lc_filter, tmpdir+"/manifest.new"],
                 ["sed", "-E", "s|^\S+\s+x(\S{" + str(address_split[0]) + "})(\S+)|"
                  +target+"/\\1/x\\1\\2|"],
                ], out=target+"/delete")

    do_exec([["tar", "-cf", "-", "volinfo", target],
             dest_run_args(desttype, [destcd + bkdir+"/"+datavol
                    +"  && tar -xmf -",

                    # Trim on dest.
                    ( " && cat "+target+"/delete  |  xargs -r rm -f"
                    + " && rm "+target+"/delete"
                    + " && find "+target+" -maxdepth 1 -type d -empty -delete"
                    ) if vol_shrank else ""
             ])])

    do_exec([["sync", "-f", "volinfo"]])

# Receive volume from archive. If no save_path specified, then verify only.
# If diff specified, compare with current source volume; with --remap option
# can be used to resync volume with archive if the deltamap or snapshots
# are lost or if the source volume reverted to an earlier state.

def receive_volume(datavol, select_ses="", save_path="", diff=False):

    def diff_compare(dbuf,z):
        if dbuf != cmpf.read(chunksize):
            print("* delta", faddr, "Z   " if z else "    ")
            if remap:
                volsegment = addr // chunksize 
                bmap_pos = volsegment // 8
                bmap_mm[bmap_pos] |= 1 << (volsegment % 8)
            return len(dbuf)
        else:
            return 0

    verify_only = options.action == "verify"
    assert not (verify_only and (diff or save_path))
    attended    = not options.unattended
    remap       = options.remap

    vgname    = aset.vgname
    vol       = aset.vols[datavol]
    volsize   = vol.volsize
    chunksize = aset.chunksize
    zeros     = bytes(chunksize)
    snap1vol  = datavol+".tick"
    sessions  = vol.sesnames

    # Set the session to retrieve
    if select_ses:
        datetime.datetime.strptime(select_ses, "%Y%m%d-%H%M%S")
        select_ses = "S_"+select_ses
        if select_ses not in sessions:
            x_it(1, "The specified session date-time does not exist.")
    elif len(sessions) > 0:
        select_ses = sessions[-1]
    else:
        x_it(1, "No sessions available.")

    if aset.compression in {"zlib","gzip"}:
        decompress  = zlib.decompress
        decomp_bits = 32 + zlib.MAX_WBITS
    # add zstd here.

    if save_path and os.path.exists(save_path) and attended:
        print("\n!! This will erase all existing data in",save_path,"!!")
        ans = input("   Are you sure? [y/N]: ")
        if ans.lower() not in {"y","yes"}:
            x_it(0,"")

    print("\nReading manifests")
    chdigits    = max_address.bit_length() // 4 # 4bits per digit
    chformat    = "x%0"+str(chdigits)+"x"
    lchunk_addr = last_chunk_addr(volsize, chunksize)
    last_chunkx = chformat % lchunk_addr
    open(tmpdir+"/manifests.cat", "wb").close()

    # Collect session manifests
    include = False
    for ses in reversed(sessions):
        if ses == select_ses:
            include = True
        elif not include:
            continue

        if vol.sessions[ses].format == "tar":
            raise NotImplementedError(
                "Receive from tarfile not yet implemented: "+ses)

        # add session column to end of each line:
        do_exec([["sed", "-E", "s|$| "+ses+"|", pjoin(ses,"manifest")]],
                cwd=pjoin(metadir+bkdir,datavol), out=">>"+tmpdir+"/manifests.cat")

    # Merge manifests and send to archive system:
    # sed is used to expand chunk info into a path and filter out any entries
    # beyond the current last chunk, then piped to cat on destination.
    # Note address_split is used to bisect filename to construct the subdir.
    cmds = [["sort", "-u", "-d", "-k2,2", tmpdir+"/manifests.cat"],
            ["tee", tmpdir+"/manifest.verify"],
            ["sed", "-E", "s|^\S+\s+x(\S{" + str(address_split[0]) + "})(\S+)\s+"
            +"(S_\S+)|\\3/\\1/x\\1\\2|;"
            +" /"+last_chunkx+"/q"],
            dest_run_args(desttype, ["cat >"+tmpdir+"/rpc/dest.lst"])
           ]
    do_exec(cmds, cwd=pjoin(metadir+bkdir,datavol))

    # Prepare save volume
    if save_path:
        # Discard all data in destination if this is a block device
        # then open for writing
        returned_home = False
        if vg_exists(os.path.dirname(save_path)):
            lv = os.path.basename(save_path)
            vg = os.path.basename(os.path.dirname(save_path))
            # Does save path == original path?
            returned_home = lv == datavol
            if not lv_exists(vg,lv):
                if vg != vgname:
                    x_it(1, "Cannot auto-create volume:"
                         " Volume group does not match config.")
                do_exec([["lvcreate", "-kn", "-ay", "-V", str(volsize)+"b",
                          "--thin", "-n", lv, vg+"/"+aset.poolname]])
            elif l_vols[lv].lv_size != volsize:
                do_exec([["lvresize", "-L", str(volsize)+"b", "-f", save_path]])

        if os.path.exists(save_path) \
        and stat.S_ISBLK(os.stat(save_path).st_mode):
            do_exec([["blkdiscard", save_path]])
            savef = open(save_path, "w+b")
        else: # file
            savef = open(save_path, "w+b")
            savef.truncate(0)          ; savef.flush()
            savef.truncate(volsize)    ; savef.flush()
        print("Saving to", save_path)

    elif diff:
        if not lv_exists(vgname, datavol):
            x_it(1, "Local volume must exist for diff.")
        if remap:
            if not lv_exists(vgname, snap1vol):
                do_exec([["lvcreate", "-pr", "-kn", "-ay", "-s", vgname+"/"+datavol,
                          "-n", snap1vol]])
                print("  Initial snapshot created for", datavol)
            if not os.path.exists(vol.mapfile):
                init_deltamap(vol.mapfile, vol.mapsize())
            bmapf = open(vol.mapfile, "r+b")
            os.ftruncate(bmapf.fileno(), vol.mapsize())
            bmap_mm = mmap.mmap(bmapf.fileno(), 0)
        else:
            if not lv_exists(vgname, snap1vol):
                print("Snapshot '.tick' not available; Comparing with"
                      " source volume instead.")
                snap1vol = datavol

            if volsize != l_vols[snap1vol].lv_size:
                x_it(1, "Volume sizes differ:"
                    "\n  Archive = %d"
                    "\n  Local   = %d" % (volsize, l_vols[snap1vol].lv_size))

        cmpf  = open(pjoin("/dev",vgname,snap1vol), "rb")
        diff_count = 0

    print("\nReceiving volume", datavol, select_ses)
    # Create retriever process using py program
    cmd = dest_run_args(desttype,
            [destcd + bkdir+"/"+datavol
            +"  && python3 "+tmpdir+"/rpc/dest_helper.py receive"
            ])
    getvol = subprocess.Popen(cmd, stdout=subprocess.PIPE)

    # Open manifest then receive, check and save data
    with open(tmpdir+"/manifest.verify", "r") as mf:
        for addr in range(0, volsize, chunksize):
            faddr = chformat % addr
            if attended:
                print(int(addr/volsize*100),"%  ",faddr,end="  ")

            cksum, fname, ses = mf.readline().strip().split()
            if fname != faddr:
                raise ValueError("Bad fname "+fname)

            # Read chunk size
            untrusted_size = int.from_bytes(getvol.stdout.read(4),"big")

            if cksum.strip() == "0":
                if untrusted_size != 0:
                    raise ValueError("Expected size 0, got %d at %s %s." 
                                     % (untrusted_size, ses, fname))

                if attended:
                    print("OK",end="\x0d")

                if save_path:
                    savef.seek(chunksize, 1)

                if diff:
                    diff_count += diff_compare(zeros,True)

                continue

            # allow for slight expansion from compression algo
            if untrusted_size > chunksize + (chunksize // 1024) \
                or untrusted_size < 1:
                    raise BufferError("Bad chunk size: %d" % untrusted_size)

            # Size is OK.
            size = untrusted_size

            # Read chunk buffer
            untrusted_buf = getvol.stdout.read(size)
            rc  = getvol.poll()
            if rc is not None and len(untrusted_buf) == 0:
                break

            if len(untrusted_buf) != size:
                with open(tmpdir+"/bufdump", "wb") as dump:
                    dump.write(untrusted_buf)
                raise BufferError("Got %d bytes, expected %d"
                                  % (len(untrusted_buf), size))
            if cksum != hashlib.sha256(untrusted_buf).hexdigest():
                with open(tmpdir+"/bufdump", "wb") as dump:
                    dump.write(untrusted_buf)
                raise ValueError("Bad hash "+fname
                    +" :: "+hashlib.sha256(untrusted_buf).hexdigest())

            # Proceed with decompress.
            # fix for zstd support
            untrusted_decomp = decompress(untrusted_buf, decomp_bits, chunksize)
            if len(untrusted_decomp) != chunksize and addr < lchunk_addr:
                raise BufferError("Decompressed to %d bytes." % len(untrusted_decomp))
            if addr == lchunk_addr and len(untrusted_decomp) != volsize - lchunk_addr:
                raise BufferError("Decompressed to %d bytes." % len(untrusted_decomp))

            # Buffer is OK...
            buf = untrusted_decomp
            if attended:
                print("OK",end="\x0d")

            if verify_only:
                continue

            if save_path:
                savef.write(buf)
            elif diff:
                diff_count += diff_compare(buf,False)

        if rc is not None and rc > 0:
            raise RuntimeError("Error code from getvol process: "+str(rc))
        if addr+len(buf) != volsize:
            raise ValueError("Received range %d does not match volume size %d."
                             % (addr+len(buf), volsize))
        print("100%")
        print("Received byte range:", addr+len(buf))

        if save_path:
            savef.flush() ; savef.close()
            if returned_home:
                if not lv_exists(vgname, snap1vol):
                    do_exec([["lvcreate", "-pr", "-kn",
                        "-ay", "-s", vgname+"/"+datavol, "-n", snap1vol]])
                    print("  Initial snapshot created for", datavol)
                if not os.path.exists(vol.mapfile):
                    init_deltamap(vol.mapfile, vol.mapsize())
                if select_ses != sessions[-1]:
                    print("Restored from older session: Volume may be out of"
                        " sync with archive until '%s --remap diff %s' is run!"
                        % (prog_name, datavol))
        elif diff:
            cmpf.close()
            if remap:
                bmapf.close()
                print("Delta bytes re-mapped:", diff_count)
                if diff_count > 0:
                    print("\nNext 'send' will bring this volume into sync.")
            elif diff_count:
                x_it(1, "%d bytes differ." % diff_count)


def show_mem_stats():
    vsz, rss = map(int, os.popen("ps -up"+str(os.getpid())).readlines()[-1].split()[4:6])
    print("\nMemory use: Max %dMB" %
        (resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * resource.getpagesize() // 1024//1024)
        )
    print("Current: vsize %d, rsize %d" % (vsz/1000,rss/1000))


# Exit with simple message

def x_it(code, text):
    sys.stderr.write(text+"\n")
    exit(code)




##  MAIN  #####################################################################

# Constants / Globals
prog_version          = "0.2.0betaZ"
format_version        = 1
prog_name             = "sparsebak"
topdir                = "/"+prog_name
metadir               = "/var/lib"
tmpdir                = "/tmp/"+prog_name
volgroups             = {}
l_vols                = {}
# Disk block size:
bs                    = 512
# LVM min blocks = 128 = 64kBytes:
lvm_block_factor      = 128
# Default archive chunk size = 64kBytes:
bkchunksize           = 1 * lvm_block_factor * bs
assert bkchunksize % (lvm_block_factor * bs) == 0
max_address           = 0xffffffffffffffff # 64bits
# for 64bits, a subdir split of 9+7 allows 2048 files per dir:
address_split         = [len(hex(max_address))-2-7, 7]
pjoin                 = os.path.join
shell_prefix          = "set -e && export LC_ALL=C\n"
os.environ["LC_ALL"]  = "C"


if sys.hexversion < 0x3050000:
    x_it(1, "Python ver. 3.5 or greater required.")

# Root user required
if os.getuid() > 0:
    x_it(1, "Must be root user.")

# Allow only one instance at a time
lockpath = "/var/lock/"+prog_name
try:
    lockf = open(lockpath, "w")
    fcntl.lockf(lockf, fcntl.LOCK_EX|fcntl.LOCK_NB)
except IOError:
    x_it(1, "ERROR: "+prog_name+" is already running.")

# Create our tmp dir
shutil.rmtree(tmpdir+"-old", ignore_errors=True)
if os.path.exists(tmpdir):
    os.rename(tmpdir, tmpdir+"-old")
os.makedirs(tmpdir+"/rpc")


# Parse Arguments:
parser = argparse.ArgumentParser(description="")
parser.add_argument("action", choices=["send","monitor","add","delete",
                    "prune","receive","verify","diff","list","version",
                    "arch-init","arch-delete",
                    "arch-deduplicate","index-test"],
                    default="monitor", help="Action to take")
parser.add_argument("-u", "--unattended", action="store_true", default=False,
                    help="Non-interactive, supress prompts")
parser.add_argument("-a", "--all", action="store_true", default=False,
                    help="Apply action to all volumes")
parser.add_argument("--all-before", dest="allbefore", action="store_true", default=False,
                    help="Select all sessions before --session date-time.")
parser.add_argument("--tarfile", action="store_true", dest="tarfile", default=False,
                    help="Store backup session as a tarfile")
parser.add_argument("--session", help="YYYYMMDD-HHMMSS[,YYYYMMDD-HHMMSS]"
                                 " select session date(s), singular or range.")
parser.add_argument("--save-to", dest="saveto", default="",
                    help="Path to store volume for receive")
parser.add_argument("--from", dest="from_arch", default="",
                    help="Address+Path of other non-configured archive (receive, verify)")
parser.add_argument("--remap", action="store_true", default=False,
                    help="Remap volume during diff")
parser.add_argument("--source", default="",
                    help="Init: LVM volgroup/pool containing source volumes")
parser.add_argument("--dest", default="",
                    help="Init: type:location of archive")
parser.add_argument("--subdir", default="",
                    help="Init: optional subdir for --dest")
parser.add_argument("--compression", default="",
                    help="Init: optional subdir for --dest")
parser.add_argument("--chunk-factor", dest="chfactor", type=int,
                    help="Init: set chunk size to N*64kB")
parser.add_argument("--testing-dedup", dest="dedup", type=int, default=0,
                    help="Test experimental deduplication (send)")
parser.add_argument("volumes", nargs="*")
options = parser.parse_args()
#subparser = parser.add_subparsers(help="sub-command help")
#prs_prune = subparser.add_parser("prune",help="prune help")


# General Configuration:

# Select dedup test algorithm.
init_dedup_index = [None, None, None, init_dedup_index3,
                    init_dedup_index4, init_dedup_index5][options.dedup]
monitor_only     = options.action == "monitor" # gather metadata without backing up
volgroups        = get_lvm_vgs()
aset             = None
destsys          = None
desttype         = None
aset, datavols   = get_configs()
if not aset.destsys and options.action in ("receive","verify") and options.from_dest:
    pass #### fill-this-in ####
elif not aset.destsys:
    x_it(1,"Local configuration not found.")

if aset.vgname in volgroups.keys():
    l_vols       = volgroups[aset.vgname].lvs
bkdir            = topdir+"/"+aset.name
if not os.path.exists(metadir+bkdir):
    os.makedirs(metadir+bkdir)
destpath         = os.path.normpath(pjoin(aset.destmountpoint,aset.destdir))
destcd           = " cd '"+destpath+"'"
destsys, desttype= detect_internal_state()
dest_run_map     = {"internal": ["sh"],
                    "ssh":      ["ssh",destsys],
                    "qubes":    ["qvm-run", "-p", destsys],
                    "qubes-ssh":["qvm-run", "-p", destsys.split("|")[0]]
                    }
detect_dest_state(destsys)

# Check volume args against config
selected_vols = options.volumes[:]
for vol in options.volumes:
    if vol not in datavols and options.action not in {"add","delete"}:
        print("Volume "+vol+" not configured; Skipping.")
        del(selected_vols[selected_vols.index(vol)])


# Process Commands:

if options.action   == "monitor":
    monitor_send(datavols, monitor_only=True)


elif options.action == "send":
    monitor_send(datavols, selected_vols, monitor_only=False)


elif options.action == "version":
    print(prog_name, "version", prog_version)


elif options.action == "prune":
    if not options.session:
        x_it(1, "Must specify --session for prune.")
    dvs = datavols if len(selected_vols) == 0 else selected_vols
    for dv in dvs:
        if dv in datavols:
            prune_sessions(dv, options.session.split(","))


elif options.action == "receive":
    if not options.saveto:
        x_it(1, "Must specify --save-to for receive.")
    if len(selected_vols) != 1:
        x_it(1, "Specify one volume for receive")
    if options.session and len(options.session.split(",")) > 1:
        x_it(1, "Specify one session for receive")
    receive_volume(selected_vols[0],
                   select_ses="" if not options.session \
                   else options.session.split(",")[0],
                   save_path=options.saveto)


elif options.action == "verify":
    if len(selected_vols) != 1:
        x_it(1, "Specify one volume for verify")
    if options.session and len(options.session.split(",")) > 1:
        x_it(1, "Specify one session for verify")
    receive_volume(selected_vols[0],
                   select_ses="" if not options.session \
                   else options.session.split(",")[0],
                   save_path="")


elif options.action == "diff":
    if selected_vols:
        receive_volume(selected_vols[0], save_path="", diff=True)


elif options.action == "list":
    if not selected_vols:
        print("Configured Volumes:\n")
        for vol in datavols:
            print(" ", vol)

    for dv in selected_vols:
        print("Sessions for volume",dv,":")
        vol = aset.vols[dv]
        lmonth = ""; count = 0; ending = "."
        for ses in vol.sesnames:
            if ses[:8] != lmonth:
                print("" if ending else "\n", flush=True)
                count = 0
            print(" ",ses[2:]+(" (tar)"
                        if vol.sessions[ses].format == "tar"
                        else ""), end="")
            ending = "\n" if count % 5 == 4 else ""
            print("", end=ending)
            lmonth = ses[:8]; count += 1

    print("" if selected_vols and ending else "\n", end="")


elif options.action == "add":
    if len(options.volumes) < 1:
        x_it(1, "A volume name is required for 'add' command.")

    aset.add_volume(options.volumes[0])
    print("Volume", options.volumes[0], "added to archive config.")


elif options.action == "delete":
    dv = selected_vols[0]
    if not options.unattended:
        print("Warning! Delete will remove ALL metadata AND archived data",
              "for volume", dv)

        ans = input("Are you sure? [y/N]: ")
        if ans.lower() not in {"y","yes"}:
            x_it(0,"")

    print("\nDeleting volume", dv, "from archive.")
    cmd = [destcd + bkdir
          +" && rm -rf " + dv
          +" && sync -f ."
          ]
    dest_run(cmd)

    if dv in aset.vols:
        aset.delete_volume(dv)


elif options.action == "untar":
    raise NotImplementedError()


elif options.action == "arch-delete":
    print("Warning! Wipe-all will remove ALL metadata AND archived data, "
          "leaving only the configuration!")

    ans = input("Are you sure? [y/N]: ")
    if ans.lower() not in {"y","yes"}:
        x_it(0,"")

    for dv in list(aset.vols):
        aset.delete_volume(dv)

    print("\nDeleting entire archive...")
    cmd = [destcd
          +" && rm -rf ."+bkdir
          +" && sync -f ."
          ]
    dest_run(cmd)


elif options.action == "arch-deduplicate":
    if options.dedup:
        dedup_existing()
    else:
        x_it(1,"Requires '--testing-dedup=N' option.")


if options.action   == "index-test":
    init_dedup_index()



print("\nDone.\n")\
