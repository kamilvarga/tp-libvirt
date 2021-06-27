import logging
import re

from avocado.utils import process
from avocado.utils import distro

from virttest import libvirt_xml
from virttest import utils_misc
from virttest import utils_package
from virttest import virsh

from virttest.staging.utils_memory import read_from_numastat


def get_qemu_total_for_nodes():
    """
    Get a Total memory taken by the qemu-kvm process on all NUMA nodes.
    """
    total_list = []
    # Get the PID of QEMU
    result = process.run('pidof qemu-kvm', shell=True)
    pid = result.stdout_text
    # Get numastat output and look for Total
    total = read_from_numastat(pid, "Total")
    logging.debug('Total memory taken by nodes:\n')
    for index in range(len(total)):
        logging.debug('\t\t\tnode{} : {}MB\n'.format(index, total[index]))
        total_list.append(int(float(total[index])))
    return total_list


def prepare_host_for_test(params, test):
    """
    Setup host and return constants used by other functions
    """
    # Create a NumaInfo object to get NUMA related information
    numa_info = utils_misc.NumaInfo()
    online_nodes = numa_info.get_online_nodes_withmem()
    numa_memory = {
        'mode': params.get('memory_mode', 'strict'),
        # If nodeset is not defined in config, take a first node with memory.
        'nodeset': params.get('memory_nodeset', online_nodes[0])
    }
    # Get the size of a main node
    nodeset_size = int(numa_info.read_from_node_meminfo(
        int(numa_memory['nodeset']), 'MemTotal'))
    # Get the size of a first neighbour with memory
    for node in online_nodes:
        if str(node) != numa_memory['nodeset']:
            neighbour = node
            break
    nodeset_nb_size = int(numa_info.read_from_node_meminfo(
        int(neighbour), 'MemTotal'))
    logging.debug('Memory available on a main node {} is {}'.
                  format(numa_memory['nodeset'], nodeset_size))
    logging.debug('Memory available on a neighbour node {} is {}'.
                  format(neighbour, nodeset_nb_size))
    # Increase a size by 50% of neighbour node
    oversize = int(nodeset_size + 0.5 * nodeset_nb_size)
    # Decrease nodeset size by 25%
    undersize = int(nodeset_size * 0.25)
    # Memory to eat is a whole nodeset + 10% of neighbour size
    memory_to_eat = int(nodeset_size + 0.1 * nodeset_nb_size)
    nodeset_string = '{},{}'.format(online_nodes[0], neighbour)
    process.run("swapoff -a", shell=True)
    if not utils_package.package_install('libcgroup-tools'):
        test.fail("Failed to install package libcgroup-tools on host.")

    return numa_memory, oversize, undersize, memory_to_eat, neighbour, nodeset_string


def prepare_guest_for_test(vm_name, session, test, oversize, nodeset_string):
    """
    Setup guest
    """
    result = virsh.numatune(vm_name, debug=True)
    if result.exit_status:
        test.fail("Something went wrong during the virsh numatune command.")
    result = virsh.numatune(vm_name, mode='0', nodeset=nodeset_string, debug=True)
    if result.exit_status:
        test.fail("Something went wrong during the 'virsh numatune 0 {}' "
                  "command.".format(nodeset_string))
    result = virsh.setmem(vm_name, oversize, debug=True)
    if result.exit_status:
        test.fail("Something went wrong during the 'virsh setmem {}' "
                  "command.".format(oversize))
    # Turn off a swap on guest
    session.cmd_status('swapoff -a', timeout=10)
    # Install the numactl package on the guest for a memhog program
    if not utils_package.package_install('numactl', session):
        test.fail("Failed to install package numactl on guest.")


def prepare_vm_xml_for_test(vm_name, numa_memory, oversize, undersize):
    """
    Setup required parameters in the VM XML for test
    """
    # Setup required parameters in the XML and start the guest
    vmxml = libvirt_xml.VMXML.new_from_dumpxml(vm_name)
    vmxml.numa_memory = numa_memory
    vmxml.max_mem = oversize
    vmxml.current_mem = undersize
    logging.debug("vm xml is %s", vmxml)
    vmxml.sync()


def check_cgget_output(test, cgget_message):
    """
    Get the cgget output and check it for required value
    """
    # Find the slice and print it with the cgget
    cpuset_slices = process.run('systemd-cgls cpuset')
    machine_found = False
    slice_line = None
    cpuset_lines = re.split('\s', cpuset_slices.stdout_text)
    for line in cpuset_lines:
        if re.search('machine\.slice', line):
            machine_found = True
            continue
        if machine_found and len(line) > 1:
            slice_line = line.strip()
            # No more lines need to be checked
            break
    slice_line = slice_line.replace('\\', '\\\\')[2:]
    result = process.run('cgget -g cpuset /machine.slice/{}/libvirt'.
                         format(slice_line), shell=True,
                         ignore_status=False)
    if cgget_message not in result.stdout_text:
        test.fail('{} not found in cgget output'.format(cgget_message))


def run(test, params, env):
    """
    Test Live update the numatune nodeset and memory can spread to other node
    automatically.
    """

    vm_name = params.get("main_vm")
    vm = env.get_vm(vm_name)
    backup_xml = libvirt_xml.VMXML.new_from_dumpxml(vm_name)

    try:
        # Prepare host
        constants = prepare_host_for_test(params, test)
        numa_memory = constants[0]
        oversize = constants[1]
        undersize = constants[2]
        memory_to_eat = constants[3]
        neighbour = constants[4]
        nodeset_string = constants[5]
        # Prepare VM XML
        prepare_vm_xml_for_test(vm_name, numa_memory, oversize, undersize)
        # Start the VM
        if vm.is_dead:
            vm.start()
        session = vm.wait_for_login()
        # Prepare guest
        prepare_guest_for_test(vm_name, session, test, oversize, nodeset_string)
        # And get the numastat prior the test
        total_prior = get_qemu_total_for_nodes()
        # Start test
        result = session.cmd_status_output('memhog -r1 {}k'.
                                           format(memory_to_eat),
                                           timeout=60)
        logging.debug(result)
        if vm.is_dead():
            test.fail('The VM crashed when memhog was executed.')
        # Get the numastat after the test
        total_after = get_qemu_total_for_nodes()
        limit = int(params.get("limit_mb"))
        # And check the limit
        if total_after[int(neighbour)] - total_prior[int(neighbour)] < limit:
            test.fail('Total memory taken by the memhog on a neighbour node{} '
                      'is not within limit: {}MB and hence, the memory was '
                      'probably not spread properly.'.format(neighbour, limit))
        # The cgget check is ignored for >= RHEL9
        if distro.detect().name == 'rhel' and int(distro.detect().version) < 9:
            cgget_message = params.get('cgget_message')
            check_cgget_output(test, cgget_message)

    finally:
        backup_xml.sync()
