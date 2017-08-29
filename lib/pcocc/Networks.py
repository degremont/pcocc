#  Copyright (C) 2014-2015 CEA/DAM/DIF
#
#  This file is part of PCOCC, a tool to easily create and deploy
#  virtual machines using the resource manager of a compute cluster.
#
#  PCOCC is free software: you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  PCOCC is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with PCOCC. If not, see <http://www.gnu.org/licenses/>

import yaml
import os
import re
import struct
import socket
import subprocess
import shlex
import signal
import time
import pwd
import random
import logging
import tempfile
import shutil
import stat
import jsonschema
import psutil
import tempfile
from abc import ABCMeta, abstractmethod

from .Backports import subprocess_check_output
from .Error import PcoccError, InvalidConfigurationError
from .Config import Config
from .Misc import DefaultValidatingDraft4Validator, IDAllocator


network_config_schema = """
type: object
patternProperties:
  "^([a-zA-Z][a-zA-Z_0-9--]*)$":
    oneOf:
      - $ref: '#/definitions/nat'
      - $ref: '#/definitions/pv'
      - $ref: '#/definitions/ib'
      - $ref: '#/definitions/bridged'
      - $ref: '#/definitions/hostib'
      - $ref: '#/definitions/genericpci'
additionalProperties: false

definitions:
  nat:
    properties:
      type:
          enum:
            - nat

      settings:
        type: object
        properties:
          nat-network:
           type: string
          vm-network:
           type: string
          vm-network-gw:
           type: string
          vm-ip:
           type: string
          vm-hwaddr:
           type: string
           default-value: '52:54:00:44:AE:5E'
           pattern: '^([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$'
          bridge:
           type: string
          bridge-hwaddr:
           type: string
           default-value: '52:54:00:C0:C0:C0'
           pattern: '^([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$'
          tap-prefix:
           type: string
          mtu:
           type: integer
           default-value: 1500
          domain-name:
           type: string
           default-value: ''
          dns-server:
           type: string
           default-value: ''
          ntp-server:
           type: string
           default-value: ''
          reverse-nat:
           type: object
          allow-outbound:
           type: string
           default-value: 'all'
        additionalProperties: false
        required:
         - nat-network
         - vm-network
         - vm-network-gw
         - vm-ip
         - bridge
         - tap-prefix
    additionalProperties: false

  pv:
    properties:
      type:
          enum:
            - pv

      settings:
        type: object
        properties:
          mac-prefix:
           type: string
           default-value: '52:54:00'
           pattern: '^([0-9a-fA-F]{2}:){0,3}[0-9a-fA-F]{2}$'
          bridge-prefix:
           type: string
          tap-prefix:
           type: string
          mtu:
           type: integer
           default-value: 1500
          host-if-suffix:
           type: string
        additionalProperties: false
        required:
         - bridge-prefix
         - tap-prefix

    additionalProperties: false

  ib:
    properties:
      type:
          enum:
            - ib

      settings:
        type: object
        properties:
          host-device:
           type: string
          min-pkey:
           type: string
           pattern: "^0x[0-9a-zA-Z]{4}$"
          max-pkey:
           type: string
           pattern: "^0x[0-9a-zA-Z]{4}$"
          license:
           type: string
          opensm-daemon:
           type: string
          opensm-partition-cfg:
           type: string
          opensm-partition-tpl:
           type: string
        additionalProperties: false
        required:
         - min-pkey
         - max-pkey
         - host-device
         - opensm-daemon
         - opensm-partition-cfg
         - opensm-partition-tpl

    additionalProperties: false

  bridged:
    properties:
      type:
          enum:
            - bridged

      settings:
        type: object
        properties:
          host-bridge:
           type: string
          tap-prefix:
           type: string
          mtu:
           type: integer
           default-value: 1500
        additionalProperties: false
        required:
         - host-bridge
         - tap-prefix
    additionalProperties: false

  hostib:
    properties:
      type:
          enum:
            - hostib

      settings:
        type: object
        properties:
          host-device:
           type: string
        additionalProperties: false
        required:
         - host-device
    additionalProperties: false

  genericpci:
    properties:
      type:
          enum:
            - genericpci

      settings:
        type: object
        properties:
          host-device-addrs:
           type: array
           items:
             type: string
          host-driver:
           type: string
        additionalProperties: false
        required:
         - host-device-addrs
         - host-driver
    additionalProperties: false

  additionalProperties: false
"""

class NetworkSetupError(PcoccError):
    def __init__(self, error):
        super(NetworkSetupError, self).__init__(
            'Failed to setup network on node: ' + error)


class VNetworkConfig(dict):
    """Manages the network configuration"""
    def load(self, filename):
        """Loads the network config

        Instantiates a dict holding a VNetwork class for each configured
        network

        """
        try:
            stream = file(filename, 'r')
            net_config = yaml.safe_load(stream)
        except yaml.YAMLError as err:
            raise InvalidConfigurationError(str(err))
        except IOError as err:
            raise InvalidConfigurationError(str(err))

        try:
            validator = DefaultValidatingDraft4Validator(VNetwork.schema)
            validator.validate(net_config)
        except jsonschema.exceptions.ValidationError as err:
            #FIXME when err.path doesnt exist (error at top level)
            message = "failed to parse configuration for network {0} \n".format(err.path[0])
            sortfunc = jsonschema.exceptions.by_relevance(frozenset(['oneOf', 'anyOf', 'enum']))
            best_errors = sorted(err.context, key=sortfunc, reverse=True)
            for e in best_errors:
                if (len(e.schema_path) == 4 and e.schema_path[1] == 'properties' and
                    e.schema_path[2] ==  'type' and e.schema_path[3] == 'enum'):
                    continue
                else:
                    message += str(e.message)
                    break
            else:
                for e in best_errors:
                    message += '\n' + str(e.message)

            raise InvalidConfigurationError(message)

        for name, net_attr in net_config.iteritems():
            self[name] = VNetwork.create(net_attr['type'],
                                         name,
                                         net_attr['settings'])

class VNetworkClass(ABCMeta):
    def __init__(cls, name, bases, dct):
        if '_schema' in dct:
            VNetwork.register_network(dct['_schema'], cls)
        super(VNetworkClass, cls).__init__(name, bases, dct)

class VNetwork(object):
    __metaclass__ = VNetworkClass

    """Base class for all network types"""
    _networks = {}
    schema = ""

    @classmethod
    def register_network(cls, subschema, network_class):
        if not cls.schema:
            cls.schema = yaml.safe_load(network_config_schema)

        subschema = yaml.safe_load(subschema)

        types = subschema['properties']['type']['enum']

        for t in types:
            cls._networks[t] = network_class

        cls.schema['patternProperties']\
            ['^([a-zA-Z][a-zA-Z_0-9--]*)$']['oneOf'].append(
                {'$ref': '#/definitions/{0}'.format(types[0])})

        cls.schema['definitions'][types[0]] = subschema

    @classmethod
    def create(cls, ntype, name, settings):
        """Factory function to create subclasses"""
        if ntype in cls._networks:
            return cls._networks[ntype](name, settings)

        # Old style networks
        if ntype == "pv":
            return VPVNetwork(name, settings)
        if ntype == "nat":
            return VNATNetwork(name, settings)
        if ntype == "ib":
            return VIBNetwork(name, settings)
        if ntype == "bridged":
            return VBridgedNetwork(name, settings)
        if ntype == "hostib":
            return VHostIBNetwork(name, settings)
        if ntype == "genericpci":
            return VGenericPCI(name, settings)

        raise InvalidConfigurationError("Unknown network type: " + ntype)

    def __init__(self, name):
        self.name = name

    def get_license(self, cluster):
        """Returns a list of batch licenses that must be allocated
        to instantiate the network"""
        return []

    def dump_resources(self, res):
        """Store config data describing the allocated resources
        in the key/value store

        Called when setting up a node for a virtual cluster

        """
        batch = Config().batch
        batch.write_key(
            'cluster',
            '{0}/{1}'.format(self.name, batch.node_rank),
            yaml.dump(res))

    def load_resources(self):
        """Read config data describing the allocated resources
        from the key/value store"""
        batch = Config().batch
        data = batch.read_key(
            'cluster',
            '{0}/{1}'.format(self.name, batch.node_rank))

        if not data:
            raise NetworkSetupError('unable to load resources for network '
                                    + self.name)

        return yaml.safe_load(data)


    def _vm_res_label(self, vm):
        return "vm-%d" % vm.rank

    def _get_net_key_path(self, key):
        """Returns path in the key/value store for a per network instance
        key

        """
        return  'net/name/{0}/{1}'.format(self.name, key)

    def _get_type_key_path(self, key):
        """Returns path in the key/value store for a per network type
        key

        """
        return  'net/type/{0}/{1}'.format(self._type, key)

class VBridgedNetwork(VNetwork):
    def __init__(self, name, settings):
        super(VBridgedNetwork, self).__init__(name)

        self._host_bridge = settings["host-bridge"]
        self._tap_prefix = settings["tap-prefix"]
        self._mtu = int(settings["mtu"])
        self._type = "direct"

    def init_node(self):
        if not bridge_exists(self._host_bridge):
            raise NetworkSetupError("Host bridge {0} doesn't exist".format(
                self._host_bridge))

    def cleanup_node(self):
        self._cleanup_stray_taps()

    def alloc_node_resources(self, cluster):
        batch = Config().batch
        net_res = {}

        for vm in cluster.vms:
            if not vm.is_on_node():
                continue

            if not self.name in vm.networks:
                continue

            vm_label = self._vm_res_label(vm)
            net_res[vm_label] = self._alloc_vm_res(vm)

        self.dump_resources(net_res)

    def free_node_resources(self, cluster):
        net_res = None
        for vm in cluster.vms:
            if not vm.is_on_node():
                continue

            if not self.name in vm.networks:
                continue

            if not net_res:
                net_res = self.load_resources()

            vm_label = self._vm_res_label(vm)
            self._cleanup_vm_res(net_res[vm_label])

    def load_node_resources(self, cluster):
        net_res = None
        for vm in cluster.vms:
            if not vm.is_on_node():
                continue

            if not self.name in vm.networks:
                continue

            if not net_res:
                net_res = self.load_resources()

            vm_label = self._vm_res_label(vm)

            try:
                hwaddr = os.environ['PCOCC_NET_{0}_HWADDR'.format(
                              self.name.upper())]
            except KeyError:
                hwaddr = [ 0x52, 0x54, 0x00,
                           random.randint(0x00, 0x7f),
                           random.randint(0x00, 0xff),
                           random.randint(0x00, 0xff) ]
                hwaddr = ':'.join(map(lambda x: "%02x" % x, hwaddr))

            vm.add_eth_if(self.name,
                          net_res[vm_label]['tap_name'],
                          hwaddr)

    def _cleanup_stray_taps(self):
        # Look for remaining taps to cleanup
        for tap_id in find_used_dev_ids(self._tap_prefix):
            logging.warning(
                'Deleting leftover tap for {0} network'.format(self.name))

            # Delete the tap
            tap_name = dev_name_from_id(self._tap_prefix,
                                        tap_id)
            tun_delete_tap(tap_name)

    def _alloc_vm_res(self, vm):
        # Allocate a local VM id unique on this node
        tap_id = find_free_dev_id(self._tap_prefix)
        # Define the VM tap name and unique IP based on the VM id
        tap_name = dev_name_from_id(self._tap_prefix, tap_id)

        # Create and enable the tap
        tun_create_tap(tap_name, Config().batch.batchuser)
        dev_enable(tap_name)
        ip_set_mtu(tap_name, self._mtu)
        bridge_add_port(tap_name, self._host_bridge)
        return {'tap_name': tap_name}

    def _cleanup_vm_res(self, resources):
        tap_name = resources['tap_name']
        # Delete the tap
        tun_delete_tap(tap_name)

class VPVNetwork(VNetwork):
    def __init__(self, name, settings):
        super(VPVNetwork, self).__init__(name)

        self._mac_prefix = settings.get("mac-prefix", "52:54:00")
        self._bridge_prefix = settings["bridge-prefix"]
        self._tap_prefix = settings["tap-prefix"]
        self._mtu = int(settings["mtu"])
        self._host_if_suffix = settings["host-if-suffix"]
        self._min_key = 1024
        self._max_key = 2 ** 16 - 1
        self._type = "pv"
        self._ida = IDAllocator(self._get_type_key_path('key_alloc_state'),
                                self._max_key - self._min_key + 1)

    def init_node(self):
        pass

    def cleanup_node(self):
        #TODO: What to do if there are unexpected taps or bridges left
        self._cleanup_stray_bridges()
        self._cleanup_stray_taps()

    def alloc_node_resources(self, cluster):
        batch = Config().batch

        bridge_name = find_free_dev_name(self._bridge_prefix)
        tap_user = batch.batchuser
        net_res = {}
        host_tunnels = {}
        local_ports = []
        master = -1

        for vm in cluster.vms:
            if self.name in vm.networks:
                if master == -1:
                    master = vm.get_host_rank()
                if vm.is_on_node():
                    break
        else:
            #No vm on node, nothing to do
            return

        if batch.node_rank == master:
            logging.info("Node is master for PV network {0}".format(
                    self.name))
        try:
            tun_id = self._min_key + self._ida.coll_alloc_one(
                master,
                '{0}_key'.format(self.name))
        except PcoccError as e:
            raise NetworkSetupError('{0}: {1}'.format(
                self.name,
                str(e)
            ))


        bridge_created = False
        for vm in cluster.vms:
            if not self.name in vm.networks:
                continue

            if not bridge_created:
                ovs_add_bridge(bridge_name)
                ip_set_mtu(bridge_name, self._mtu)
                bridge_created = True

            hwaddr = self._gen_vm_hwaddr(vm)
            if vm.is_on_node():
                tap_name = find_free_dev_name(self._tap_prefix)
                tun_create_tap(tap_name, tap_user)
                dev_enable(tap_name)
                ip_set_mtu(tap_name, self._mtu)
                port_id = ovs_add_port(tap_name, bridge_name)

                local_ports.append(port_id)

                # Incoming packets to the VM are directly
                # sent to the destination tap
                ovs_add_flow(bridge_name,
                             0, 3000,
                             "idle_timeout=0,hard_timeout=0,"
                             "dl_dst=%s,actions=output:%s"
                             % (hwaddr, port_id))

                # Flood packets sent from the VM without a known
                # destination
                # FIXME: answer
                # directly to ARP requests and drop other broadcast
                # packets as this is too inefficient
                ovs_add_flow(bridge_name,
                             0, 2000,
                             "in_port=%s,"
                             "idle_timeout=0,hard_timeout=0,"
                             "actions=flood" % (
                        port_id))


                vm_label = self._vm_res_label(vm)
                net_res[vm_label] = {'tap_name': tap_name,
                                     'hwaddr': hwaddr,
                                     'port_id': port_id}

            else:
                host = vm.get_host()
                if host not in host_tunnels:
                    tunnel_port_id = ovs_add_tunnel(bridge_name,
                                                    "htun-%s-%s" % (
                                                        bridge_name,
                                                        len(host_tunnels)),
                                                    "vxlan",
                                                    "%s%s" % (
                            host,
                            self._host_if_suffix),
                                                    tun_id)
                    host_tunnels[host] = tunnel_port_id

                # Directly forward packets for a remote VM to the
                # correct destination
                ovs_add_flow(bridge_name,
                             0, 3000,
                             "idle_timeout=0,hard_timeout=0,"
                             "dl_dst=%s,actions=output:%s"
                             % (hwaddr, host_tunnels[host]))

        # Incoming broadcast packets: output to all local VMs
        # TODO: answer directly to ARP requests and drop other
        # broadcast packets
        if local_ports:
            ovs_add_flow(bridge_name,
                         0, 1000,
                         "idle_timeout=0,hard_timeout=0,"
                         "actions=output:%s" % (
                    ','.join([str(port) for port in local_ports])))

        net_res['global'] = {'bridge_name': bridge_name,
                             'tun_id': tun_id,
                             'master': master}
        self.dump_resources(net_res)

    def free_node_resources(self, cluster):
        net_res = None
        bridge_name = None
        master = -1
        for vm in cluster.vms:
            if not vm.is_on_node():
                continue

            if not self.name in vm.networks:
                continue

            if not net_res:
                net_res = self.load_resources()
                bridge_name = net_res['global']['bridge_name']
                master = net_res['global']['master']

            vm_label = self._vm_res_label(vm)

            # Remove the tap from the bridge
            tap_name = net_res[vm_label]['tap_name']
            ovs_del_port(tap_name, bridge_name)
            tun_delete_tap(tap_name)

        if bridge_name:
            ovs_del_bridge(bridge_name)

        if master == Config().batch.node_rank:
            # Free tunnel key
            try:
                self._ida.free_one(int(net_res['global']['tun_id']) - self._min_key)
            except PcoccError as e:
                raise NetworkSetupError('{0}: {1}'.format(
                    self.name,
                    str(e)
                ))

            # Cleanup keystore
            Config().batch.delete_dir(
                'cluster',
                self._get_net_key_path(''))

    def load_node_resources(self, cluster):
        net_res = None
        for vm in cluster.vms:
            if not vm.is_on_node():
                continue

            if not self.name in vm.networks:
                continue

            if not net_res:
                net_res = self.load_resources()

            vm_label = self._vm_res_label(vm)
            vm.add_eth_if(self.name,
                          net_res[vm_label]['tap_name'],
                          net_res[vm_label]['hwaddr'])

    def _cleanup_stray_bridges(self):
        # Look for remaining bridges to cleanup
        for bridge_id in find_used_dev_ids(self._bridge_prefix):
            logging.warning(
                'Deleting leftover bridge for {0} network'.format(self.name))
            # Delete the bridge
            bridge_name = dev_name_from_id(self._bridge_prefix,
                                           bridge_id)
            ovs_del_bridge(bridge_name)

    def _cleanup_stray_taps(self):
        # Look for remaining taps to cleanup
        for tap_id in find_used_dev_ids(self._tap_prefix):
            logging.warning(
                'Deleting leftover tap for {0} network'.format(self.name))

            # Delete the tap
            tap_name = dev_name_from_id(self._tap_prefix,
                                        tap_id)
            tun_delete_tap(tap_name)

    def _gen_vm_hwaddr(self, vm):
        hw_prefix = self._mac_prefix # Complete prefixes only
        prefix_len = len(hw_prefix.replace(':', ''))
        suffix_len = 12 - prefix_len
        hw_suffix = ("%x"%(vm.rank)).zfill(suffix_len)
        hw_suffix = ':'.join(
            hw_suffix[i:i+2] for i in xrange(0, len(hw_suffix), 2))

        return hw_prefix + ':' + hw_suffix

class VGenericPCI(VNetwork):
    def __init__(self, name, settings):
        super(VGenericPCI, self).__init__(name)
        self._type = "genericpci"
        self._device_addrs = settings["host-device-addrs"]
        self._host_driver = settings["host-driver"]

    def init_node(self):
        for dev_addr in self._device_addrs:
            pci_enable_driver(dev_addr, self._host_driver)
            pci_enable_driver(dev_addr, 'vfio-pci')

    def cleanup_node(self):
        deleted_devs = []
        bound_devices = pci_list_vfio_devices()
        for dev_addr in bound_devices:
            if dev_addr in self._device_addrs:
                pci_unbind_vfio(dev_addr, self._host_driver)
                deleted_devs += dev_addr

        if len(deleted_devs) > 0:
            logging.warning(
                'Deleted {0} leftover PCI devices of type {1}'.format(
                    len(deleted_devs), self.name))

    def alloc_node_resources(self, cluster):
        batch = Config().batch
        net_res = {}

        for vm in cluster.vms:
            if not vm.is_on_node():
                continue

            if not self.name in vm.networks:
                continue

            try:
                bound_devices = pci_list_vfio_devices()

                dev_addr = ""
                for dev_addr in self._device_addrs:
                    if dev_addr not in bound_devices:
                        break
                else:
                    raise NetworkSetupError('unable to find a free '
                                            'PCI device of type {1}'.format(self.name))


                pci_bind_vfio(dev_addr, batch.batchuser)
                vm_label = self._vm_res_label(vm)
                net_res[vm_label] = {'dev_addr': dev_addr}
            except Exception as e:
                self.dump_resources(net_res)
                raise

        self.dump_resources(net_res)

    def free_node_resources(self, cluster):
        net_res = None
        batch = Config().batch
        for vm in cluster.vms:
            if not vm.is_on_node():
                continue

            if not self.name in vm.networks:
                continue

            if not net_res:
                net_res = self.load_resources()

            vm_label = self._vm_res_label(vm)
            dev_addr = net_res[vm_label]['dev_addr']

            pci_unbind_vfio(dev_addr, self._host_driver)

    def load_node_resources(self, cluster):
        net_res = None
        for vm in cluster.vms:
            if not vm.is_on_node():
                continue

            if not self.name in vm.networks:
                continue

            if not net_res:
                net_res = self.load_resources()

            vm_label = self._vm_res_label(vm)
            vm.add_vfio_if(self.name,
                           net_res[vm_label]['dev_addr'])


class VNATNetwork(VNetwork):
    def __init__(self, name, settings):
        super(VNATNetwork, self).__init__(name)

        self._type = "nat"
        self._bridge_name = settings["bridge"]

        # Check nat bits >= vm bits
        self._nat_network = settings["nat-network"].split("/")[0]
        self._nat_network_bits = int(settings["nat-network"].split("/")[1])
        self._vm_network = settings["vm-network"].split("/")[0]
        self._vm_network_bits = int(settings["vm-network"].split("/")[1])

        # Remove (auto computed) vms first n ips, gw (host): last
        self._vm_network_gw = settings["vm-network-gw"]
        self._vm_ip = settings["vm-ip"]

        # Interface prefix (default to net name)
        self._tap_prefix = settings["tap-prefix"]

        self._mtu = int(settings["mtu"])

        # prefix
        self._vm_hwaddr = settings["vm-hwaddr"]
        self._bridge_hwaddr = settings["bridge-hwaddr"]

        # remove (one per cluster)
        self._dnsmasq_pid_filename = "/var/run/pcocc_dnsmasq.pid"

        # defaults to pcocc.domain_name
        self._domain_name = settings["domain-name"]
        self._dns_server = settings["dns-server"]
        self._ntp_server = settings["ntp-server"]

        # gateway type (none/per-host/per-cluster/external)

        # Add ip/port range filters
        if settings["allow-outbound"] == 'none':
            self._allow_outbound = False
        elif settings["allow-outbound"] == 'all':
            self._allow_outbound = True
        else:
            raise InvalidConfigurationError(
                '%s is not a valid value '
                'for allow-outbound' % settings["allow-outbound"])

        if "reverse-nat" in settings:
            self._vm_rnat_port = int(settings["reverse-nat"]["vm-port"])
            self._host_rnat_port_range = (
                int(settings["reverse-nat"]["min-host-port"]),
                int(settings["reverse-nat"]["max-host-port"]))

    def kill_dnsmasq(self):
        # Terminate dnsmasq
        if os.path.isfile(self._dnsmasq_pid_filename):
            with open(self._dnsmasq_pid_filename, 'r') as f:
                pid = f.read()
                try:
                    os.kill(int(pid), signal.SIGTERM)
                except (OSError ,ValueError):
                    pass
            os.remove(self._dnsmasq_pid_filename)

    def has_dnsmasq(self):
        if os.path.isfile(self._dnsmasq_pid_filename):
            with open(self._dnsmasq_pid_filename, 'r') as f:
                pid = f.read()
                try:
                    os.kill(int(pid), 0)
                    return True
                except (OSError, ValueError):
                    return False
        else:
            return False

    def init_node(self):
        if not ovs_bridge_exists(self._bridge_name):
            # Create nat bridge
            ovs_add_bridge(self._bridge_name, self._bridge_hwaddr)
            self.kill_dnsmasq()
        else:
            #Check bridge settings
            ovs_add_bridge(self._bridge_name, self._bridge_hwaddr)

        # Configure the bridge with the GW ip on the VM network
        ip_add_idemp(self._vm_network_gw,
                     self._vm_network_bits,
                     self._bridge_name)

        # Also give the bridge an IP on the NAT network with unique
        # IPs for each VM
        bridge_nat_ip = get_ip_on_network(self._nat_network, 1)
        ip_add_idemp(bridge_nat_ip,
                     self._vm_network_bits,
                     self._bridge_name)

        if not self.has_dnsmasq():
            # Start a dnsmasq server to answer DHCP requests
            dnsmasq_opts = ""
            if self._ntp_server:
                dnsmasq_opts+="--dhcp-option=option:ntp-server,{0} ".format(
                    self._ntp_server)

            if self._dns_server:
                dnsmasq_opts+="--dhcp-option=option:dns-server,{0} ".format(
                    self._dns_server)

            subprocess.check_call(
                shlex.split("/usr/sbin/dnsmasq --strict-order "
                            "--bind-interfaces "
                            "--pid-file=%s "
                            "--conf-file= --interface=%s "
                            "--except-interface=lo --leasefile-ro "
                            "--dhcp-lease-max=512 "
                            "--dhcp-no-override "
                            "--dhcp-host %s,%s "
                            "--dhcp-option=option:domain-name,%s "
                            "--dhcp-option=119,%s "
                            "%s"
                            "--dhcp-option=option:netmask,%s "
                            "--dhcp-option=option:router,%s "
                            "-F %s,static " %(
                        self._dnsmasq_pid_filename,
                        self._bridge_name,
                        self._vm_hwaddr,
                        self._vm_ip,
                        self._domain_name.split(',')[0],
                        self._domain_name,
                        dnsmasq_opts,
                        num_to_dotted_quad(make_mask(self._vm_network_bits)),
                        self._vm_network_gw,
                        self._vm_ip)))

        # Enable Routing for the bridge only
        subprocess.check_call("echo 1 > /proc/sys/net/ipv4/ip_forward",
                      shell=True)
        subprocess.check_call("iptables -P FORWARD DROP",
                      shell=True)

        ipt_append_rule_idemp("-d %s/%d -o %s -p tcp -m tcp --dport 22 "
                              "-m state --state NEW -j ACCEPT"
                              % (self._nat_network,
                                 self._vm_network_bits,
                                 self._bridge_name),
                              "FORWARD")

        ipt_append_rule_idemp("-d %s/%d -o %s "
                              "-m state --state RELATED,ESTABLISHED "
                              "-j ACCEPT"
                              % (self._nat_network,
                                 self._vm_network_bits,
                                 self._bridge_name),
                              "FORWARD")

        if self._allow_outbound:
            ipt_append_rule_idemp("-s %s/%d -i %s -j ACCEPT"
                                  % (self._nat_network,
                                     self._vm_network_bits,
                                     self._bridge_name),
                                  "FORWARD")
        else:
            ipt_append_rule_idemp("-s %s/%d -i %s "
                                  "-m state --state RELATED,ESTABLISHED -j ACCEPT"
                                  % (self._nat_network,
                                     self._vm_network_bits,
                                     self._bridge_name),
                                  "FORWARD")

        # Enable NAT to/from the bridge for unique vm adresses
        ipt_append_rule_idemp("-s %s/%d ! -d %s/%d -p tcp -j MASQUERADE "
                              "--to-ports 1024-65535"
                              % (self._nat_network,
                                 self._vm_network_bits,
                                 self._nat_network,
                                 self._vm_network_bits),
                              "POSTROUTING", "nat")

        ipt_append_rule_idemp("-s %s/%d ! -d %s/%d -p udp -j MASQUERADE "
                              "--to-ports 1024-65535" % (self._nat_network,
                                                         self._vm_network_bits,
                                                         self._nat_network,
                                                         self._vm_network_bits),
                              "POSTROUTING", "nat")

        ipt_append_rule_idemp("-s %s/%d ! -d %s/%d -j MASQUERADE"
                              % (self._nat_network,
                                 self._vm_network_bits,
                                 self._nat_network,
                                 self._vm_network_bits),
                              "POSTROUTING", "nat")

        # Deliver ARP requests from each port to the bridge and only
        # to the bridge
        ovs_add_flow(self._bridge_name,
                     0, 1000,
                     "idle_timeout=0,hard_timeout=0,"
                     "dl_type=0x0806,nw_dst=%s,actions=local"
                     % (self._vm_network_gw))

        # Flood ARP answers from the bridge to each port
        ovs_add_flow(self._bridge_name,
                     0, 1000,
                     "in_port=local,idle_timeout=0,hard_timeout=0,"
                     "dl_type=0x0806,nw_dst=%s,actions=flood"%(self._vm_ip))

        # Flood DHCP answers from the bridge to each port
        ovs_add_flow(self._bridge_name,
                     0, 0,
                     "idle_timeout=0,hard_timeout=0,"
                     "in_port=LOCAL,udp,tp_dst=68,actions=FLOOD")




    def cleanup_node(self):
        # Disable routing
        subprocess.check_call("echo 0 > /proc/sys/net/ipv4/ip_forward",
                      shell=True)
        subprocess.check_call("iptables -P FORWARD ACCEPT",
                      shell=True)

        # Remove bridge
        ovs_del_bridge(self._bridge_name)

        # Remove routing rules
        ipt_delete_rule_idemp("-d %s/%d -o %s -m state "
                              "--state RELATED,ESTABLISHED "
                              "-j ACCEPT"
                              % (self._nat_network,
                                 self._nat_network_bits,
                                 self._bridge_name),
                              "FORWARD")

        if self._allow_outbound:
            ipt_delete_rule_idemp("-s %s/%d -i %s -j ACCEPT"
                                  % (self._nat_network,
                                     self._vm_network_bits,
                                     self._bridge_name),
                                  "FORWARD")
        else:
            ipt_delete_rule_idemp("-s %s/%d -i %s "
                                  "-m state --state RELATED,ESTABLISHED "
                                  "-j ACCEPT"
                                  % (self._nat_network,
                                     self._vm_network_bits,
                                     self._bridge_name),
                                  "FORWARD")

        # Remove NAT rules
        ipt_delete_rule_idemp("-d %s/%d -o %s -p tcp -m tcp "
                              "--dport 22 -m state "
                              "--state NEW -j ACCEPT"
                              % (self._nat_network,
                                 self._nat_network_bits,
                                 self._bridge_name),
                              "FORWARD")

        ipt_delete_rule_idemp("-s %s/%d ! -d %s/%d -p tcp -j MASQUERADE "
                              "--to-ports 1024-65535"
                              % (self._nat_network,
                                 self._nat_network_bits,
                                 self._nat_network,
                                self._nat_network_bits),
                              "POSTROUTING", "nat")

        ipt_delete_rule_idemp("-s %s/%d ! -d %s/%d -p udp -j MASQUERADE "
                              "--to-ports 1024-65535"
                              % (self._nat_network,
                                 self._nat_network_bits,
                                 self._nat_network,
                                 self._nat_network_bits),
                              "POSTROUTING", "nat")

        ipt_delete_rule_idemp("-s %s/%d ! -d %s/%d -j MASQUERADE"
                              % (self._nat_network,
                                 self._nat_network_bits,
                                 self._nat_network,
                                 self._nat_network_bits),
                              "POSTROUTING", "nat")

        # Look for remaining taps to cleanup
        for tap_id in find_used_dev_ids(self._tap_prefix):
            logging.warning(
                'Deleting leftover tap for {0} network'.format(self.name))

            # Delete the tap
            tun_delete_tap(dev_name_from_id(self._tap_prefix,
                                            tap_id))


        self.kill_dnsmasq()

    def alloc_node_resources(self, cluster):
        batch = Config().batch

        net_res = {}
        for vm in cluster.vms:
            if not vm.is_on_node():
                continue

            if not self.name in vm.networks:
                continue

            vm_label = self._vm_res_label(vm)
            net_res[vm_label] = self._alloc_vm_res(vm)

        self.dump_resources(net_res)

    def free_node_resources(self, cluster):
        net_res = None
        for vm in cluster.vms:
            if not vm.is_on_node():
                continue

            if not self.name in vm.networks:
                continue

            if not net_res:
                net_res = self.load_resources()

            vm_label = self._vm_res_label(vm)
            self._cleanup_vm_res(net_res[vm_label])

    def load_node_resources(self, cluster):
        net_res = None
        for vm in cluster.vms:
            if not vm.is_on_node():
                continue

            if not self.name in vm.networks:
                continue

            if not net_res:
                net_res = self.load_resources()

            vm_label = self._vm_res_label(vm)

            if 'host_port' in net_res[vm_label]:
                vm.add_eth_if(self.name,
                              net_res[vm_label]['tap_name'],
                              net_res[vm_label]['hwaddr'],
                              net_res[vm_label]['host_port'])
            else:
                vm.add_eth_if(self.name,
                              net_res[vm_label]['tap_name'],
                              net_res[vm_label]['hwaddr'])


    def _alloc_vm_res(self, vm):
        # Allocate a local VM id unique on this node
        nat_id = find_free_dev_id(self._tap_prefix)

        # Define the VM tap name and unique IP based on the VM id
        tap_name = dev_name_from_id(self._tap_prefix, nat_id)
        vm_nat_ip = self._vm_ip_from_id(nat_id)

        # Create and enable the tap
        tun_create_tap(tap_name, Config().batch.batchuser)
        dev_enable(tap_name)

        # Connect it to the bridge
        vm_port_id = ovs_add_port(tap_name, self._bridge_name)

        # Rewrite outgoing packets with the VM unique IP
        ovs_add_flow(self._bridge_name,
                     0, 1000,
                     "in_port=%d,idle_timeout=0,hard_timeout=0,"
                     "dl_type=0x0800,nw_src=%s,actions=mod_nw_src:%s,local"
                     % (vm_port_id,
                        self._vm_ip,
                        vm_nat_ip))

        # Rewrite incoming packets with the VM real IP
        ovs_add_flow(self._bridge_name,
                     0, 1000,
                     "in_port=local,idle_timeout=0,hard_timeout=0,"
                     "dl_type=0x0800,nw_dst=%s,actions=mod_nw_dst:%s,output:%d"
                     % (vm_nat_ip,
                        self._vm_ip,
                        vm_port_id))

        # Handle DHCP requests from the VM locally
        ovs_add_flow(self._bridge_name,
                     0, 1000,
                     "in_port=%d,idle_timeout=0,hard_timeout=0,"
                     "udp,tp_dst=67,priority=0,actions=local"
                     % (vm_port_id))

        # Add a permanent ARP entry for the VM unique IP
        # so that its packets are injected in the bridge
        ip_arp_add(vm_nat_ip, self._vm_hwaddr, self._bridge_name)


        alloc_res = {'tap_name': tap_name,
                     'hwaddr': self._vm_hwaddr,
                     'nat_ip': vm_nat_ip}

        # Reverse NAT towards a VM port
        if hasattr(self, '_vm_rnat_port'):
            #TODO: how to better reserve and select a free port ?
            host_port = ( self._host_rnat_port_range[0] +
                          id_from_dev_name(self._tap_prefix, tap_name) )

            if host_port > self._host_rnat_port_range[1]:
                raise NetworkSetupError('Unable to find a free host port for '
                                        'reverse NAT')

            ipt_append_rule_idemp(
                "-d %s/32 -p tcp -m tcp --dport %s "
                "-j DNAT --to-destination %s:%d"
                % (resolve_host(socket.gethostname()),
                   host_port,
                   vm_nat_ip, self._vm_rnat_port),
                "PREROUTING", "nat")

            ipt_append_rule_idemp(
                "-d %s/32 -p tcp -m tcp --dport %s "
                "-j DNAT --to-destination %s:%d"
                % (resolve_host(socket.gethostname()),
                   host_port,
                   vm_nat_ip, self._vm_rnat_port),
                "OUTPUT", "nat")

            alloc_res['host_port'] =  host_port
            Config().batch.write_key(
                'cluster',
                'rnat/{0}/{1}'.format(vm.rank, self._vm_rnat_port),
                host_port
            )

        return alloc_res

    def _cleanup_vm_res(self, resources):
        tap_name = resources['tap_name']
        vm_nat_ip = resources['nat_ip']

        # Compute the port id on the bridge
        vm_port_id = ovs_get_port_id(tap_name, self._bridge_name)

        # Delete flows on the OVS bridge
        ovs_del_flows(self._bridge_name,
                      "table=0,in_port=%d,dl_type=0x0800,nw_src=%s" % (
                vm_port_id, self._vm_ip))
        ovs_del_flows(self._bridge_name,
                      "table=0,in_port=local,dl_type=0x0800,nw_dst=%s" % (
                vm_nat_ip))

        # Remove the tap from the bridge
        ovs_del_port(tap_name, self._bridge_name)

        # Delete the tap
        tun_delete_tap(tap_name)

        # Delete the permanent ARP entry
        ip_arp_del(vm_nat_ip, self._vm_hwaddr, self._bridge_name)

        # Delete the reverse NAT rule if needed
        if('host_port' in resources):
            host_port = int(resources['host_port'])
            ipt_delete_rule_idemp("-d %s/32 -p tcp -m tcp"
                                  " --dport %s -j DNAT "
                                  "--to-destination %s:%d"
                                  % (resolve_host(socket.gethostname()),
                                     host_port, vm_nat_ip, self._vm_rnat_port),
                                  "PREROUTING", "nat")

            ipt_delete_rule_idemp("-d %s/32 -p tcp -m tcp"
                                  " --dport %s -j DNAT "
                                  "--to-destination %s:%d"
                                  % (resolve_host(socket.gethostname()),
                                     host_port, vm_nat_ip, self._vm_rnat_port),
                                  "OUTPUT", "nat")


    def _vm_ip_from_id(self, nat_id):
        # First IP is for the bridge
        return get_ip_on_network(self._nat_network, nat_id + 2)

    @staticmethod
    def get_rnat_host_port(vm_rank, port):
        return Config().batch.read_key(
            'cluster',
            'rnat/{0}/{1}'.format(vm_rank, port),
            blocking=False)


# Schema to validate individual pkey entries in the key/value store
pkey_entry_schema = """
type: object
properties:
  vf_guids:
    type: array
    items:
      type: string
      pattern: "^0x[0-9a-zA-Z]{16}$"
  host_guids:
    type: array
    items:
      type: string
      pattern: "^0x[0-9a-zA-Z]{16}$"
required:
    - vf_guids
    - host_guids
"""

class VHostIBNetwork(VNetwork):
    def __init__(self, name, settings):
        super(VHostIBNetwork, self).__init__(name)

        self._type = "hostib"
        self._device_name = settings["host-device"]

    def init_node(self):
        # We can probably remove this once we get kernels with the
        # driver_override feature.  For now we need to use new_id but
        # this binds all unbound devices so we start by binding them
        # to pci-stub.
        vf_enable_driver(self._device_name, 'pci-stub')
        vf_enable_driver(self._device_name, 'vfio-pci')

    def cleanup_node(self):
        deleted_vfs = cleanup_all_vfs(self._device_name)
        if len(deleted_vfs) > 0:
            logging.warning(
                'Deleted {0} leftover VFs for {1} network'.format(
                    len(deleted_vfs), self.name))

    @property
    def _dev_vf_type(self):
        return device_vf_type(self._device_name)

    def _gen_guid_suffix(self):
        return ''.join(['%02x' % random.randint(0,0xff) for _ in xrange(6)])

    def alloc_node_resources(self, cluster):
        batch = Config().batch
        net_res = {}

        for vm in cluster.vms:
            if not vm.is_on_node():
                continue

            if not self.name in vm.networks:
                continue

            try:
                port_guid = os.environ['PCOCC_NET_{0}_PORT_GUID'.format(
                                        self.name.upper())]
            except KeyError:
                port_guid ='0xc1cc' + self._gen_guid_suffix()

            try:
                node_guid = os.environ['PCOCC_NET_{0}_NODE_GUID'.format(
                                        self.name.upper())]
            except KeyError:
                node_guid ='0xd1cc' + self._gen_guid_suffix()

            try:
                device_name = self._device_name
                vf_addr = find_free_vf(device_name)

                pci_bind_vfio(vf_addr, batch.batchuser)
                if (self._dev_vf_type == VFType.MLX4):
                    vf_allow_host_pkeys(device_name,
                                        vf_addr)
                else:
                    vf_set_guid(device_name, vf_addr,
                                port_guid,
                                node_guid)

                vm_label = self._vm_res_label(vm)
                net_res[vm_label] = {'vf_addr': vf_addr}
            except Exception as e:
                self.dump_resources(net_res)
                raise

        self.dump_resources(net_res)

    def _free_node_vfs(self, cluster):
        net_res = None
        batch = Config().batch
        for vm in cluster.vms:
            if not vm.is_on_node():
                continue

            if not self.name in vm.networks:
                continue

            if not net_res:
                net_res = self.load_resources()

            vm_label = self._vm_res_label(vm)

            device_name = self._device_name
            vf_addr = net_res[vm_label]['vf_addr']

            vf_unbind_vfio(vf_addr)
            if (self._dev_vf_type == VFType.MLX4):
                vf_clear_pkeys(device_name, vf_addr)
            else:
                vf_unset_guid(device_name, vf_addr)


    def free_node_resources(self, cluster):
        return self._free_node_vfs(cluster)

    def load_node_resources(self, cluster):
        net_res = None
        for vm in cluster.vms:
            if not vm.is_on_node():
                continue

            if not self.name in vm.networks:
                continue

            if not net_res:
                net_res = self.load_resources()

            vm_label = self._vm_res_label(vm)
            vm.add_vfio_if(self.name,
                           net_res[vm_label]['vf_addr'])

class VIBNetwork(VHostIBNetwork):
    def __init__(self, name, settings):
        super(VIBNetwork, self).__init__(name, settings)

        self._type = "ib"
        self._device_name = settings["host-device"]
        self._min_pkey   = int(settings["min-pkey"], 0)
        self._max_pkey   = int(settings["max-pkey"], 0)
        self._license_name = settings.get("license", None)
        self._opensm_partition_cfg = settings["opensm-partition-cfg"]
        self._opensm_partition_tpl = settings["opensm-partition-tpl"]
        self._opensm_daemon = settings["opensm-daemon"]

        self._ida = IDAllocator(self._get_type_key_path('key_alloc_state'),
                                self._max_pkey - self._min_pkey + 1)

    def get_license(self, cluster):
        if self._license_name:
            for vm in cluster.vms:
                if self.name in vm.networks:
                    return [self._license_name]

        return []

    def alloc_node_resources(self, cluster):
        batch = Config().batch
        net_res = {}

        # First pass, find out which Hosts/VMs need to be managed
        net_hosts = set()
        net_vms = []
        for vm in cluster.vms:
            if not self.name in vm.networks:
                continue
            net_hosts.add(vm.get_host_rank())
            if vm.is_on_node():
                net_vms.append(vm)

        # No VM on node, nothing to do
        if not net_vms:
            return

        # First host becomes master for setting up this network
        master = False
        if batch.node_rank == sorted(net_hosts)[0]:
            master = True

        # Master allocates a pkey and broadcasts to the others
        if master:
            logging.info("Node is master for IB network {0}".format(
                    self.name))
        try:
            pkey_index = self._ida.alloc_one(master, '{0}_pkey'.format(self.name))
        except PcoccError as e:
            raise NetworkSetupError('{0}: {1}'.format(
                    self.name,
                    str(e)
                    ))

        my_pkey = self._min_pkey + pkey_index
        logging.info("Using PKey 0x{0:04x} for network {1}".format(
            my_pkey,
            self.name))

        # Write guids needed for our host
        host_guid = get_phys_port_guid(self._device_name)
        batch.write_key(
            'cluster',
            self._get_net_key_path('guids/' + str(batch.node_rank)),
            host_guid)

        # Master waits until all hosts have written their guids
        # and updates opensm
        if master:
            logging.info("Collecting GUIDs from all hosts for {0}".format(
                    self.name))
            global_guids = batch.wait_child_count('cluster',
                                                  self._get_net_key_path('guids'),
                                                  len(net_hosts))
            sm_config = {}
            sm_config['host_guids'] = [ str(child.value) for child
                                       in global_guids.children ]
            sm_config['vf_guids'] = [ vm_get_guid(vm, my_pkey) for vm
                                      in cluster.vms
                                      if self.name in vm.networks ]

            logging.info("Requesting OpenSM update for {0}".format(
                    self.name))
            batch.write_key('global', 'opensm/pkeys/' + str(hex(my_pkey)),
                            sm_config)

        net_res['master'] = master
        net_res['pkey'] = my_pkey
        net_res['pkey_index'] = pkey_index

        # Setup VFs for our VMs
        for vm in net_vms:
            try:
                device_name = self._device_name
                vf_addr = find_free_vf(device_name)
                pci_bind_vfio(vf_addr, batch.batchuser)

                if (self._dev_vf_type == VFType.MLX4):
                    # We may have to retry if opensm is slow to propagate PKeys
                    for i in range(5):
                        try:
                            vf_set_pkey(device_name, vf_addr, my_pkey)
                            break
                        except NetworkSetupError:
                            if i == 4:
                                raise
                            logging.warning("PKey not yet ready, sleeping...")
                            time.sleep(1 + i*2)
                else:
                    vf_set_guid(device_name, vf_addr,
                                vm_get_guid(vm, my_pkey),
                                vm_get_node_guid(vm, my_pkey))

                vm_label = self._vm_res_label(vm)
                net_res[vm_label] = {'vf_addr': vf_addr}
            except Exception as e:
                self.dump_resources(net_res)
                raise

        self.dump_resources(net_res)

    def free_node_resources(self, cluster):
        net_res = None
        batch = Config().batch

        self._free_node_vfs(cluster)

        if net_res and net_res['master']:
            # Update opensm
            pkey_key =  'opensm/pkeys/' + str(hex(net_res['pkey']))
            batch.delete_key('global', pkey_key)

            # Free pkey
            try:
                self._ida.free_one(net_res['pkey_index'])
            except PcoccError as e:
                raise NetworkSetupError('{0}: {1}'.format(
                    self.name,
                    str(e)
                ))

            # Cleanup keystore
            batch.delete_dir(
                'cluster',
                self._get_net_key_path(''))

    def pkey_daemon(self):
        batch = Config().batch

        while True:
            pkeys = {}
            pkey_path = batch.get_key_path('global', 'opensm/pkeys')

            # Read config for all pkeys
            ret, last_index  = batch.read_dir_index('global', 'opensm/pkeys')
            while not ret:
                logging.warning("PKey path doesn't exist")
                ret, last_index  = batch.wait_key_index('global',
                                                        'opensm/pkeys',
                                                        last_index,
                                                        timeout=0)

            logging.info("PKey change detected: refreshing configuration")

            for child in ret.children:
                # Ignore directory key
                if child.key == pkey_path:
                    continue

                # Find keys matching a valid PKey value
                m = re.match(r'{0}/(0x\d\d\d\d)$'.format(pkey_path), child.key)
                if not m:
                    logging.warning("Invalid entry in PKey directory: " +
                                    child.key)
                    continue
                pkey = m.group(1)

                # Load configuration and validate against schema
                try:
                    config = yaml.safe_load(child.value)
                    jsonschema.validate(config,
                                        yaml.safe_load(pkey_entry_schema))
                    pkeys[pkey] = config
                except yaml.YAMLError as e:
                    logging.warning("Misconfigured PKey {0}: {1}".format(
                             pkey, e))
                    continue
                except jsonschema.ValidationError as e:
                    logging.warning("Misconfigured PKey {0}: {1}".format(
                            pkey, e))
                    continue

            tmp = tempfile.NamedTemporaryFile(delete=False)
            with open(self._opensm_partition_tpl) as f:
                lines = f.readlines()
                tmp.writelines(lines)

            tmp.write('\n')

            for pkey, config in pkeys.iteritems():
                partline = 'PK_{0}={0} , ipoib'.format(pkey)
                for vf_guids in chunks(config['vf_guids'], 128):
                    partline_vf = ', indx0 : ' + ', '.join(g + '=full'
                                                           for g in vf_guids)
                    tmp.write(partline + partline_vf + ' ; \n')

                partline += ': '

                for host_guids in chunks(config['host_guids'], 128):
                    tmp.write(partline +
                              ', '.join(g + '=full'
                                        for g in host_guids) +
                              ' ; \n')

            tmp.close()
            shutil.move(tmp.name, self._opensm_partition_cfg)
            os.chmod(self._opensm_partition_cfg,
                     stat.S_IRUSR | stat.S_IWUSR | stat.S_IROTH | stat.S_IRGRP)


            for proc in psutil.process_iter():
                if isinstance(proc.name, basestring):
                    procname = proc.name
                else:
                    procname = proc.name()

                if procname == self._opensm_daemon:
                    proc.send_signal(signal.SIGHUP)

            # Wait for next update
            batch.wait_key_index('global', 'opensm/pkeys', last_index,
                                 timeout=0)


def netns_decorate(func):
    def wrap_netns(*args, **kwargs):
        if 'netns' in kwargs:
            kwargs['exec_wrap'] = ['ip', 'netns', 'exec', kwargs['netns']]
        else:
            kwargs['exec_wrap'] = []
        return func(*args, **kwargs)
    return wrap_netns

def device_vf_type(device_name):
    if device_name[:4] == 'mlx4':
        return VFType.MLX4
    elif device_name[:4] == 'mlx5':
        return VFType.MLX5

    raise NetworkSetupError('Cannot determine VF type for device %s' % device_name)

def make_mask(num_bits):
    "return a mask of num_bits as a long integer"
    return ((2L<<num_bits-1) - 1) << (32 - num_bits)

def dotted_quad_to_num(ip):
    "convert decimal dotted quad string to long integer"
    return struct.unpack('!L', socket.inet_aton(ip))[0]

def num_to_dotted_quad(addr):
    "convert long int to dotted quad string"
    return socket.inet_ntoa(struct.pack('!L', addr))

def network_mask(ip, bits):
    "Convert a network address to a long integer"
    return dotted_quad_to_num(ip) & make_mask(bits)

def address_in_network(ip, net):
    "Is an address in a network"
    return ip & net == net

def mac_prefix_len(prefix):
    return len(prefix.replace(':', ''))

def mac_suffix_len(prefix):
    return 12 - mac_prefix_len(prefix)

def mac_suffix_count(prefix):
    return 16 ** mac_suffix_len(prefix)

def mac_gen_hwaddr(prefix, num):
    max_id = mac_suffix_count(prefix)
    if num < 0:
        num = max_id + num
    if num < 0 or num >= max_id:
        raise ValueError('Invalid id for this MAC prefix')
    suffix = ("%x"%(num)).zfill(mac_suffix_len(prefix))
    suffix = ':'.join(
        suffix[i:i+2] for i in xrange(0, len(suffix), 2))
    return prefix + ':' + suffix

def ovs_add_bridge(brname, hwaddr=None):
    cmd = ["ovs-vsctl", "--may-exist", "add-br", brname]
    if not (hwaddr is None):
        cmd += [ "--", "set", "bridge", brname,
                 "other-config:hwaddr={0}".format(hwaddr)]
    subprocess.check_call(cmd)
    # Drop the ovs default flow, only allow packets that we want
    # TODO: Is it possible to create an ovs bridge without this
    # rule ?
    ovs_del_flows(brname, "--strict priority=0")
    subprocess.check_call(["ip", "link", "set",  brname, "up"])

def ovs_del_bridge(brname):
    subprocess.check_call(["ovs-vsctl", "--if-exist", "del-br", brname])

def ovs_enable_bridge_stp(brname):
    subprocess.check_call(["ovs-vsctl", "set", "bridge", brname,
                           "stp_enable=true"])

def bridge_exists(brname):
    """ returns whether brname is a bridge (linux or ovs) """
    return (os.path.exists('/sys/devices/virtual/net/{0}/bridge/'.format(brname)) or
           ovs_bridge_exists(brname))

def ovs_bridge_exists(brname):
    match = re.search(r'Bridge %s' % (brname),
                  subprocess_check_output(["ovs-vsctl", "show"]))
    if match:
        return True
    else:
        return False

def ovs_create_group(brname, group_id):
    subprocess.check_call(["ovs-ofctl", "add-group", "-OOpenFlow13",
                           brname,
                           'group_id={0},type=all'.format(group_id)])

def ovs_set_group_members(brname, group_id, members):
    bucket=''
    for m in members:
        bucket += ',bucket=output:{0}'.format(m)

    subprocess.check_call(["ovs-ofctl", "mod-group", "-OOpenFlow13",
                           brname,
                           'group_id={0},type=all'.format(group_id) + bucket])

def ovs_add_flow(brname, table, priority, match, action=None, cookie=None):
    if action:
        action = "actions="+action

    if cookie:
        cookie = "cookie="+cookie

    flow = 'table={0}, priority={1}, {2}, {3}'.format(
        table, priority, match, action)
    subprocess.check_call(["ovs-ofctl", "add-flow", "-OOpenFlow13", brname, flow])

def ovs_del_flows(brname, flow):
    subprocess.check_call(["ovs-ofctl", "del-flows", brname] + flow.split())

def ovs_get_port_id(tapname, brname):
    match = re.search(r'(\d+)\(%s\)' % (tapname),
                  subprocess_check_output(["ovs-ofctl", "show", brname]))
    if match:
        return int(match.group(1))
    else:
        raise KeyError('{0} not found on {1}'.format(tapname, brname))

def ovs_add_port(tapname, brname):
    subprocess.check_call(["ovs-vsctl", "add-port", brname, tapname])
    return ovs_get_port_id(tapname, brname)

def ovs_del_port(tapname, brname):
    subprocess.check_call(["ovs-vsctl", "del-port", brname, tapname])

def ovs_add_tunnel(brname, tun_name, tun_type, host, tun_id):
    subprocess.check_call(["ovs-vsctl", "add-port", brname,
                       tun_name, "--", "set", "interface", tun_name,
                       "type=%s" % (tun_type),
                       "options:remote_ip=%s" % (
            resolve_host(host)), "options:key=%s" % (tun_id)])
    return ovs_get_port_id(tun_name, brname)

def ipt_append_rule(rule, chain, table = None):
    if table:
        table_args = ["-t", table]
    else:
        table_args = []

    subprocess.check_call(["iptables"] + table_args + ["-A",
                                                       chain] +
                          rule.split())

def ipt_rule_exists(rule, chain, table = None):
    if table:
        table_args = ["-t", table]
    else:
        table_args = []

    try:
        subprocess.check_call(["iptables"] + table_args + ["-C",
                                                           chain] +
                              rule.split(), stderr=open(os.devnull))
        return True
    except subprocess.CalledProcessError as err:
        return False

def ipt_append_rule_idemp(rule, chain, table = None):
    if not ipt_rule_exists(rule, chain, table):
        ipt_append_rule(rule, chain, table)

def ipt_delete_rule(rule, chain, table = None):
    if table:
        table_args = ["-t", table]
    else:
        table_args = []

    subprocess.check_call(["iptables"] + table_args + ["-D",
                                                       chain] +
                          rule.split())

def ipt_delete_rule_idemp(rule, chain, table = None):
    if ipt_rule_exists(rule, chain, table):
        ipt_delete_rule(rule, chain, table)

def ipt_flush_table(table = None):
    if table:
        table_args = ["-t", table]
    else:
        table_args = []

    subprocess.check_call(["iptables"] + table_args + ["-F"])


def static_var(varname, value):
    """Used as a decorator to provide the equivalent of a static variable"""
    def decorate(func):
        setattr(func, varname, value)
        return func
    return decorate

@static_var("ipversion", 0)
def ip_has_tuntap():
    """Returns True if the iproute tool supports the tuntap command"""
    if ip_has_tuntap.ipversion==0:
        version_string = subprocess_check_output(['ip', '-V'])
        match = re.search(r'iproute2-ss(\d+)', version_string)
        ip_has_tuntap.ipversion = int(match.group(1))

    return ip_has_tuntap.ipversion >= 100519

def bridge_add_port(tapname, bridgename):
    subprocess.check_call(["ip", "link", "set", tapname, "master",
                           bridgename])

@netns_decorate
def veth_delete(name, **kwargs):
    subprocess.check_call(kwargs['exec_wrap'] + ["ip", "link", "del", name],
                          stdout=open(os.devnull))

def veth_create_pair(name1, name2):
    subprocess.check_call(["ip", "link", "add", name1, "type", "veth",
                           "peer", "name", name2], stdout=open(os.devnull))

def tun_create_tap(name, user):
    if ip_has_tuntap():
        subprocess.check_call(["ip", "tuntap", "add", name, "mode", "tap",
                               "user", user], stdout=open(os.devnull))
    else:
        subprocess.check_call(["tunctl", "-u", user, "-t", name],
                              stdout=open(os.devnull))

def tun_delete_tap(name):
    if ip_has_tuntap():
        subprocess.check_call(["ip", "tuntap", "del", name, "mode", "tap"])
    else:
        subprocess.check_call(["tunctl", "-d", name])

@netns_decorate
def dev_enable(name, **kwargs):
    subprocess.check_call(kwargs['exec_wrap'] +
                          ["ip", "link", "set", name, "up"])

def dev_set_hwaddr(name, hwaddr):
    subprocess.check_call(["ip", "link", "set", name, "address", hwaddr])

def dev_set_netns(name, netns):
    subprocess.check_call(["ip", "link", "set", name, "netns", netns])

def netns_create(name):
    subprocess.check_call(["ip", "netns", "add", name])

def netns_delete(name):
    subprocess.check_call(["ip", "netns", "delete", name])

@netns_decorate
def ip_set_mtu(dev, mtu, **kwargs):
    subprocess.check_call(kwargs['exec_wrap'] +
                          ["ip", "link", "set", dev, "mtu", "%d" % (mtu)])


@netns_decorate
def ip_route_add(networkbits, gw, **kwargs):
    subprocess_check_output(kwargs['exec_wrap'] +
                            ["ip", "route", "add", networkbits, "via", gw],
                            stderr=subprocess.STDOUT)

@netns_decorate
def ip_add(ip, bits, dev, **kwargs):
    subprocess_check_output(kwargs['exec_wrap'] +
                            ["ip", "addr", "add",
                             "%s/%d" % (ip, bits), "broadcast",
                             get_ip_on_network(
                num_to_dotted_quad(network_mask(ip, bits)),
                2**(32-bits) - 1), "dev", dev],
                            stderr=subprocess.STDOUT)

def ip_add_idemp(ip, bits, dev, **kwargs):
    try:
        ip_add(ip, bits, dev, **kwargs)
    except subprocess.CalledProcessError as err:
        if err.output != "RTNETLINK answers: File exists\n":
            raise

def ip_arp_add(ip, hwaddr, dev):
    subprocess.check_call(["ip", "neigh", "replace", ip, "lladdr", hwaddr,
                       "nud", "permanent", "dev", dev])

def ip_arp_del(ip, hwaddr, dev):
    subprocess.check_call(["ip", "neigh", "del", ip, "lladdr", hwaddr,
                       "nud", "permanent", "dev", dev])

def get_ip_on_network(netaddr, offset):
    return num_to_dotted_quad(dotted_quad_to_num(netaddr) + offset)


def resolve_host(host):
    data = socket.gethostbyname_ex(host)
    return data[2][0]


def dev_name_from_id(prefix, dev_id):
    return "%s%d" % (prefix, dev_id)

def id_from_dev_name(prefix, devname):
    assert(prefix)

    match = re.match(r"%s(\d+)" % (prefix), devname)
    if match:
        return int(match.group(1))
    else:
        return -1

def find_free_dev_id(prefix):
    used_ids = find_used_dev_ids(prefix)

    for pos, nat_id in enumerate(sorted(used_ids)):
        if (pos < nat_id):
            return pos

    return len(used_ids)

def find_used_dev_ids(prefix):
    return [  id_from_dev_name(prefix, devname)
              for devname in os.listdir("/sys/devices/virtual/net")
              if id_from_dev_name(prefix, devname) != -1 ]

def find_free_dev_name(prefix):
    dev_id = find_free_dev_id(prefix)
    return dev_name_from_id(prefix, dev_id)

class VFType:
    MLX4 = 1
    MLX5 = 2


def pci_enable_driver(dev_addr, driver_name):
    device_path = os.path.join("/sys/bus/pci/devices/", dev_addr)
    driver_path = os.path.join("/sys/bus/pci/drivers", driver_name, 'new_id')

    with open(os.path.join(device_path, 'vendor'), 'r') as f:
        vendor_id=f.read()

    with open(os.path.join(device_path, 'device'), 'r') as f:
        device_id=f.read()

    with open(driver_path, 'w') as f:
        f.write('%s %s' % (vendor_id, device_id))

def pci_list_vfio_devices():
    return os.listdir("/sys/bus/pci/drivers/vfio-pci")

def vf_enable_driver(device_name, driver_name):
    device_path = "/sys/class/infiniband/%s/device/virtfn0" % (device_name)
    dev_addr = os.path.basename(os.readlink(device_path))
    pci_enable_driver(dev_addr, driver_name)

def find_free_vf(device_name):
    return _perform_on_vfs(device_name, 'find')

def cleanup_all_vfs(device_name):
    return _perform_on_vfs(device_name, 'cleanup')

def _perform_on_vfs(device_name, action, *args):
    device_path = "/sys/class/infiniband/%s/device" % (device_name)
    bound_devices = pci_list_vfio_devices()

    vf_list = []
    for virtfn in os.listdir(device_path):
        m = re.match(r'virtfn(\d+)', virtfn)

        if not re.match(r'virtfn(\d+)', virtfn):
            continue

        vf_id = m.group(1)

        vf_addr = os.path.basename(os.readlink(
            os.path.join(device_path, virtfn)))

        if action == 'find' and vf_addr not in bound_devices:
            return vf_addr
        elif action == 'cleanup' and vf_addr in bound_devices:
            pci_unbind_vfio(vf_addr)
            vf_list.append(vf_addr)
        elif action == 'getid' and vf_addr == args[0]:
            return int(vf_id)


    if action == 'find':
        raise NetworkSetupError('unable to find a free '
                                'VF for device %s' % device_name)
    elif action=='cleanup':
        return vf_list

def pci_find_iommu_group(dev_addr):
    iommu_group = '/sys/bus/pci/drivers/vfio-pci/%s/iommu_group' % dev_addr
    iommu_group = os.path.basename(os.readlink(iommu_group))

    return iommu_group

def pci_bind_vfio(dev_addr, batch_user):
    with open('/sys/bus/pci/devices/{0}/driver/unbind'.format(dev_addr), 'w') as f:
        f.write(dev_addr)

    with open('/sys/bus/pci/drivers/vfio-pci/bind', 'w') as f:
        f.write(dev_addr)

    iommu_group = pci_find_iommu_group(dev_addr)

    uid = pwd.getpwnam(batch_user).pw_uid
    # FIXME: This seems to be required to prevent a race
    # between char device creation and chown
    time.sleep(0.1)
    os.chown(os.path.join('/dev/vfio/', iommu_group), uid, -1)

def pci_unbind_vfio(dev_addr, host_driver='pci-stub'):
    with open('/sys/bus/pci/drivers/vfio-pci/unbind', 'w') as f:
        f.write(dev_addr)

    with open('/sys/bus/pci/drivers/{0}/bind'.format(host_driver), 'w') as f:
        f.write(dev_addr)

def find_pkey_idx(device_name, pkey_value):
    pkey_idx_path = "/sys/class/infiniband/%s/ports/1/pkeys" % (
        device_name)

    for pkey_idx in os.listdir(pkey_idx_path):
        this_pkey_idx_path=os.path.join(pkey_idx_path, pkey_idx)
        with open(this_pkey_idx_path) as f:
            try:
                this_pkey_value = int(f.read().strip(), 0)
            except ValueError:
                continue

            if this_pkey_value & 0x7fff == pkey_value & 0x7fff:
                return pkey_idx

    raise NetworkSetupError('pkey %s not found on device %s' % (
        hex(pkey_value), device_name))

def vf_allow_host_pkeys(device_name, vf_addr):
    device_path = "/sys/class/infiniband/{0}".format(device_name)
    num_ports = len(os.listdir(os.path.join(device_path, "ports")))

    for port in xrange(1, num_ports+1):
        pkeys_path = os.path.join(device_path, "ports", str(port),
                                  "pkeys")
        pkey_idx_path = os.path.join(device_path, "iov", vf_addr,
                                     "ports", str(port), "pkey_idx")

        idx = 0
        for pkey_idx in os.listdir(pkeys_path):
            p = os.path.join(pkeys_path, pkey_idx)
            with open(p) as f:
                try:
                    this_pkey_value = int(f.read().strip(), 0)
                except ValueError:
                    continue

                if this_pkey_value:
                    with open(os.path.join(pkey_idx_path, str(idx)), 'w') as f:
                        f.write(pkey_idx)
                    idx+=1

def vf_clear_pkeys(device_name, vf_addr):
    device_path = '/sys/class/infiniband/{0}'.format(device_name)
    num_ports = len(os.listdir(os.path.join(device_path, 'ports')))

    for port in xrange(1, num_ports+1):
        pkey_idx_path = os.path.join(device_path, 'iov', vf_addr,
                                     'ports', str(port), 'pkey_idx')

        for pkey_idx in os.listdir(pkey_idx_path):
            this_pkey_idx_path = os.path.join(pkey_idx_path, pkey_idx)
            with open(this_pkey_idx_path, 'w') as f:
                f.write('none')

def vf_set_pkey(device_name, vf_addr, pkey_value):
    pkey_idx_path = "/sys/class/infiniband/%s/iov/%s/ports/1/pkey_idx" % (
        device_name, vf_addr)

    user_pkey_idx = find_pkey_idx(device_name, pkey_value)
    with open(os.path.join(pkey_idx_path, '0'), 'w') as f:
        f.write(user_pkey_idx)

    def_pkey_idx = find_pkey_idx(device_name, 0xffff)
    with open(os.path.join(pkey_idx_path, '1'), 'w') as f:
        f.write(def_pkey_idx)

def vf_unset_pkey(device_name, vf_addr):
    pkey_idx_path = "/sys/class/infiniband/%s/iov/%s/ports/1/pkey_idx" % (
        device_name, vf_addr)

    with open(os.path.join(pkey_idx_path, '0'), 'w') as f:
        f.write('none')

    with open(os.path.join(pkey_idx_path, '1'), 'w') as f:
        f.write('none')

def vf_id_from_addr(device, vf_addr):
    return _perform_on_vfs(device, 'getid', vf_addr)

def vf_unset_guid(device_name, vf_addr):
    vf_id = vf_id_from_addr(device_name, vf_addr)
    sriov_path = '/sys/class/infiniband/{0}/device/sriov/{1}'.format(device_name, vf_id)

    with open(os.path.join(sriov_path, 'policy'), 'w') as f:
        f.write('Down\n')

def vf_set_guid(device_name, vf_addr, guid, node_guid):
    vf_id = vf_id_from_addr(device_name, vf_addr)
    sriov_path = '/sys/class/infiniband/{0}/device/sriov/{1}'.format(device_name, vf_id)

    with open(os.path.join(sriov_path, 'policy'), 'w') as f:
        f.write('Follow\n')

    with open(os.path.join(sriov_path, 'node'), 'w') as f:
        f.write(guid_hex_to_col(node_guid))

    with open(os.path.join(sriov_path, 'port'), 'w') as f:
        f.write(guid_hex_to_col(guid))

def vm_get_guid(vm, pkey_id):
    pkey_high = pkey_id / 0x100
    pkey_low = pkey_id % 0x100
    vm_high = vm.rank / 0x100
    vm_low = vm.rank % 0x100

    return '0xc0cc{0:02x}{1:02x}00{2:02x}{3:02x}00'.format(pkey_high, pkey_low,
                                                        vm_high, vm_low)

def vm_get_node_guid(vm, pkey_id):
    pkey_high = pkey_id / 0x100
    pkey_low = pkey_id % 0x100
    vm_high = vm.rank / 0x100
    vm_low = vm.rank % 0x100

    return '0xd0cc{0:02x}{1:02x}00{2:02x}{3:02x}00'.format(pkey_high, pkey_low,
                                                            vm_high, vm_low)

def get_phys_port_guid(device_name):
    return subprocess_check_output(['ibstat', '-p',
                                    device_name]).splitlines()[0]

def guid_hex_to_col(guid):
    res = ':'.join(guid[c:c+2] for c in xrange(2, len(guid), 2))
    return res

def chunks(array, n):
    """Yield successive n-sized chunks from array."""
    for i in range(0, len(array), n):
        yield array[i:i+n]

# At the end to prevent circular includes
import pcocc.EthNetwork
