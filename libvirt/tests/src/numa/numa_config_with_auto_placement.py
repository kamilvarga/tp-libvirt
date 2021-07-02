import logging
import re

from virttest import virsh
from virttest import utils_misc
from virttest import libvirt_xml
from avocado.core.exceptions import TestFail


def run_all_commands_default(vm_name):
    """
    Run all the following commands with a default values:
    numatune, vcpupin, emulatorpin, iothreadinfo
    """
    commands = [virsh.numatune, virsh.vcpupin, virsh.emulatorpin,
                virsh.iothreadinfo]
    for command in commands:
        result = command(vm_name, debug=True)
        if result.exit_status:
            raise TestFail("Something went wrong during the virsh {} command: "
                           "{}".format(command, result.stderr_text))


def get_cpu_range(numa_info):
    """
    Get the range of all CPUs available on the system as a string
    """
    cpus_dict = numa_info.get_all_node_cpus()
    cpus = []
    for key in cpus_dict:
        cpus_node_list = [int(cpu) for cpu in cpus_dict[key].split(' ')
                          if cpu.isnumeric()]
        logging.debug('Following cpus found for node {}: {}'.
                      format(key, cpus_node_list))
        cpus += cpus_node_list
    cpu_range = '{}-{}'.format(min(cpus), max(cpus))
    logging.debug('The expected available cpu range is {}'.format(cpu_range))
    return cpu_range


def check_numatune(vm_name, config=''):
    """
    Check the output of the numatune command with auto placement.
    """
    result = virsh.numatune(vm_name, options=config, debug=True,
                            ignore_status=False)
    look_for = re.search(r'numa_nodeset\s*\:.*', result.stdout_text)
    if look_for:
        look_for = look_for.group().split(':')
        logging.debug('Looking for numa_nodeset in stdout and {} found.'
                      ''.format(look_for))
        target = re.sub('\s', '', look_for[-1])
        if target:
            raise TestFail('Nodeset should be empty , but {} found there.'.
                           format(target))
        else:
            logging.debug('numa_nodeset is empty as expected.')


def check_vcpupin(vm_name, numa_info, config=''):
    """
    Check the output of the vcpupin command with auto placement.
    """
    cpu_range = get_cpu_range(numa_info)
    result = virsh.vcpupin(vm_name, options=config, debug=True,
                           ignore_status=False)
    for node in numa_info.get_online_nodes():
        if re.search('{}\s*{}'.format(node, cpu_range), result.stdout_text):
            logging.debug('Expected cpu range: {} found in stdout for '
                          'node: {}.'.format(cpu_range, node))
        else:
            raise TestFail('Expected cpu range: {} not found in stdout of '
                           'vcpupin command.'.format(cpu_range))


def check_emulatorpin(vm_name, numa_info, config=''):
    """
    Check the output of the emulatorpin command with auto placement.
    """
    cpu_range = get_cpu_range(numa_info)
    result = virsh.emulatorpin(vm_name, options=config, debug=True,
                               ignore_status=False)
    if re.search('\*:\s*{}'.format(cpu_range), result.stdout_text):
        logging.debug('Expected cpu range: {} found in stdout for '
                      'emulatorpin.'.format(cpu_range))
    else:
        raise TestFail('Expected cpu range: {} not found in stdout of '
                       'emulatorpin command.'.format(cpu_range))


def check_iothreadinfo(vm_name, numa_info, iothread_no_check, config=''):
    """
    Check the output of the iothreadinfo command with auto placement.
    """
    cpu_range = get_cpu_range(numa_info)
    result = virsh.iothreadinfo(vm_name, options=config, debug=True,
                                ignore_status=False)
    # Skip the check, when there is no IOThreads
    for message in iothread_no_check.split(';'):
        if re.search(message, result.stdout_text):
            logging.debug('Skipping check for IOThread as there is no '
                          'IOthread available on this setup.')
            return
    for node in numa_info.get_online_nodes():
        if re.search('{}\s*{}'.format(node, cpu_range),
                     result.stdout_text):
            logging.debug(
                'Expected cpu range: {} found in stdout for '
                'node: {}.'.format(cpu_range, node))
        else:
            raise TestFail('Expected cpu range: {} not found in stdout '
                           'of iothreadinfo command.'.format(cpu_range))


def run(test, params, env):
    vcpu_placement = params.get("vcpu_placement")
    vm_name = params.get("main_vm")
    vm = env.get_vm(vm_name)
    backup_xml = libvirt_xml.VMXML.new_from_dumpxml(vm_name)

    mem_tuple = ('memory_mode', 'memory_placement')
    numa_memory = {}
    for mem_param in mem_tuple:
        value = params.get(mem_param)
        if value:
            numa_memory[mem_param.split('_')[1]] = value
    try:
        if vm.is_alive():
            vm.destroy()
        vmxml = libvirt_xml.VMXML.new_from_dumpxml(vm_name)
        vmxml.numa_memory = numa_memory
        vmxml.placement = vcpu_placement
        logging.debug("vm xml is %s", vmxml)
        vmxml.sync()
        vm.start()
        vm.wait_for_login()
        # Check after VM start
        run_all_commands_default(vm_name)

        check_numatune(vm_name, 'config')
        numa_info = utils_misc.NumaInfo()
        check_vcpupin(vm_name, numa_info, '--config')
        check_emulatorpin(vm_name, numa_info, 'config')
        iothread_no_check = params.get("iothread_no_check_message")
        check_iothreadinfo(vm_name, numa_info, iothread_no_check, '--config')
        # Check after destroying the VM - results should remain same as with
        # --config parameter
        vm.destroy(gracefully=False)
        # with --config parameter
        check_numatune(vm_name, 'config')
        check_vcpupin(vm_name, numa_info, '--config')
        check_emulatorpin(vm_name, numa_info, 'config')
        check_iothreadinfo(vm_name, numa_info, iothread_no_check, '--config')
        # without --config parameter
        without = True
        check_numatune(vm_name)
        check_vcpupin(vm_name, numa_info)
        check_emulatorpin(vm_name, numa_info)
        check_iothreadinfo(vm_name, numa_info, iothread_no_check)

    except Exception as e:
        test.fail('Unexpected failure during the test: {}'.format(e))
    finally:
        if vm.is_alive():
            vm.destroy(gracefully=False)
        backup_xml.sync()
