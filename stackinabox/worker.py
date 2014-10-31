# Copyright 2014 Cloudbase Solutions Srl
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import logging
import os
import platform
import pythoncom
import threading
import time

from PyQt4 import QtCore

from stackinabox import actions
from stackinabox import rdo
from stackinabox import security

LOG = logging


class _VMConsoleThread(threading.Thread):
    def __init__(self, console_named_pipe, stdout_callback):
        super(_VMConsoleThread, self).__init__()
        self.setDaemon(True)
        self._console_named_pipe = console_named_pipe
        self._stdout_callback = stdout_callback

    def run(self):
        with open(self._console_named_pipe, 'rb') as vm_console_pipe:
            while True:
                data = vm_console_pipe.readline()
                # Exit loop when the VM reboots
                if not data:
                    break
                self._stdout_callback(data)
                # TODO(alexpilotti): Fix why the heck CentOS gets stuck here
                # instead of rebooting and remove this awful workaround :)
                if data.find("Reached target Shutdown.") != -1:
                    break


class Worker(QtCore.QObject):
    finished = QtCore.pyqtSignal()
    stdout_data_ready = QtCore.pyqtSignal(str)
    stderr_data_ready = QtCore.pyqtSignal(str)
    status_changed = QtCore.pyqtSignal(str)
    error = QtCore.pyqtSignal(Exception)

    def __init__(self):
        super(Worker, self).__init__()

        self._term_type = None
        self._term_cols = None
        self._term_rows = None

        def stdout_callback(data):
            self.stdout_data_ready.emit(data)
        self._stdout_callback = stdout_callback

        def stderr_callback(data):
            self.stderr_data_ready.emit(data)
        self._stderr_callback = stderr_callback

    def set_term_info(self, term_type, cols, rows):
        self._term_type = term_type
        self._term_cols = cols
        self._term_rows = rows
        LOG.debug("Term info set: %s" %
                  str((self._term_type, self._term_cols, self._term_rows)))

    @QtCore.pyqtSlot()
    def started(self):
        LOG.info("Started")

    def _get_mac_address(self, vm_network_config, vnic_name):
        return [vnic_cfg[2] for vnic_cfg in vm_network_config
                if vnic_cfg[1] == vnic_name][0]

    def _deploy_openstack_vm(self, dep_actions):
        vm_dir = "C:\\VM"
        external_vswitch_name = "external"
        vm_name = "openstack-controller"
        vm_admin_user = "root"
        max_vm_mem_mb = None
        # TODO(alexpilotti): Add support for more OSs
        pxe_os_id = "centos7"
        console_named_pipe = r"\\.\pipe\%s" % vm_name

        # inst_repo = "http://10.14.0.142/centos/7.0/os/x86_64"
        inst_repo = "http://mirror.centos.org/centos/7/os/x86_64"

        vfd_path = os.path.join(vm_dir, "floppy.vfd")

        self.status_changed.emit('Generating random password...')
        password = security.get_random_password()
        # TODO Remove
        password = "Passw0rd"

        LOG.debug("Password: %s" % password)

        self.status_changed.emit('Generating MD5 password...')
        encrypted_password = security.get_password_md5(password)

        if not os.path.isdir(vm_dir):
            os.makedirs(vm_dir)

        self.status_changed.emit('Check if OpenStack controller VM exists...')
        dep_actions.check_remove_vm(vm_name)

        self.status_changed.emit('Creating virtual switches...')
        internal_network_config = dep_actions.get_internal_network_config()
        dep_actions.create_vswitches(external_vswitch_name,
                                     internal_network_config)

        self.status_changed.emit('Creating the OpenStack controller VM...')
        vm_network_config = dep_actions.create_openstack_vm(
            vm_name, vm_dir, max_vm_mem_mb, vfd_path, external_vswitch_name,
            console_named_pipe)

        LOG.debug("VNIC Network config: %s " % vm_network_config)

        mgmt_ext_mac_address = self._get_mac_address(vm_network_config,
                                                     "%s-mgmt-ext" % vm_name)
        mgmt_int_mac_address = self._get_mac_address(vm_network_config,
                                                     "%s-mgmt-int" % vm_name)
        data_mac_address = self._get_mac_address(vm_network_config,
                                                 "%s-data" % vm_name)
        ext_mac_address = self._get_mac_address(vm_network_config,
                                                "%s-ext" % vm_name)
        pxe_mac_address = self._get_mac_address(vm_network_config,
                                                "%s-pxe" % vm_name)

        dep_actions.create_kickstart_vfd(vfd_path, encrypted_password,
                                         mgmt_ext_mac_address,
                                         mgmt_int_mac_address,
                                         data_mac_address,
                                         ext_mac_address,
                                         inst_repo)

        vnic_ip_info = dep_actions.get_openstack_vm_ip_info(
            vm_network_config, internal_network_config["subnet"])

        LOG.debug("VNIC PXE IP info: %s " % vnic_ip_info)

        self.status_changed.emit('Starting PXE daemons...')
        dep_actions.start_pxe_service(
            internal_network_config["host_ip"],
            [vnic_ip[1:] for vnic_ip in vnic_ip_info], pxe_os_id)

        dep_actions.generate_mac_pxelinux_cfg(
            pxe_mac_address,
            mgmt_ext_mac_address.replace('-', ':'),
            inst_repo)

        self.status_changed.emit('PXE booting OpenStack controller VM...')
        dep_actions.start_openstack_vm()

        LOG.debug("Reading from console")
        console_thread = _VMConsoleThread(console_named_pipe,
                                          self._stdout_callback)
        console_thread.start()
        console_thread.join()

        self.status_changed.emit('Rebooting OpenStack controller VM...')
        dep_actions.reboot_openstack_vm()

        LOG.info("PXE booting done")

        vm_int_mgmt_ip = [vnic_ip[2] for vnic_ip in vnic_ip_info
                          if vnic_ip[0] == "%s-mgmt-int" % vm_name][0]
        LOG.debug("Connection info: %s " %
                  str((vm_int_mgmt_ip, vm_admin_user, password)))

        return (vm_int_mgmt_ip, vm_admin_user, password)

    def _install_rdo(self, rdo_installer, host, username, password):
        max_connect_attempts = 10
        reboot_sleep_s = 10

        try:
            self.status_changed.emit(
                'Waiting for the RDO VM to reboot...')
            time.sleep(reboot_sleep_s)

            self.status_changed.emit(
                'Enstablishing SSH connection with RDO VM...')
            rdo_installer.connect(host, username, password,
                                  self._term_type, self._term_cols,
                                  self._term_rows, max_connect_attempts)

            self.status_changed.emit('Updating RDO VM...')
            rdo_installer.update_os()

            self.status_changed.emit('Installing RDO...')
            rdo_installer.install_rdo(password)

            self.status_changed.emit('Checking if rebooting the RDO VM is '
                                     'required...')
            if rdo_installer.check_new_kernel():
                self.status_changed.emit('Rebooting RDO VM...')
                rdo_installer.reboot()

                time.sleep(reboot_sleep_s)

                self.status_changed.emit(
                    'Enstablishing SSH connection with RDO VM...')
                rdo_installer.connect(host, username, password,
                                      self._term_type, self._term_cols,
                                      self._term_rows, max_connect_attempts)

            self.status_changed.emit("Retrieving OpenStack configuration...")
            nova_config = rdo_installer.get_nova_config()

            self.status_changed.emit('RDO successfully deployed!')

            return nova_config
        finally:
            rdo_installer.disconnect()

    def _install_local_hyperv_compute(self, dep_actions, nova_config):
        self.status_changed.emit('Checking if the OpenStack components for '
                                 'Hyper-V are already installed...')
        msi_info = dep_actions.check_hyperv_compute_installed()
        if msi_info:
            self.status_changed.emit('Uninstalling a previous version of the '
                                     'Hyper-V OpenStack components...')
            dep_actions.uninstall_product(msi_info[0])

        msi_path = "hyperv_nova_compute.msi"
        try:
            self.status_changed.emit('Downloading Hyper-V OpenStack '
                                     'components...')
            dep_actions.download_hyperv_compute_msi(msi_path)
            self.status_changed.emit('Installing Hyper-V OpenStack '
                                     'components...')
            dep_actions.install_hyperv_compute(msi_path, nova_config)
        finally:
            os.remove(msi_path)

        self.status_changed.emit('Hyper-V OpenStack installed successfully')

    def _validate_deployment(self, rdo_installer):
        self.status_changed.emit('Validating OpenStack deployment...')
        # Skip for now
        # rdo_installer.check_hyperv_compute_services(platform.node())

    def _create_cirros_image(self, dep_actions, openstack_cred):
        image_path = "cirros.vhdx.gz"
        self.status_changed.emit('Downloading Cirros VHDX image...')
        dep_actions.download_cirros_image(image_path)
        self.status_changed.emit('Removing existing images...')
        dep_actions.delete_existing_images(openstack_cred)
        self.status_changed.emit('Uploading Cirros VHDX image in Glance...')
        dep_actions.create_cirros_image(openstack_cred, image_path)
        os.remove(image_path)

    @QtCore.pyqtSlot()
    def deploy_openstack(self):
        pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
        dep_actions = actions.DeploymentActions()

        try:
            dep_actions.check_platform_requirements()
            rdo_installer = rdo.RDOInstaller(self._stdout_callback,
                                             self._stderr_callback)

            (mgmt_ip, ssh_user, ssh_password) = self._deploy_openstack_vm(
                dep_actions)
            nova_config = self._install_rdo(rdo_installer, mgmt_ip, ssh_user,
                                            ssh_password)
            self._install_local_hyperv_compute(dep_actions, nova_config)
            self._validate_deployment(rdo_installer)

            openstack_cred = dep_actions.get_openstack_credentials(
                mgmt_ip, ssh_password)
            self._create_cirros_image(dep_actions, openstack_cred)

            self.status_changed.emit('Your OpenStack deployment is ready!')
        except Exception as ex:
            LOG.exception(ex)
            LOG.error(ex)
            self.error.emit(ex)
        finally:
            dep_actions.stop_pxe_service()
