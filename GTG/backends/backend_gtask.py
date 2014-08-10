# -*- coding: utf-8 -*-
# -----------------------------------------------------------------------------
# Getting Things Gnome! - a personal organizer for the GNOME desktop
# Copyright (c) 2008-2009 - Lionel Dricot & Bertrand Rousseau
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. See the GNU General Public License for more
# details.
#
# You should have received a copy of the GNU General Public License along with
# this program.  If not, see <http://www.gnu.org/licenses/>.
# -----------------------------------------------------------------------------

"""Google Tasks backend"""

import os
import httplib2
import uuid
import datetime
from subprocess import Popen

from GTG import _
from GTG.backends.backendsignals import BackendSignals
from GTG.backends.genericbackend import GenericBackend
from GTG.backends.periodicimportbackend import PeriodicImportBackend
from GTG.backends.syncengine import SyncEngine, SyncMeme
from GTG.core import CoreConfig
from GTG.tools.interruptible import interruptible
from GTG.tools.logger import Log
from GTG.tools.dates import Date

# External libraries
from apiclient.discovery import build as build_service
from oauth2client.client import FlowExchangeError
from oauth2client.client import OAuth2WebServerFlow
from oauth2client.file import Storage

class Backend(PeriodicImportBackend):
    # Credence for authorizing GTG as an app
    CLIENT_ID = "362778520156-v39jobt9ltb69hbrine4k49tpdplc05t.apps.googleusercontent.com"
    CLIENT_SECRET = "VOESkYrhqhDpfn1X1pFsTotY"

    _general_description = {
        GenericBackend.BACKEND_NAME: "backend_gtask",
        GenericBackend.BACKEND_HUMAN_NAME: _("Google Task"),
        GenericBackend.BACKEND_AUTHORS: ["Madhumitha Viswanathan", "Izidor Matušov", "Parth Panchal"],
        GenericBackend.BACKEND_TYPE: GenericBackend.TYPE_READWRITE,
        GenericBackend.BACKEND_DESCRIPTION:
        _("This service synchronizes your tasks with the web service"
          " Google Tasks:\n\thttps://www.gmail.com/mail/help/tasks/\n\n"
          "Note: This product uses the Google Tasks API but is not"
          " endorsed or certified by Google"),
    }

    _static_parameters = { \
        "period": { \
            GenericBackend.PARAM_TYPE: GenericBackend.TYPE_INT, \
            GenericBackend.PARAM_DEFAULT_VALUE: 10, },
        "is-first-run": { \
            GenericBackend.PARAM_TYPE: GenericBackend.TYPE_BOOL, \
            GenericBackend.PARAM_DEFAULT_VALUE: True, },
        }

    def __init__(self, parameters):
        '''
        See GenericBackend for an explanation of this function.
        Re-loads the saved state of the synchronization
        '''
        super(Backend, self).__init__(parameters)
        self.storage = None
        self.service = None
        self.authenticated = False
        #loading the list of already imported tasks
        self.data_path = os.path.join('backends/gtask/', "tasks_dict-%s" %\
                                     self.get_id())
        self.sync_engine = self._load_pickled_file(self.data_path, \
                                                   SyncEngine())

    def save_state(self):
        '''
        See GenericBackend for an explanation of this function.
        Saves the state of the synchronization.
        '''
        self._store_pickled_file(self.data_path, self.sync_engine)

    def initialize(self):
        """
        Intialize backend: try to authenticate. If it fails, request an authorization.
        """
        super(Backend, self).initialize()
        path = os.path.join(CoreConfig().get_data_dir(), 'backends/gtask', 'storage_file-%s' % self.get_id())
        # Try to create leading directories that path
        path_dir = os.path.dirname(path)
        if not os.path.isdir(path_dir):
            os.makedirs(path_dir)

        self.storage = Storage(path)
        self.authenticate()

    def authenticate(self):
        """ Try to authenticate by already existing credences or request an authorization """
        self.authenticated = False

        credentials = self.storage.get()
        if credentials is None or credentials.invalid == True:
            self.request_authorization()
        else:
            self.apply_credentials(credentials)

    def apply_credentials(self, credentials):
        """ Finish authentication or request for an authorization by applying the credentials """
        http = httplib2.Http()
        http = credentials.authorize(http)

        # Build a service object for interacting with the API.
        self.service = build_service(serviceName='tasks', version='v1', http=http,
                    developerKey='AIzaSyD-z8QJ6RjyknF5mfGlFE8-W70MBv7Mrtw')

        self.authenticated = True

    def _authorization_step2(self, code):
        credential = self.flow.step2_exchange(code)

        self.storage.put(credential)
        credential.set_store(self.storage)

        return credential

    def request_authorization(self):
        """ Make the first step of authorization and open URL for allowing the access """
        self.flow = OAuth2WebServerFlow(client_id=self.CLIENT_ID,
            client_secret=self.CLIENT_SECRET,
            scope='https://www.googleapis.com/auth/tasks',
            user_agent='GTG')

        oauth_callback = 'oob'
        Popen(["xdg-open", self.flow.step1_get_authorize_url(oauth_callback)])

        # Request the code from user
        BackendSignals().interaction_requested(self.get_id(),
            "You need to authenticate to <b>Google</b>. A browser "
            "is opening a page where you allow GTG to access your tasks. "
            "When you have received a code, press 'Continue'.",
            BackendSignals().INTERACTION_TEXT,
            "on_authentication_step")

    def on_authentication_step(self, step_type="", code=""):
        """ First time return specification of dialog.
            The second time grab the code and make the second, last
            step of authorization, afterwards apply the new credentials """

        if step_type == "get_ui_dialog_text":
            return "Code request", "Insert the code you should have received "\
                                  "through your web browser here:"
        elif step_type == "set_text":
            try:
                credentials = self._authorization_step2(code)
            except FlowExchangeError as e:
                # Show an error to user and end
                self.quit(disable = True)
                BackendSignals().backend_failed(self.get_id(), 
                            BackendSignals.ERRNO_AUTHENTICATION)
                return

            self.apply_credentials(credentials)
            # Request periodic import, avoid waiting a long time
            self.start_get_tasks()

    def do_periodic_import(self):
        # Wait until authentication
        if not self.authenticated:
            return

        gtasklist = self.service.tasks().list(tasklist='@default').execute()
        for gtask in gtasklist['items']:
            self._process_gtask(gtask['id'])
        
        gtask_ids = [gtask['id'] for gtask in gtasklist['items']]
         
        stored_task_ids = self.sync_engine.get_all_remote()
        for gtask in set(stored_task_ids).difference(set(gtask_ids)):
            self.on_gtask_deleted(gtask, None)

    @interruptible
    def on_gtask_deleted(self, gtask, something):
        '''
        Callback, executed when a Google Task is deleted.
        Deletes the related GTG task.

        @param gtask: the id of the Google Task 
        @param something: not used, here for signal callback compatibility
        '''
        with self.datastore.get_backend_mutex():
            self.cancellation_point()
            try:
                tid = self.sync_engine.get_local_id(gtask)
            except KeyError:
                return
            if self.datastore.has_task(tid):
                self.datastore.request_task_deletion(tid)
                self.break_relationship(remote_id = gtask)

    @interruptible
    def remove_task(self, tid):
        '''
        See GenericBackend for an explanation of this function.
        '''
        with self.datastore.get_backend_mutex():
            self.cancellation_point()
            try:
                gtask = self.sync_engine.get_remote_id(tid)
            except KeyError:
                return
            #the remote task might have been already deleted manually (or by
            #another app)
            try:
                self.service.tasks().delete(tasklist='@default', task=gtask).execute()
            except Exception as e:
                #FIXME:need to see if disconnected...
                pass
            self.break_relationship(local_id = tid)

    def _process_gtask(self, gtask):
        '''
        Given a Google Task id, finds out if it must be synced to a GTG note and, 
        if so, it carries out the synchronization (by creating or updating a GTG
        task, or deleting itself if the related task has been deleted)

        @param gtask: a Google Task id
        '''
        with self.datastore.get_backend_mutex():
            self.cancellation_point()
            is_syncable = self._google_task_is_syncable(gtask)
            action, tid = self.sync_engine.analyze_remote_id(gtask, \
                         self.datastore.has_task, \
                         self._google_task_exists(gtask), is_syncable)
            Log.debug("processing google tasks (%s, %s)" % (action, is_syncable))
            if action == SyncEngine.ADD:
                tid = str(uuid.uuid4())
                task = self.datastore.task_factory(tid)
                self._populate_task(task, gtask)
                self.record_relationship(local_id = tid,\
                            remote_id = gtask, \
                            meme = SyncMeme(task.get_modified(),
                                            self.get_modified_for_task(gtask),
                                            self.get_id()))
                self.datastore.push_task(task)

            elif action == SyncEngine.REMOVE:
                self.service.tasks().delete(tasklist='@default', task=gtask).execute()
                self.break_relationship(local_id = tid)
                try:
                    self.sync_engine.break_relationship(remote_id = gtask)
                except KeyError:
                    pass
            
            elif action == SyncEngine.UPDATE:
                task = self.datastore.get_task(tid)
                meme = self.sync_engine.get_meme_from_remote_id(gtask)
                newest = meme.which_is_newest(task.get_modified(),
                                     self.get_modified_for_task(gtask))
                if newest == "remote":
                    self._populate_task(task, gtask)
                    meme.set_local_last_modified(task.get_modified())
                    meme.set_remote_last_modified(\
                                        self.get_modified_for_task(gtask))
                    self.save_state()

            elif action == SyncEngine.LOST_SYNCABILITY:
                self._exec_lost_syncability(tid, gtask)
        

    @interruptible
    def set_task(self, task):
        '''
        See GenericBackend for an explanation of this function.
        '''
        # Skip if not authenticated
        if not self.authenticated:
            return 

        self.cancellation_point()
        is_syncable = self._gtg_task_is_syncable_per_attached_tags(task)
        tid = task.get_id()
        with self.datastore.get_backend_mutex():
            action, gtask_id = self.sync_engine.analyze_local_id(tid, \
                           self.datastore.has_task(tid), self._google_task_exists, \
                                                        is_syncable)
            Log.debug("processing gtg (%s, %d)" % (action, is_syncable))
            if action == SyncEngine.ADD:
                gtask = {'title': ' ',}
                gtask_id = self._populate_gtask(gtask, task)
                self.record_relationship( \
                    local_id = tid, remote_id = gtask_id, \
                    meme = SyncMeme(task.get_modified(),\
                                    self.get_modified_for_task(gtask_id),\
                                    "GTG"))

            elif action == SyncEngine.REMOVE:
                self.datastore.request_task_deletion(tid)
                try:
                    self.sync_engine.break_relationship(local_id = tid)
                    self.save_state()
                except KeyError:
                    pass
                
            elif action == SyncEngine.UPDATE:
                meme = self.sync_engine.get_meme_from_local_id(\
                                                    task.get_id())
                newest = meme.which_is_newest(task.get_modified(),
                                     self.get_modified_for_task(gtask_id))
                if newest == "local":
                    gtask = self.service.tasks().get(tasklist='@default', task=gtask_id).execute()
                    self._update_gtask(gtask, task)
                    meme.set_local_last_modified(task.get_modified())
                    meme.set_remote_last_modified(\
                                        self.get_modified_for_task(gtask_id))
                    self.save_state()
            
            elif action == SyncEngine.LOST_SYNCABILITY:
                self._exec_lost_syncability(tid, note)

###############################################################################
### Helper methods ############################################################
###############################################################################

    @interruptible
    def on_gtask_saved(self, gtask):
        '''
        Callback, executed when a Google task is saved by Google Tasks itself
        Updates the related GTG task (or creates one, if necessary).

        @param gtask: the id of the Google Taskk
        '''
        self.cancellation_point()

        @interruptible
        def _execute_on_gtask_saved(self, gtask):
            self.cancellation_point()
            self._process_gtask(gtask)
            self.save_state()

    def _google_task_is_syncable(self, gtask):
        '''
        Returns True if this Google Task should be synced into GTG tasks.

        @param gtask: the google task id
        @returns Boolean
        '''
        return True

    def _google_task_exists(self, gtask):
        '''
        Returns True if a task exists with the given id.

        @param gtask: the Google Task id
        @returns Boolean
        '''
        try:        
            self.service.tasks().get(tasklist='@default', task=gtask).execute()
            return True
        except:
            return False

    def get_modified_for_task(self, gtask):
        '''
        Returns the modification time for the given google task id.

        @param gtask: the google task id
        @returns datetime.datetime
        '''
        gtask_instance = self.service.tasks().get(tasklist='@default', task=gtask).execute()
        modified_time = datetime.datetime.strptime(gtask_instance['updated'], "%Y-%m-%dT%H:%M:%S.%fZ" )
        return modified_time

    def _populate_task(self, task, gtask):
        '''
        Copies the content of a Google task into a GTG task.

        @param task: a GTG Task
        @param gtask: a Google Task id
        '''
        gtask_instance = self.service.tasks().get(tasklist='@default', task=gtask).execute()
        text = ' '#gtask_instance['notes']
        if text == None :
            text = ' '
        #update the tags list
        #task.set_only_these_tags(extract_tags_from_text(text))
        title = gtask_instance['title']
        task.set_title(title)
        task.set_text(text)

        #FIXME set_due_date to be added
        task.add_remote_id(self.get_id(), gtask)

    def _populate_gtask(self, gtask, task):
        '''
        Copies the content of a task into a Google Task.

        @param gtask: a Google Task 
        @param task: a GTG Task
        '''
        title = task.get_title()
        content = task.get_excerpt(strip_subtasks=False)

        gtask = {
                'title': title,
                'notes': content,
        }
     
        #start_time = task.get_start_date().to_py_date().strftime('%Y-%m-%dT%H:%M:%S.000Z' )
        due = task.get_due_date()
        if due != Date.no_date():
            gtask['due'] = due.to_py_date().strftime('%Y-%m-%dT%H:%M:%S.000Z' )
    
        result = self.service.tasks().insert(tasklist = '@default', body = gtask).execute()
        return result['id']
    
    def _update_gtask(self, gtask, task):
        '''
        Updates the content of a Google task if some change is made in the GTG Task.

        @param gtask: a Google Task
        @param task: a GTG Task
        '''
        title = task.get_title()
        content = task.get_excerpt(strip_subtasks=False)
     
        #start_time = task.get_start_date().to_py_date().strftime('%Y-%m-%dT%H:%M:%S.000Z' )
        due = task.get_due_date()
        if due != no_date:
            gtask['due'] = due.to_py_date().strftime('%Y-%m-%dT%H:%M:%S.000Z' )
    
        gtask['title'] = title
        gtask['notes'] = content
        result = self.service.tasks().update(tasklist = '@default', task = gtask['id'], body = gtask).execute()
  
    def _exec_lost_syncability(self, tid, gtask):
        '''
        Executed when a relationship between tasks loses its syncability
        property. See SyncEngine for an explanation of that.
        This function finds out which object (task/note) is the original one
        and which is the copy, and deletes the copy.

        @param tid: a GTG task tid
        @param gtask: a Google task id
        '''
        self.cancellation_point()
        meme = self.sync_engine.get_meme_from_remote_id(gtask)
        #First of all, the relationship is lost
        self.sync_engine.break_relationship(remote_id = gtask)
        if meme.get_origin() == "GTG":
            self.service.tasks().delete(tasklist='@default', task=gtask).execute()
            
        else:
            self.datastore.request_task_deletion(tid)

    def break_relationship(self, *args, **kwargs):
        '''
        Proxy method for SyncEngine.break_relationship, which also saves the
        state of the synchronization.
        '''
        try:
            self.sync_engine.break_relationship(*args, **kwargs)
            #we try to save the state at each change in the sync_engine:
            #it's slower, but it should avoid widespread task
            #duplication
            self.save_state()
        except KeyError:
            pass

    def record_relationship(self, *args, **kwargs):
        '''
        Proxy method for SyncEngine.break_relationship, which also saves the
        state of the synchronization.
        '''
        
        self.sync_engine.record_relationship(*args, **kwargs)
        #we try to save the state at each change in the sync_engine:
        #it's slower, but it should avoid widespread task
        #duplication
        self.save_state()
