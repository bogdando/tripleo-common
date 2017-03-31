# Copyright 2016 Red Hat, Inc.
# All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.
import logging
import os
import shutil
import tempfile
import yaml

from heatclient import exc as heatexceptions
from keystoneauth1 import exceptions as keystoneauth_exc
from mistral.workflow import utils as mistral_workflow_utils
from mistralclient.api import base as mistralclient_base
from oslo_concurrency import processutils
import six
from swiftclient import exceptions as swiftexceptions

from tripleo_common.actions import base
from tripleo_common import constants
from tripleo_common import exception
from tripleo_common.utils import plan as plan_utils
from tripleo_common.utils import swift as swiftutils
from tripleo_common.utils import tarball
from tripleo_common.utils.validations import pattern_validator


LOG = logging.getLogger(__name__)

default_container_headers = {
    constants.TRIPLEO_META_USAGE_KEY: 'plan'
}


class CreateContainerAction(base.TripleOAction):
    """Creates an object container

    This action creates an object container for a given name.  If a container
    with the same name already exists an exception is raised.
    """

    def __init__(self, container):
        super(CreateContainerAction, self).__init__()
        self.container = container

    def run(self, context):
        oc = self.get_object_client(context)

        # checks to see if a container has a valid name
        if not pattern_validator(constants.PLAN_NAME_PATTERN, self.container):
            message = ("Unable to create plan. The plan name must "
                       "only contain letters, numbers or dashes")
            return mistral_workflow_utils.Result(error=message)

        # checks to see if a container with that name exists
        if self.container in [container["name"] for container in
                              oc.get_account()[1]]:
            result_string = ("A container with the name %s already"
                             " exists.") % self.container
            return mistral_workflow_utils.Result(error=result_string)
        oc.put_container(self.container, headers=default_container_headers)


class MigratePlanAction(base.TripleOAction):
    """Migrate plan from using a Mistral environment to using Swift

    This action creates a plan-environment.yaml file based on the
    Mistral environment or the default environment, if the file doesn't
    already exist in Swift.

    This action will be deleted in Queens, as it will no longer be
    needed by then - all plans will include plan-environment.yaml by
    default.
    """

    def __init__(self, plan):
        super(MigratePlanAction, self).__init__()
        self.plan = plan

    def run(self, context):
        swift = self.get_object_client(context)
        mistral = self.get_workflow_client(context)
        from_mistral = False

        try:
            env = plan_utils.get_env(swift, self.plan)
        except swiftexceptions.ClientException:
            # The plan has not been migrated yet. Check if there is a
            # Mistral environment.
            try:
                env = mistral.environments.get(self.plan).variables
                from_mistral = True
            except (mistralclient_base.APIException,
                    keystoneauth_exc.http.NotFound):
                # No Mistral env and no template: likely deploying old
                # templates aka previous version of OpenStack.
                env = {'version': 1.0,
                       'name': self.plan,
                       'description': '',
                       'template': 'overcloud.yaml',
                       'environments': [
                           {'path': 'overcloud-resource-registry-puppet.yaml'}
                       ]}

            # Store the environment info into Swift
            plan_utils.put_env(swift, env)
            if from_mistral:
                mistral.environments.delete(self.plan)


class ListPlansAction(base.TripleOAction):
    """Lists deployment plans

    This action lists all deployment plans residing in the undercloud.  A
    deployment plan consists of a container marked with metadata
    'x-container-meta-usage-tripleo'.
    """

    def run(self, context):
        # Plans consist of a container object marked with metadata to ensure it
        # isn't confused with another container
        plan_list = []
        oc = self.get_object_client(context)

        for item in oc.get_account()[1]:
            container = oc.get_container(item['name'])[0]
            if constants.TRIPLEO_META_USAGE_KEY in container.keys():
                plan_list.append(item['name'])
        return list(set(plan_list))


class DeletePlanAction(base.TripleOAction):
    """Deletes a plan and associated files

    Deletes a plan by deleting the container matching plan_name. It
    will not delete the plan if a stack exists with the same name.

    Raises StackInUseError if a stack with the same name as plan_name
    exists.
    """

    def __init__(self, container):
        super(DeletePlanAction, self).__init__()
        self.container = container

    def run(self, context):
        error_text = None
        # heat throws HTTPNotFound if the stack is not found
        try:
            stack = self.get_orchestration_client(context).stacks.get(
                self.container
            )
        except heatexceptions.HTTPNotFound:
            pass
        else:
            if stack is not None:
                raise exception.StackInUseError(name=self.container)

        try:
            swift = self.get_object_client(context)
            swiftutils.delete_container(swift, self.container)
        except swiftexceptions.ClientException as ce:
            LOG.exception("Swift error deleting plan.")
            error_text = ce.msg
        except Exception as err:
            LOG.exception("Error deleting plan.")
            error_text = six.text_type(err)

        if error_text:
            return mistral_workflow_utils.Result(error=error_text)


class ListRolesAction(base.TripleOAction):
    """Returns a deployment plan's roles

    Parses roles_data.yaml and returns the names of all available roles.

    :param container: name of the Swift container / plan name
    :return: list of roles in the container's deployment plan
    """

    def __init__(self, container=constants.DEFAULT_CONTAINER_NAME):
        super(ListRolesAction, self).__init__()
        self.container = container

    def run(self, context):
        try:
            swift = self.get_object_client(context)
            roles_data = yaml.safe_load(swift.get_object(
                self.container, constants.OVERCLOUD_J2_ROLES_NAME)[1])
        except Exception as err:
            err_msg = ("Error retrieving roles data from deployment plan: %s"
                       % err)
            LOG.exception(err_msg)
            return mistral_workflow_utils.Result(error=err_msg)

        return [role['name'] for role in roles_data]


class ExportPlanAction(base.TripleOAction):
    """Exports a deployment plan

    This action exports a deployment plan with a given name. The plan
    templates are downloaded from the Swift container, packaged up in a tarball
    and uploaded to Swift.
    """

    def __init__(self, plan, delete_after, exports_container):
        super(ExportPlanAction, self).__init__()
        self.plan = plan
        self.delete_after = delete_after
        self.exports_container = exports_container

    def _download_templates(self, swift, tmp_dir):
        """Download templates to a temp folder."""
        template_files = swift.get_container(self.plan)[1]

        for tf in template_files:
            filename = tf['name']
            contents = swift.get_object(self.plan, filename)[1]
            path = os.path.join(tmp_dir, filename)
            dirname = os.path.dirname(path)

            if not os.path.exists(dirname):
                os.makedirs(dirname)

            with open(path, 'w') as f:
                f.write(contents)

    def _create_and_upload_tarball(self, swift, tmp_dir):
        """Create a tarball containing the tmp_dir and upload it to Swift."""
        tarball_name = '%s.tar.gz' % self.plan
        headers = {'X-Delete-After': self.delete_after}

        # make sure the root container which holds all plan exports exists
        try:
            swift.get_container(self.exports_container)
        except swiftexceptions.ClientException:
            swift.put_container(self.exports_container)

        with tempfile.NamedTemporaryFile() as tmp_tarball:
            tarball.create_tarball(tmp_dir, tmp_tarball.name)

            swift.put_object(self.exports_container, tarball_name, tmp_tarball,
                             headers=headers)

    def run(self, context):
        swift = self.get_object_client()
        tmp_dir = tempfile.mkdtemp()

        try:
            self._download_templates(swift, tmp_dir)
            self._create_and_upload_tarball(swift, tmp_dir)
        except swiftexceptions.ClientException as err:
            msg = "Error attempting an operation on container: %s" % err
            return mistral_workflow_utils.Result(error=msg)
        except (OSError, IOError) as err:
            msg = "Error while writing file: %s" % err
            return mistral_workflow_utils.Result(error=msg)
        except processutils.ProcessExecutionError as err:
            msg = "Error while creating a tarball: %s" % err
            return mistral_workflow_utils.Result(error=msg)
        except Exception as err:
            msg = "Error exporting plan: %s" % err
            return mistral_workflow_utils.Result(error=msg)
        finally:
            shutil.rmtree(tmp_dir)
