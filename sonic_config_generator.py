#!/usr/bin/env python3
"""
SONiC Configuration Generator

This module provides common methods for generating Enterprise SONiC
configuration commands from parsed configuration data structures.
"""

import re
from typing import Dict, List, Optional
from base_migrator import (
    BaseMigrator, VlanConfig, PortChannelConfig, PhysicalInterfaceConfig,
    LoopbackConfig, StaticRouteConfig, DCBXConfig, SyslogConfig,
    RadiusConfig, SnmpConfig, sanitize_for_output
)


class SonicConfigGenerator:
    """Generates SONiC configuration from parsed configuration data"""
    
    def __init__(self, migrator: BaseMigrator):
        """Initialize with a migrator instance"""
        self.migrator = migrator
    
    def _quote_description(self, desc: str) -> str:
        """Return description always enclosed in double quotes (safe for single or multi-word)."""
        if not desc:
            return desc
        s = str(desc).strip()
        if s.startswith('"') and s.endswith('"'):
            return s
        return f'"{s}"'

    @staticmethod
    def _is_ip_literal(value: str) -> bool:
        """Return True when value is an IPv4 or IPv6 address literal.

        Used by HW-10 update-source emission: EAS expects either an IP
        literal OR the 'interface' keyword followed by a name. When the
        value is an IP, the 'interface' keyword must NOT be emitted.
        """
        if not value:
            return False
        v = str(value).strip()
        # IPv4 dotted-quad
        if re.match(r'^\d{1,3}(\.\d{1,3}){3}$', v):
            return True
        # IPv6: presence of ':' is a sufficient and conservative heuristic;
        # any source-config value that contains a colon is not an interface name.
        if ':' in v:
            return True
        return False

    def _emit_update_source(self, value: str, indent: str = '  ') -> str:
        """Build a single 'update-source ...' line for a BGP neighbor.

        HW-10: EAS '(config-router-bgp-neighbor)# update-source ?' returns
        either an IP literal (A.B.C.D / A::B) OR the keyword 'interface'
        followed by an interface name. A bare interface token (e.g.
        'update-source Loopback0') is rejected. Emit the 'interface'
        keyword for interface-name inputs and pass IP literals through
        unchanged.
        """
        v = (value or '').strip()
        if self._is_ip_literal(v):
            return f'{indent}update-source {v}'
        canonical = self._canonicalize_source_interface(v)
        return f'{indent}update-source interface {canonical}'

    def _canonicalize_source_interface(self, name: str) -> str:
        """Normalize a source-interface or update-source value to EAS-accepted case.

        HW-5: EAS IS-CLI is case-sensitive on interface names used as values
        to 'update-source' (and similar 'source-interface' contexts). Source
        parsers may emit lowercase 'loopback0' (NX-OS) or the abbreviated
        Cumulus form 'lo'; EAS expects 'Loopback0' (capital L). Apply the
        canonical form here so callers do not have to remember the rule.
        """
        if not name:
            return name
        val = str(name).strip()
        # Cumulus abbreviation: 'lo' -> 'Loopback0'
        if val.lower() == 'lo':
            return 'Loopback0'
        # Loopback normalization: match 'loopback<N>' or 'Loopback<N>' in any case
        m = re.match(r'^loopback(\d+)$', val, re.IGNORECASE)
        if m:
            return f'Loopback{m.group(1)}'
        return val
    
    def generate_sonic_config(self, user_inputs: Dict[str, str]) -> str:
        """Generate SONiC configuration from parsed data.

        FR-2 canonical form, non-DCBX:
          sonic-cli
          configure terminal
          ... body ...
          end
          write memory

        FR-2 canonical form, DCBX-present (exactly two blocks, one marker):
          sonic-cli
          configure terminal
          ... pre-reboot body ...
          end
          write memory
          ! --- PASTE AFTER REBOOT ---
          sonic-cli
          configure terminal
          buffer init lossless
          ... post-reboot body ...
          end
          write memory
        """
        config_lines = []

        # Single atomic header; no double-entry, no mid-paste write memory inside config mode.
        config_lines.extend([
            '! Enter the SONiC CLI shell, then paste the commands below.',
            '! They form one atomic configure-terminal session.',
            'sonic-cli',
            '!',
            'configure terminal',
            '!',
        ])

        # Hostname + interface-naming standard (inside the same configure-terminal session)
        if self.migrator.hostname:
            # HW-1/HW-7: EAS has no 'spanning-tree enable'. The valid
            # sub-keyword is 'spanning-tree mode <mst|pvst|rapid-pvst>'.
            # EAS does NOT accept 'rstp' or 'mstp' (round-2 hardware
            # validation, hw-validation/HW_VALIDATION_REPORT_V2.md).
            # Normalize any source keyword and default to 'rapid-pvst' when
            # the source had no explicit mode (rapid spanning-tree is the
            # safest enable form for a migrated config).
            raw_mode = (getattr(self.migrator, 'stp_mode', '') or '').strip().lower()
            stp_mode_map = {
                'rstp': 'rapid-pvst',
                'rapid-pvst': 'rapid-pvst',
                'mstp': 'mst',
                'mst': 'mst',
                'pvst': 'pvst',
                'pvst+': 'pvst',
                'rapid-pvst+': 'rapid-pvst',
            }
            stp_mode = stp_mode_map.get(raw_mode, 'rapid-pvst')
            # Interface-naming standard requires exiting and re-entering sonic-cli
            # before the new mode is honored by the parser (confirmed on live EAS
            # 2026-04-16). Split the session with a clear paste boundary so the
            # interface commands that follow run under the new naming mode.
            config_lines.extend([
                f'hostname {self.migrator.hostname}',
                'interface-naming standard',
                'end',
                'exit',
                '! ============================================================',
                '! PASTE BOUNDARY: exit sonic-cli and re-enter before continuing.',
                '! interface-naming standard takes effect only on a fresh',
                '! sonic-cli session. In the Linux shell run:',
                '!   exit',
                '!   sonic-cli',
                '! Then resume pasting below.',
                '! ============================================================',
                'sonic-cli',
                '!',
                'configure terminal',
                '!',
                f'spanning-tree mode {stp_mode}',
                '!'
            ])
        
        # Management interface
        self._generate_management_config(config_lines, user_inputs)
        
        # User configuration
        self._generate_user_config(config_lines, user_inputs)
        
        # Port-channel configuration (non-MCLAG POs first, then MCLAG domain block, then MCLAG POs)
        self._generate_port_channel_config(config_lines, user_inputs)
        
        # Global LLDP configuration
        if self.migrator.global_settings.get('lldp_enabled'):
            config_lines.extend([
                'lldp enable',
                'lldp tlv-select system-capabilities',
                'lldp tlv-select management-address',
                '!'
            ])
        
        # DCBX configuration
        self._generate_dcbx_config(config_lines)
        
        # Syslog configuration
        self._generate_syslog_config(config_lines)
        
        # NTP configuration (after syslog if present)
        self._generate_ntp_config(config_lines, user_inputs)
        
        # DNS configuration (Dell Enterprise SONiC: /etc/resolv.conf)
        self._generate_dns_config(config_lines)
        
        # RADIUS configuration 
        self._generate_radius_config(config_lines, user_inputs)
        
        # SNMP configuration
        self._generate_snmp_config(config_lines)
        
        # Apply VLAN 1 assignments to interfaces without switchport config
        self.migrator._apply_vlan1_assignments()
        
        # Transfer MTU from interfaces to their PortChannels
        self.migrator._transfer_mtu_to_port_channels()
        
        # VLAN configuration (create + description + IP/MTU/VRRP in single block per VLAN)
        self._generate_vlan_config(config_lines)
        
        # Interface range configurations
        self._generate_interface_range_config(config_lines)
        
        # Physical interface configurations
        self._generate_physical_interface_config(config_lines)
        
        # Loopback interfaces
        self._generate_loopback_config(config_lines)
        
        # Static routes
        self._generate_static_routes_config(config_lines)
        
        # IP prefix-lists and route-maps (before BGP, which may reference them)
        self._generate_prefix_list_config(config_lines)
        self._generate_route_map_config(config_lines)
        
        # BGP configuration
        self._generate_bgp_config(config_lines)
        
        # Close the (first) configure-terminal session cleanly.
        config_lines.extend([
            'end',
            'write memory'
        ])

        # DCBX path: emit a single reboot marker and a second sonic-cli envelope for
        # buffer init lossless plus any post-reboot DCBX commands. No other re-entry
        # into sonic-cli ever appears in the output.
        if self.migrator.dcbx_configs:
            config_lines.extend([
                '! --- PASTE AFTER REBOOT ---',
                '! DCBX-IEEE configuration detected. Only DCBX-IEEE is supported in SONiC.',
                '! The buffer init lossless command will prompt you to save and reboot.',
                '! After the switch comes back up, re-enter the SONiC CLI shell and paste below.',
                'sonic-cli',
                '!',
                'configure terminal',
                '!',
                'buffer init lossless',
                '!',
                'end',
                'write memory',
            ])

        return '\n'.join(config_lines)
    
    def _generate_management_config(self, config_lines: List[str], user_inputs: Dict[str, str]):
        """Generate management interface configuration.

        FR-3: emit 'ip vrf mgmt' exactly once before the first 'interface Management 0'
        block, and only when a management interface is actually configured. Dane
        confirmed 2026-04-16 that 'mgmt' is NOT a built-in VRF on Enterprise Advanced
        SONiC; only 'default' is built in. The VRF must be created.
        """
        # When source has no explicit OOB management, do not create Management 0 IP that mirrors an SVI
        svi_ips = set()
        for vlan in (self.migrator.vlans or {}).values():
            if getattr(vlan, 'ip_address', None):
                svi_ips.add(vlan.ip_address)
        has_explicit_mgmt = getattr(self.migrator, 'has_explicit_management_config', True)

        # Decide which branch will actually emit a Management 0 block, and collect its lines.
        mgmt_block: List[str] = []
        if self.migrator.management_ip and self.migrator.management_ip != 'dhcp':
            if has_explicit_mgmt or self.migrator.management_ip not in svi_ips:
                mgmt_block.append('interface Management 0')
                if self.migrator.management_mask:
                    cidr = self.migrator._mask_to_cidr(self.migrator.management_mask)
                    ip_config = f'  ip address {self.migrator.management_ip}/{cidr}'
                    if user_inputs.get('management_gateway'):
                        ip_config += f' gwaddr {user_inputs["management_gateway"]}'
                    mgmt_block.append(ip_config)
                mgmt_block.append('exit')
        elif user_inputs.get('management_ip_cidr'):
            mgmt_ip = user_inputs['management_ip_cidr'].split('/')[0]
            if has_explicit_mgmt or mgmt_ip not in svi_ips or not getattr(self.migrator, 'has_explicit_management_config', True):
                mgmt_block.append('interface Management 0')
                ip_config = f'  ip address {user_inputs["management_ip_cidr"]}'
                if user_inputs.get('management_gateway'):
                    ip_config += f' gwaddr {user_inputs["management_gateway"]}'
                mgmt_block.append(ip_config)
                mgmt_block.append('exit')

        if not mgmt_block:
            return

        # Emit VRF then the management interface block.
        config_lines.append('ip vrf mgmt')
        config_lines.append('!')
        config_lines.extend(mgmt_block)
        config_lines.append('!')
    
    def _generate_user_config(self, config_lines: List[str], user_inputs: Dict[str, str]):
        """Generate user configuration (dedupe by lowercase username; add fallback admin only if missing)."""
        seen_lower = set()
        for username, user_info in self.migrator.users.items():
            if username.lower() in seen_lower:
                continue
            seen_lower.add(username.lower())
            password = user_inputs.get(f'{username.lower()}_password', user_info.get('password', ''))
            if '<password>' in password:
                password = user_inputs.get('admin_password', '<password>')
            role = user_info.get('role', 'user')
            config_lines.append(f'username {username} password {password} role {role}')
        
        # Ensure lowercase admin user exists only if not already in parsed users
        if 'admin' not in seen_lower:
            admin_password = user_inputs.get('admin_password', '<password>')
            config_lines.append(f'username admin password {admin_password} role admin')
        
        # Fix admin users that might not have role properly set
        for username, user_info in self.migrator.users.items():
            if user_info.get('role') == 'admin' and username.lower() == 'admin':
                password = user_inputs.get(f'{username.lower()}_password', user_info.get('password', ''))
                if '<password>' in password:
                    password = user_inputs.get('admin_password', '<password>')
                for i, line in enumerate(config_lines):
                    if line.startswith(f'username {username} password {password}') and 'role' not in line:
                        config_lines[i] = f'username {username} password {password} role admin'
        
        if config_lines and not config_lines[-1] == '!':
            config_lines.append('!')
    
    def _generate_vlan_config(self, config_lines: List[str]):
        """Generate VLAN configuration (create + description + IP/MTU/VRRP in single block per VLAN)"""
        if not self.migrator.vlans:
            return
        # VRR (Cumulus) → VRRP behavioral note (active/active vs active/standby)
        if hasattr(self.migrator, 'vrr_configs') and self.migrator.vrr_configs:
            config_lines.append('! VRR (Cumulus Linux) is active/active; SONiC VRRP is active/standby.')
            config_lines.append('! Review VRRP priority and preempt to match desired failover behavior.')
            config_lines.append('!')

        for vlan_id, vlan in sorted(self.migrator.vlans.items(), key=lambda x: int(x[0]) if x[0].isdigit() else 999):
            config_lines.append(f'interface vlan {vlan_id}')
            if vlan.description:
                config_lines.append(f'  description {self._quote_description(vlan.description)}')
            # MTU only when source had explicit "mtu X"
            if vlan.mtu_configured:
                config_lines.append(f'  mtu {vlan.mtu}')
            # VRRP if present (per-vlan vrrp_configs first, then global groups; dedupe by vrid)
            vrrp_groups = list(vlan.vrrp_configs) if vlan.vrrp_configs else []
            vrids_seen = {str(g.get('vrid')) for g in vrrp_groups}
            if self.migrator.vrrp_config.get('groups'):
                for g in self.migrator.vrrp_config['groups']:
                    if g.get('vlan') == vlan_id and str(g.get('vrid')) not in vrids_seen:
                        vrrp_groups.append(g)
                        vrids_seen.add(str(g.get('vrid')))
            # Fallback: VRR/address-virtual stored only in vrr_configs (vrid from MAC if not set)
            if hasattr(self.migrator, 'vrr_configs') and vlan_id in self.migrator.vrr_configs and not vlan.vrrp_configs:
                vrr_config = self.migrator.vrr_configs[vlan_id]
                vip = vrr_config.get('vip', '').split('/')[0] if '/' in vrr_config.get('vip', '') else vrr_config.get('vip', '')
                vrid = vrr_config.get('vrid', '1')
                vrrp_groups.append({'vrid': vrid, 'vip': vip, 'vlan': vlan_id})
            # Deduplicate VRRP commands by (vrid, vip) for this VLAN
            seen_vrrp = set()
            vrrp_groups_deduped = []
            for g in vrrp_groups:
                key = (str(g.get('vrid')), str(g.get('vip', '')))
                if key in seen_vrrp:
                    continue
                seen_vrrp.add(key)
                vrrp_groups_deduped.append(g)
            for vrrp_group in vrrp_groups_deduped:
                if not vrrp_group.get('vip'):
                    continue
                if vrrp_group.get('vlan') is not None and str(vrrp_group.get('vlan')) != str(vlan_id):
                    continue
                line = f'  vrrp {vrrp_group["vrid"]} ipv4 {vrrp_group["vip"]}'
                if vrrp_group.get('priority') is not None:
                    line += f' priority {vrrp_group["priority"]}'
                if vrrp_group.get('preempt'):
                    line += ' preempt'
                config_lines.append(line)
            # IP address if present
            if vlan.ip_address:
                if vlan.subnet_mask:
                    cidr = self.migrator._mask_to_cidr(vlan.subnet_mask)
                    config_lines.append(f'  ip address {vlan.ip_address}/{cidr}')
                else:
                    config_lines.append(f'  ip address {vlan.ip_address}')
            config_lines.append('exit')
            config_lines.append('!')

        config_lines.append('!')
    
    def _generate_mclag_config(self, config_lines: List[str], user_inputs: Dict[str, str]):
        """Generate MCLAG domain block (space-prefixed sub-commands and trailing exit)."""
        if not self.migrator.mlag_config:
            return
        
        # Use domain_id from config, fallback to peer_link_po, then default to 99
        domain_id = self.migrator.mlag_config.get('domain_id') or self.migrator.mlag_config.get('peer_link_po', '99')
        config_lines.append(f'mclag domain {domain_id}')
        
        if 'peer_link_po' in self.migrator.mlag_config:
            config_lines.append(f'  peer-link PortChannel {self.migrator.mlag_config["peer_link_po"]}')
        
        if 'system_mac' in self.migrator.mlag_config:
            config_lines.append(f'  mclag-system-mac {self.migrator.mlag_config["system_mac"]}')
        
        # delay-restore: only emit if value is not 300 (Enterprise SONiC default)
        delay_restore = self.migrator.mlag_config.get('delay_restore')
        if delay_restore is not None and delay_restore != 300:
            config_lines.append(f'  delay-restore {delay_restore}')
        
        # Source IP = Management0 IP (this switch's management interface)
        if self.migrator.management_ip and self.migrator.management_ip != 'dhcp':
            config_lines.append(f'  source-ip {self.migrator.management_ip}')
        elif user_inputs.get('management_ip_cidr'):
            config_lines.append(f'  source-ip {user_inputs["management_ip_cidr"].split("/")[0]}')
        else:
            config_lines.append('  source-ip <Management0-IP>')
        
        # Peer IP = use only the value from the script prompt (VPC/ICP peering may not use Management0 in source config)
        peer_ip = user_inputs.get('mclag_peer_ip')
        if peer_ip:
            config_lines.append(f'  peer-ip {peer_ip}')
        else:
            config_lines.append('  peer-ip <Enter peer Management0 IP at script prompt>')
        
        config_lines.append('exit')
        config_lines.append('!')
    
    def _get_sonic_range_name(self, range_spec: str) -> str:
        """Get SONiC interface range name; range specs stored without 'range ' get it added for conversion."""
        sonic_range = self.migrator.convert_interface_name(range_spec)
        if not sonic_range.startswith('range ') and '-' in range_spec:
            alt = self.migrator.convert_interface_name('range ' + range_spec)
            if alt.startswith('range '):
                return alt
        # HW-3/HW-4 defensive: EAS 'interface-naming standard' mode uses 'Eth'
        # (not 'Ethernet') inside range specs. Any 'range Ethernet' that leaks
        # through parser changes is normalized to the canonical 'range Eth' so
        # a regression in a per-vendor parser cannot reintroduce the cascade
        # failure observed on SSE-T8164.
        if re.match(r'^range\s+Ethernet\b', sonic_range, re.IGNORECASE):
            sonic_range = re.sub(r'^range\s+Ethernet\s*', 'range Eth ', sonic_range, count=1, flags=re.IGNORECASE)
        return sonic_range

    def _is_portchannel_range(self, range_spec: str) -> bool:
        """Return True if this range spec is a PortChannel range (emit with PortChannel block section)."""
        sonic = self._get_sonic_range_name(range_spec)
        return sonic.startswith('range PortChannel') or sonic.lower().startswith('range port-channel')

    def _emit_single_interface_range_block(self, config_lines: List[str], range_spec: str, range_cmds: List[str]) -> None:
        """Emit one interface range block (used for both Ethernet and PortChannel ranges)."""
        sonic_range = self._get_sonic_range_name(range_spec)
        config_lines.append(f'interface {sonic_range}')
        access_vlan = None
        trunk_vlans = None
        switchport_mode = None
        for cmd in range_cmds:
            if cmd.strip() in ['exit', 'end', 'write memory']:
                continue
            # Skip the source "interface range ..." header line (we emit our own)
            if cmd.strip().lower().startswith('interface range'):
                continue
            cmd_lower = cmd.lower().strip()
            if cmd_lower.startswith('switchport mode'):
                switchport_mode = cmd_lower.split()[-1] if len(cmd_lower.split()) > 2 else None
                continue
            if cmd_lower.startswith('speed'):
                speed_value = cmd.split()[1] if len(cmd.split()) > 1 else None
                if speed_value:
                    normalized_speed = self._normalize_speed(speed_value)
                    if normalized_speed:
                        config_lines.append(f'  speed {normalized_speed}')
                continue
            if cmd_lower.startswith('mtu '):
                mtu_value = cmd.split()[1] if len(cmd.split()) > 1 else None
                if mtu_value:
                    config_lines.append(f'  mtu {mtu_value}')
                continue
            if cmd_lower.startswith('switchport access vlan'):
                access_vlan = cmd.split()[-1] if len(cmd.split()) > 3 else None
                continue
            if cmd_lower.startswith('switchport trunk allowed vlan'):
                trunk_vlans = ' '.join(cmd.split()[4:]) if len(cmd.split()) > 4 else None
                continue
            if cmd_lower in ['shutdown', 'no shutdown']:
                continue
            if cmd.strip() and not cmd_lower.startswith('switchport'):
                if cmd_lower.startswith('description '):
                    desc_parts = cmd.split(' ', 1)
                    if len(desc_parts) > 1:
                        desc_value = desc_parts[1].strip()
                        config_lines.append(f'  description {self._quote_description(desc_value)}')
                    else:
                        config_lines.append(f'  {sanitize_for_output(cmd.strip())}')
                else:
                    config_lines.append(f'  {sanitize_for_output(cmd.strip())}')
        if switchport_mode == 'trunk' and trunk_vlans:
            config_lines.append(f'  switchport trunk allowed vlan {trunk_vlans}')
        elif switchport_mode == 'access' and access_vlan:
            config_lines.append(f'  switchport access vlan {access_vlan}')
        config_lines.append('  no shutdown')
        config_lines.append('exit')
        config_lines.append('!')

    def _generate_interface_range_config(self, config_lines: List[str]):
        """Generate interface range configurations (Ethernet ranges only; PortChannel ranges are in PortChannel section)."""
        if not hasattr(self.migrator, 'range_configs') or not self.migrator.range_configs:
            return
        for range_spec, range_cmds in self.migrator.range_configs.items():
            if self._is_portchannel_range(range_spec):
                continue
            self._emit_single_interface_range_block(config_lines, range_spec, range_cmds)
        if hasattr(self.migrator, 'range_configs') and self.migrator.range_configs and any(
            not self._is_portchannel_range(rs) for rs in self.migrator.range_configs
        ):
            config_lines.append('!')
    
    def _generate_physical_interface_config(self, config_lines: List[str]):
        """Generate physical interface configurations"""
        if not self.migrator.physical_interfaces:
            return
        
        # Sort interfaces by final SONiC interface number for cleaner output
        sorted_interfaces = sorted(
            self.migrator.physical_interfaces.items(),
            key=lambda x: self.migrator._extract_sonic_port_number(
                self.migrator.convert_interface_name(x[1].interface)
            )
        )
        
        for interface, intf_config in sorted_interfaces:
            sonic_intf = self.migrator.convert_interface_name(interface)
            config_lines.append(f'interface {sonic_intf}')
            
            # Special handling for interfaces with channel-groups
            if intf_config.channel_group:
                # For channel-group interfaces: speed, mtu, fec, description, channel-group, no shutdown, and LLDP

                # Add speed if present
                if intf_config.speed:
                    # Convert speed format: "forced 1G" -> "1000", "forced 10G" -> "10000", etc.
                    speed_value = self._normalize_speed(intf_config.speed)
                    if speed_value:
                        config_lines.append(f'  speed {speed_value}')

                # HW-9: EAS rejects 'channel-group <N>' when the member port
                # MTU does not match the parent PortChannel MTU
                # ("% Error: Configuration not allowed when port MTU not
                # same as portchannel MTU"). NX-OS / EOS inherit member
                # MTU implicitly; EAS does not. Cross-reference the
                # parent PortChannel's explicit MTU and emit a matching
                # 'mtu <pc_mtu>' BEFORE 'channel-group <N>' so the
                # channel-group binding is accepted.
                pc_id = str(intf_config.channel_group)
                pc_cfg = (self.migrator.port_channels or {}).get(pc_id)
                pc_mtu_emitted = False
                if pc_cfg is not None and getattr(pc_cfg, 'mtu_configured', False):
                    config_lines.append(f'  mtu {pc_cfg.mtu}')
                    pc_mtu_emitted = True

                # Emit member-port MTU only when source had explicit "mtu X"
                # AND we have not already emitted a parent-derived MTU above.
                if intf_config.mtu_configured and not pc_mtu_emitted:
                    config_lines.append(f'  mtu {intf_config.mtu}')

                # Add FEC configuration if present (stays on Eth interface, not PortChannel)
                if intf_config.fec and intf_config.fec != 'auto':
                    config_lines.append(f'  {intf_config.fec}')

                # Add description if present
                if intf_config.description:
                    config_lines.append(f'  description {self._quote_description(intf_config.description)}')

                # Add channel-group (always for channel-group interfaces)
                config_lines.append(f'  channel-group {intf_config.channel_group}')

                # Add no shutdown (always for channel-group interfaces)
                config_lines.append('  no shutdown')
            else:
                # Normal interface without channel-group - add all configuration
                # Emit MTU only when source had explicit "mtu X"
                if intf_config.mtu_configured:
                    config_lines.append(f'  mtu {intf_config.mtu}')
                
                if intf_config.speed:
                    # Convert speed format: "forced 1G" -> "1000", "forced 10G" -> "10000", etc.
                    speed_value = self._normalize_speed(intf_config.speed)
                    if speed_value:
                        config_lines.append(f'  speed {speed_value}')
                
                if intf_config.fec and intf_config.fec != 'auto':
                    config_lines.append(f'  {intf_config.fec}')
                
                # Add description if present
                if intf_config.description:
                    config_lines.append(f'  description {self._quote_description(intf_config.description)}')
                
                # L3 routed: ip address implies L3 (no switchport in SONiC output)
                if getattr(intf_config, 'l3_routed', False) or getattr(intf_config, 'ip_address', ''):
                    if intf_config.ip_address:
                        if intf_config.subnet_mask:
                            cidr = self.migrator._mask_to_cidr(intf_config.subnet_mask)
                            config_lines.append(f'  ip address {intf_config.ip_address}/{cidr}')
                        else:
                            config_lines.append(f'  ip address {intf_config.ip_address}')
                        config_lines.append('  ! OSPF not translated; configure manually if needed')
                    # Skip switchport block for L3
                else:
                    # Add VLAN configuration (no switchport mode command needed)
                    # switchport access vlan = native/untagged VLAN
                    # switchport trunk allowed vlan = tagged VLANs
                    # switchport access vlan = native/untagged VLAN (output uses access vlan)
                    if intf_config.switchport_mode == 'trunk':
                        if intf_config.native_vlan:
                            config_lines.append(f'  switchport access vlan {intf_config.native_vlan}')
                        if intf_config.allowed_vlans:
                            vlans = ','.join(intf_config.allowed_vlans)
                            config_lines.append(f'  switchport trunk allowed vlan {vlans}')
                        else:
                            # Trunk mode without specific VLANs (e.g., "members all" in JunOS)
                            config_lines.append('  ! WARNING: Original config had "all VLANs" - configure manually if needed')
                            config_lines.append('  switchport access vlan 1')
                    elif intf_config.switchport_mode == 'access' and intf_config.access_vlan:
                        config_lines.append(f'  switchport access vlan {intf_config.access_vlan}')
                
                if intf_config.shutdown:
                    config_lines.append('  shutdown')
                else:
                    config_lines.append('  no shutdown')
            
            # LLDP settings apply to ALL interfaces (both with and without channel-groups)
            for lldp_setting in intf_config.lldp_settings:
                # Convert LLDP command (OS-specific conversion would be handled by migrator)
                if lldp_setting.strip():
                    config_lines.append(f'  {lldp_setting.strip()}')
            
            # DCBX settings apply to interfaces that have DCBX enabled
            if intf_config.dcbx_enabled:
                config_lines.append('  dcbx enable')
                config_lines.append('  dcbx tlv-select pfc')
            
            config_lines.append('exit')
            config_lines.append('!')
        
        config_lines.append('!')
    
    def _emit_one_port_channel_block(self, config_lines: List[str], po_id: str, po_config: PortChannelConfig, include_mclag: bool) -> None:
        """Emit one PortChannel interface block (optionally with mclag sub-command)."""
        config_lines.append(f'interface PortChannel {po_id} mode active')
        
        if po_config.mtu_configured:
            config_lines.append(f'  mtu {po_config.mtu}')
        
        if po_config.description:
            config_lines.append(f'  description {self._quote_description(po_config.description)}')
        
        if getattr(po_config, 'l3_routed', False) or getattr(po_config, 'ip_address', ''):
            if po_config.ip_address:
                if po_config.subnet_mask:
                    cidr = self.migrator._mask_to_cidr(po_config.subnet_mask)
                    config_lines.append(f'  ip address {po_config.ip_address}/{cidr}')
                else:
                    config_lines.append(f'  ip address {po_config.ip_address}')
                config_lines.append('  ! OSPF not translated; configure manually if needed')
        else:
            if po_config.mode == 'trunk':
                if po_config.native_vlan:
                    config_lines.append(f'  switchport access vlan {po_config.native_vlan}')
                if po_config.allowed_vlans:
                    vlans = ','.join(po_config.allowed_vlans)
                    config_lines.append(f'  switchport trunk allowed vlan {vlans}')
                else:
                    config_lines.append('  ! WARNING: Original config had "all VLANs" - configure manually if needed')
                    config_lines.append('  switchport access vlan 1')
            elif po_config.mode == 'access' and po_config.access_vlan:
                config_lines.append(f'  switchport access vlan {po_config.access_vlan}')
        
        if include_mclag and po_config.mlag_enabled and self.migrator.mlag_config and self.migrator.mlag_config.get('peer_link_po'):
            peer_link_po = self.migrator.mlag_config['peer_link_po']
            if po_id != peer_link_po:
                mclag_domain = self.migrator.mlag_config.get('domain_id') or peer_link_po
                config_lines.append(f'  mclag {mclag_domain}')
        
        if po_config.spanning_tree_disable:
            # HW-1 defensive: EAS per-interface STP-disable keyword is 'no spanning-tree'
            # (no 'enable' trailing keyword exists). Keep emission minimal and
            # hardware-valid.
            config_lines.append('  no spanning-tree')
        
        config_lines.append('exit')
        config_lines.append('!')

    def _materialize_pc_range_members(self, start: int, end: int, range_cmds: List[str]) -> None:
        """Create individual PortChannelConfig entries for members of a suppressed
        parser-level port-channel range, deriving settings from the range's
        sub-commands. Members that already exist in port_channels are preserved
        (their individual settings take precedence).
        """
        if self.migrator.port_channels is None:
            self.migrator.port_channels = {}
        description = ''
        mtu = 9000
        mtu_configured = False
        mode = ''
        allowed_vlans: List[str] = []
        access_vlan = ''
        native_vlan = ''
        for cmd in range_cmds:
            c = cmd.strip()
            if c.lower().startswith('description '):
                description = c.split(' ', 1)[1]
            elif c.lower().startswith('mtu '):
                try:
                    mtu = int(c.split()[1])
                    mtu_configured = True
                except (IndexError, ValueError):
                    pass
            elif c.lower().startswith('switchport mode '):
                mode = c.split()[-1]
            elif c.lower().startswith('switchport trunk allowed vlan '):
                vlans = ' '.join(c.split()[4:])
                allowed_vlans = [v.strip() for v in vlans.split(',')]
                mode = mode or 'trunk'
            elif c.lower().startswith('switchport access vlan '):
                access_vlan = c.split()[-1]
                mode = mode or 'access'
            elif c.lower().startswith('switchport trunk native vlan '):
                native_vlan = c.split()[-1]
                mode = mode or 'trunk'
        for n in range(start, end + 1):
            key = str(n)
            if key in self.migrator.port_channels:
                continue
            self.migrator.port_channels[key] = PortChannelConfig(
                po_id=key,
                description=description,
                mtu=mtu,
                mtu_configured=mtu_configured,
                mode=mode,
                allowed_vlans=list(allowed_vlans),
                access_vlan=access_vlan,
                native_vlan=native_vlan,
            )

    def _po_settings_tuple(self, po_config: PortChannelConfig):
        """Return a hashable tuple of the fields that must be identical across PortChannels
        in order to be emitted as a range block (FR-5)."""
        return (
            po_config.description,
            po_config.mtu,
            po_config.mtu_configured,
            po_config.mode,
            tuple(po_config.allowed_vlans),
            po_config.access_vlan,
            po_config.native_vlan,
            po_config.mlag_enabled,
            po_config.spanning_tree_disable,
            po_config.l3_routed,
            po_config.ip_address,
            po_config.subnet_mask,
        )

    def _plan_port_channel_emission(self):
        """Apply FR-5: decide which PortChannel IDs to emit as ranges vs individuals.

        Rule: emit range form 'interface PortChannel M-N' iff the range covers
        >= 3 consecutive PortChannels AND all PortChannels in [M..N] share
        identical sub-command settings. Otherwise emit individual blocks.
        The two forms are mutually exclusive per PortChannel.

        Returns (ranges_to_emit, individual_ids_to_emit) where ranges_to_emit
        is a list of (range_id, representative_po_config) tuples sorted by
        start number, and individual_ids_to_emit is a sorted list of po_ids
        that should be emitted as single blocks (never members of a range).
        """
        all_pos = self.migrator.port_channels or {}
        ranges: List[tuple] = []   # list of (start:int, end:int, range_id:str)
        for po_id in all_pos:
            if '-' in po_id:
                parts = po_id.split('-')
                if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                    start, end = int(parts[0]), int(parts[1])
                    if end >= start:
                        ranges.append((start, end, po_id))

        ranges.sort()
        emit_as_range: List[tuple] = []
        covered_by_range: set = set()

        for start, end, range_id in ranges:
            span = end - start + 1
            if span < 3:
                # Rule requires >= 3 consecutive PortChannels; emit as individuals instead.
                continue
            # Collect the range config itself plus any individual PortChannel in [start..end].
            range_po = all_pos[range_id]
            member_configs = [range_po]
            for n in range(start, end + 1):
                key = str(n)
                if key in all_pos:
                    member_configs.append(all_pos[key])
            # Require identical settings across the range object and all individual peers.
            reference = self._po_settings_tuple(range_po)
            if all(self._po_settings_tuple(pc) == reference for pc in member_configs):
                emit_as_range.append((range_id, range_po))
                for n in range(start, end + 1):
                    covered_by_range.add(str(n))
                covered_by_range.add(range_id)

        # Non-emitted range objects (span < 3, or settings differ) are treated as
        # a request to emit each member individually. Backfill each member from
        # the range config when no individual object exists.
        for start, end, range_id in ranges:
            if range_id in covered_by_range:
                continue
            range_po = all_pos[range_id]
            for n in range(start, end + 1):
                key = str(n)
                if key not in all_pos:
                    # Materialize a copy so the emit pass has a block to render.
                    import copy
                    cloned = copy.deepcopy(range_po)
                    cloned.po_id = key
                    all_pos[key] = cloned
            # Exclude the range_id itself from emission; its members now stand alone.
            covered_by_range.add(range_id + '_EXCLUDE')

        individual_ids: List[str] = []
        for po_id in all_pos:
            if '-' in po_id:
                # Skip any range object: either handled above as range or expanded to members.
                continue
            if po_id in covered_by_range:
                # Member of an emitted range block; suppressed to avoid duplicate declaration.
                continue
            individual_ids.append(po_id)
        individual_ids.sort(key=lambda x: int(x) if x.isdigit() else 999)

        return emit_as_range, individual_ids

    def _generate_port_channel_config(self, config_lines: List[str], user_inputs: Dict[str, str]):
        """Generate port-channel config per FR-5.

        Non-MCLAG individual POs first, then MCLAG domain block, then MCLAG POs.
        Range blocks (emitted per FR-5 rule) follow the same MCLAG/non-MCLAG
        ordering as individual blocks.
        """
        # Apply FR-5 to parser-level interface_range configs for port-channels (e.g.
        # 'interface range port-channel 30-35', stored in range_configs rather than
        # port_channels). The rule: emit range form iff span >= 3 AND no individual
        # PortChannel in [M..N] already exists. Otherwise, materialize individual
        # PortChannel objects for uncovered members (so FR-5's "every PortChannel
        # declared exactly once" invariant still holds) and suppress the range.
        pc_range_specs = []
        if hasattr(self.migrator, 'range_configs') and self.migrator.range_configs:
            for rs, cmds in self.migrator.range_configs.items():
                if not self._is_portchannel_range(rs):
                    continue
                m = re.search(r'(\d+)\s*-\s*(\d+)', rs)
                if not m:
                    pc_range_specs.append((rs, cmds))
                    continue
                start, end = int(m.group(1)), int(m.group(2))
                span = end - start + 1
                suppress_range = False
                reason_individual = False
                if span < 3:
                    suppress_range = True
                    reason_individual = True
                else:
                    members_present_individually = [
                        str(n) for n in range(start, end + 1)
                        if str(n) in (self.migrator.port_channels or {})
                    ]
                    if members_present_individually:
                        suppress_range = True
                        reason_individual = True

                if suppress_range:
                    if reason_individual:
                        # Materialize uncovered members from the range's sub-commands so
                        # every PortChannel in [start..end] is emitted as an individual.
                        self._materialize_pc_range_members(start, end, cmds)
                    continue
                pc_range_specs.append((rs, cmds))

        # FR-5: decide range vs individual emission AFTER materialization so new
        # individual PortChannel objects are picked up.
        emit_as_range, individual_ids = self._plan_port_channel_emission()

        if not individual_ids and not emit_as_range and not pc_range_specs:
            return

        peer_link_po = self.migrator.mlag_config.get('peer_link_po') if self.migrator.mlag_config else None
        sorted_pos = [(pid, self.migrator.port_channels[pid]) for pid in individual_ids]

        # First pass: non-MCLAG individual PortChannels (or the peer-link itself).
        for po_id, po_config in sorted_pos:
            if not po_config.mlag_enabled or po_id == peer_link_po:
                self._emit_one_port_channel_block(config_lines, po_id, po_config, include_mclag=False)

        # First-pass ranges: non-MCLAG range blocks (keep FR-5 range-vs-individual mutually exclusive).
        for range_id, range_po in emit_as_range:
            if not range_po.mlag_enabled:
                self._emit_one_port_channel_block(config_lines, range_id, range_po, include_mclag=False)

        # MCLAG domain block.
        if self.migrator.mlag_config:
            self._generate_mclag_config(config_lines, user_inputs)

        # Second pass: MCLAG member individual PortChannels (excluding peer-link).
        for po_id, po_config in sorted_pos:
            if po_config.mlag_enabled and po_id != peer_link_po:
                self._emit_one_port_channel_block(config_lines, po_id, po_config, include_mclag=True)

        # Second-pass ranges: MCLAG range blocks.
        for range_id, range_po in emit_as_range:
            if range_po.mlag_enabled:
                self._emit_one_port_channel_block(config_lines, range_id, range_po, include_mclag=True)

        # Parser-level interface range port-channel blocks (kept for compatibility with
        # configs that expressed PortChannel ranges via 'interface range port-channel ...').
        for range_spec, range_cmds in pc_range_specs:
            self._emit_single_interface_range_block(config_lines, range_spec, range_cmds)

        if sorted_pos or emit_as_range or pc_range_specs:
            config_lines.append('!')
    
    def _generate_loopback_config(self, config_lines: List[str]):
        """Generate loopback interface configurations"""
        if not self.migrator.loopbacks:
            return
        
        for loopback_id, loopback in sorted(self.migrator.loopbacks.items()):
            config_lines.append(f'interface {loopback.interface}')
            
            if loopback.description:
                config_lines.append(f'  description {self._quote_description(loopback.description)}')
            
            if loopback.ip_address:
                if loopback.subnet_mask:
                    cidr = self.migrator._mask_to_cidr(loopback.subnet_mask)
                    config_lines.append(f'  ip address {loopback.ip_address}/{cidr}')
                else:
                    config_lines.append(f'  ip address {loopback.ip_address}')
            
            config_lines.append('exit')
            config_lines.append('!')
        
        config_lines.append('!')
    
    def _normalize_speed(self, speed: str) -> Optional[str]:
        """Normalize speed values to SONiC format
        
        Converts formats like:
        - "forced 1G" -> "1000"
        - "forced 10G" -> "10000"
        - "1000" -> "1000"
        - "10000" -> "10000"
        """
        if not speed:
            return None
        
        speed_lower = speed.lower().strip()
        
        # Remove "forced" keyword if present
        if 'forced' in speed_lower:
            speed_lower = speed_lower.replace('forced', '').strip()
        
        # Convert G suffix to numeric value
        if speed_lower.endswith('g'):
            speed_num = speed_lower[:-1].strip()
            try:
                num = float(speed_num)
                return str(int(num * 1000))
            except ValueError:
                pass
        
        # If it's already a number, return as-is
        if speed_lower.isdigit():
            return speed_lower
        
        # Try to extract number from string
        import re
        match = re.search(r'(\d+)', speed_lower)
        if match:
            return match.group(1)
        
        return None
    
    def _generate_static_routes_config(self, config_lines: List[str]):
        """Generate static route configurations"""
        if not self.migrator.static_routes:
            return
        
        for route in self.migrator.static_routes:
            if route.network and route.next_hop:
                if route.mask:
                    cidr = self.migrator._mask_to_cidr(route.mask)
                    network = f'{route.network}/{cidr}'
                else:
                    network = route.network
                
                if route.interface:
                    config_lines.append(f'ip route {network} {route.interface}')
                else:
                    config_lines.append(f'ip route {network} {route.next_hop}')
        
        if self.migrator.static_routes:
            config_lines.append('!')
    
    def _generate_prefix_list_config(self, config_lines: List[str]):
        """Generate IP prefix-list configuration (Enterprise SONiC: ip prefix-list <name> seq <n> permit|deny <prefix> [ge] [le])"""
        if not self.migrator.prefix_lists:
            return
        config_lines.append('! IP prefix-lists')
        for list_name, entries in self.migrator.prefix_lists.items():
            for e in sorted(entries, key=lambda x: x.seq):
                line = f'ip prefix-list {e.list_name} seq {e.seq} {e.action} {e.prefix}'
                if e.ge is not None:
                    line += f' ge {e.ge}'
                if e.le is not None:
                    line += f' le {e.le}'
                config_lines.append(line)
        config_lines.append('!')
    
    def _generate_route_map_config(self, config_lines: List[str]):
        """Generate route-map configuration (Enterprise SONiC: route-map <name> permit|deny <seq> then match/set).
        Enterprise SONiC implicitly drops if no sequence matches; no explicit final deny needed."""
        if not self.migrator.route_maps:
            return
        config_lines.append('! Route-maps')
        for map_name, entries in self.migrator.route_maps.items():
            for entry in sorted(entries, key=lambda x: x.seq):
                config_lines.append(f'route-map {entry.map_name} {entry.action} {entry.seq}')
                for m in entry.matches:
                    # Convert interface names in "match interface ..." to SONiC format
                    if m.startswith('match interface '):
                        parts = m.split(None, 2)
                        if len(parts) >= 3:
                            intfs = parts[2].split()
                            converted = [self.migrator.convert_interface_name(i) for i in intfs]
                            config_lines.append(f' match interface {" ".join(converted)}')
                        else:
                            config_lines.append(f' {m}')
                    else:
                        config_lines.append(f' {m}')
                for s in entry.sets:
                    config_lines.append(f' {s}')
        config_lines.append('!')
    
    def _generate_bgp_config(self, config_lines: List[str]):
        """Generate BGP configuration"""
        if not self.migrator.bgp_config or 'asn' not in self.migrator.bgp_config:
            return
        
        config_lines.append(f'router bgp {self.migrator.bgp_config["asn"]}')
        
        # Router ID if present
        if 'router_id' in self.migrator.bgp_config:
            config_lines.append(f' router-id {self.migrator.bgp_config["router_id"]}')
        
        # Address family with redistribute statements.
        # FR-4: Cisco NX-OS / Arista EOS use 'redistribute direct'; EAS FRR uses
        # 'redistribute connected'. Map the first token and preserve the rest
        # (e.g., the route-map suffix) untouched.
        config_lines.append(' address-family ipv4 unicast')
        for redistribute in self.migrator.bgp_config.get('redistribute', []):
            tokens = redistribute.split()
            if tokens and tokens[0] == 'direct':
                tokens[0] = 'connected'
                redistribute = ' '.join(tokens)
            config_lines.append(f'  redistribute {redistribute}')
        config_lines.append(' exit')
        
        # Peer groups - each with their own address-family
        for pg_name, pg_config in self.migrator.bgp_config.get('peer_groups', {}).items():
            config_lines.append(f' peer-group {pg_name}')
            config_lines.append('  address-family ipv4 unicast')
            config_lines.append('   activate')
            config_lines.append('  exit')
            # Get remote AS from peer group config or use local ASN
            remote_as = pg_config.get('remote_as', self.migrator.bgp_config['asn'])
            config_lines.append(f'  remote-as {remote_as}')
        
        # Neighbors with peer-group assignments
        for member in self.migrator.bgp_config.get('peer_group_members', []):
            config_lines.append(f' neighbor {member["neighbor"]}')
            config_lines.append(f'  peer-group {member["peer_group"]}')
            # Per-neighbor remote-as overrides group when present (e.g. different AS per neighbor)
            nbr_remote_as = self.migrator.bgp_config.get('neighbor_remote_as', {}).get(member["neighbor"])
            if nbr_remote_as is not None:
                config_lines.append(f'  remote-as {nbr_remote_as}')
            
            # Add description if present
            if 'neighbor_descriptions' in self.migrator.bgp_config:
                desc = self.migrator.bgp_config['neighbor_descriptions'].get(member["neighbor"])
                if desc:
                    config_lines.append(f'  description {self._quote_description(desc)}')
            
            # Add ebgp-multihop if present
            if 'neighbor_multihop' in self.migrator.bgp_config:
                multihop = self.migrator.bgp_config['neighbor_multihop'].get(member["neighbor"])
                if multihop:
                    config_lines.append(f'  ebgp-multihop {multihop}')
            
            # Add update-source if present
            if 'neighbor_update_source' in self.migrator.bgp_config:
                update_source = self.migrator.bgp_config['neighbor_update_source'].get(member["neighbor"])
                if update_source:
                    # HW-5: canonicalize interface casing for EAS (Loopback0, not loopback0)
                    # HW-10: emit 'interface' keyword for interface-name source
                    config_lines.append(self._emit_update_source(update_source))
            
            # Route-maps on neighbor
            if 'neighbor_route_map_in' in self.migrator.bgp_config:
                rm_in = self.migrator.bgp_config['neighbor_route_map_in'].get(member["neighbor"])
                if rm_in:
                    config_lines.append(f'  route-map {rm_in} in')
            if 'neighbor_route_map_out' in self.migrator.bgp_config:
                rm_out = self.migrator.bgp_config['neighbor_route_map_out'].get(member["neighbor"])
                if rm_out:
                    config_lines.append(f'  route-map {rm_out} out')
        
        # Standalone neighbors (individual_neighbors: no peer-group)
        for neighbor_ip, nbr_cfg in sorted(self.migrator.bgp_config.get('individual_neighbors', {}).items()):
            remote_as = nbr_cfg.get('remote_as', self.migrator.bgp_config['asn'])
            config_lines.append(f' neighbor {neighbor_ip}')
            config_lines.append(f'  remote-as {remote_as}')
            if 'neighbor_descriptions' in self.migrator.bgp_config:
                desc = self.migrator.bgp_config['neighbor_descriptions'].get(neighbor_ip)
                if desc:
                    config_lines.append(f'  description {self._quote_description(desc)}')
            if 'neighbor_update_source' in self.migrator.bgp_config:
                update_source = self.migrator.bgp_config['neighbor_update_source'].get(neighbor_ip)
                if update_source:
                    # HW-5: canonicalize interface casing for EAS (Loopback0, not loopback0)
                    # HW-10: emit 'interface' keyword for interface-name source
                    config_lines.append(self._emit_update_source(update_source))
            if 'neighbor_multihop' in self.migrator.bgp_config:
                multihop = self.migrator.bgp_config['neighbor_multihop'].get(neighbor_ip)
                if multihop:
                    config_lines.append(f'  ebgp-multihop {multihop}')
            # Route-maps on neighbor
            if 'neighbor_route_map_in' in self.migrator.bgp_config:
                rm_in = self.migrator.bgp_config['neighbor_route_map_in'].get(neighbor_ip)
                if rm_in:
                    config_lines.append(f'  route-map {rm_in} in')
            if 'neighbor_route_map_out' in self.migrator.bgp_config:
                rm_out = self.migrator.bgp_config['neighbor_route_map_out'].get(neighbor_ip)
                if rm_out:
                    config_lines.append(f'  route-map {rm_out} out')
        
        config_lines.append('exit')
        config_lines.append('!')
    
    def _generate_dcbx_config(self, config_lines: List[str]):
        """Generate DCBX configuration for SONiC"""
        if not self.migrator.dcbx_configs:
            return
        
        # Global DCBX enable
        config_lines.extend([
            'dcbx enable',
            '!'
        ])
    
    def _generate_syslog_config(self, config_lines: List[str]):
        """Generate Syslog configuration for SONiC"""
        if not self.migrator.syslog_config.servers:
            return
        
        for server in self.migrator.syslog_config.servers:
            config_lines.extend([
                f'logging server {server}',
                '!'
            ])
    
    def _generate_ntp_config(self, config_lines: List[str], user_inputs: Optional[Dict[str, str]] = None):
        """Generate NTP configuration for SONiC (include prefer flag when present in source or from prompt)"""
        ntp_servers = self.migrator.global_settings.get('ntp_servers', [])
        preferred_server = (
            self.migrator.global_settings.get('ntp_preferred_server') or
            (user_inputs and user_inputs.get('ntp_preferred_server'))
        )
        
        if ntp_servers:
            # Output preferred server first with prefer flag, then remaining servers
            if preferred_server and preferred_server in ntp_servers:
                config_lines.extend([
                    f'ntp server {preferred_server} prefer',
                    '!'
                ])
                for server in ntp_servers:
                    if server != preferred_server:
                        config_lines.extend([
                            f'ntp server {server}',
                            '!'
                        ])
            else:
                for server in ntp_servers:
                    config_lines.extend([
                        f'ntp server {server}',
                        '!'
                    ])
        elif 'ntp_server' in self.migrator.global_settings:
            # Fallback to single ntp_server setting
            single = self.migrator.global_settings["ntp_server"]
            prefer_suffix = ' prefer' if preferred_server == single else ''
            config_lines.extend([
                f'ntp server {single}{prefer_suffix}',
                '!'
            ])
    
    def _generate_dns_config(self, config_lines: List[str]):
        """Generate DNS configuration for Enterprise SONiC CLI (ip name-server source-interface, ip name-server <ip> [vrf])"""
        name_servers = self.migrator.global_settings.get('name_servers') or []
        if not name_servers:
            return
        # Step 1: Source interface for DNS queries (Enterprise SONiC requires this)
        # Use Management 0 as default; user can change to Loopback 0 or Vlan <id> if needed
        config_lines.extend([
            '! DNS server configuration (Enterprise SONiC 5.18)',
            'ip name-server source-interface Management 0',
        ])
        # Step 2: Add each name server (ip name-server <ip> [vrf vrf-name])
        # Only append "vrf x" when source had a VRF other than default
        for entry in name_servers:
            if isinstance(entry, dict):
                ip = entry['ip']
                vrf = entry.get('vrf')
            else:
                ip = entry
                vrf = None
            if vrf and vrf != 'default':
                config_lines.append(f'ip name-server {ip} vrf {vrf}')
            else:
                config_lines.append(f'ip name-server {ip}')
        config_lines.append('!')
    
    def _generate_radius_config(self, config_lines: List[str], user_inputs: Dict[str, str]):
        """Generate RADIUS configuration for SONiC"""
        if not self.migrator.radius_config or not self.migrator.radius_config.host:
            return
        
        # Always use user input for RADIUS key since source config keys are encrypted
        # The parsed key from config is encrypted and cannot be used directly
        radius_key = user_inputs.get('radius_key', 'CHANGE_ME')
        # If prompt input was misaligned (e.g. gateway/IP consumed as key), avoid emitting an IP as key
        if radius_key and re.match(r'^\d+\.\d+\.\d+\.\d+', radius_key.strip()):
            radius_key = 'CHANGE_ME'
        
        # Build radius-server command with available parameters
        radius_cmd = f'radius-server host {self.migrator.radius_config.host} key {radius_key}'
        if self.migrator.radius_config.retransmit:
            radius_cmd += f' retransmit {self.migrator.radius_config.retransmit}'
        if self.migrator.radius_config.timeout:
            radius_cmd += f' timeout {self.migrator.radius_config.timeout}'
        
        config_lines.extend([
            radius_cmd,
            '!',
            'aaa authentication login default group radius local',
            'aaa authentication login console local',
            '!'
        ])
    
    def _generate_snmp_config(self, config_lines: List[str]):
        """Generate SNMP configuration for SONiC"""
        if not self.migrator.snmp_config.communities:
            return
        
        for community_name, permission in self.migrator.snmp_config.communities.items():
            # HW-2: EAS IS-CLI does not accept a trailing 'ro'/'rw' keyword on
            # 'snmp-server community'. The valid form is
            # 'snmp-server community <name> [group <group-name>]'. Drop the
            # permission keyword. RO/RW distinction via ACL-backed groups is a
            # follow-on item (file separately when required).
            _ = permission  # retained in parser for future group mapping
            config_lines.extend([
                f'snmp-server community {community_name}',
                '!'
            ])
