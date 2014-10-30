import logging
import os
import paramiko

from stackinabox import utils

LOG = logging


class RDOInstaller(object):

    def __init__(self, stdout_callback, stderr_callback):
        self._stdout_callback = stdout_callback
        self._stderr_callback = stderr_callback
        self._ssh = None

    def _exec_shell_cmd_check_exit_status_single(self, cmd):
        chan = self._ssh.invoke_shell(term=self._term_type,
                                      width=self._term_cols,
                                      height=self._term_rows)
        # Close session after the command executed
        chan.send("%s\nexit\n" % cmd)

        running = True
        while running:
            if chan.recv_ready():
                data = chan.recv(4096).decode('ascii')
                self._stdout_callback(data)
            if chan.recv_stderr_ready():
                data = chan.recv_stderr(4096).decode('ascii')
                self._stderr_callback(data)
            if chan.exit_status_ready():
                running = False

        exit_status = chan.recv_exit_status()
        if exit_status:
            raise Exception("Command failed with exit code: %d" % exit_status)

    def _exec_shell_cmd_check_exit_status(self, cmd):
        utils.retry_action(
            lambda: self._exec_shell_cmd_check_exit_status_single(cmd),
            interval=5)

    def _exec_cmd_single(self, cmd):
        chan = self._ssh.get_transport().open_session()
        chan.exec_command(cmd)
        return chan.recv_exit_status()

    def _exec_cmd(self, cmd):
        return utils.retry_action(lambda: self._exec_cmd_single(cmd))

    def _connect_single(self, host, username, password, term_type, term_cols,
                        term_rows):
        self.disconnect()
        LOG.debug("connecting")

        self._term_type = term_type
        self._term_cols = term_cols
        self._term_rows = term_rows

        self._ssh = paramiko.SSHClient()
        self._ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self._ssh.connect(host, username=username, password=password)
        LOG.debug("connected")

    def connect(self, host, username, password, term_type, term_cols,
                term_rows, max_attempts=1):
        utils.retry_action(
            lambda: self._connect_single(
                host, username, password, term_type, term_cols, term_rows),
            max_attempts=max_attempts)

    def disconnect(self):
        if self._ssh:
            self._ssh.close()
            self._ssh = None

    def update_os(self):
        LOG.info("Updating OS")
        self._exec_shell_cmd_check_exit_status('yum update -y')
        LOG.info("OS updated")

    def reboot(self):
        LOG.info("Rebooting")
        self._exec_cmd("reboot")
        self.disconnect()

    def _exec_utils_function(self, cmd):
        centos_utils = "centos-utils.sh"
        self._copy_resource_file(centos_utils)
        return self._exec_cmd(". /root/%(centos_utils)s && %(cmd)s" %
                              {"centos_utils": centos_utils, "cmd": cmd})

    def check_new_kernel(self):
        return self._exec_utils_function("check_new_kernel")

    def _get_config_value(self, config_file, section, name):
        stdin, stdout, stderr = self._ssh.exec_command(
            '/usr/bin/openstack-config --get \"%(config_file)s\" '
            '\"%(section)s\" \"%(name)s\"' %
            {'config_file': config_file, 'section': section, 'name': name})
        return stdout.read()[:-1]

    def get_nova_config(self):
        config_file = "/etc/nova/nova.conf"
        section = "DEFAULT"
        config = {}
        for name in ["rabbit_hosts", "rabbit_userid", "rabbit_password",
                     "glance_api_servers", "neutron_url",
                     "neutron_admin_auth_url", "neutron_admin_tenant_name",
                     "neutron_admin_username", "neutron_admin_password"]:
            config[name] = self._get_config_value(config_file, section, name)
        return {section: config}

    def _copy_resource_file_single(self, file_name):
        LOG.debug("Copying %s" % file_name)
        sftp = self._ssh.open_sftp()
        path = os.path.join(utils.get_resources_dir(), file_name)
        sftp.put(path, '/root/%s' % file_name)
        sftp.close()
        LOG.debug("%s copied" % file_name)

    def _copy_resource_file(self, file_name):
        return utils.retry_action(
            lambda: self._copy_resource_file_single(file_name))

    def _check_hyperv_compute_services(self, host_name):
        if (self._exec_utils_function(
                "source ~/keystonerc_admin && check_nova_service_up %s" %
                host_name) != 0):
            raise Exception("The Hyper-V nova-compute service is not enabled "
                            "in RDO")
        if (self._exec_utils_function(
                "source ~/keystonerc_admin && check_neutron_agent_up %s" %
                host_name) != 0):
            raise Exception("The Hyper-V neutron agent is not enabled in RDO")

    def check_hyperv_compute_services(self, host_name):
        utils.retry_action(
            lambda: self._check_hyperv_compute_services(host_name), interval=5)

    def install_rdo(self, rdo_admin_password):
        install_script = 'install-rdo.sh'
        self._copy_resource_file(install_script)

        LOG.info("Installing RDO")
        self._exec_shell_cmd_check_exit_status(
            '/bin/chmod u+x /root/%(install_script)s && '
            '/root/%(install_script)s \"%(rdo_admin_password)s\"' %
            {'install_script': install_script,
             'rdo_admin_password': rdo_admin_password})
        LOG.info("RDO installed")
