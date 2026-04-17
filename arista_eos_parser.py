#!/usr/bin/env python3
"""
Arista EOS Configuration Parser

This module provides parsing logic for Arista EOS configurations
and converts them to Enterprise SONiC format.
"""

import re
from typing import Dict, List, Optional
from base_migrator import (
    BaseMigrator, VlanConfig, PortChannelConfig, PhysicalInterfaceConfig,
    LoopbackConfig, StaticRouteConfig, PrefixListEntry, RouteMapEntry,
    sanitize_for_output
)


class AristaEOSMigrator(BaseMigrator):
    """Migrator for Arista EOS configurations"""
    
    def __init__(self):
        """Initialize the Arista EOS migrator"""
        super().__init__()
        self.range_configs: Dict[str, List[str]] = {}
        self.current_neighbor: Optional[str] = None
        self.current_route_map_name: Optional[str] = None
        self.current_route_map_seq: Optional[int] = None
    
    def parse_config(self, config: str):
        """Parse EOS configuration into structured data"""
        self.reset_state()
        # Reset parser-specific state
        self.current_neighbor = None
        self.current_route_map_name = None
        self.current_route_map_seq = None
        
        lines = config.split('\n')
        
        for line_num, line in enumerate(lines, start=1):
            original_line = line
            line = line.strip()
            self.current_line_number = line_num
            
            # Skip empty lines and comments
            if not line or line.startswith('!') or line.startswith('#'):
                continue
            
            self._parse_line(line, line_num)
    
    def _parse_line(self, line: str, line_num: int):
        """Parse individual configuration line"""
        
        # Exit commands (handle first)
        if line == 'exit':
            self._handle_exit()
            return
        
        # Section start commands - return so the header line is not re-dispatched to section parsers
        if line.startswith('interface '):
            self._parse_interface_start(line)
            return
        
        if line.startswith('vlan '):
            self._parse_vlan_start(line)
            return
        
        if line.startswith('router bgp'):
            self._parse_bgp_start(line)
            return
        
        if line.startswith('router vrrp'):
            self.current_section = 'vrrp'
            self.push_context('router vrrp')
            return
        
        if line.startswith('mlag configuration'):
            self._parse_mlag_config_start(line)
            return
        
        if line.startswith('route-map '):
            self._parse_route_map_start(line)
            return
        if line.startswith('ip prefix-list '):
            self._parse_prefix_list_line(line)
            return
        if self.current_section == 'route_map':
            self._parse_route_map_line(line)
            return
        
        if line.startswith('line console') or line.startswith('line vty'):
            self.current_section = 'line'
            self.push_context(line.strip())
            return
        
        # Known EOS platform line - no SONiC equivalent; do not flag as unsupported
        if line.strip() == 'service routing protocols model multi-agent':
            return
        
        # Global configuration parsing (check these first as they can appear anywhere)
        # Only "ip route ..." is a static route; "ip router ospf ..." is OSPF interface config (unsupported)
        parts_check = line.split()
        if len(parts_check) >= 3 and parts_check[0] == 'ip' and parts_check[1] == 'route':
            self._parse_static_route(line)
            return

        # Context-sensitive parsing
        if self.current_section == 'vlan' and self.current_vlan:
            self._parse_vlan_config(line)
        
        elif self.current_section == 'vlan_interface' and self.current_vlan:
            self._parse_vlan_interface_config(line)
        
        elif self.current_section == 'interface' and self.current_interface:
            self._parse_interface_config(line)
        
        elif self.current_section == 'port-channel' and self.current_po:
            self._parse_port_channel_config(line)
        
        elif self.current_section == 'bgp':
            self._parse_bgp_config(line)
        
        elif self.current_section == 'vrrp':
            self._parse_vrrp_config(line)
        
        elif self.current_section == 'mlag':
            self._parse_mlag_config(line)
        
        elif self.current_section == 'loopback':
            self._parse_loopback_config(line)
        
        elif self.current_section == 'line':
            self.log_unsupported_feature(line, self.UNSUPPORTED_MSG)
        
        # Global configuration parsing
        elif line.startswith('hostname'):
            self.hostname = sanitize_for_output(' '.join(line.split()[1:]))
        
        elif line.startswith('ip address') and self.current_section != 'loopback':
            # Only parse as management IP if not in loopback context
            self._parse_ip_address(line)
        
        elif line.startswith('username'):
            self._parse_username(line)
        
        elif line.startswith('ntp server'):
            parts = line.split()
            if len(parts) >= 3:
                server_ip = sanitize_for_output(parts[2])
                if 'ntp_servers' not in self.global_settings:
                    self.global_settings['ntp_servers'] = []
                self.global_settings['ntp_servers'].append(server_ip)
                if 'ntp_server' not in self.global_settings:
                    self.global_settings['ntp_server'] = server_ip
                if len(parts) > 3 and parts[3].lower() == 'prefer':
                    self.global_settings['ntp_preferred_server'] = server_ip

        elif line.startswith('logging host'):
            server_ip = sanitize_for_output(line.split()[2])
            self.syslog_config.servers.append(server_ip)
        
        elif line.startswith('radius-server host'):
            self._parse_radius_config(line)
        
        elif line.startswith('aaa authentication login'):
            # AAA login method (default/console group radius local); we output equivalent when RADIUS is configured
            pass
        elif line.startswith('aaa authorization ') or line.startswith('aaa accounting '):
            # AAA authorization/accounting; accept as known
            pass
        
        elif line.startswith('snmp-server community'):
            self._parse_snmp_community(line)
        elif line.startswith('spanning-tree mode '):
            # HW-1/HW-7: EOS supports 'mstp', 'rapid-pvst', 'rstp'. Record
            # the source-config keyword; the generator normalization map
            # translates it to the EAS-accepted form
            # (rapid-pvst | mst | pvst).
            parts = line.split()
            if len(parts) >= 3:
                self.stp_mode = parts[2].lower()
        elif line.startswith('ip name-server'):
            # EOS: ip name-server [vrf <name>] <ip> or multiple IPs per line
            parts = line.split()
            vrf_name = None
            i = 1  # skip "ip name-server"
            if i < len(parts) and parts[i] == 'vrf' and i + 1 < len(parts):
                vrf_name = parts[i + 1]
                i += 2
            if 'name_servers' not in self.global_settings:
                self.global_settings['name_servers'] = []
            seen_ips = {e['ip'] if isinstance(e, dict) else e for e in self.global_settings['name_servers']}
            for part in parts[i:]:
                if re.match(r'^\d+\.\d+\.\d+\.\d+$', part) and part not in seen_ips:
                    self.global_settings['name_servers'].append({'ip': part, 'vrf': vrf_name})
                    seen_ips.add(part)
            if self.global_settings.get('name_servers') and 'name_server' not in self.global_settings:
                first = self.global_settings['name_servers'][0]
                self.global_settings['name_server'] = first['ip'] if isinstance(first, dict) else first
        else:
            self.log_unsupported_feature(line, self.UNSUPPORTED_MSG)
    
    def _parse_ip_address(self, line: str):
        """Parse IP address configuration"""
        parts = line.split()
        if len(parts) >= 3:
            if parts[2].lower() == 'dhcp':
                self.management_ip = 'dhcp'
            else:
                # EOS uses CIDR notation
                ip_with_cidr = parts[2]
                if '/' in ip_with_cidr:
                    self.management_ip, cidr = ip_with_cidr.split('/')
                    self.management_mask = self._cidr_to_mask(int(cidr))
                else:
                    self.management_ip = ip_with_cidr
    
    def _parse_username(self, line: str):
        """Parse username configuration"""
        parts = line.split()
        if len(parts) >= 2:
            username = sanitize_for_output(parts[1])
            # Extract role
            role = 'user'
            if 'role' in line:
                role_index = parts.index('role')
                if role_index + 1 < len(parts):
                    role_part = parts[role_index + 1]
                    if 'admin' in role_part.lower():
                        role = 'admin'
                    else:
                        role = sanitize_for_output(role_part)

            self.users[username] = {
                'password': '<password>',  # Will be prompted
                'role': role
            }
    
    def _parse_vlan_start(self, line: str):
        """Parse VLAN section start"""
        vlan_spec = line.split()[1]
        
        # Handle VLAN ranges like "100-105"
        if '-' in vlan_spec and not vlan_spec.startswith('-') and not vlan_spec.endswith('-'):
            try:
                start_vlan, end_vlan = vlan_spec.split('-')
                start_num = int(start_vlan)
                end_num = int(end_vlan)
                
                # Create individual VLANs for the range
                for vlan_num in range(start_num, end_num + 1):
                    vlan_id = str(vlan_num)
                    if vlan_id not in self.vlans:
                        self.vlans[vlan_id] = VlanConfig(vlan_id=vlan_id)
                
                # Set current VLAN to the first in range for any subsequent config
                self.current_vlan = start_vlan
            except (ValueError, IndexError):
                # If range parsing fails, treat as single VLAN
                self.current_vlan = vlan_spec
                if vlan_spec not in self.vlans:
                    self.vlans[vlan_spec] = VlanConfig(vlan_id=vlan_spec)
        else:
            # Single VLAN
            self.current_vlan = vlan_spec
            if vlan_spec not in self.vlans:
                self.vlans[vlan_spec] = VlanConfig(vlan_id=vlan_spec)
        
        self.current_section = 'vlan'
        self.push_context(f'vlan {self.current_vlan}')
    
    def _parse_vlan_config(self, line: str):
        """Parse VLAN configuration lines"""
        if not self.current_vlan:
            return
            
        vlan = self.vlans[self.current_vlan]
        
        if line.startswith('name '):
            vlan.name = ' '.join(line.split()[1:])
            vlan.description = vlan.name
        elif line.startswith('description '):
            vlan.description = ' '.join(line.split()[1:])
        else:
            self.log_unsupported_feature(line, self.UNSUPPORTED_MSG)
    
    def _parse_interface_start(self, line: str):
        """Parse interface section start"""
        parts = line.split()
        if len(parts) >= 2:
            intf_spec = parts[1]
            
            if intf_spec.startswith('Port-Channel') or intf_spec.startswith('port-channel'):
                # Handle port-channel
                po_id = intf_spec.replace('Port-Channel', '').replace('port-channel', '')
                if po_id not in self.port_channels:
                    self.port_channels[po_id] = PortChannelConfig(po_id=po_id)
                self.current_section = 'port-channel'
                self.current_po = po_id
                self.current_interface = None
                self.push_context(f'interface Port-Channel{po_id}')
            
            elif intf_spec == 'range':
                # Handle interface ranges
                interface_range = ' '.join(parts[1:])
                self.current_interface = interface_range
                self.current_section = 'interface'
                if interface_range not in self.range_configs:
                    self.range_configs[interface_range] = []
                self.push_context(f'interface {interface_range}')
            
            elif intf_spec.startswith('Vlan') or intf_spec.startswith('vlan'):
                vlan_id = intf_spec.replace('Vlan', '').replace('vlan', '')
                self.current_vlan = vlan_id
                self.current_section = 'vlan_interface'
                self.current_interface = f'Vlan{vlan_id}'
                if vlan_id not in self.vlans:
                    self.vlans[vlan_id] = VlanConfig(vlan_id=vlan_id)
                self.push_context(f'interface Vlan{vlan_id}')
            
            elif intf_spec.startswith('Loopback') or intf_spec.startswith('loopback'):
                loopback_id = intf_spec.replace('Loopback', '').replace('loopback', '')
                interface = f'Loopback{loopback_id}'
                if interface not in self.loopbacks:
                    self.loopbacks[interface] = LoopbackConfig(interface=interface)
                self.current_interface = interface
                self.current_section = 'loopback'
                self.push_context(f'interface {interface}')
            
            elif intf_spec.startswith('Ethernet') or intf_spec.startswith('ethernet'):
                interface = intf_spec
                self.current_interface = interface
                self.current_section = 'interface'
                if interface not in self.physical_interfaces:
                    self.physical_interfaces[interface] = PhysicalInterfaceConfig(interface=interface)
                self.push_context(f'interface {interface}')
    
    def _parse_interface_config(self, line: str):
        """Parse physical interface configuration"""
        if not self.current_interface:
            return
        
        # Handle range interfaces
        if 'range' in self.current_interface.lower():
            self._parse_interface_range_config(line)
            return
        
        # Handle VLAN interfaces
        if self.current_section == 'vlan_interface':
            self._parse_vlan_interface_config(line)
            return
        
        # Get or create interface config
        if self.current_interface not in self.physical_interfaces:
            self.physical_interfaces[self.current_interface] = PhysicalInterfaceConfig(interface=self.current_interface)
        
        intf = self.physical_interfaces[self.current_interface]
        
        if line.startswith('mtu '):
            intf.mtu = int(line.split()[1])
            intf.mtu_configured = True
        elif line.startswith('speed '):
            speed_val = line.split()[1]
            # EOS uses "forced 10G" format
            if 'forced' in speed_val.lower():
                intf.speed = speed_val.split()[1] if len(speed_val.split()) > 1 else speed_val
            else:
                intf.speed = speed_val
        elif line.startswith('no fec') or line.startswith('fec'):
            if 'no fec' in line:
                intf.fec = 'no fec'
            else:
                intf.fec = line.split()[1] if len(line.split()) > 1 else 'auto'
        elif line.startswith('channel-group '):
            parts = line.split()
            po_id = parts[1]
            intf.channel_group = po_id
            # Ensure PortChannel object exists even if not explicitly defined in config
            if po_id not in self.port_channels:
                self.port_channels[po_id] = PortChannelConfig(po_id=po_id)
        elif line.startswith('description '):
            intf.description = ' '.join(line.split()[1:])
        elif line.startswith('switchport mode '):
            intf.switchport_mode = line.split()[-1]
        elif line.startswith('switchport access vlan '):
            intf.access_vlan = line.split()[-1]
            intf.switchport_mode = 'access'
        elif line.startswith('switchport trunk allowed vlan '):
            vlans = ' '.join(line.split()[4:])
            intf.allowed_vlans = [v.strip() for v in vlans.replace('add', '').replace('remove', '').split(',')]
            intf.switchport_mode = 'trunk'
        elif line.startswith('switchport trunk native vlan '):
            intf.native_vlan = line.split()[-1]
            intf.switchport_mode = 'trunk'
        elif line.startswith('shutdown'):
            intf.shutdown = True
        elif line.startswith('no shutdown'):
            intf.shutdown = False
        elif line.startswith('lldp'):
            intf.lldp_settings.append(sanitize_for_output(line))
        elif line.startswith('mlag '):
            # MLAG ID on interface
            mlag_id = line.split()[-1]
            if 'mlag_ids' not in self.mlag_config:
                self.mlag_config['mlag_ids'] = []
            self.mlag_config['mlag_ids'].append(mlag_id)
        else:
            self.log_unsupported_feature(line, self.UNSUPPORTED_MSG)
    
    def _parse_interface_range_config(self, line: str):
        """Parse interface range configuration"""
        if not hasattr(self, 'range_configs'):
            self.range_configs = {}
        
        if self.current_interface not in self.range_configs:
            self.range_configs[self.current_interface] = []
        
        self.range_configs[self.current_interface].append(line)
    
    def _parse_loopback_config(self, line: str):
        """Parse loopback interface configuration"""
        if not self.current_interface or self.current_interface not in self.loopbacks:
            return
        
        loopback = self.loopbacks[self.current_interface]
        
        if line.startswith('description '):
            loopback.description = ' '.join(line.split()[1:])
        elif line.startswith('ip address '):
            parts = line.split()
            if len(parts) >= 3:
                ip_with_cidr = parts[2]
                if '/' in ip_with_cidr:
                    loopback.ip_address, cidr = ip_with_cidr.split('/')
                    loopback.subnet_mask = self._cidr_to_mask(int(cidr))
                else:
                    loopback.ip_address = ip_with_cidr
                    if len(parts) >= 4:
                        loopback.subnet_mask = parts[3]
        else:
            self.log_unsupported_feature(line, self.UNSUPPORTED_MSG)
    
    def _parse_vlan_interface_config(self, line: str):
        """Parse VLAN interface configuration (IP addresses, MTU, etc.)"""
        if not self.current_vlan or self.current_vlan not in self.vlans:
            return
            
        vlan = self.vlans[self.current_vlan]
        
        if line.startswith('description '):
            vlan.description = ' '.join(line.split()[1:])
        elif line.startswith('ip address '):
            parts = line.split()
            if len(parts) >= 3:
                ip_with_cidr = parts[2]
                if '/' in ip_with_cidr:
                    vlan.ip_address, cidr = ip_with_cidr.split('/')
                    vlan.subnet_mask = self._cidr_to_mask(int(cidr))
                else:
                    vlan.ip_address = ip_with_cidr
                    if len(parts) >= 4:
                        vlan.subnet_mask = parts[3]
        elif line.startswith('mtu '):
            vlan.mtu = int(line.split()[1])
            vlan.mtu_configured = True
        elif line.startswith('vrrp '):
            # VRRP configuration
            parts = line.split()
            if len(parts) >= 3:
                vrid = parts[1]
                if 'groups' not in self.vrrp_config:
                    self.vrrp_config['groups'] = []
                # Try to extract VIP
                vip = None
                for i, part in enumerate(parts):
                    if part == 'ip' and i + 1 < len(parts):
                        vip = parts[i + 1]
                        break
                if vip:
                    self.vrrp_config['groups'].append({
                        'vrid': vrid,
                        'vip': vip,
                        'vlan': self.current_vlan
                    })
        elif line.startswith('shutdown') or line.startswith('no shutdown'):
            # Valid SVI command; SONiC may not have direct equivalent but accept as known
            pass
        else:
            self.log_unsupported_feature(line, self.UNSUPPORTED_MSG)
    
    def _parse_port_channel_config(self, line: str):
        """Parse port-channel configuration"""
        if not self.current_po:
            return
        
        po = self.port_channels[self.current_po]
        
        if line.startswith('mtu '):
            po.mtu = int(line.split()[1])
            po.mtu_configured = True
        elif line.startswith('description '):
            po.description = ' '.join(line.split()[1:])
        elif line.startswith('switchport mode '):
            po.mode = line.split()[-1]
        elif line.startswith('switchport trunk allowed vlan '):
            vlans = ' '.join(line.split()[4:])
            po.allowed_vlans = [v.strip() for v in vlans.replace('add', '').replace('remove', '').split(',')]
            po.mode = 'trunk'
        elif line.startswith('switchport trunk native vlan '):
            po.native_vlan = line.split()[-1]
            po.mode = 'trunk'
        elif line.startswith('switchport access vlan '):
            po.access_vlan = line.split()[-1]
            po.mode = 'access'
        elif line.startswith('mlag ') or 'mlag configuration' in line:
            po.mlag_enabled = True
            if 'peer-link' in line:
                self.mlag_config['peer_link_po'] = self.current_po
        elif line.startswith('spanning-tree port type edge') or line.startswith('spanning-tree disable'):
            po.spanning_tree_disable = True
        else:
            self.log_unsupported_feature(line, self.UNSUPPORTED_MSG)
    
    def _parse_bgp_start(self, line: str):
        """Parse BGP configuration start"""
        parts = line.split()
        if len(parts) >= 3:
            asn = parts[2]
            self.bgp_config['asn'] = asn
            self.bgp_config['neighbors'] = []
            self.bgp_config['peer_groups'] = {}
            self.bgp_config['redistribute'] = []
            self.current_section = 'bgp'
            self.push_context(f'router bgp {asn}')
    
    def _parse_bgp_config(self, line: str):
        """Parse BGP configuration lines"""
        # If we see a new top-level BGP command or a new neighbor, end current neighbor block
        if (line.startswith('router-id ') or line.startswith('redistribute ') or 
            line.startswith('bgp asn ') or (line.startswith('neighbor ') and self.current_neighbor)):
            if self.current_neighbor:
                self.pop_context()
                self.current_neighbor = None
        
        if line.startswith('router-id '):
            self.bgp_config['router_id'] = line.split()[1]
        elif line.startswith('bgp asn '):
            # EOS specific
            self.bgp_config['asn'] = line.split()[2]
        elif line.startswith('redistribute '):
            self.bgp_config['redistribute'].append(' '.join(line.split()[1:]))
        elif line.startswith('neighbor ') and 'peer-group' in line:
            # Handle "neighbor X peer-group Y" - peer group assignment
            parts = line.split()
            if len(parts) >= 4 and parts[2] == 'peer-group':
                neighbor = parts[1]
                peer_group = parts[3]
                if 'peer_group_members' not in self.bgp_config:
                    self.bgp_config['peer_group_members'] = []
                self.bgp_config['peer_group_members'].append({
                    'neighbor': neighbor,
                    'peer_group': peer_group
                })
        elif line.startswith('neighbor '):
            # Handle neighbor configuration - this line starts a neighbor block
            parts = line.split()
            if len(parts) >= 2:
                neighbor = parts[1]
                self.current_neighbor = neighbor
                self.push_context(f'neighbor {neighbor}')
                if 'individual_neighbors' not in self.bgp_config:
                    self.bgp_config['individual_neighbors'] = {}
                if neighbor not in self.bgp_config['individual_neighbors']:
                    self.bgp_config['individual_neighbors'][neighbor] = {}
                # Parse remote-as, description, update-source, ebgp-multihop on same line
                if 'remote-as' in line:
                    try:
                        as_index = parts.index('remote-as')
                        if as_index + 1 < len(parts):
                            self.bgp_config['individual_neighbors'][neighbor]['remote_as'] = parts[as_index + 1]
                    except (ValueError, IndexError):
                        pass
                if 'description' in line:
                    try:
                        desc_index = parts.index('description')
                        desc = ' '.join(parts[desc_index + 1:])
                        if 'neighbor_descriptions' not in self.bgp_config:
                            self.bgp_config['neighbor_descriptions'] = {}
                        self.bgp_config['neighbor_descriptions'][neighbor] = desc
                    except (ValueError, IndexError):
                        pass
                if 'update-source' in line:
                    try:
                        us_index = parts.index('update-source')
                        update_source = ' '.join(parts[us_index + 1:])
                        if 'neighbor_update_source' not in self.bgp_config:
                            self.bgp_config['neighbor_update_source'] = {}
                        self.bgp_config['neighbor_update_source'][neighbor] = update_source
                    except (ValueError, IndexError):
                        pass
                if 'ebgp-multihop' in line:
                    try:
                        mh_index = parts.index('ebgp-multihop')
                        multihop_val = parts[mh_index + 1] if mh_index + 1 < len(parts) else '3'
                        if 'neighbor_multihop' not in self.bgp_config:
                            self.bgp_config['neighbor_multihop'] = {}
                        self.bgp_config['neighbor_multihop'][neighbor] = multihop_val
                    except (ValueError, IndexError):
                        pass
                if 'route-map' in line:
                    try:
                        route_map_index = parts.index('route-map')
                        if route_map_index + 2 < len(parts):
                            rm_name = parts[route_map_index + 1]
                            direction = parts[route_map_index + 2].lower()
                            if direction == 'in':
                                if 'neighbor_route_map_in' not in self.bgp_config:
                                    self.bgp_config['neighbor_route_map_in'] = {}
                                self.bgp_config['neighbor_route_map_in'][neighbor] = rm_name
                            elif direction == 'out':
                                if 'neighbor_route_map_out' not in self.bgp_config:
                                    self.bgp_config['neighbor_route_map_out'] = {}
                                self.bgp_config['neighbor_route_map_out'][neighbor] = rm_name
                    except (ValueError, IndexError):
                        pass
        elif self.current_neighbor:
            # We're in a neighbor block - parse neighbor-specific config
            parts = line.split()
            neighbor = self.current_neighbor
            
            if 'remote-as' in line:
                try:
                    as_index = parts.index('remote-as')
                    if as_index + 1 < len(parts):
                        remote_as = parts[as_index + 1]
                        if 'individual_neighbors' not in self.bgp_config:
                            self.bgp_config['individual_neighbors'] = {}
                        self.bgp_config['individual_neighbors'][neighbor] = {
                            'remote_as': remote_as
                        }
                except (ValueError, IndexError):
                    pass
            elif 'description' in line:
                try:
                    desc_index = parts.index('description')
                    desc = ' '.join(parts[desc_index + 1:])
                    if 'neighbor_descriptions' not in self.bgp_config:
                        self.bgp_config['neighbor_descriptions'] = {}
                    self.bgp_config['neighbor_descriptions'][neighbor] = desc
                except (ValueError, IndexError):
                    pass
            elif 'route-map' in line:
                try:
                    route_map_index = parts.index('route-map')
                    if route_map_index + 2 < len(parts):
                        rm_name = parts[route_map_index + 1]
                        direction = parts[route_map_index + 2].lower()  # in | out
                        if direction == 'in':
                            if 'neighbor_route_map_in' not in self.bgp_config:
                                self.bgp_config['neighbor_route_map_in'] = {}
                            self.bgp_config['neighbor_route_map_in'][neighbor] = rm_name
                        elif direction == 'out':
                            if 'neighbor_route_map_out' not in self.bgp_config:
                                self.bgp_config['neighbor_route_map_out'] = {}
                            self.bgp_config['neighbor_route_map_out'][neighbor] = rm_name
                except (ValueError, IndexError):
                    pass
            elif 'ebgp-multihop' in line:
                if 'neighbor_multihop' not in self.bgp_config:
                    self.bgp_config['neighbor_multihop'] = {}
                try:
                    multihop_index = parts.index('ebgp-multihop')
                    multihop_val = parts[multihop_index + 1] if multihop_index + 1 < len(parts) else '3'
                    self.bgp_config['neighbor_multihop'][neighbor] = multihop_val
                except (ValueError, IndexError):
                    pass
            elif 'update-source' in line:
                if 'neighbor_update_source' not in self.bgp_config:
                    self.bgp_config['neighbor_update_source'] = {}
                try:
                    update_source_index = parts.index('update-source')
                    update_source = ' '.join(parts[update_source_index + 1:])
                    self.bgp_config['neighbor_update_source'][neighbor] = update_source
                except (ValueError, IndexError):
                    pass
            elif line == 'exit':
                # End of neighbor block
                self.pop_context()
                self.current_neighbor = None
        elif line.startswith('address-family '):
            # Address family - already handled in context
            pass
        else:
            self.log_unsupported_feature(line, self.UNSUPPORTED_MSG)
    
    def _parse_vrrp_config(self, line: str):
        """Parse VRRP configuration"""
        if line.startswith('interface vlan') or line.startswith('interface Vlan'):
            vlan_id = line.split()[-1].replace('Vlan', '').replace('vlan', '')
            self.vrrp_config['vlan'] = vlan_id
        elif line.startswith('vrrp ') and 'ipv4' in line:
            parts = line.split()
            if len(parts) >= 4:
                vrid = parts[1]
                vip = parts[3]
                if 'groups' not in self.vrrp_config:
                    self.vrrp_config['groups'] = []
                self.vrrp_config['groups'].append({
                    'vrid': vrid,
                    'vip': vip,
                    'vlan': self.vrrp_config.get('vlan', '1')
                })
    
    def _parse_mlag_config_start(self, line: str):
        """Parse MLAG configuration start"""
        self.current_section = 'mlag'
        self.push_context('mlag configuration')
    
    def _parse_mlag_config(self, line: str):
        """Parse MLAG configuration"""
        if 'domain-id' in line:
            domain_id = line.split()[-1]
            self.mlag_config['domain_id'] = domain_id
        elif 'local-interface' in line:
            local_intf = line.split()[-1]
            self.mlag_config['local_interface'] = local_intf
        elif 'peer-address' in line:
            peer_ip = line.split()[-1]
            self.mlag_config['peer_address'] = peer_ip
        elif 'peer-link' in line:
            peer_link = line.split()[-1]
            self.mlag_config['peer_link_po'] = peer_link.replace('Port-Channel', '').replace('port-channel', '')
        elif 'system-mac' in line:
            self.mlag_config['system_mac'] = line.split()[-1]
        elif 'reload-delay' in line and 'mlag' in line:
            # reload-delay mlag <seconds> -> SONiC delay-restore (only emit if != 300)
            parts = line.split()
            if len(parts) >= 3 and parts[2].isdigit():
                self.mlag_config['delay_restore'] = int(parts[2])
        else:
            self.log_unsupported_feature(line, self.UNSUPPORTED_MSG)
    
    def _parse_static_route(self, line: str):
        """Parse static route configuration"""
        parts = line.split()
        if len(parts) >= 3:
            route = StaticRouteConfig()
            # Format: "ip route <network> <next-hop>" or "ip route <network>/<cidr> <next-hop>"
            network_part = parts[2]
            if '/' in network_part:
                # CIDR notation: "ip route 0.0.0.0/0 192.168.10.254"
                route.network, cidr = network_part.split('/')
                route.mask = self._cidr_to_mask(int(cidr))
                if len(parts) >= 4:
                    route.next_hop = parts[3]
            elif len(parts) >= 4:
                # Format: "ip route <network> <mask> <next-hop>"
                route.network = network_part
                route.mask = parts[3]
                if len(parts) >= 5:
                    route.next_hop = parts[4]
            elif len(parts) >= 3:
                # Format: "ip route <network> <next-hop>" (no mask)
                route.network = network_part
                if len(parts) >= 4:
                    route.next_hop = parts[3]
            
            if route.network:  # Only add if we successfully parsed
                self.static_routes.append(route)
    
    def _parse_radius_config(self, line: str):
        """Parse RADIUS server configuration"""
        from base_migrator import RadiusConfig

        parts = line.split()
        if len(parts) >= 3 and parts[1] == 'host':
            if not self.radius_config:
                self.radius_config = RadiusConfig()

            self.radius_config.host = sanitize_for_output(parts[2])

            # Parse optional parameters
            i = 3
            while i < len(parts):
                if i + 1 < len(parts):
                    if parts[i] == 'timeout':
                        self.radius_config.timeout = int(parts[i + 1])
                        i += 2
                    elif parts[i] == 'retransmit':
                        self.radius_config.retransmit = int(parts[i + 1])
                        i += 2
                    elif parts[i] == 'key':
                        # EOS format: key 7 "radiuskey123" or key "radiuskey123"
                        # Skip encryption type (7) if present and get the actual key
                        if i + 2 < len(parts) and parts[i + 1].isdigit():
                            # Format: key 7 "radiuskey123"
                            self.radius_config.key = sanitize_for_output(parts[i + 2].strip('"'))
                            i += 3
                        elif i + 1 < len(parts):
                            # Format: key "radiuskey123" or key radiuskey123
                            self.radius_config.key = sanitize_for_output(parts[i + 1].strip('"'))
                            i += 2
                        else:
                            i += 1
                    else:
                        i += 1
                else:
                    i += 1

    def _parse_snmp_community(self, line: str):
        """Parse SNMP community configuration"""
        # Format: snmp-server community <name> [ro|rw]
        parts = line.split()
        if len(parts) >= 3:
            community_name = sanitize_for_output(parts[2])
            # Default to read-write if not specified, but check for ro/rw
            permission = 'rw'  # default
            if len(parts) >= 4:
                if parts[3].lower() == 'ro':
                    permission = 'ro'
                elif parts[3].lower() == 'rw':
                    permission = 'rw'
            self.snmp_config.communities[community_name] = permission
    
    def _parse_route_map_start(self, line: str):
        """Parse route-map <name> permit|deny <seq> (Arista EOS)"""
        parts = line.split()
        if len(parts) >= 4:
            name = parts[1]
            action = parts[2].lower()
            try:
                seq = int(parts[3])
            except ValueError:
                return
            if self.current_section == 'route_map' and self.current_route_map_name:
                self.pop_context()
            self.current_section = 'route_map'
            self.current_route_map_name = name
            self.current_route_map_seq = seq
            self.push_context(f'route-map {name}')
            if name not in self.route_maps:
                self.route_maps[name] = []
            self.route_maps[name].append(RouteMapEntry(map_name=name, seq=seq, action=action, matches=[], sets=[]))
    
    def _parse_route_map_line(self, line: str):
        """Parse match/set line inside route-map (Arista EOS)"""
        stripped = line.strip()
        if not self.current_route_map_name or self.current_route_map_name not in self.route_maps:
            self.current_section = 'global'
            return
        entries = self.route_maps[self.current_route_map_name]
        if not entries:
            self.current_section = 'global'
            return
        if stripped.startswith('match '):
            entries[-1].matches.append(sanitize_for_output(stripped))
        elif stripped.startswith('set '):
            entries[-1].sets.append(sanitize_for_output(stripped))
        else:
            self.current_section = 'global'
    
    def _parse_prefix_list_line(self, line: str):
        """Parse ip prefix-list <name> seq <n> permit|deny <prefix> [ge <x>] [le <y>] (Arista EOS)"""
        parts = line.split()
        if len(parts) < 5 or parts[0] != 'ip' or parts[1] != 'prefix-list':
            return
        list_name = parts[2]
        if parts[3] != 'seq':
            return
        try:
            seq = int(parts[4])
        except ValueError:
            return
        if len(parts) < 7:
            return
        action = parts[5].lower()
        prefix = parts[6]
        ge_val, le_val = None, None
        i = 7
        while i + 1 < len(parts):
            if parts[i] == 'ge':
                try:
                    ge_val = int(parts[i + 1])
                except ValueError:
                    pass
                i += 2
            elif parts[i] == 'le':
                try:
                    le_val = int(parts[i + 1])
                except ValueError:
                    pass
                i += 2
            else:
                i += 1
        if list_name not in self.prefix_lists:
            self.prefix_lists[list_name] = []
        self.prefix_lists[list_name].append(PrefixListEntry(list_name=list_name, seq=seq, action=action, prefix=prefix, ge=ge_val, le=le_val))
    
    def _handle_exit(self):
        """Handle exit commands"""
        if self.current_section in ['vlan', 'vlan_interface', 'interface', 'port-channel', 'bgp', 'vrrp', 'mlag', 'loopback', 'route_map', 'line']:
            if self.current_section == 'route_map':
                self.current_route_map_name = None
                self.current_route_map_seq = None
            self.pop_context()
            self.current_section = 'global'
            self.current_vlan = None
            self.current_interface = None
            self.current_po = None
    
    def convert_interface_name(self, interface: str) -> str:
        """Convert EOS interface name to SONiC interface name"""
        # Handle range specifications
        if 'range' in interface.lower():
            return self._convert_interface_range(interface)
        
        # Pattern-based conversion
        # Handle Ethernet1 -> Eth 1/1 (assuming single slot)
        ethernet_match = re.match(r'Ethernet(\d+)', interface, re.IGNORECASE)
        if ethernet_match:
            port = ethernet_match.group(1)
            return f'Eth 1/{port}'
        
        # Handle Port-Channel -> PortChannel
        po_match = re.match(r'Port-Channel(\d+)', interface, re.IGNORECASE)
        if po_match:
            po_id = po_match.group(1)
            return f'PortChannel {po_id}'
        
        # Handle loopback
        if 'loopback' in interface.lower():
            lo_match = re.search(r'loopback(\d+)', interface, re.IGNORECASE)
            if lo_match:
                lo_id = lo_match.group(1)
                return f'Loopback{lo_id}'
        
        # Return as-is if no conversion found
        return interface
    
    def _convert_interface_range(self, interface: str) -> str:
        """Convert interface range specification"""
        # Handle "interface range Ethernet1-10"
        range_match = re.search(r'range\s+Ethernet(\d+)-(\d+)', interface, re.IGNORECASE)
        if range_match:
            start = range_match.group(1)
            end = range_match.group(2)
            return f'range Eth 1/{start}-1/{end}'
        
        # Handle "interface range Port-Channel 1-5" or "interface range Port-Channel30-35" (no space before numbers)
        po_range_match = re.search(r'range\s+Port-Channel\s*(\d+)-(\d+)', interface, re.IGNORECASE)
        if po_range_match:
            start = po_range_match.group(1)
            end = po_range_match.group(2)
            return f'range PortChannel {start}-{end}'
        
        return interface
