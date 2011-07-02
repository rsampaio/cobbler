"""
Repositories in cobbler are way to create a local mirror of a yum repository.
When used in conjunction with a mirrored kickstart tree (see "cobbler import")
outside bandwidth needs can be reduced and/or eliminated.

Copyright 2006-2009, Red Hat, Inc
Michael DeHaan <mdehaan@redhat.com>

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

import item_repo as repo
import utils
import collection
from cexceptions import *
from utils import _
import os.path

TESTMODE = False

#--------------------------------------------

class Repos(collection.Collection):

    def collection_type(self):
        return "repo"

    def factory_produce(self,config,seed_data):
        """
        Return a Distro forged from seed_data
        """
        return repo.Repo(config).from_datastruct(seed_data)

    def remove(self,name,with_delete=True,with_sync=True,with_triggers=True,recursive=False,logger=None):
        """
        Remove element named 'name' from the collection
        """

        # NOTE: with_delete isn't currently meaningful for repos
        # but is left in for consistancy in the API.  Unused.
        name = name.lower()
        obj = self.find(name=name)
        if obj is not None:
            if with_delete:
                if with_triggers: 
                    utils.run_triggers(self.config.api, obj, "/var/lib/cobbler/triggers/delete/repo/pre/*", [], logger)

            del self.listing[name]
            self.config.serialize_delete(self, obj)

            if with_delete:
                if with_triggers: 
                    utils.run_triggers(self.config.api, obj, "/var/lib/cobbler/triggers/delete/repo/post/*", [], logger)
                    utils.run_triggers(self.config.api, obj, "/var/lib/cobbler/triggers/change/*", [], logger)
           
                #FIXME: better use config.settings() webdir?
                path = "/var/www/cobbler/repo_mirror/%s" % obj.name
                if os.path.exists("/srv/www/"):
                    path = "/srv/www/cobbler/repo_mirror/%s" % obj.name
                if os.path.exists(path):
                    utils.rmtree(path)

            return True

        raise CX(_("cannot delete an object that does not exist: %s") % name)

