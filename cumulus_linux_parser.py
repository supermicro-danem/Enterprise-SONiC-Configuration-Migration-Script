#!/usr/bin/env python3
"""
Cumulus Linux Configuration Parser

This module provides parsing logic for Cumulus Linux NCLU configurations
and converts them to Enterprise SONiC format.
"""

import re
from typing import Dict, List, Optional
from base_migrator import (
    BaseMigrator, VlanConfig, PortChannelConfig, PhysicalInterfaceConfig,
    LoopbackConfig, StaticRouteConfig, RadiusConfig
)


class CumulusLinuxMigrator(BaseMigrator):
    """Migrator for Cumulus Linux NCLU configurations"""
    
    def __init__(self):
        """Initialize the Cumulus Linux migrator"""
        super().__init__()
        self.current_bond: Optional[str] = None
        self.current_vlan_svi: Optional[str] = None
        self.bond_slaves: Dict[str, List[str]] = {}  # bond_name -> list of interfaces
        self.vrr_configs: Dict[str, Dict] = {}  # vlan_id -> {mac, vip}
        self.ntp_servers: List[str] = []
    
    def parse_config(self, config: str):
        """Parse Cumulus NCLU configuration into structured data"""
        self.reset_state()
        self.current_bond = None
        self.current_vlan_svi = None
        self.bond_slaves = {}
        self.vrr_configs = {}
        self.ntp_servers = []
        self.has_explicit_management_config = False  # NCLU has no net add for OOB management
        
        lines = config.split('\n')
        
        for line_num, line in enumerate(lines, start=1):
            original_line = line
            line = line.strip()
            self.current_line_number = line_num
            
            # Skip empty lines and comments
            if not line or line.startswith('#'):
                continue
            
            # Only process 'net add' commands
            if not line.startswith('net add'):
                continue
            
            self._parse_line(line, line_num)
        
        self._normalize_bgp_for_sonic()
    
    def _parse_line(self, line: str, line_num: int):
        """Parse individual NCLU command line"""
        # Remove 'net add' prefix
        cmd = line.replace('net add', '').strip()
        parts = cmd.split()
        
        if not parts:
            return
        
        # Hostname
        if parts[0] == 'hostname':
            self._parse_hostname(cmd)
        
        # Loopback
        elif parts[0] == 'loopback':
            self._parse_loopback(cmd)
        
        # DNS
        elif parts[0] == 'dns' and len(parts) > 1 and parts[1] == 'nameserver':
            self._parse_dns(cmd)
        
        # NTP
        elif parts[0] == 'time' and len(parts) > 1 and parts[1] == 'ntp':
            self._parse_ntp(cmd)
        
        # Syslog
        elif parts[0] == 'syslog':
            self._parse_syslog(cmd)
        
        # SNMP
        elif parts[0] == 'snmp-server':
            self._parse_snmp(cmd)
        
        # Bridge
        elif parts[0] == 'bridge':
            self._parse_bridge(cmd)
        
        # Interface
        elif parts[0] == 'interface':
            self._parse_interface(cmd)
        
        # Bond
        elif parts[0] == 'bond':
            self._parse_bond(cmd)
        
        # VLAN
        elif parts[0] == 'vlan':
            self._parse_vlan(cmd)
        
        # BGP
        elif parts[0] == 'bgp':
            self._parse_bgp(cmd)
        
        # Routing (static routes)
        elif parts[0] == 'routing' and len(parts) > 1 and parts[1] == 'route':
            self._parse_static_route(cmd)
        
        # Any other net add command is unsupported
        else:
            self.log_unsupported_feature(line, self.UNSUPPORTED_MSG)
    
    def convert_interface_name(self, interface: str) -> str:
        """Convert Cumulus interface name (swpX) to SONiC interface name (Eth 1/X)"""
        # Handle swp interfaces: swp1 -> Eth 1/1, swp2 -> Eth 1/2, etc.
        if interface.startswith('swp'):
            # Extract number from swp<N>
            match = re.search(r'swp(\d+)', interface)
            if match:
                port_num = match.group(1)
                return f'Eth 1/{port_num}'
        
        # Handle loopback
        if interface == 'lo':
            return 'Loopback0'
        
        # Return as-is if not recognized
        return interface
    
    def _parse_hostname(self, cmd: str):
        """Parse hostname command: net add hostname <name>"""
        parts = cmd.split()
        if len(parts) >= 2:
            self.hostname = parts[1]
    
    def _parse_loopback(self, cmd: str):
        """Parse loopback command: net add loopback lo ip address <ip>/<cidr>"""
        # net add loopback lo ip address 10.0.0.1/32
        parts = cmd.split()
        if 'ip' in parts and 'address' in parts:
            ip_index = parts.index('address')
            if ip_index + 1 < len(parts):
                ip_cidr = parts[ip_index + 1]
                if '/' in ip_cidr:
                    ip, cidr = ip_cidr.split('/')
                    loopback = LoopbackConfig(interface='Loopback0', ip_address=ip)
                    loopback.subnet_mask = self._cidr_to_mask(int(cidr))
                    if 'alias' in parts:
                        alias_index = parts.index('alias')
                        if alias_index + 1 < len(parts):
                            loopback.description = ' '.join(parts[alias_index + 1:])
                    self.loopbacks['0'] = loopback
    
    def _parse_dns(self, cmd: str):
        """Parse DNS nameserver command: net add dns nameserver ipv4 <ip>"""
        parts = cmd.split()
        if 'ipv4' in parts:
            ipv4_index = parts.index('ipv4')
            if ipv4_index + 1 < len(parts):
                dns_server = parts[ipv4_index + 1]
                if 'name_servers' not in self.global_settings:
                    self.global_settings['name_servers'] = []
                self.global_settings['name_servers'].append({'ip': dns_server, 'vrf': None})
    
    def _parse_ntp(self, cmd: str):
        """Parse NTP server command: net add time ntp server <ip> [iburst]"""
        # net add time ntp server 10.1.1.1 iburst
        parts = cmd.split()
        if 'server' in parts:
            server_index = parts.index('server')
            if server_index + 1 < len(parts):
                ntp_server = parts[server_index + 1]
                self.ntp_servers.append(ntp_server)
                if 'ntp_servers' not in self.global_settings:
                    self.global_settings['ntp_servers'] = []
                self.global_settings['ntp_servers'].append(ntp_server)
                if 'ntp_server' not in self.global_settings:
                    self.global_settings['ntp_server'] = ntp_server
    
    def _parse_syslog(self, cmd: str):
        """Parse syslog host command: net add syslog host ipv4 <ip> port udp <port>"""
        parts = cmd.split()
        if 'host' in parts and 'ipv4' in parts:
            ipv4_index = parts.index('ipv4')
            if ipv4_index + 1 < len(parts):
                syslog_server = parts[ipv4_index + 1]
                self.syslog_config.servers.append(syslog_server)
    
    def _parse_snmp(self, cmd: str):
        """Parse SNMP server commands"""
        parts = cmd.split()
        
        # net add snmp-server readonly-community <name> access any
        if 'readonly-community' in parts:
            ro_index = parts.index('readonly-community')
            if ro_index + 1 < len(parts):
                community_name = parts[ro_index + 1]
                self.snmp_config.communities[community_name] = 'ro'
        
        # net add snmp-server readwrite-community <name> access any
        elif 'readwrite-community' in parts:
            rw_index = parts.index('readwrite-community')
            if rw_index + 1 < len(parts):
                community_name = parts[rw_index + 1]
                self.snmp_config.communities[community_name] = 'rw'
    
    def _parse_bridge(self, cmd: str):
        """Parse bridge commands: net add bridge bridge <option>"""
        parts = cmd.split()
        
        # net add bridge bridge vlan-aware
        if 'vlan-aware' in parts:
            # VLAN-aware mode - implicit in SONiC
            pass
        
        # net add bridge bridge vids <list>
        elif 'vids' in parts:
            vids_index = parts.index('vids')
            if vids_index + 1 < len(parts):
                vids_str = parts[vids_index + 1]
                # Parse comma-separated VLAN IDs
                vlan_ids = [v.strip() for v in vids_str.split(',')]
                self.global_settings['bridge_vids'] = vlan_ids
                for vlan_id in vlan_ids:
                    if vlan_id not in self.vlans:
                        self.vlans[vlan_id] = VlanConfig(vlan_id=vlan_id)
        
        # net add bridge bridge pvid <vlan>
        elif 'pvid' in parts:
            pvid_index = parts.index('pvid')
            if pvid_index + 1 < len(parts):
                # Default native VLAN - store in global settings
                self.global_settings['bridge_pvid'] = parts[pvid_index + 1]
    
    def _parse_interface(self, cmd: str):
        """Parse interface commands: net add interface <name> <option>"""
        parts = cmd.split()
        if len(parts) < 2:
            return
        
        interface_name = parts[1]
        
        # Handle peerlink.4094 (MLAG control interface)
        if interface_name == 'peerlink.4094':
            self._parse_clag_interface(cmd)
            return
        
        # Convert interface name
        sonic_intf = self.convert_interface_name(interface_name)
        
        # Create interface if not exists
        if interface_name not in self.physical_interfaces:
            self.physical_interfaces[interface_name] = PhysicalInterfaceConfig(interface=interface_name)
        
        intf = self.physical_interfaces[interface_name]
        
        # net add interface swp1 alias <description>
        if 'alias' in parts:
            alias_index = parts.index('alias')
            if alias_index + 1 < len(parts):
                intf.description = ' '.join(parts[alias_index + 1:])
        
        # net add interface swp1 mtu <mtu>
        elif 'mtu' in parts:
            mtu_index = parts.index('mtu')
            if mtu_index + 1 < len(parts):
                intf.mtu = int(parts[mtu_index + 1])
                intf.mtu_configured = True
        
        # net add interface swp1 link speed <speed>
        elif 'link' in parts and 'speed' in parts:
            speed_index = parts.index('speed')
            if speed_index + 1 < len(parts):
                speed_val = parts[speed_index + 1]
                # Convert to numeric: 1000 -> 1000, 10000 -> 10000
                intf.speed = speed_val
        
        # net add interface swp1 bridge access <vlan>
        elif 'bridge' in parts and 'access' in parts:
            access_index = parts.index('access')
            if access_index + 1 < len(parts):
                vlan_id = parts[access_index + 1]
                intf.switchport_mode = 'access'
                intf.access_vlan = vlan_id
        
        # net add interface swp1 bridge trunk vlans <list>
        elif 'bridge' in parts and 'trunk' in parts and 'vlans' in parts:
            vlans_index = parts.index('vlans')
            if vlans_index + 1 < len(parts):
                vlans_str = parts[vlans_index + 1]
                vlans = [v.strip() for v in vlans_str.split(',')]
                intf.switchport_mode = 'trunk'
                intf.allowed_vlans = vlans
        
        # net add interface swp1 bridge pvid <vlan>
        elif 'bridge' in parts and 'pvid' in parts:
            pvid_index = parts.index('pvid')
            if pvid_index + 1 < len(parts):
                native_vlan = parts[pvid_index + 1]
                # Per-port native VLAN → switchport trunk native vlan
                intf.native_vlan = native_vlan
                if intf.switchport_mode != 'access':
                    intf.switchport_mode = 'trunk'
    
    def _parse_bond(self, cmd: str):
        """Parse bond commands: net add bond <name> <option>"""
        parts = cmd.split()
        if len(parts) < 2:
            return
        
        bond_name = parts[1]
        
        # Map bond to port-channel
        # bond1 -> PortChannel 1, bond20 -> PortChannel 20, peerlink -> special handling
        if bond_name == 'peerlink':
            # peerlink is the MLAG peer-link bond
            po_id = '10'  # Default, or extract from MLAG config if available
            if 'peer_link_po' in self.mlag_config:
                po_id = self.mlag_config['peer_link_po']
            else:
                # Use a default or extract from context
                self.mlag_config['peer_link_po'] = po_id
        else:
            # Extract number from bond<N>
            match = re.search(r'bond(\d+)', bond_name)
            if match:
                po_id = match.group(1)
            else:
                return  # Unknown bond format
        
        # Create port-channel if not exists
        if po_id not in self.port_channels:
            self.port_channels[po_id] = PortChannelConfig(po_id=po_id)
        
        po = self.port_channels[po_id]
        
        # Peer-link: ensure trunk with bridge VLAN set (no explicit "bond peerlink bridge trunk vlans" in NCLU)
        if bond_name == 'peerlink':
            po.mode = 'trunk'
            if not po.allowed_vlans:
                po.allowed_vlans = list(self.global_settings.get('bridge_vids', [])) or list(self.vlans.keys())
        
        # net add bond bond1 alias <description>
        if 'alias' in parts:
            alias_index = parts.index('alias')
            if alias_index + 1 < len(parts):
                po.description = ' '.join(parts[alias_index + 1:])
        
        # net add bond bond1 bond slaves <list>
        elif 'bond' in parts and 'slaves' in parts:
            slaves_index = parts.index('slaves')
            if slaves_index + 1 < len(parts):
                slaves_str = parts[slaves_index + 1]
                slaves = [s.strip() for s in slaves_str.split(',')]
                self.bond_slaves[bond_name] = slaves
                # Set channel-group on physical interfaces
                for slave in slaves:
                    if slave not in self.physical_interfaces:
                        self.physical_interfaces[slave] = PhysicalInterfaceConfig(interface=slave)
                    self.physical_interfaces[slave].channel_group = po_id
        
        # net add bond bond1 mtu <mtu>
        elif 'mtu' in parts:
            mtu_index = parts.index('mtu')
            if mtu_index + 1 < len(parts):
                po.mtu = int(parts[mtu_index + 1])
                po.mtu_configured = True
        
        # net add bond bond1 bond lacp-rate <fast|slow>
        elif 'bond' in parts and 'lacp-rate' in parts:
            # LACP rate - note for SONiC but not directly translatable
            pass
        
        # net add bond bond1 bridge access <vlan>
        elif 'bridge' in parts and 'access' in parts:
            access_index = parts.index('access')
            if access_index + 1 < len(parts):
                vlan_id = parts[access_index + 1]
                po.mode = 'access'
                po.access_vlan = vlan_id
        
        # net add bond bond1 bridge trunk vlans <list>
        elif 'bridge' in parts and 'trunk' in parts and 'vlans' in parts:
            vlans_index = parts.index('vlans')
            if vlans_index + 1 < len(parts):
                vlans_str = parts[vlans_index + 1]
                vlans = [v.strip() for v in vlans_str.split(',')]
                po.mode = 'trunk'
                po.allowed_vlans = vlans
        
        # net add bond bond1 clag id <id>
        elif 'clag' in parts and 'id' in parts:
            clag_id_index = parts.index('id')
            if clag_id_index + 1 < len(parts):
                clag_id = parts[clag_id_index + 1]
                po.mlag_enabled = True
                # Store clag id - will be used for mclag domain
                if 'clag_id' not in self.mlag_config:
                    self.mlag_config['clag_id'] = clag_id
                # Also store domain_id if not set
                if 'domain_id' not in self.mlag_config:
                    self.mlag_config['domain_id'] = clag_id
    
    def _parse_clag_interface(self, cmd: str):
        """Parse clag configuration on peerlink.4094: net add interface peerlink.4094 clag <option>"""
        parts = cmd.split()
        
        # net add interface peerlink.4094 clag backup-ip <ip>
        if 'clag' in parts and 'backup-ip' in parts:
            backup_ip_index = parts.index('backup-ip')
            if backup_ip_index + 1 < len(parts):
                peer_ip = parts[backup_ip_index + 1]
                self.mlag_config['peer_address'] = peer_ip
        
        # net add interface peerlink.4094 clag peer-ip linklocal
        elif 'clag' in parts and 'peer-ip' in parts and 'linklocal' in parts:
            # Link-local peer IP - handled automatically by Cumulus
            pass
        
        # net add interface peerlink.4094 clag priority <priority>
        elif 'clag' in parts and 'priority' in parts:
            priority_index = parts.index('priority')
            if priority_index + 1 < len(parts):
                # Store priority for reference
                self.mlag_config['clag_priority'] = parts[priority_index + 1]
        
        # net add interface peerlink.4094 clag sys-mac <mac>
        elif 'clag' in parts and 'sys-mac' in parts:
            sys_mac_index = parts.index('sys-mac')
            if sys_mac_index + 1 < len(parts):
                sys_mac = parts[sys_mac_index + 1]
                self.mlag_config['system_mac'] = sys_mac
    
    def _parse_vlan(self, cmd: str):
        """Parse VLAN SVI commands: net add vlan <id> <option>"""
        parts = cmd.split()
        if len(parts) < 2:
            return
        
        vlan_id = parts[1]
        
        # Ensure VLAN exists
        if vlan_id not in self.vlans:
            self.vlans[vlan_id] = VlanConfig(vlan_id=vlan_id)
        
        vlan = self.vlans[vlan_id]
        
        # net add vlan 10 alias <description>
        if 'alias' in parts:
            alias_index = parts.index('alias')
            if alias_index + 1 < len(parts):
                vlan.description = ' '.join(parts[alias_index + 1:])
        
        # net add vlan 10 ip address <ip>/<cidr>
        elif 'ip' in parts and 'address' in parts:
            address_index = parts.index('address')
            if address_index + 1 < len(parts):
                ip_cidr = parts[address_index + 1]
                if '/' in ip_cidr:
                    ip, cidr = ip_cidr.split('/')
                    vlan.ip_address = ip
                    vlan.subnet_mask = self._cidr_to_mask(int(cidr))
        
        # net add vlan 10 mtu <mtu>
        elif 'mtu' in parts:
            mtu_index = parts.index('mtu')
            if mtu_index + 1 < len(parts):
                # MTU on VLAN interface - store for SVI generation
                vlan.mtu = int(parts[mtu_index + 1])
                vlan.mtu_configured = True
        
        # net add vlan 10 ip address-virtual <mac> <vip>/<cidr>
        elif 'ip' in parts and 'address-virtual' in parts:
            address_virtual_index = parts.index('address-virtual')
            if address_virtual_index + 2 < len(parts):
                vrr_mac = parts[address_virtual_index + 1]
                vrr_vip = parts[address_virtual_index + 2]
                # Store VRR configuration for VRRP translation
                self.vrr_configs[vlan_id] = {
                    'mac': vrr_mac,
                    'vip': vrr_vip
                }
                # VRRP group number from VRR MAC: 00:00:5e:00:01:XX -> vrid = XX (hex to decimal)
                vrid = '1'
                if vrr_mac and ':' in vrr_mac:
                    octets = vrr_mac.lower().split(':')
                    if len(octets) == 6 and octets[:5] == ['00', '00', '5e', '00', '01']:
                        try:
                            vrid = str(int(octets[5], 16))
                        except ValueError:
                            pass
                if '/' in vrr_vip:
                    vip_ip = vrr_vip.split('/')[0]
                else:
                    vip_ip = vrr_vip
                vlan.vrrp_configs.append({
                    'vrid': vrid,
                    'vip': vip_ip,
                    'vlan': vlan_id
                })
    
    def _parse_bgp(self, cmd: str):
        """Parse BGP commands: net add bgp <option>"""
        parts = cmd.split()
        
        # net add bgp autonomous-system <asn>
        if 'autonomous-system' in parts:
            asn_index = parts.index('autonomous-system')
            if asn_index + 1 < len(parts):
                asn = parts[asn_index + 1]
                self.bgp_config['asn'] = asn
                if 'neighbors' not in self.bgp_config:
                    self.bgp_config['neighbors'] = []
        
        # net add bgp router-id <rid>
        elif 'router-id' in parts:
            rid_index = parts.index('router-id')
            if rid_index + 1 < len(parts):
                router_id = parts[rid_index + 1]
                self.bgp_config['router_id'] = router_id
        
        # net add bgp neighbor <ip> remote-as <asn>
        elif 'neighbor' in parts and 'remote-as' in parts:
            neighbor_index = parts.index('neighbor')
            remote_as_index = parts.index('remote-as')
            if neighbor_index + 1 < len(parts) and remote_as_index + 1 < len(parts):
                neighbor_ip = parts[neighbor_index + 1]
                remote_as = parts[remote_as_index + 1]
                neighbor = {
                    'ip': neighbor_ip,
                    'remote_as': remote_as
                }
                if 'neighbors' not in self.bgp_config:
                    self.bgp_config['neighbors'] = []
                self.bgp_config['neighbors'].append(neighbor)
        
        # net add bgp neighbor <ip> description <desc>
        elif 'neighbor' in parts and 'description' in parts:
            neighbor_index = parts.index('neighbor')
            desc_index = parts.index('description')
            if neighbor_index + 1 < len(parts) and desc_index + 1 < len(parts):
                neighbor_ip = parts[neighbor_index + 1]
                description = ' '.join(parts[desc_index + 1:])
                # Find and update neighbor
                if 'neighbors' in self.bgp_config:
                    for neighbor in self.bgp_config['neighbors']:
                        if neighbor.get('ip') == neighbor_ip:
                            neighbor['description'] = description
                            break
        
        # net add bgp neighbor <ip> update-source <source>
        elif 'neighbor' in parts and 'update-source' in parts:
            neighbor_index = parts.index('neighbor')
            update_source_index = parts.index('update-source')
            if neighbor_index + 1 < len(parts) and update_source_index + 1 < len(parts):
                neighbor_ip = parts[neighbor_index + 1]
                update_source = parts[update_source_index + 1]
                # Find and update neighbor
                if 'neighbors' in self.bgp_config:
                    for neighbor in self.bgp_config['neighbors']:
                        if neighbor.get('ip') == neighbor_ip:
                            neighbor['update_source'] = update_source
                            break
        
        # net add bgp neighbor <ip> ebgp-multihop <hops>
        elif 'neighbor' in parts and 'ebgp-multihop' in parts:
            neighbor_index = parts.index('neighbor')
            multihop_index = parts.index('ebgp-multihop')
            if neighbor_index + 1 < len(parts) and multihop_index + 1 < len(parts):
                neighbor_ip = parts[neighbor_index + 1]
                hops = parts[multihop_index + 1]
                # Find and update neighbor
                if 'neighbors' in self.bgp_config:
                    for neighbor in self.bgp_config['neighbors']:
                        if neighbor.get('ip') == neighbor_ip:
                            neighbor['ebgp_multihop'] = hops
                            break
        
        # net add bgp ipv4 unicast redistribute connected
        elif 'ipv4' in parts and 'unicast' in parts and 'redistribute' in parts:
            redistribute_index = parts.index('redistribute')
            if redistribute_index + 1 < len(parts):
                redistribute_type = parts[redistribute_index + 1]
                if 'redistribute' not in self.bgp_config:
                    self.bgp_config['redistribute'] = []
                self.bgp_config['redistribute'].append(redistribute_type)
    
    def _normalize_bgp_for_sonic(self):
        """Convert Cumulus BGP neighbors list to generator format: individual_neighbors, neighbor_descriptions, etc."""
        if 'neighbors' not in self.bgp_config or not self.bgp_config['neighbors']:
            return
        self.bgp_config['individual_neighbors'] = {}
        self.bgp_config['neighbor_descriptions'] = {}
        self.bgp_config['neighbor_update_source'] = {}
        self.bgp_config['neighbor_multihop'] = {}
        for n in self.bgp_config['neighbors']:
            ip = n.get('ip')
            if not ip:
                continue
            self.bgp_config['individual_neighbors'][ip] = {
                'remote_as': n.get('remote_as', self.bgp_config.get('asn', '')),
            }
            if n.get('description'):
                self.bgp_config['neighbor_descriptions'][ip] = n['description']
            src = n.get('update_source', '')
            if src:
                # Cumulus "lo" -> SONiC Loopback0
                self.bgp_config['neighbor_update_source'][ip] = 'Loopback0' if src.lower() == 'lo' else src
            if n.get('ebgp_multihop'):
                self.bgp_config['neighbor_multihop'][ip] = n['ebgp_multihop']
    
    def _parse_static_route(self, cmd: str):
        """Parse static route command: net add routing route <network>/<cidr> <next-hop>"""
        # net add routing route 0.0.0.0/0 192.168.10.254
        parts = cmd.split()
        if 'route' in parts:
            route_index = parts.index('route')
            if route_index + 2 < len(parts):
                network_cidr = parts[route_index + 1]
                next_hop = parts[route_index + 2]
                
                route = StaticRouteConfig()
                if '/' in network_cidr:
                    network, cidr = network_cidr.split('/')
                    route.network = network
                    route.mask = self._cidr_to_mask(int(cidr))
                else:
                    route.network = network_cidr
                
                route.next_hop = next_hop
                self.static_routes.append(route)
