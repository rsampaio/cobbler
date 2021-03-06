"""
This is some of the code behind 'cobbler sync'.

Copyright 2006-2009, Red Hat, Inc
Michael DeHaan <mdehaan@redhat.com>
John Eckersberg <jeckersb@redhat.com>

This program is free software; you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation; either version 2 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program; if not, write to the Free Software
Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA
02110-1301  USA
"""

import os
import os.path
import shutil
import time
import sys
import glob
import traceback
import errno
import re
from utils import popen2
from shlex import shlex


import utils
from cexceptions import *
import templar

import item_distro
import item_profile
import item_repo
import item_system

from utils import _

# FIXME: add --quiet depending on if not --verbose?
RSYNC_CMD =  "rsync -a %s '%s' %s --exclude-from=/etc/cobbler/rsync.exclude --progress"

def register():
   """
   The mandatory cobbler module registration hook.
   """
   return "manage/import"


class ImportVMWareManager:

    def __init__(self,config,logger):
        """
        Constructor
        """
        self.logger        = logger
        self.config        = config
        self.api           = config.api
        self.distros       = config.distros()
        self.profiles      = config.profiles()
        self.systems       = config.systems()
        self.settings      = config.settings()
        self.repos         = config.repos()
        self.templar       = templar.Templar(config)

    # required function for import modules
    def what(self):
        return "import/vmware"

    # required function for import modules
    def check_for_signature(self,path,cli_breed):
       signatures = [
           'VMware/RPMS',
           'imagedd.bz2',
       ]

       #self.logger.info("scanning %s for a vmware-based signature" % path)
       for signature in signatures:
           d = os.path.join(path,signature)
           if os.path.exists(d):
               self.logger.info("Found a vmware compatible signature: %s" % signature)
               return (True,signature)

       if cli_breed and cli_breed in self.get_valid_breeds():
           self.logger.info("Warning: No distro signature for kernel at %s, using value from command line" % path)
           return (True,None)

       return (False,None)

    # required function for import modules
    def run(self,pkgdir,mirror,mirror_name,network_root=None,kickstart_file=None,rsync_flags=None,arch=None,breed=None,os_version=None):
        self.pkgdir = pkgdir
        self.mirror = mirror
        self.mirror_name = mirror_name
        self.network_root = network_root
        self.kickstart_file = kickstart_file
        self.rsync_flags = rsync_flags
        self.arch = arch
        self.breed = breed
        self.os_version = os_version

        # some fixups for the XMLRPC interface, which does not use "None"
        if self.arch == "":           self.arch           = None
        if self.mirror == "":         self.mirror         = None
        if self.mirror_name == "":    self.mirror_name    = None
        if self.kickstart_file == "": self.kickstart_file = None
        if self.os_version == "":     self.os_version     = None
        if self.rsync_flags == "":    self.rsync_flags    = None
        if self.network_root == "":   self.network_root   = None

        # If no breed was specified on the command line, set it to "redhat" for this module
        if self.breed == None:
            self.breed = "vmware"

        # debug log stuff for testing
        #self.logger.info("self.pkgdir = %s" % str(self.pkgdir))
        #self.logger.info("self.mirror = %s" % str(self.mirror))
        #self.logger.info("self.mirror_name = %s" % str(self.mirror_name))
        #self.logger.info("self.network_root = %s" % str(self.network_root))
        #self.logger.info("self.kickstart_file = %s" % str(self.kickstart_file))
        #self.logger.info("self.rsync_flags = %s" % str(self.rsync_flags))
        #self.logger.info("self.arch = %s" % str(self.arch))
        #self.logger.info("self.breed = %s" % str(self.breed))
        #self.logger.info("self.os_version = %s" % str(self.os_version))

        # both --import and --name are required arguments

        if self.mirror is None:
            utils.die(self.logger,"import failed.  no --path specified")
        if self.mirror_name is None:
            utils.die(self.logger,"import failed.  no --name specified")

        # if --arch is supplied, validate it to ensure it's valid

        if self.arch is not None and self.arch != "":
            self.arch = self.arch.lower()
            if self.arch == "x86":
                # be consistent
                self.arch = "i386"
            if self.arch not in self.get_valid_arches():
                utils.die(self.logger,"arch must be one of: %s" % string.join(self.get_valid_arches(),", "))

        # if we're going to do any copying, set where to put things
        # and then make sure nothing is already there.

        self.path = os.path.normpath( "%s/ks_mirror/%s" % (self.settings.webdir, self.mirror_name) )
        if os.path.exists(self.path) and self.arch is None:
            # FIXME : Raise exception even when network_root is given ?
            utils.die(self.logger,"Something already exists at this import location (%s).  You must specify --arch to avoid potentially overwriting existing files." % self.path)

        # import takes a --kickstart for forcing selection that can't be used in all circumstances

        if self.kickstart_file and not self.breed:
            utils.die(self.logger,"Kickstart file can only be specified when a specific breed is selected")

        if self.os_version and not self.breed:
            utils.die(self.logger,"OS version can only be specified when a specific breed is selected")

        if self.breed and self.breed.lower() not in self.get_valid_breeds():
            utils.die(self.logger,"Supplied import breed is not supported by this module")

        # if --arch is supplied, make sure the user is not importing a path with a different
        # arch, which would just be silly.

        if self.arch:
            # append the arch path to the name if the arch is not already
            # found in the name.
            for x in self.get_valid_arches():
                if self.path.lower().find(x) != -1:
                    if self.arch != x :
                        utils.die(self.logger,"Architecture found on pathname (%s) does not fit the one given in command line (%s)"%(x,self.arch))
                    break
            else:
                # FIXME : This is very likely removed later at get_proposed_name, and the guessed arch appended again
                self.path += ("-%s" % self.arch)

        # make the output path and mirror content but only if not specifying that a network
        # accessible support location already exists (this is --available-as on the command line)

        if self.network_root is None:
            # we need to mirror (copy) the files

            utils.mkdir(self.path)

            # prevent rsync from creating the directory name twice
            # if we are copying via rsync

            if not self.mirror.endswith("/"):
                self.mirror = "%s/" % self.mirror

            if self.mirror.startswith("http://") or self.mirror.startswith("ftp://") or self.mirror.startswith("nfs://"):

                # http mirrors are kind of primative.  rsync is better.
                # that's why this isn't documented in the manpage and we don't support them.
                # TODO: how about adding recursive FTP as an option?

                utils.die(self.logger,"unsupported protocol")

            else:

                # good, we're going to use rsync..
                # we don't use SSH for public mirrors and local files.
                # presence of user@host syntax means use SSH

                spacer = ""
                if not self.mirror.startswith("rsync://") and not self.mirror.startswith("/"):
                    spacer = ' -e "ssh" '
                rsync_cmd = RSYNC_CMD
                if self.rsync_flags:
                    rsync_cmd = rsync_cmd + " " + self.rsync_flags

                # kick off the rsync now

                utils.run_this(rsync_cmd, (spacer, self.mirror, self.path), self.logger)

        else:

            # rather than mirroring, we're going to assume the path is available
            # over http, ftp, and nfs, perhaps on an external filer.  scanning still requires
            # --mirror is a filesystem path, but --available-as marks the network path

            if not os.path.exists(self.mirror):
                utils.die(self.logger, "path does not exist: %s" % self.mirror)

            # find the filesystem part of the path, after the server bits, as each distro
            # URL needs to be calculated relative to this.

            if not self.network_root.endswith("/"):
                self.network_root = self.network_root + "/"
            self.path = os.path.normpath( self.mirror )
            valid_roots = [ "nfs://", "ftp://", "http://" ]
            for valid_root in valid_roots:
                if self.network_root.startswith(valid_root):
                    break
            else:
                utils.die(self.logger, "Network root given to --available-as must be nfs://, ftp://, or http://")
            if self.network_root.startswith("nfs://"):
                try:
                    (a,b,rest) = self.network_root.split(":",3)
                except:
                    utils.die(self.logger, "Network root given to --available-as is missing a colon, please see the manpage example.")

        # now walk the filesystem looking for distributions that match certain patterns

        self.logger.info("adding distros")
        distros_added = []
        # FIXME : search below self.path for isolinux configurations or known directories from TRY_LIST
        os.path.walk(self.path, self.distro_adder, distros_added)

        # find out if we can auto-create any repository records from the install tree

        #if self.network_root is None:
        #    self.logger.info("associating repos")
        #    # FIXME: this automagic is not possible (yet) without mirroring
        #    self.repo_finder(distros_added)

        # find the most appropriate answer files for each profile object

        self.logger.info("associating kickstarts")
        self.kickstart_finder(distros_added)

        # ensure bootloaders are present
        self.api.pxegen.copy_bootloaders()

        return True

    # required function for import modules
    def get_valid_arches(self):
        return ["i386", "x86_64", "x86",]

    # required function for import modules
    def get_valid_breeds(self):
        return ["vmware",]

    # required function for import modules
    def get_valid_os_versions(self):
        return ["esx4","esxi"]

    def get_valid_repo_breeds(self):
        return ["rsync", "rhn", "yum",]

    def get_release_files(self):
        """
        Find distro release packages.
        """
        data = glob.glob(os.path.join(self.get_pkgdir(), "vmware-esx-vmware-release-*"))
        data2 = []
        for x in data:
            b = os.path.basename(x)
            if b.find("vmware") != -1:
                data2.append(x)
        if len(data2) == 0:
            # ESXi maybe?
            return glob.glob(os.path.join(self.get_rootdir(), "vmkernel.gz"))
        return data2

    def get_tree_location(self, distro):
        """
        Once a distribution is identified, find the part of the distribution
        that has the URL in it that we want to use for kickstarting the
        distribution, and create a ksmeta variable $tree that contains this.
        """

        base = self.get_rootdir()

        if self.network_root is None:
            dest_link = os.path.join(self.settings.webdir, "links", distro.name)
            # create the links directory only if we are mirroring because with
            # SELinux Apache can't symlink to NFS (without some doing)
            if not os.path.exists(dest_link):
                try:
                    os.symlink(base, dest_link)
                except:
                    # this shouldn't happen but I've seen it ... debug ...
                    self.logger.warning("symlink creation failed: %(base)s, %(dest)s") % { "base" : base, "dest" : dest_link }
            # how we set the tree depends on whether an explicit network_root was specified
            tree = "http://@@http_server@@/cblr/links/%s" % (distro.name)
            self.set_install_tree(distro, tree)
        else:
            # where we assign the kickstart source is relative to our current directory
            # and the input start directory in the crawl.  We find the path segments
            # between and tack them on the network source path to find the explicit
            # network path to the distro that Anaconda can digest.
            tail = self.path_tail(self.path, base)
            tree = self.network_root[:-1] + tail
            self.set_install_tree(distro, tree)

        return

    def repo_finder(self, distros_added):
        """
        This routine looks through all distributions and tries to find
        any applicable repositories in those distributions for post-install
        usage.
        """

        for distro in distros_added:
            self.logger.info("traversing distro %s" % distro.name)
            # FIXME : Shouldn't decide this the value of self.network_root ?
            if distro.kernel.find("ks_mirror") != -1:
                basepath = os.path.dirname(distro.kernel)
                top = self.get_rootdir()
                self.logger.info("descent into %s" % top)
                # FIXME : The location of repo definition is known from breed
                os.path.walk(top, self.repo_scanner, distro)
            else:
                self.logger.info("this distro isn't mirrored")

    def repo_scanner(self,distro,dirname,fnames):
        """
        This is an os.path.walk routine that looks for potential yum repositories
        to be added to the configuration for post-install usage.
        """

        matches = {}
        for x in fnames:
            if x == "base" or x == "repodata":
                self.logger.info("processing repo at : %s" % dirname)
                # only run the repo scanner on directories that contain a comps.xml
                gloob1 = glob.glob("%s/%s/*comps*.xml" % (dirname,x))
                if len(gloob1) >= 1:
                    if matches.has_key(dirname):
                        self.logger.info("looks like we've already scanned here: %s" % dirname)
                        continue
                    self.logger.info("need to process repo/comps: %s" % dirname)
                    self.process_comps_file(dirname, distro)
                    matches[dirname] = 1
                else:
                    self.logger.info("directory %s is missing xml comps file, skipping" % dirname)
                    continue

    def process_comps_file(self, comps_path, distro):
        """
        When importing Fedora/EL certain parts of the install tree can also be used
        as yum repos containing packages that might not yet be available via updates
        in yum.  This code identifies those areas.
        """

        processed_repos = {}

        masterdir = "repodata"
        if not os.path.exists(os.path.join(comps_path, "repodata")):
            # older distros...
            masterdir = "base"

        # figure out what our comps file is ...
        self.logger.info("looking for %(p1)s/%(p2)s/*comps*.xml" % { "p1" : comps_path, "p2" : masterdir })
        files = glob.glob("%s/%s/*comps*.xml" % (comps_path, masterdir))
        if len(files) == 0:
            self.logger.info("no comps found here: %s" % os.path.join(comps_path, masterdir))
            return # no comps xml file found

        # pull the filename from the longer part
        comps_file = files[0].split("/")[-1]

        try:
            # store the yum configs on the filesystem so we can use them later.
            # and configure them in the kickstart post, etc

            counter = len(distro.source_repos)

            # find path segment for yum_url (changing filesystem path to http:// trailing fragment)
            seg = comps_path.rfind("ks_mirror")
            urlseg = comps_path[seg+10:]

            # write a yum config file that shows how to use the repo.
            if counter == 0:
                dotrepo = "%s.repo" % distro.name
            else:
                dotrepo = "%s-%s.repo" % (distro.name, counter)

            fname = os.path.join(self.settings.webdir, "ks_mirror", "config", "%s-%s.repo" % (distro.name, counter))

            repo_url = "http://@@http_server@@/cobbler/ks_mirror/config/%s-%s.repo" % (distro.name, counter)
            repo_url2 = "http://@@http_server@@/cobbler/ks_mirror/%s" % (urlseg)

            distro.source_repos.append([repo_url,repo_url2])

            # NOTE: the following file is now a Cheetah template, so it can be remapped
            # during sync, that's why we have the @@http_server@@ left as templating magic.
            # repo_url2 is actually no longer used. (?)

            config_file = open(fname, "w+")
            config_file.write("[core-%s]\n" % counter)
            config_file.write("name=core-%s\n" % counter)
            config_file.write("baseurl=http://@@http_server@@/cobbler/ks_mirror/%s\n" % (urlseg))
            config_file.write("enabled=1\n")
            config_file.write("gpgcheck=0\n")
            config_file.write("priority=$yum_distro_priority\n")
            config_file.close()

            # don't run creatrepo twice -- this can happen easily for Xen and PXE, when
            # they'll share same repo files.

            if not processed_repos.has_key(comps_path):
                utils.remove_yum_olddata(comps_path)
                #cmd = "createrepo --basedir / --groupfile %s %s" % (os.path.join(comps_path, masterdir, comps_file), comps_path)
                cmd = "createrepo %s --groupfile %s %s" % (self.settings.createrepo_flags,os.path.join(comps_path, masterdir, comps_file), comps_path)
                utils.subprocess_call(self.logger, cmd, shell=True)
                processed_repos[comps_path] = 1
                # for older distros, if we have a "base" dir parallel with "repodata", we need to copy comps.xml up one...
                p1 = os.path.join(comps_path, "repodata", "comps.xml")
                p2 = os.path.join(comps_path, "base", "comps.xml")
                if os.path.exists(p1) and os.path.exists(p2):
                    shutil.copyfile(p1,p2)

        except:
            self.logger.error("error launching createrepo (not installed?), ignoring")
            utils.log_exc(self.logger)

    def distro_adder(self,distros_added,dirname,fnames):
        """
        This is an os.path.walk routine that finds distributions in the directory
        to be scanned and then creates them.
        """

        # FIXME: If there are more than one kernel or initrd image on the same directory,
        # results are unpredictable

        initrd = None
        kernel = None

        for x in fnames:
            adtls = []

            fullname = os.path.join(dirname,x)
            if os.path.islink(fullname) and os.path.isdir(fullname):
                if fullname.startswith(self.path):
                    self.logger.warning("avoiding symlink loop")
                    continue
                self.logger.info("following symlink: %s" % fullname)
                os.path.walk(fullname, self.distro_adder, distros_added)

            if ( x.startswith("initrd") or x.startswith("ramdisk.image.gz") or x.startswith("vmkboot.gz") ) and x != "initrd.size":
                initrd = os.path.join(dirname,x)
            if ( x.startswith("vmlinu") or x.startswith("kernel.img") or x.startswith("linux") or x.startswith("mboot.c32") ) and x.find("initrd") == -1:
                kernel = os.path.join(dirname,x)

            # if we've collected a matching kernel and initrd pair, turn the in and add them to the list
            if initrd is not None and kernel is not None:
                adtls.append(self.add_entry(dirname,kernel,initrd))
                kernel = None
                initrd = None

            for adtl in adtls:
                distros_added.extend(adtl)

    def add_entry(self,dirname,kernel,initrd):
        """
        When we find a directory with a valid kernel/initrd in it, create the distribution objects
        as appropriate and save them.  This includes creating xen and rescue distros/profiles
        if possible.
        """

        proposed_name = self.get_proposed_name(dirname,kernel)
        proposed_arch = self.get_proposed_arch(dirname)

        if self.arch and proposed_arch and self.arch != proposed_arch:
            utils.die(self.logger,"Arch from pathname (%s) does not match with supplied one %s"%(proposed_arch,self.arch))

        archs = self.learn_arch_from_tree()
        if not archs:
            if self.arch:
                archs.append( self.arch )
        else:
            if self.arch and self.arch not in archs:
                utils.die(self.logger, "Given arch (%s) not found on imported tree %s"%(self.arch,self.get_pkgdir()))
        if proposed_arch:
            if archs and proposed_arch not in archs:
                self.logger.warning("arch from pathname (%s) not found on imported tree %s" % (proposed_arch,self.get_pkgdir()))
                return

            archs = [ proposed_arch ]

        if len(archs)>1:
            self.logger.warning("- Warning : Multiple archs found : %s" % (archs))

        distros_added = []

        for pxe_arch in archs:
            name = proposed_name + "-" + pxe_arch
            existing_distro = self.distros.find(name=name)

            if existing_distro is not None:
                self.logger.warning("skipping import, as distro name already exists: %s" % name)
                continue

            else:
                self.logger.info("creating new distro: %s" % name)
                distro = self.config.new_distro()

            if name.find("-autoboot") != -1:
                # this is an artifact of some EL-3 imports
                continue

            distro.set_name(name)
            distro.set_kernel(kernel)
            distro.set_initrd(initrd)
            distro.set_arch(pxe_arch)
            distro.set_breed(self.breed)
            # If a version was supplied on command line, we set it now
            if self.os_version:
                distro.set_os_version(self.os_version)

            self.distros.add(distro,save=True)
            distros_added.append(distro)

            existing_profile = self.profiles.find(name=name)

            # see if the profile name is already used, if so, skip it and
            # do not modify the existing profile

            if existing_profile is None:
                self.logger.info("creating new profile: %s" % name)
                #FIXME: The created profile holds a default kickstart, and should be breed specific
                profile = self.config.new_profile()
            else:
                self.logger.info("skipping existing profile, name already exists: %s" % name)
                continue

            # save our minimal profile which just points to the distribution and a good
            # default answer file

            profile.set_name(name)
            profile.set_distro(name)
            profile.set_kickstart(self.kickstart_file)

            # We just set the virt type to vmware for these
            # since newer VMwares support running ESX as a guest for testing

            profile.set_virt_type("vmware")

            # save our new profile to the collection

            self.profiles.add(profile,save=True)

        return distros_added

    def get_proposed_name(self,dirname,kernel=None):
        """
        Given a directory name where we have a kernel/initrd pair, try to autoname
        the distribution (and profile) object based on the contents of that path
        """

        if self.network_root is not None:
            name = self.mirror_name + "-".join(self.path_tail(os.path.dirname(self.path),dirname).split("/"))
        else:
            # remove the part that says /var/www/cobbler/ks_mirror/name
            name = "-".join(dirname.split("/")[5:])

        if kernel is not None and kernel.find("PAE") != -1:
            name = name + "-PAE"

        # These are all Ubuntu's doing, the netboot images are buried pretty
        # deep. ;-) -JC
        name = name.replace("-netboot","")
        name = name.replace("-ubuntu-installer","")
        name = name.replace("-amd64","")
        name = name.replace("-i386","")

        # we know that some kernel paths should not be in the name

        name = name.replace("-images","")
        name = name.replace("-pxeboot","")
        name = name.replace("-install","")
        name = name.replace("-isolinux","")

        # some paths above the media root may have extra path segments we want
        # to clean up

        name = name.replace("-os","")
        name = name.replace("-tree","")
        name = name.replace("var-www-cobbler-", "")
        name = name.replace("ks_mirror-","")
        name = name.replace("--","-")

        # remove any architecture name related string, as real arch will be appended later

        name = name.replace("chrp","ppc64")

        for separator in [ '-' , '_'  , '.' ] :
            for arch in [ "i386" , "x86_64" , "ia64" , "ppc64", "ppc32", "ppc", "x86" , "s390x", "s390" , "386" , "amd" ]:
                name = name.replace("%s%s" % ( separator , arch ),"")

        return name

    def get_proposed_arch(self,dirname):
        """
        Given an directory name, can we infer an architecture from a path segment?
        """
        if dirname.find("x86_64") != -1 or dirname.find("amd") != -1:
            return "x86_64"
        if dirname.find("ia64") != -1:
            return "ia64"
        if dirname.find("i386") != -1 or dirname.find("386") != -1 or dirname.find("x86") != -1:
            return "i386"
        if dirname.find("s390x") != -1:
            return "s390x"
        if dirname.find("s390") != -1:
            return "s390"
        if dirname.find("ppc64") != -1 or dirname.find("chrp") != -1:
            return "ppc64"
        if dirname.find("ppc32") != -1:
            return "ppc"
        if dirname.find("ppc") != -1:
            return "ppc"
        return None

    def arch_walker(self,foo,dirname,fnames):
        """
        See docs on learn_arch_from_tree.

        The TRY_LIST is used to speed up search, and should be dropped for default importer
        Searched kernel names are kernel-header, linux-headers-, kernel-largesmp, kernel-hugemem

        This method is useful to get the archs, but also to package type and a raw guess of the breed
        """

        # try to find a kernel header RPM and then look at it's arch.
        for x in fnames:
            if self.match_kernelarch_file(x):
                for arch in self.get_valid_arches():
                    if x.find(arch) != -1:
                        foo[arch] = 1
                for arch in [ "i686" , "amd64" ]:
                    if x.find(arch) != -1:
                        foo[arch] = 1

    def kickstart_finder(self,distros_added):
        """
        For all of the profiles in the config w/o a kickstart, use the
        given kickstart file, or look at the kernel path, from that,
        see if we can guess the distro, and if we can, assign a kickstart
        if one is available for it.
        """
        for profile in self.profiles:
            distro = self.distros.find(name=profile.get_conceptual_parent().name)
            if distro is None or not (distro in distros_added):
                continue

            kdir = os.path.dirname(distro.kernel)
            if self.kickstart_file == None:
                for rpm in self.get_release_files():
                    # FIXME : This redhat specific check should go into the importer.find_release_files method
                    if rpm.find("notes") != -1:
                        continue
                    results = self.scan_pkg_filename(rpm)
                    # FIXME : If os is not found on tree but set with CLI, no kickstart is searched
                    if results is None:
                        self.logger.warning("No version found on imported tree")
                        continue
                    (flavor, major, minor, release, update) = results
                    version , ks = self.set_variance(flavor, major, minor, release, update, distro.arch)
                    if self.os_version:
                        if self.os_version != version:
                            utils.die(self.logger,"CLI version differs from tree : %s vs. %s" % (self.os_version,version))
                    ds = self.get_datestamp()
                    distro.set_comment("%s.%s.%s update %s" % (version,minor,release,update))
                    distro.set_os_version(version)
                    if ds is not None:
                        distro.set_tree_build_time(ds)
                    profile.set_kickstart(ks)
                    if flavor == "esxi":
                        self.logger.info("This is an ESXi distro - adding extra PXE files to fetchable-files list")
                        # add extra files to fetchable_files in the distro
                        fetchable_files = ''
                        for file in ('vmkernel.gz','sys.vgz','cim.vgz','ienviron.vgz','install.vgz'):
                           fetchable_files += '$img_path/%s=%s/%s ' % (file,self.path,file)
                        distro.set_fetchable_files(fetchable_files.strip())
                    self.profiles.add(profile,save=True)

            self.configure_tree_location(distro)
            self.distros.add(distro,save=True) # re-save
            self.api.serialize()

    def configure_tree_location(self, distro):
        """
        Once a distribution is identified, find the part of the distribution
        that has the URL in it that we want to use for kickstarting the
        distribution, and create a ksmeta variable $tree that contains this.
        """

        base = self.get_rootdir()

        if self.network_root is None:
            dest_link = os.path.join(self.settings.webdir, "links", distro.name)
            # create the links directory only if we are mirroring because with
            # SELinux Apache can't symlink to NFS (without some doing)
            if not os.path.exists(dest_link):
                try:
                    os.symlink(base, dest_link)
                except:
                    # this shouldn't happen but I've seen it ... debug ...
                    self.logger.warning("symlink creation failed: %(base)s, %(dest)s") % { "base" : base, "dest" : dest_link }
            # how we set the tree depends on whether an explicit network_root was specified
            tree = "http://@@http_server@@/cblr/links/%s" % (distro.name)
            self.set_install_tree( distro, tree)
        else:
            # where we assign the kickstart source is relative to our current directory
            # and the input start directory in the crawl.  We find the path segments
            # between and tack them on the network source path to find the explicit
            # network path to the distro that Anaconda can digest.
            tail = utils.path_tail(self.path, base)
            tree = self.network_root[:-1] + tail
            self.set_install_tree( distro, tree)

    def get_rootdir(self):
        return self.mirror

    def get_pkgdir(self):
        if not self.pkgdir:
            return None
        return os.path.join(self.get_rootdir(),self.pkgdir)

    def set_install_tree(self, distro, url):
        distro.ks_meta["tree"] = url

    def learn_arch_from_tree(self):
        """
        If a distribution is imported from DVD, there is a good chance the path doesn't
        contain the arch and we should add it back in so that it's part of the
        meaningful name ... so this code helps figure out the arch name.  This is important
        for producing predictable distro names (and profile names) from differing import sources
        """
        result = {}
        # FIXME : this is called only once, should not be a walk
        if self.get_pkgdir():
            os.path.walk(self.get_pkgdir(), self.arch_walker, result)
        if result.pop("amd64",False):
            result["x86_64"] = 1
        if result.pop("i686",False):
            result["i386"] = 1
        return result.keys()

    def match_kernelarch_file(self, filename):
        """
        Is the given filename a kernel filename?
        """

        if not filename.endswith("rpm") and not filename.endswith("deb"):
            return False
        for match in ["kernel-header", "kernel-source", "kernel-smp", "kernel-largesmp", "kernel-hugemem", "linux-headers-", "kernel-devel", "kernel-"]:
            if filename.find(match) != -1:
                return True
        return False

    def scan_pkg_filename(self, rpm):
        """
        Determine what the distro is based on the release package filename.
        """
        rpm_file = os.path.basename(rpm)

        if rpm_file.lower().find("-esx-") != -1:
            flavor = "esx"
            match = re.search(r'release-(\d)+-(\d)+\.(\d)+\.(\d)+-(\d)\.', rpm_file)
            if match:
                major   = match.group(2)
                minor   = match.group(3)
                release = match.group(4)
                update  = match.group(5)
            else:
                # FIXME: what should we do if the re fails above?
                return None
        elif rpm_file.lower() == "vmkernel.gz":
            flavor  = "esxi"
            major   = 0
            minor   = 0
            release = 0
            update  = 0

            # this should return something like:
            # VMware ESXi 4.1.0 [Releasebuild-260247], built on May 18 2010
            # though there will most likely be multiple results
            scan_cmd = 'gunzip -c %s | strings | grep -i "^vmware esxi"' % rpm
            (data,rc) = utils.subprocess_sp(self.logger, scan_cmd)
            lines = data.split('\n')
            m = re.compile(r'ESXi (\d)+\.(\d)+\.(\d)+ \[Releasebuild-([\d]+)\]')
            for line in lines:
                match = m.search(line)
                if match:
                    major   = match.group(1)
                    minor   = match.group(2)
                    release = match.group(3)
                    update  = match.group(4)
                    break
            else:
                return None

        #self.logger.info("DEBUG: in scan_pkg_filename() - major=%s, minor=%s, release=%s, update=%s" % (major,minor,release,update))
        return (flavor, major, minor, release, update)

    def get_datestamp(self):
        """
        Based on a VMWare tree find the creation timestamp
        """
        pass

    def set_variance(self, flavor, major, minor, release, update, arch):
        """
        Set distro specific versioning.
        """
        os_version = "%s%s" % (flavor, major)
        if flavor == "esx4":
            ks = "/var/lib/cobbler/kickstarts/esx.ks"
        elif flavor == "esxi4":
            ks = "/var/lib/cobbler/kickstarts/esxi.ks"
        else:
            ks = "/var/lib/cobbler/kickstarts/default.ks"
        return os_version , ks

# ==========================================================================

def get_import_manager(config,logger):
    return ImportVMWareManager(config,logger)
