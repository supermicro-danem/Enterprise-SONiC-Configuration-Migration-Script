#!/usr/bin/env python3
"""
Juniper JunOS Configuration Parser (QFX)

This module provides parsing logic for Juniper JunOS (QFX) configurations
and converts them to Enterprise SONiC format.
"""

import re
from typing import Dict, List, Optional, Tuple
from base_migrator import (
    BaseMigrator, VlanConfig, PortChannelConfig, PhysicalInterfaceConfig,
    LoopbackConfig, StaticRouteConfig, PrefixListEntry, RouteMapEntry
)


class JuniperJunOSMigrator(BaseMigrator):
    """Migrator for Juniper JunOS (QFX) configurations"""
    
    def __init__(self):
        """Initialize the Juniper JunOS migrator"""
        super().__init__()
        self.config_stack: List[str] = []  # Stack for hierarchical parsing
        self.current_path: List[str] = []  # Current path in hierarchy
        self.pending_vlan_mappings: List[Dict] = []  # Store VLAN name references to resolve later
        # Policy-statement parsing state (term/from/then)
        self.current_policy_statement: Optional[str] = None
        self.current_term_name: Optional[str] = None
        self.current_from: List[Tuple[str, str]] = []  # (type, value) e.g. ("interface", "lo0.0")
        self.current_then: Optional[str] = None  # "accept" | "reject"
    
    def parse_config(self, config: str):
        """Parse JunOS configuration into structured data"""
        self.reset_state()
        self.config_stack = []
        self.current_path = []
        lines = config.split('\n')
        brace_count = 0
        in_string = False
        current_line = ""
        
        for line_num, line in enumerate(lines, start=1):
            self.current_line_number = line_num
            stripped = line.strip()
            
            # Skip empty lines and comments
            if not stripped or stripped.startswith('##') or (stripped.startswith('#') and not stripped.startswith('##')):
                continue
            
            # Handle hierarchical structure
            self._parse_hierarchical_line(stripped, line_num)
        
        # Finalize last policy-statement term if any
        self._finalize_policy_term()
        self.current_policy_statement = None
        
        # After parsing is complete, resolve any pending VLAN name-to-ID mappings
        self._resolve_vlan_name_mappings()
    
    def _resolve_vlan_name_mappings(self):
        """Resolve VLAN name references to numeric IDs. Can be called at end of parse or incrementally when vlans are defined."""
        applied = []
        for mapping in list(self.pending_vlan_mappings):
            interface_name = mapping['interface_name']
            vlan_names = mapping['vlan_names']
            is_port_channel = mapping['is_port_channel']
            is_trunk = mapping['is_trunk']
            existing_vlans = mapping.get('existing_vlans', [])
            
            # Resolve VLAN names to IDs (match name case-insensitively, normalize hyphens/spaces)
            def _norm(s):
                return re.sub(r'[\s\-]+', '-', (s or '').strip().lower()).strip('-')
            resolved_vlans = list(existing_vlans)  # Start with any already resolved VLANs
            resolved_count = 0
            for vlan_name in vlan_names:
                n = _norm(vlan_name)
                for vlan_id, vlan_obj in self.vlans.items():
                    if _norm(vlan_obj.name) == n:
                        resolved_vlans.append(vlan_id)
                        resolved_count += 1
                        break
            if not resolved_vlans:
                continue
            # Only remove from pending when fully resolved (so partial apply doesn't drop the mapping)
            fully_resolved = (resolved_count >= len(vlan_names))
            
            # Apply resolved VLANs to the interface (use actual mode from interface at resolution time)
            if is_port_channel:
                ae_id = interface_name.replace('ae', '')
                if ae_id not in self.port_channels:
                    self.port_channels[ae_id] = PortChannelConfig(po_id=ae_id)
                
                if is_trunk:
                    # Merge with existing allowed VLANs
                    if self.port_channels[ae_id].allowed_vlans:
                        # Combine and deduplicate
                        all_vlans = list(set(self.port_channels[ae_id].allowed_vlans + resolved_vlans))
                        self.port_channels[ae_id].allowed_vlans = sorted(all_vlans, key=lambda x: int(x) if x.isdigit() else 999)
                    else:
                        self.port_channels[ae_id].allowed_vlans = resolved_vlans
                    # Ensure mode is set
                    if not self.port_channels[ae_id].mode:
                        self.port_channels[ae_id].mode = 'trunk'
                else:
                    # Access mode - use first VLAN
                    if not self.port_channels[ae_id].access_vlan:
                        self.port_channels[ae_id].access_vlan = resolved_vlans[0]
                    # Ensure mode is set
                    if not self.port_channels[ae_id].mode:
                        self.port_channels[ae_id].mode = 'access'
                if fully_resolved:
                    applied.append(mapping)
            else:
                if interface_name not in self.physical_interfaces:
                    self.physical_interfaces[interface_name] = PhysicalInterfaceConfig(interface=interface_name)
                
                # Check actual mode from interface config (may have been set after pending mapping was created)
                actual_is_trunk = is_trunk
                if self.physical_interfaces[interface_name].switchport_mode == 'trunk':
                    actual_is_trunk = True
                elif self.physical_interfaces[interface_name].switchport_mode == 'access':
                    actual_is_trunk = False
                
                if actual_is_trunk:
                    # Merge with existing allowed VLANs
                    if self.physical_interfaces[interface_name].allowed_vlans:
                        # Combine and deduplicate
                        all_vlans = list(set(self.physical_interfaces[interface_name].allowed_vlans + resolved_vlans))
                        self.physical_interfaces[interface_name].allowed_vlans = sorted(all_vlans, key=lambda x: int(x) if x.isdigit() else 999)
                    else:
                        self.physical_interfaces[interface_name].allowed_vlans = resolved_vlans
                else:
                    # Access mode - use first VLAN
                    if not self.physical_interfaces[interface_name].access_vlan:
                        self.physical_interfaces[interface_name].access_vlan = resolved_vlans[0]
                    # Ensure switchport_mode is set
                    if not self.physical_interfaces[interface_name].switchport_mode:
                        self.physical_interfaces[interface_name].switchport_mode = 'access'
                if fully_resolved:
                    applied.append(mapping)
        
        # Remove only the mappings we fully applied (allows incremental resolution when vlans are parsed after interfaces)
        for mapping in applied:
            try:
                self.pending_vlan_mappings.remove(mapping)
            except ValueError:
                pass
    
    def _parse_hierarchical_line(self, line: str, line_num: int):
        """Parse a line in hierarchical JunOS format"""
        # Count braces to handle nested structures
        open_braces = line.count('{')
        close_braces = line.count('}')
        
        # Handle opening braces
        if open_braces > 0:
            # Extract the statement before the first brace
            parts = line.split('{', 1)
            statement = parts[0].strip()
            
            if statement:
                self._process_statement(statement, line_num)
            
            # Push to stack for each opening brace
            for _ in range(open_braces):
                self.config_stack.append(statement if statement else 'block')
                self.current_path.append(statement if statement else 'block')
                self.push_context(statement if statement else 'block')
                statement = 'block'  # Subsequent braces are just blocks
        
        # Handle closing braces
        if close_braces > 0:
            for _ in range(close_braces):
                if self.config_stack:
                    self.config_stack.pop()
                if self.current_path:
                    self.current_path.pop()
                self.pop_context()
        
        # Handle statements with semicolon (but no braces on this line)
        if ';' in line and open_braces == 0 and close_braces == 0:
            statement = line.rstrip(';').strip()
            if statement:
                self._process_statement(statement, line_num)
    
    def _process_statement(self, statement: str, line_num: int):
        """Process a configuration statement"""
        # Skip empty statements
        if not statement.strip():
            return
        
        # Get current context
        path_str = ' > '.join(self.current_path) if self.current_path else 'global'
        
        # System configuration
        if 'system' in self.current_path:
            self._parse_system_config(statement, line_num)
        
        # VLAN members within interface config (nested under vlan { members ... })
        # This must come BEFORE the general interfaces check to handle nested vlan blocks
        if any('vlan' in p for p in self.current_path) and 'interfaces' in self.current_path and 'members' in statement:
            # This handles "vlan { members ... }" nested inside interface config
            # Get the interface name from the path (path can have "unit 0" so match xe/et/ge/ae/irb)
            interface_name = None
            for path_part in self.current_path:
                if path_part.startswith(('et-', 'xe-', 'ge-', 'ae', 'irb')):
                    interface_name = path_part
                    break
            
            # Ensure port-channel exists if it's an ae interface
            if interface_name and interface_name.startswith('ae'):
                ae_id = interface_name.replace('ae', '')
                if ae_id not in self.port_channels:
                    self.port_channels[ae_id] = PortChannelConfig(po_id=ae_id)
            
            # Also check if we need to create the interface object
            if interface_name and interface_name not in self.physical_interfaces and not interface_name.startswith('ae'):
                self.physical_interfaces[interface_name] = PhysicalInterfaceConfig(interface=interface_name)
            
            if interface_name:
                # Parse VLAN members with deferred resolution
                if 'all' in statement:
                    vlans = []
                else:
                    # Try numeric IDs first (must contain at least one digit)
                    vlan_match = re.search(r'members\s+\[?\s*([\d\s,]+)\]?', statement)
                    if vlan_match:
                        vlans_str = vlan_match.group(1)
                        # Only use this match if it contains at least one digit
                        if any(c.isdigit() for c in vlans_str):
                            vlans = [v.strip() for v in re.split(r'[\s,]+', vlans_str) if v.strip()]
                        else:
                            vlan_match = None  # Force it to try the next regex
                    
                    if not vlan_match:
                        # Try single numeric
                        vlan_match = re.search(r'members\s+(\d+)', statement)
                        if vlan_match:
                            vlans = [vlan_match.group(1)]
                    
                    if not vlan_match:
                            # Try VLAN names - use deferred resolution
                            vlan_name_match = re.search(r'members\s+\[?\s*([A-Za-z0-9_\-\s,]+)\]?', statement)
                            if vlan_name_match:
                                vlans_str = vlan_name_match.group(1)
                                vlan_names = [v.strip() for v in re.split(r'[\s,]+', vlans_str) if v.strip()]
                                # Try immediate resolution, but store for deferred resolution if needed
                                vlans = []
                                unresolved_names = []
                                for vlan_name in vlan_names:
                                    # Check if it's actually a numeric ID
                                    if vlan_name.isdigit():
                                        vlans.append(vlan_name)
                                    else:
                                        # Try to find VLAN ID by name
                                        found = False
                                        for vlan_id, vlan_obj in self.vlans.items():
                                            if vlan_obj.name == vlan_name:
                                                vlans.append(vlan_id)
                                                found = True
                                                break
                                        if not found:
                                            unresolved_names.append(vlan_name)
                                
                                # If we have unresolved names, store for later resolution
                                if unresolved_names:
                                    # Determine if trunk or access mode from path or interface config
                                    # First check if mode is already set on the interface/port-channel (most reliable)
                                    is_trunk = False
                                    if interface_name.startswith('ae'):
                                        ae_id = interface_name.replace('ae', '')
                                        if ae_id in self.port_channels and self.port_channels[ae_id].mode:
                                            is_trunk = self.port_channels[ae_id].mode == 'trunk'
                                    else:
                                        if interface_name in self.physical_interfaces and self.physical_interfaces[interface_name].switchport_mode:
                                            is_trunk = self.physical_interfaces[interface_name].switchport_mode == 'trunk'
                                    
                                    # If mode not set yet, check path for interface-mode trunk/access
                                    if not is_trunk:
                                        path_str = ' '.join(self.current_path).lower()
                                        is_trunk = 'interface-mode trunk' in path_str or ('trunk' in path_str and 'access' not in path_str and 'interface-mode' in path_str)
                                    
                                    self.pending_vlan_mappings.append({
                                        'interface_name': interface_name,
                                        'vlan_names': unresolved_names,
                                        'is_port_channel': interface_name.startswith('ae'),
                                        'is_trunk': is_trunk,
                                        'existing_vlans': vlans  # Keep any resolved VLANs
                                    })
                            else:
                                # Single VLAN name - use deferred resolution
                                vlan_name_match = re.search(r'members\s+([A-Za-z0-9_\-]+)', statement)
                                if vlan_name_match:
                                    vlan_name = vlan_name_match.group(1)
                                    # Check if it's actually a numeric ID
                                    if vlan_name.isdigit():
                                        vlans = [vlan_name]
                                    else:
                                        # Try to find VLAN ID by name
                                        found = False
                                        for vlan_id, vlan_obj in self.vlans.items():
                                            if vlan_obj.name == vlan_name:
                                                vlans = [vlan_id]
                                                found = True
                                                break
                                        if not found:
                                            # Store for later resolution
                                            # Determine if trunk or access mode from path or interface config
                                            # First check if mode is already set on the interface/port-channel (most reliable)
                                            is_trunk = False
                                            if interface_name.startswith('ae'):
                                                ae_id = interface_name.replace('ae', '')
                                                if ae_id in self.port_channels and self.port_channels[ae_id].mode:
                                                    is_trunk = self.port_channels[ae_id].mode == 'trunk'
                                            else:
                                                if interface_name in self.physical_interfaces and self.physical_interfaces[interface_name].switchport_mode:
                                                    is_trunk = self.physical_interfaces[interface_name].switchport_mode == 'trunk'
                                            
                                            # If mode not set yet, check path for interface-mode trunk/access
                                            if not is_trunk:
                                                path_str = ' '.join(self.current_path).lower()
                                                is_trunk = 'interface-mode trunk' in path_str or ('trunk' in path_str and 'access' not in path_str and 'interface-mode' in path_str)
                                            
                                            self.pending_vlan_mappings.append({
                                                'interface_name': interface_name,
                                                'vlan_names': [vlan_name],
                                                'is_port_channel': interface_name.startswith('ae'),
                                                'is_trunk': is_trunk,
                                                'existing_vlans': []
                                            })
                                else:
                                    vlans = []
                
                # Apply VLANs to interface (only if we have resolved VLANs)
                if vlans:
                    if interface_name.startswith('ae'):
                        ae_id = interface_name.replace('ae', '')
                        if ae_id not in self.port_channels:
                            self.port_channels[ae_id] = PortChannelConfig(po_id=ae_id)
                        if self.port_channels[ae_id].mode == 'trunk':
                            if self.port_channels[ae_id].allowed_vlans:
                                # Merge
                                all_vlans = list(set(self.port_channels[ae_id].allowed_vlans + vlans))
                                self.port_channels[ae_id].allowed_vlans = sorted(all_vlans, key=lambda x: int(x) if x.isdigit() else 999)
                            else:
                                self.port_channels[ae_id].allowed_vlans = vlans
                        elif self.port_channels[ae_id].mode == 'access':
                            if not self.port_channels[ae_id].access_vlan:
                                self.port_channels[ae_id].access_vlan = vlans[0]
                    else:
                        if interface_name not in self.physical_interfaces:
                            self.physical_interfaces[interface_name] = PhysicalInterfaceConfig(interface=interface_name)
                        if self.physical_interfaces[interface_name].switchport_mode == 'trunk':
                            if self.physical_interfaces[interface_name].allowed_vlans:
                                # Merge
                                all_vlans = list(set(self.physical_interfaces[interface_name].allowed_vlans + vlans))
                                self.physical_interfaces[interface_name].allowed_vlans = sorted(all_vlans, key=lambda x: int(x) if x.isdigit() else 999)
                            else:
                                self.physical_interfaces[interface_name].allowed_vlans = vlans
                        elif self.physical_interfaces[interface_name].switchport_mode == 'access':
                            if not self.physical_interfaces[interface_name].access_vlan:
                                self.physical_interfaces[interface_name].access_vlan = vlans[0]
        
        # Interfaces
        elif 'interfaces' in self.current_path:
            # VLAN SVI: interfaces { vlan { unit X { family inet { address ... } } } } - handle here so we don't dispatch to _parse_interfaces_config
            if ('vlan' in self.current_path and any('unit' in p for p in self.current_path) and
                    'family inet' in self.current_path and 'address' in statement):
                ip_match = re.search(r'(\d+\.\d+\.\d+\.\d+)/(\d+)', statement)
                if ip_match:
                    vlan_id = None
                    for path_part in self.current_path:
                        if path_part.strip().lower().startswith('unit'):
                            unit_match = re.search(r'unit\s+(\d+)', path_part, re.IGNORECASE)
                            if unit_match:
                                vlan_id = unit_match.group(1)
                                break
                            idx = self.current_path.index(path_part)
                            if idx + 1 < len(self.current_path) and self.current_path[idx + 1].isdigit():
                                vlan_id = self.current_path[idx + 1]
                                break
                    if vlan_id:
                        if vlan_id not in self.vlans:
                            self.vlans[vlan_id] = VlanConfig(vlan_id=vlan_id)
                        self.vlans[vlan_id].ip_address = ip_match.group(1)
                        cidr = int(ip_match.group(2))
                        self.vlans[vlan_id].subnet_mask = self._cidr_to_mask(cidr)
            else:
                self._parse_interfaces_config(statement, line_num)
        
        # Legacy handler for vlan { members ... } (kept for backward compatibility)
        elif 'vlan' in self.current_path and 'interfaces' in self.current_path:
            # This handles "vlan { members ... }" nested inside interface config
            if 'members' in statement:
                # Get the interface name from the path
                # The path structure is: ['interfaces', 'xe-0/0/0', 'unit 0', 'family ethernet-switching', 'vlan']
                interface_name = None
                for path_part in self.current_path:
                    if path_part.startswith(('et-', 'xe-', 'ge-', 'ae', 'irb')):
                        interface_name = path_part
                        break
                
                # Also check if we need to create the interface object
                if interface_name and interface_name not in self.physical_interfaces and not interface_name.startswith('ae'):
                    self.physical_interfaces[interface_name] = PhysicalInterfaceConfig(interface=interface_name)
                
                if interface_name:
                    # Parse VLAN members (same logic as in _parse_interfaces_config)
                    if 'all' in statement:
                        vlans = []
                    else:
                        # Try numeric IDs first
                        vlan_match = re.search(r'members\s+\[?\s*([\d\s,]+)\]?', statement)
                        if vlan_match:
                            vlans_str = vlan_match.group(1)
                            vlans = [v.strip() for v in re.split(r'[\s,]+', vlans_str) if v.strip()]
                        else:
                            # Try single numeric
                            vlan_match = re.search(r'members\s+(\d+)', statement)
                            if vlan_match:
                                vlans = [vlan_match.group(1)]
                            else:
                                # Try VLAN names
                                vlan_name_match = re.search(r'members\s+\[?\s*([A-Za-z0-9_\-\s,]+)\]?', statement)
                                if vlan_name_match:
                                    vlans_str = vlan_name_match.group(1)
                                    vlan_names = [v.strip() for v in re.split(r'[\s,]+', vlans_str) if v.strip()]
                                    # Try immediate resolution, but store for deferred resolution if needed
                                    vlans = []
                                    unresolved_names = []
                                    for vlan_name in vlan_names:
                                        # Check if it's actually a numeric ID
                                        if vlan_name.isdigit():
                                            vlans.append(vlan_name)
                                        else:
                                            # Try to find VLAN ID by name
                                            found = False
                                            for vlan_id, vlan_obj in self.vlans.items():
                                                if vlan_obj.name == vlan_name:
                                                    vlans.append(vlan_id)
                                                    found = True
                                                    break
                                            if not found:
                                                unresolved_names.append(vlan_name)
                                    
                                    # If we have unresolved names, store for later resolution
                                    if unresolved_names:
                                        # Determine if trunk or access mode
                                        # Check the path to see if we're in a trunk or access context
                                        is_trunk = 'trunk' in ' '.join(self.current_path).lower() or 'interface-mode trunk' in ' '.join(self.current_path).lower()
                                        if not is_trunk:
                                            # Check if mode is already set on the interface
                                            if interface_name.startswith('ae'):
                                                ae_id = interface_name.replace('ae', '')
                                                if ae_id in self.port_channels:
                                                    is_trunk = self.port_channels[ae_id].mode == 'trunk'
                                            else:
                                                if interface_name in self.physical_interfaces:
                                                    is_trunk = self.physical_interfaces[interface_name].switchport_mode == 'trunk'
                                        
                                        self.pending_vlan_mappings.append({
                                            'interface_name': interface_name,
                                            'vlan_names': unresolved_names,
                                            'is_port_channel': interface_name.startswith('ae'),
                                            'is_trunk': is_trunk,
                                            'existing_vlans': vlans  # Keep any resolved VLANs
                                        })
                                else:
                                    # Single VLAN name
                                    vlan_name_match = re.search(r'members\s+([A-Za-z0-9_\-]+)', statement)
                                    if vlan_name_match:
                                        vlan_name = vlan_name_match.group(1)
                                        # Check if it's actually a numeric ID
                                        if vlan_name.isdigit():
                                            vlans = [vlan_name]
                                        else:
                                            # Try to find VLAN ID by name
                                            found = False
                                            for vlan_id, vlan_obj in self.vlans.items():
                                                if vlan_obj.name == vlan_name:
                                                    vlans = [vlan_id]
                                                    found = True
                                                    break
                                            if not found:
                                                # Store for later resolution
                                                # Check the path to see if we're in a trunk or access context
                                                is_trunk = 'trunk' in ' '.join(self.current_path).lower() or 'interface-mode trunk' in ' '.join(self.current_path).lower()
                                                if not is_trunk:
                                                    # Check if mode is already set on the interface
                                                    if interface_name.startswith('ae'):
                                                        ae_id = interface_name.replace('ae', '')
                                                        if ae_id in self.port_channels:
                                                            is_trunk = self.port_channels[ae_id].mode == 'trunk'
                                                    else:
                                                        if interface_name in self.physical_interfaces:
                                                            is_trunk = self.physical_interfaces[interface_name].switchport_mode == 'trunk'
                                                
                                                self.pending_vlan_mappings.append({
                                                    'interface_name': interface_name,
                                                    'vlan_names': [vlan_name],
                                                    'is_port_channel': interface_name.startswith('ae'),
                                                    'is_trunk': is_trunk,
                                                    'existing_vlans': []
                                                })
                                    else:
                                        vlans = []
                    
                    # Apply VLANs to interface
                    if interface_name.startswith('ae'):
                        ae_id = interface_name.replace('ae', '')
                        if ae_id not in self.port_channels:
                            self.port_channels[ae_id] = PortChannelConfig(po_id=ae_id)
                        if self.port_channels[ae_id].mode == 'trunk':
                            if vlans:
                                self.port_channels[ae_id].allowed_vlans = vlans
                        elif self.port_channels[ae_id].mode == 'access' and vlans:
                            self.port_channels[ae_id].access_vlan = vlans[0]
                    else:
                        if interface_name not in self.physical_interfaces:
                            self.physical_interfaces[interface_name] = PhysicalInterfaceConfig(interface=interface_name)
                        if self.physical_interfaces[interface_name].switchport_mode == 'trunk':
                            if vlans:
                                self.physical_interfaces[interface_name].allowed_vlans = vlans
                        elif self.physical_interfaces[interface_name].switchport_mode == 'access' and vlans:
                            self.physical_interfaces[interface_name].access_vlan = vlans[0]
        
        # VLANs
        elif 'vlans' in self.current_path:
            self._parse_vlans_config(statement, line_num)
        
        # Protocols
        elif 'protocols' in self.current_path:
            self._parse_protocols_config(statement, line_num)
        
        # Routing options
        elif 'routing-options' in self.current_path:
            self._parse_routing_options(statement, line_num)
        
        # SNMP
        elif 'snmp' in self.current_path:
            self._parse_snmp_config(statement, line_num)
        
        # Policy-options (prefix-list, policy-statement -> SONiC prefix-list / route-map)
        elif 'policy-options' in self.current_path:
            self._parse_policy_options(statement, line_num)
        
        # Handle version statement at top level
        elif not self.current_path and statement.startswith('version'):
            # Version statement - just skip it
            pass
        else:
            self.log_unsupported_feature(statement, self.UNSUPPORTED_MSG)
    
    def _finalize_policy_term(self):
        """Build RouteMapEntry from current term (current_from/current_then) and append to route_maps."""
        if not self.current_policy_statement:
            return
        if self.current_policy_statement not in self.route_maps:
            self.route_maps[self.current_policy_statement] = []
        entries = self.route_maps[self.current_policy_statement]
        # Only add entry if we have from/then content (avoid empty stub when we have real terms)
        if not self.current_from and self.current_then is None:
            self.current_term_name = None
            self.current_from = []
            self.current_then = None
            return
        action = 'permit' if self.current_then == 'accept' or self.current_then is None else 'deny'
        seq = 10 * (len(entries) + 1)
        matches = []
        for kind, value in self.current_from:
            if kind == 'interface':
                # value can be "lo0.0" or "irb.100" or "lo0.0 irb.100" (from list)
                intfs = value.split()
                converted = [self.convert_interface_name(i) for i in intfs]
                matches.append('match interface ' + ' '.join(converted))
            elif kind == 'prefix-list':
                matches.append(f'match ip address prefix-list {value}')
        entries.append(RouteMapEntry(
            map_name=self.current_policy_statement,
            seq=seq,
            action=action,
            matches=matches,
            sets=[]
        ))
        self.current_term_name = None
        self.current_from = []
        self.current_then = None
    
    def _parse_policy_options(self, statement: str, line_num: int):
        """Parse Juniper policy-options: prefix-list (CIDR lines) and policy-statement (terms with from/then)."""
        # Inside prefix-list BLOCK: statement can be a CIDR (e.g. 10.0.0.0/8)
        prefix_list_name = None
        for p in self.current_path:
            if p.startswith('prefix-list '):
                prefix_list_name = p.replace('prefix-list ', '', 1).strip()
                break
        if prefix_list_name and re.match(r'^\d+\.\d+\.\d+\.\d+/\d+$', statement.strip()):
            if prefix_list_name not in self.prefix_lists:
                self.prefix_lists[prefix_list_name] = []
            seq = 10 * (len(self.prefix_lists[prefix_list_name]) + 1)
            self.prefix_lists[prefix_list_name].append(
                PrefixListEntry(list_name=prefix_list_name, seq=seq, action='permit', prefix=statement.strip(), ge=None, le=None)
            )
            return
        # prefix-list NAME (sibling): finalize current policy's last term before entering prefix-list block
        if statement.startswith('prefix-list '):
            self._finalize_policy_term()
            self.current_policy_statement = None
            return
        # policy-statement NAME: finalize previous policy's last term, start new policy
        if statement.startswith('policy-statement '):
            self._finalize_policy_term()
            parts = statement.split(None, 1)
            if len(parts) >= 2:
                self.current_policy_statement = parts[1].strip()
                self.current_term_name = '_default'  # so "then" without "term" still creates an entry
                self.current_from = []
                self.current_then = None
                self.route_maps[self.current_policy_statement] = []
            return
        # term TERM_NAME: finalize current term, start new term
        if statement.startswith('term '):
            self._finalize_policy_term()
            parts = statement.split(None, 1)
            if len(parts) >= 2:
                self.current_term_name = parts[1].strip()
                self.current_from = []
                self.current_then = None
            return
        # Inside "from" block: collect interface, prefix-list, etc.
        if 'from' in self.current_path and self.current_policy_statement and self.current_term_name is not None:
            if statement.startswith('interface '):
                # "interface lo0.0" or "interface [ irb.100 irb.200 ]"
                rest = statement.replace('interface', '', 1).strip()
                if rest.startswith('[') and ']' in rest:
                    inner = re.search(r'\[([^\]]+)\]', rest)
                    if inner:
                        intfs = [x.strip() for x in inner.group(1).split()]
                        self.current_from.append(('interface', ' '.join(intfs)))
                    else:
                        self.current_from.append(('interface', rest))
                else:
                    self.current_from.append(('interface', rest))
                return
            if statement.startswith('prefix-list '):
                parts = statement.split(None, 1)
                if len(parts) >= 2:
                    self.current_from.append(('prefix-list', parts[1].strip()))
                return
            if 'protocol' in statement:
                # protocol direct; - skip or map; SONiC may not have direct match
                return
        # Inside "then" block: accept | reject (or same line "then accept" / "then reject")
        if self.current_policy_statement and self.current_term_name is not None:
            if statement.strip() == 'accept':
                self.current_then = 'accept'
                return
            if statement.strip() == 'reject':
                self.current_then = 'reject'
                return
            if statement.strip() in ('then accept', 'then reject'):
                self.current_then = statement.split()[-1]
                return
            if 'then' in self.current_path and 'load-balance' in statement:
                self.current_then = 'accept'
                return
        # Other policy-options lines - accept without logging
        pass
    
    def _parse_system_config(self, statement: str, line_num: int):
        """Parse system configuration"""
        if 'host-name' in statement:
            parts = statement.split()
            if len(parts) >= 2:
                self.hostname = parts[1].strip('"')
        
        elif 'name-server' in statement or ('name-server' in self.current_path and re.match(r'^\d+\.\d+\.\d+\.\d+', statement.strip())):
            # Name server IP can be on its own line within name-server block (no VRF in typical JunOS)
            ns_match = re.search(r'(\d+\.\d+\.\d+\.\d+)', statement)
            if ns_match:
                if 'name_servers' not in self.global_settings:
                    self.global_settings['name_servers'] = []
                ip = ns_match.group(1)
                self.global_settings['name_servers'].append({'ip': ip, 'vrf': None})
        
        elif 'server' in statement and 'ntp' in self.current_path:
            parts = statement.split()
            if len(parts) >= 1:
                ntp_match = re.search(r'(\d+\.\d+\.\d+\.\d+)', statement)
                if ntp_match:
                    server_ip = ntp_match.group(1)
                    if 'ntp_servers' not in self.global_settings:
                        self.global_settings['ntp_servers'] = []
                    self.global_settings['ntp_servers'].append(server_ip)
                    if 'ntp_server' not in self.global_settings:
                        self.global_settings['ntp_server'] = server_ip
                    if 'prefer' in statement.lower():
                        self.global_settings['ntp_preferred_server'] = server_ip
        
        elif 'host' in statement and 'syslog' in self.current_path:
            syslog_match = re.search(r'(\d+\.\d+\.\d+\.\d+)', statement)
            if syslog_match:
                self.syslog_config.servers.append(syslog_match.group(1))
        
        elif 'radius-server' in self.current_path:
            self._parse_radius_config_junos(statement, line_num)
        else:
            self.log_unsupported_feature(statement, self.UNSUPPORTED_MSG)
    
    def _parse_interfaces_config(self, statement: str, line_num: int):
        """Parse interfaces configuration"""
        # Check if we're in a specific interface
        interface_name = None
        for path_part in self.current_path:
            if path_part.startswith('xe-') or path_part.startswith('et-') or path_part.startswith('ae') or (path_part.startswith('lo') and re.match(r'lo\d+', path_part)):
                interface_name = path_part
                break
        
        if not interface_name:
            return
        
        # When inside vlan { members ... } add to pending for later resolution (vlans section may not be parsed yet)
        if any('vlan' in p for p in self.current_path) and 'members' in statement:
            if 'all' in statement:
                pass  # members all handled elsewhere
            else:
                vlan_name_match = re.search(r'members\s+\[?\s*([A-Za-z0-9_\-\s,]+)\]?', statement)
                if vlan_name_match:
                    vlans_str = vlan_name_match.group(1)
                    vlan_names = [v.strip() for v in re.split(r'[\s,]+', vlans_str) if v.strip()]
                    if vlan_names:
                        vlans = []
                        unresolved_names = []
                        for vlan_name in vlan_names:
                            if vlan_name.isdigit():
                                vlans.append(vlan_name)
                            else:
                                found = False
                                for vlan_id, vlan_obj in self.vlans.items():
                                    if vlan_obj.name and vlan_obj.name.strip().lower() == vlan_name.strip().lower():
                                        vlans.append(vlan_id)
                                        found = True
                                        break
                                if not found:
                                    unresolved_names.append(vlan_name)
                        if unresolved_names:
                            is_trunk = (self.physical_interfaces.get(interface_name) and self.physical_interfaces[interface_name].switchport_mode == 'trunk') or (interface_name.startswith('ae') and self.port_channels.get(interface_name.replace('ae', '')) and self.port_channels[interface_name.replace('ae', '')].mode == 'trunk')
                            self.pending_vlan_mappings.append({
                                'interface_name': interface_name,
                                'vlan_names': unresolved_names,
                                'is_port_channel': interface_name.startswith('ae'),
                                'is_trunk': is_trunk,
                                'existing_vlans': vlans
                            })
                        elif vlans:
                            if interface_name.startswith('ae'):
                                ae_id = interface_name.replace('ae', '')
                                if ae_id not in self.port_channels:
                                    self.port_channels[ae_id] = PortChannelConfig(po_id=ae_id)
                                if self.port_channels[ae_id].mode == 'trunk':
                                    self.port_channels[ae_id].allowed_vlans = list(set((self.port_channels[ae_id].allowed_vlans or []) + vlans))
                                else:
                                    if not self.port_channels[ae_id].access_vlan:
                                        self.port_channels[ae_id].access_vlan = vlans[0]
                            else:
                                if interface_name not in self.physical_interfaces:
                                    self.physical_interfaces[interface_name] = PhysicalInterfaceConfig(interface=interface_name)
                                if self.physical_interfaces[interface_name].switchport_mode == 'trunk':
                                    self.physical_interfaces[interface_name].allowed_vlans = list(set((self.physical_interfaces[interface_name].allowed_vlans or []) + vlans))
                                else:
                                    if not self.physical_interfaces[interface_name].access_vlan:
                                        self.physical_interfaces[interface_name].access_vlan = vlans[0]
            return
        
        # Handle interface-level statements
        if 'description' in statement:
            desc_match = re.search(r'"([^"]+)"', statement)
            if desc_match:
                if interface_name.startswith('ae'):
                    # Aggregated Ethernet (port-channel)
                    ae_id = interface_name.replace('ae', '')
                    if ae_id not in self.port_channels:
                        self.port_channels[ae_id] = PortChannelConfig(po_id=ae_id)
                    self.port_channels[ae_id].description = desc_match.group(1)
                elif interface_name.startswith('lo') and re.match(r'lo\d+', interface_name):
                    # Loopback (lo0, lo1, ...)
                    if interface_name not in self.loopbacks:
                        self.loopbacks[interface_name] = LoopbackConfig(interface=interface_name)
                    self.loopbacks[interface_name].description = desc_match.group(1)
                else:
                    # Physical interface
                    if interface_name not in self.physical_interfaces:
                        self.physical_interfaces[interface_name] = PhysicalInterfaceConfig(interface=interface_name)
                    self.physical_interfaces[interface_name].description = desc_match.group(1)
        
        # Handle ether-options (for LAG membership)
        elif '802.3ad' in statement or 'ae' in statement:
            ae_match = re.search(r'ae(\d+)', statement)
            if ae_match:
                ae_id = ae_match.group(1)
                if interface_name not in self.physical_interfaces:
                    self.physical_interfaces[interface_name] = PhysicalInterfaceConfig(interface=interface_name)
                self.physical_interfaces[interface_name].channel_group = ae_id
                # Ensure PortChannel object exists even if not explicitly defined in config
                if ae_id not in self.port_channels:
                    self.port_channels[ae_id] = PortChannelConfig(po_id=ae_id)
        
        # Handle loopback (lo0, lo1, ...) unit family inet address
        elif interface_name and interface_name.startswith('lo') and re.match(r'lo\d+', interface_name) and any('unit' in p for p in self.current_path) and 'family inet' in self.current_path:
            if 'address' in statement:
                ip_match = re.search(r'(\d+\.\d+\.\d+\.\d+)/(\d+)', statement)
                if ip_match:
                    if interface_name not in self.loopbacks:
                        self.loopbacks[interface_name] = LoopbackConfig(interface=interface_name)
                    self.loopbacks[interface_name].ip_address = ip_match.group(1)
                    cidr = int(ip_match.group(2))
                    self.loopbacks[interface_name].subnet_mask = self._cidr_to_mask(cidr)
        
        # Handle unit 0 family ethernet-switching
        elif any('unit' in p for p in self.current_path) and 'family ethernet-switching' in self.current_path:
            if 'interface-mode' in statement:
                # JunOS uses "interface-mode access" or "interface-mode trunk"
                mode_match = re.search(r'interface-mode\s+(\w+)', statement)
                if mode_match:
                    mode = mode_match.group(1)
                    # Ensure interface_name is extracted from path if not already set
                    if not interface_name:
                        for path_part in self.current_path:
                            if path_part.startswith(('xe-', 'et-', 'ge-', 'ae', 'irb')):
                                interface_name = path_part
                                break
                    
                    if interface_name and interface_name.startswith('ae'):
                        ae_id = interface_name.replace('ae', '')
                        if ae_id not in self.port_channels:
                            self.port_channels[ae_id] = PortChannelConfig(po_id=ae_id)
                        if mode == 'trunk':
                            self.port_channels[ae_id].mode = 'trunk'
                        elif mode == 'access':
                            self.port_channels[ae_id].mode = 'access'
                    elif interface_name:
                        if interface_name not in self.physical_interfaces:
                            self.physical_interfaces[interface_name] = PhysicalInterfaceConfig(interface=interface_name)
                        if mode == 'trunk':
                            self.physical_interfaces[interface_name].switchport_mode = 'trunk'
                        elif mode == 'access':
                            self.physical_interfaces[interface_name].switchport_mode = 'access'
            elif 'port-mode' in statement:
                mode_match = re.search(r'port-mode\s+(\w+)', statement)
                if mode_match:
                    mode = mode_match.group(1)
                    # Ensure interface_name is extracted from path if not already set
                    if not interface_name:
                        for path_part in self.current_path:
                            if path_part.startswith(('xe-', 'et-', 'ge-', 'ae', 'irb')):
                                interface_name = path_part
                                break
                    
                    if interface_name and interface_name.startswith('ae'):
                        ae_id = interface_name.replace('ae', '')
                        if ae_id not in self.port_channels:
                            self.port_channels[ae_id] = PortChannelConfig(po_id=ae_id)
                        if mode == 'trunk':
                            self.port_channels[ae_id].mode = 'trunk'
                        elif mode == 'access':
                            self.port_channels[ae_id].mode = 'access'
                    elif interface_name:
                        if interface_name not in self.physical_interfaces:
                            self.physical_interfaces[interface_name] = PhysicalInterfaceConfig(interface=interface_name)
                        if mode == 'trunk':
                            self.physical_interfaces[interface_name].switchport_mode = 'trunk'
                        elif mode == 'access':
                            self.physical_interfaces[interface_name].switchport_mode = 'access'
            
            # Handle VLAN members - can be directly in family ethernet-switching or nested in vlan { members ... }
            # Note: This handles members when NOT nested in vlan block (rare, but possible)
            # The nested vlan { members ... } case is handled above
            if 'members' in statement and 'vlan' not in self.current_path:
                # VLAN members - can be numeric IDs or VLAN names
                if 'all' in statement:
                    # members all - means all VLANs (1-4094)
                    # For SONiC, we'll log this as it needs special handling
                    self.log_warning(statement, 'VLAN members "all" - will need to configure manually in SONiC')
                    # Set to empty to indicate all VLANs
                    vlans = []
                else:
                    # Try to match numeric VLAN IDs first
                    vlan_match = re.search(r'members\s+\[?\s*([\d\s,]+)\]?', statement)
                    if vlan_match:
                        vlans_str = vlan_match.group(1)
                        vlans = [v.strip() for v in re.split(r'[\s,]+', vlans_str) if v.strip()]
                    else:
                        # Try single numeric VLAN member without brackets
                        vlan_match = re.search(r'members\s+(\d+)', statement)
                        if vlan_match:
                            vlans = [vlan_match.group(1)]
                        else:
                            # Try to match VLAN names - use deferred resolution
                            vlan_name_match = re.search(r'members\s+\[?\s*([A-Za-z0-9_\-\s,]+)\]?', statement)
                            if vlan_name_match:
                                vlans_str = vlan_name_match.group(1)
                                vlan_names = [v.strip() for v in re.split(r'[\s,]+', vlans_str) if v.strip()]
                                # Try immediate resolution, but store for deferred resolution if needed
                                vlans = []
                                unresolved_names = []
                                for vlan_name in vlan_names:
                                    # Check if it's actually a numeric ID
                                    if vlan_name.isdigit():
                                        vlans.append(vlan_name)
                                    else:
                                        # Try to find VLAN ID by name
                                        found = False
                                        for vlan_id, vlan_obj in self.vlans.items():
                                            if vlan_obj.name == vlan_name:
                                                vlans.append(vlan_id)
                                                found = True
                                                break
                                        if not found:
                                            unresolved_names.append(vlan_name)
                                
                                # If we have unresolved names, store for later resolution
                                if unresolved_names:
                                    # Determine if trunk or access mode
                                    is_trunk = False
                                    if interface_name.startswith('ae'):
                                        ae_id = interface_name.replace('ae', '')
                                        if ae_id in self.port_channels:
                                            is_trunk = self.port_channels[ae_id].mode == 'trunk'
                                    else:
                                        if interface_name in self.physical_interfaces:
                                            is_trunk = self.physical_interfaces[interface_name].switchport_mode == 'trunk'
                                    
                                    self.pending_vlan_mappings.append({
                                        'interface_name': interface_name,
                                        'vlan_names': unresolved_names,
                                        'is_port_channel': interface_name.startswith('ae'),
                                        'is_trunk': is_trunk,
                                        'existing_vlans': vlans  # Keep any resolved VLANs
                                    })
                            else:
                                # Single VLAN name without brackets - use deferred resolution
                                vlan_name_match = re.search(r'members\s+([A-Za-z0-9_\-]+)', statement)
                                if vlan_name_match:
                                    vlan_name = vlan_name_match.group(1)
                                    # Check if it's actually a numeric ID
                                    if vlan_name.isdigit():
                                        vlans = [vlan_name]
                                    else:
                                        # Try to find VLAN ID by name
                                        found = False
                                        for vlan_id, vlan_obj in self.vlans.items():
                                            if vlan_obj.name == vlan_name:
                                                vlans = [vlan_id]
                                                found = True
                                                break
                                        if not found:
                                            # Store for later resolution
                                            is_trunk = False
                                            if interface_name.startswith('ae'):
                                                ae_id = interface_name.replace('ae', '')
                                                if ae_id in self.port_channels:
                                                    is_trunk = self.port_channels[ae_id].mode == 'trunk'
                                            else:
                                                if interface_name in self.physical_interfaces:
                                                    is_trunk = self.physical_interfaces[interface_name].switchport_mode == 'trunk'
                                            
                                            self.pending_vlan_mappings.append({
                                                'interface_name': interface_name,
                                                'vlan_names': [vlan_name],
                                                'is_port_channel': interface_name.startswith('ae'),
                                                'is_trunk': is_trunk,
                                                'existing_vlans': []
                                            })
                                else:
                                    vlans = []
                
                if interface_name.startswith('ae'):
                    ae_id = interface_name.replace('ae', '')
                    if ae_id not in self.port_channels:
                        self.port_channels[ae_id] = PortChannelConfig(po_id=ae_id)
                    if self.port_channels[ae_id].mode == 'trunk':
                        if vlans:
                            self.port_channels[ae_id].allowed_vlans = vlans
                        else:
                            # members all - set to empty to indicate all VLANs
                            self.port_channels[ae_id].allowed_vlans = []
                    elif self.port_channels[ae_id].mode == 'access' and vlans:
                        self.port_channels[ae_id].access_vlan = vlans[0]
                else:
                    if interface_name not in self.physical_interfaces:
                        self.physical_interfaces[interface_name] = PhysicalInterfaceConfig(interface=interface_name)
                    if self.physical_interfaces[interface_name].switchport_mode == 'trunk':
                        if vlans:
                            self.physical_interfaces[interface_name].allowed_vlans = vlans
                        else:
                            # members all - set to empty to indicate all VLANs
                            self.physical_interfaces[interface_name].allowed_vlans = []
                    elif self.physical_interfaces[interface_name].switchport_mode == 'access' and vlans:
                        self.physical_interfaces[interface_name].access_vlan = vlans[0]
        
        # Handle unit for VLAN interfaces (interfaces { vlan { unit X { ... } } })
        elif 'vlan' in self.current_path and any('unit' in p for p in self.current_path) and 'interfaces' in self.current_path:
            if 'family inet' in self.current_path:
                if 'address' in statement:
                    # IP address
                    ip_match = re.search(r'(\d+\.\d+\.\d+\.\d+)/(\d+)', statement)
                    if ip_match:
                        vlan_id = None
                        # Find VLAN ID from unit in path (path part may be "unit 100" or "unit" with next part "100")
                        for path_part in self.current_path:
                            if path_part.strip().lower().startswith('unit'):
                                unit_match = re.search(r'unit\s+(\d+)', path_part, re.IGNORECASE)
                                if unit_match:
                                    vlan_id = unit_match.group(1)
                                    break
                                idx = self.current_path.index(path_part)
                                if idx + 1 < len(self.current_path) and self.current_path[idx + 1].isdigit():
                                    vlan_id = self.current_path[idx + 1]
                                    break
                        
                        if vlan_id:
                            if vlan_id not in self.vlans:
                                self.vlans[vlan_id] = VlanConfig(vlan_id=vlan_id)
                            self.vlans[vlan_id].ip_address = ip_match.group(1)
                            cidr = int(ip_match.group(2))
                            self.vlans[vlan_id].subnet_mask = self._cidr_to_mask(cidr)
                
                elif 'description' in statement:
                    # Description for VLAN interface
                    desc_match = re.search(r'"([^"]+)"', statement)
                    if desc_match:
                        vlan_id = None
                        for path_part in self.current_path:
                            if path_part.strip().lower().startswith('unit'):
                                unit_match = re.search(r'unit\s+(\d+)', path_part, re.IGNORECASE)
                                if unit_match:
                                    vlan_id = unit_match.group(1)
                                    break
                                idx = self.current_path.index(path_part)
                                if idx + 1 < len(self.current_path) and self.current_path[idx + 1].isdigit():
                                    vlan_id = self.current_path[idx + 1]
                                    break
                        
                        if vlan_id:
                            if vlan_id not in self.vlans:
                                self.vlans[vlan_id] = VlanConfig(vlan_id=vlan_id)
                            self.vlans[vlan_id].description = desc_match.group(1)
        
        # Handle loopback (lo0, lo1, ...) unit family inet - derive interface from path
        elif 'interfaces' in self.current_path and 'unit' in self.current_path and 'family inet' in self.current_path:
            loopback_name = None
            for path_part in self.current_path:
                if path_part.startswith('lo') and re.match(r'lo\d+', path_part):
                    loopback_name = path_part
                    break
            if loopback_name and 'address' in statement:
                ip_match = re.search(r'(\d+\.\d+\.\d+\.\d+)/(\d+)', statement)
                if ip_match:
                    if loopback_name not in self.loopbacks:
                        self.loopbacks[loopback_name] = LoopbackConfig(interface=loopback_name)
                    self.loopbacks[loopback_name].ip_address = ip_match.group(1)
                    cidr = int(ip_match.group(2))
                    self.loopbacks[loopback_name].subnet_mask = self._cidr_to_mask(cidr)
        else:
            self.log_unsupported_feature(statement, self.UNSUPPORTED_MSG)
    
    def _parse_vlans_config(self, statement: str, line_num: int):
        """Parse VLANs configuration"""
        # Find current VLAN name from path (e.g., "Compute_Prod" from vlans > Compute_Prod)
        vlan_name = None
        for i, path_part in enumerate(self.current_path):
            if path_part == 'vlans' and i + 1 < len(self.current_path):
                vlan_name = self.current_path[i + 1]
                break
        
        if not vlan_name:
            return
        
        # Extract VLAN ID from vlan-id statement
        if 'vlan-id' in statement:
            vlan_id_match = re.search(r'vlan-id\s+(\d+)', statement)
            if vlan_id_match:
                vlan_id = vlan_id_match.group(1)
                if vlan_id not in self.vlans:
                    self.vlans[vlan_id] = VlanConfig(vlan_id=vlan_id)
                
                vlan = self.vlans[vlan_id]
                # Set VLAN name from the path
                if not vlan.name:
                    vlan.name = vlan_name
                if not vlan.description:
                    vlan.description = vlan_name
        else:
            # For other statements, find the VLAN by matching the name
            # We need to find which VLAN ID corresponds to this name
            vlan_id = None
            for vid, vlan_obj in self.vlans.items():
                if vlan_obj.name == vlan_name:
                    vlan_id = vid
                    break
            
            if vlan_id:
                vlan = self.vlans[vlan_id]
                
                if 'description' in statement:
                    desc_match = re.search(r'"([^"]+)"', statement)
                    if desc_match:
                        vlan.description = desc_match.group(1)
                        if not vlan.name:
                            vlan.name = vlan.description
                
                elif 'name' in statement and 'vlan-id' not in statement:
                    name_match = re.search(r'name\s+"?([^";]+)"?', statement)
                    if name_match:
                        vlan.name = name_match.group(1).strip('"')
                        if not vlan.description:
                            vlan.description = vlan.name
                else:
                    self.log_unsupported_feature(statement, self.UNSUPPORTED_MSG)
            else:
                self.log_unsupported_feature(statement, self.UNSUPPORTED_MSG)
    
    def _parse_protocols_config(self, statement: str, line_num: int):
        """Parse protocols configuration"""
        # BGP
        if 'bgp' in self.current_path:
            self._parse_bgp_config_junos(statement, line_num)
        
        # VRRP
        elif 'vrrp' in self.current_path:
            self._parse_vrrp_config_junos(statement, line_num)
        
        # MC-LAG
        elif 'mc-lag' in self.current_path:
            self._parse_mclag_config_junos(statement, line_num)
        else:
            self.log_unsupported_feature(statement, self.UNSUPPORTED_MSG)
    
    def _parse_bgp_config_junos(self, statement: str, line_num: int):
        """Parse BGP configuration in JunOS format"""
        if any('group' in p for p in self.current_path):
            # BGP group - path part may be "group external-peers" or "external-peers"
            group_name = None
            for path_part in self.current_path:
                if path_part in ('protocols', 'bgp', 'group'):
                    continue
                if 'group' in path_part:
                    # e.g. "group external-peers" -> external-peers
                    parts = path_part.split(None, 1)
                    group_name = parts[1] if len(parts) > 1 else path_part.replace('group', '').strip()
                else:
                    group_name = path_part
                if group_name:
                    break
            
            if group_name:
                if 'peer_groups' not in self.bgp_config:
                    self.bgp_config['peer_groups'] = {}
                if group_name not in self.bgp_config['peer_groups']:
                    self.bgp_config['peer_groups'][group_name] = {}
                
                # Opening line "neighbor X.X.X.X {" adds the neighbor (path doesn't have 'neighbor' yet)
                neighbor_open = re.match(r'neighbor\s+(\d+\.\d+\.\d+\.\d+)\s*$', statement.strip())
                if neighbor_open:
                    neighbor_ip = neighbor_open.group(1)
                    if 'peer_group_members' not in self.bgp_config:
                        self.bgp_config['peer_group_members'] = []
                    if not any(m.get('neighbor') == neighbor_ip and m.get('peer_group') == group_name for m in self.bgp_config['peer_group_members']):
                        self.bgp_config['peer_group_members'].append({
                            'neighbor': neighbor_ip,
                            'peer_group': group_name
                        })
                elif any('neighbor' in p for p in self.current_path):
                    # Neighbor within group (e.g. when processing "description" etc. - neighbor already added by opening line)
                    neighbor_ip = None
                    for path_part in self.current_path:
                        ip_m = re.search(r'\d+\.\d+\.\d+\.\d+', path_part)
                        if ip_m:
                            neighbor_ip = ip_m.group(0)
                            break
                    
                    if neighbor_ip:
                        if 'peer_group_members' not in self.bgp_config:
                            self.bgp_config['peer_group_members'] = []
                        if not any(m.get('neighbor') == neighbor_ip and m.get('peer_group') == group_name for m in self.bgp_config['peer_group_members']):
                            self.bgp_config['peer_group_members'].append({
                                'neighbor': neighbor_ip,
                                'peer_group': group_name
                            })
                
                # Helper: get neighbor IP from path (path part may be "neighbor 10.10.10.1" or "10.10.10.1")
                def _bgp_neighbor_from_path():
                    for path_part in self.current_path:
                        ip_m = re.search(r'\d+\.\d+\.\d+\.\d+', path_part)
                        if ip_m:
                            return ip_m.group(0)
                    return None
                
                if 'peer-as' in statement:
                    asn_match = re.search(r'peer-as\s+(\d+)', statement)
                    if asn_match:
                        remote_as = asn_match.group(1)
                        self.bgp_config['peer_groups'][group_name]['remote_as'] = remote_as
                        # Per-neighbor remote-as (overrides group when present)
                        nbr = _bgp_neighbor_from_path()
                        if nbr:
                            if 'neighbor_remote_as' not in self.bgp_config:
                                self.bgp_config['neighbor_remote_as'] = {}
                            self.bgp_config['neighbor_remote_as'][nbr] = remote_as
                
                if 'description' in statement:
                    desc_match = re.search(r'"([^"]+)"', statement)
                    if desc_match:
                        if any('neighbor' in p for p in self.current_path):
                            neighbor_ip = _bgp_neighbor_from_path()
                            if neighbor_ip:
                                if 'neighbor_descriptions' not in self.bgp_config:
                                    self.bgp_config['neighbor_descriptions'] = {}
                                self.bgp_config['neighbor_descriptions'][neighbor_ip] = desc_match.group(1)
                        else:
                            if 'group_descriptions' not in self.bgp_config:
                                self.bgp_config['group_descriptions'] = {}
                            self.bgp_config['group_descriptions'][group_name] = desc_match.group(1)
                
                elif 'multihop' in statement:
                    ttl_match = re.search(r'ttl\s+(\d+)', statement)
                    if ttl_match:
                        nbr = _bgp_neighbor_from_path()
                        if nbr:
                            if 'neighbor_multihop' not in self.bgp_config:
                                self.bgp_config['neighbor_multihop'] = {}
                            self.bgp_config['neighbor_multihop'][nbr] = ttl_match.group(1)
                elif 'ttl' in statement:
                    # JunOS: multihop { ttl 3; } — ttl on its own line
                    ttl_match = re.search(r'ttl\s+(\d+)', statement)
                    if ttl_match:
                        nbr = _bgp_neighbor_from_path()
                        if nbr:
                            if 'neighbor_multihop' not in self.bgp_config:
                                self.bgp_config['neighbor_multihop'] = {}
                            self.bgp_config['neighbor_multihop'][nbr] = ttl_match.group(1)
                elif 'import' in statement:
                    imp_match = re.search(r'import\s+(\S+)', statement)
                    if imp_match:
                        nbr = _bgp_neighbor_from_path()
                        if nbr:
                            if 'neighbor_route_map_in' not in self.bgp_config:
                                self.bgp_config['neighbor_route_map_in'] = {}
                            self.bgp_config['neighbor_route_map_in'][nbr] = imp_match.group(1).rstrip(';')
                elif 'export' in statement:
                    exp_match = re.search(r'export\s+(\S+)', statement)
                    if exp_match:
                        nbr = _bgp_neighbor_from_path()
                        if nbr:
                            if 'neighbor_route_map_out' not in self.bgp_config:
                                self.bgp_config['neighbor_route_map_out'] = {}
                            self.bgp_config['neighbor_route_map_out'][nbr] = exp_match.group(1).rstrip(';')
                else:
                    self.log_unsupported_feature(statement, self.UNSUPPORTED_MSG)
        
        elif 'local-address' in statement:
            local_addr_match = re.search(r'(\d+\.\d+\.\d+\.\d+)', statement)
            if local_addr_match:
                if 'router_id' not in self.bgp_config:
                    self.bgp_config['router_id'] = local_addr_match.group(1)
        else:
            self.log_unsupported_feature(statement, self.UNSUPPORTED_MSG)
    
    def _parse_vrrp_config_junos(self, statement: str, line_num: int):
        """Parse VRRP configuration in JunOS format"""
        if any('group' in p for p in self.current_path):
            # VRRP group - path part may be "vrrp-group-1" or "group vrrp-group-1"
            vrid = None
            for path_part in self.current_path:
                vrid_match = re.search(r'vrrp-group-(\d+)', path_part, re.IGNORECASE)
                if vrid_match:
                    vrid = vrid_match.group(1)
                    break
                if path_part not in ('protocols', 'vrrp', 'group') and path_part.replace('vrrp-group-', '').isdigit():
                    vrid = path_part.replace('vrrp-group-', '')
                    break
            
            if vrid:
                # Store VRRP group info
                if 'vrrp_groups' not in self.vrrp_config:
                    self.vrrp_config['vrrp_groups'] = {}
                if vrid not in self.vrrp_config['vrrp_groups']:
                    self.vrrp_config['vrrp_groups'][vrid] = {}
                
                if 'interface' in statement:
                    # Extract VLAN ID from interface vlan.X
                    vlan_match = re.search(r'vlan\.(\d+)', statement)
                    if vlan_match:
                        vlan_id = vlan_match.group(1)
                        self.vrrp_config['vrrp_groups'][vrid]['vlan'] = vlan_id
                
                elif 'virtual-address' in statement:
                    vip_match = re.search(r'(\d+\.\d+\.\d+\.\d+)', statement)
                    if vip_match:
                        self.vrrp_config['vrrp_groups'][vrid]['vip'] = vip_match.group(1)
                        # Get VLAN ID if we have it
                        vlan_id = self.vrrp_config['vrrp_groups'][vrid].get('vlan', '1')
                        group_entry = {
                            'vrid': vrid,
                            'vip': vip_match.group(1),
                            'vlan': vlan_id
                        }
                        if self.vrrp_config['vrrp_groups'][vrid].get('priority') is not None:
                            group_entry['priority'] = self.vrrp_config['vrrp_groups'][vrid]['priority']
                        if self.vrrp_config['vrrp_groups'][vrid].get('preempt'):
                            group_entry['preempt'] = True
                        if 'groups' not in self.vrrp_config:
                            self.vrrp_config['groups'] = []
                        self.vrrp_config['groups'].append(group_entry)
                elif 'priority' in statement:
                    pri_match = re.search(r'priority\s+(\d+)', statement)
                    if pri_match:
                        pri_val = int(pri_match.group(1))
                        self.vrrp_config['vrrp_groups'][vrid]['priority'] = pri_val
                        for g in (self.vrrp_config.get('groups') or []):
                            if str(g.get('vrid')) == str(vrid):
                                g['priority'] = pri_val
                                break
                elif 'preempt' in statement:
                    self.vrrp_config['vrrp_groups'][vrid]['preempt'] = True
                    for g in (self.vrrp_config.get('groups') or []):
                        if str(g.get('vrid')) == str(vrid):
                            g['preempt'] = True
                            break
                else:
                    self.log_unsupported_feature(statement, self.UNSUPPORTED_MSG)
        else:
            self.log_unsupported_feature(statement, self.UNSUPPORTED_MSG)
    
    def _parse_mclag_config_junos(self, statement: str, line_num: int):
        """Parse MC-LAG configuration in JunOS format"""
        if 'icp' in self.current_path:
            if 'peer-address' in statement:
                peer_match = re.search(r'(\d+\.\d+\.\d+\.\d+)', statement)
                if peer_match:
                    self.mlag_config['peer_address'] = peer_match.group(1)
        
        elif 'chassis-id' in statement:
            chassis_match = re.search(r'chassis-id\s+(\d+)', statement)
            if chassis_match:
                self.mlag_config['chassis_id'] = chassis_match.group(1)
        
        elif 'interface' in self.current_path:
            # MC-LAG interface
            interface_name = None
            for path_part in self.current_path:
                if path_part.startswith('ae'):
                    interface_name = path_part
                    break
            
            if interface_name:
                ae_id = interface_name.replace('ae', '')
                if ae_id not in self.port_channels:
                    self.port_channels[ae_id] = PortChannelConfig(po_id=ae_id)
                self.port_channels[ae_id].mlag_enabled = True
                
                if 'mc-ae-id' in statement:
                    mcae_match = re.search(r'mc-ae-id\s+(\d+)', statement)
                    if mcae_match:
                        self.mlag_config['mc_ae_id'] = mcae_match.group(1)
                else:
                    self.log_unsupported_feature(statement, self.UNSUPPORTED_MSG)
            else:
                self.log_unsupported_feature(statement, self.UNSUPPORTED_MSG)
        else:
            self.log_unsupported_feature(statement, self.UNSUPPORTED_MSG)
    
    def _parse_routing_options(self, statement: str, line_num: int):
        """Parse routing options"""
        if 'static' in self.current_path:
            if 'route' in statement:
                # Static route
                route_match = re.search(r'route\s+([\d./]+)\s+next-hop\s+([\d.]+)', statement)
                if route_match:
                    route = StaticRouteConfig()
                    network = route_match.group(1)
                    if '/' in network:
                        route.network, cidr = network.split('/')
                        route.mask = self._cidr_to_mask(int(cidr))
                    else:
                        route.network = network
                    route.next_hop = route_match.group(2)
                    self.static_routes.append(route)
        
        elif 'autonomous-system' in statement:
            as_match = re.search(r'autonomous-system\s+(\d+)', statement)
            if as_match:
                if 'asn' not in self.bgp_config:
                    self.bgp_config['asn'] = as_match.group(1)
        
        elif 'router-id' in statement:
            router_id_match = re.search(r'(\d+\.\d+\.\d+\.\d+)', statement)
            if router_id_match:
                if 'router_id' not in self.bgp_config:
                    self.bgp_config['router_id'] = router_id_match.group(1)
        else:
            self.log_unsupported_feature(statement, self.UNSUPPORTED_MSG)
    
    def _parse_snmp_config(self, statement: str, line_num: int):
        """Parse SNMP configuration"""
        # Handle 'community public' statement - extract community name
        if statement.startswith('community '):
            community_name = statement.replace('community ', '').strip()
            if community_name:
                # Initialize community with default permission if not already set
                if community_name not in self.snmp_config.communities:
                    self.snmp_config.communities[community_name] = 'rw'  # default
        
        # Handle authorization statements within community blocks
        elif any('community' in path_part for path_part in self.current_path):
            # Extract community name from path
            # Path format: ['snmp', 'community public']
            community_name = None
            for path_part in self.current_path:
                if path_part.startswith('community '):
                    # Extract name from 'community public' format
                    community_name = path_part.replace('community ', '').strip()
                    break
            
            if community_name:
                # Initialize community with default permission if not already set
                if community_name not in self.snmp_config.communities:
                    self.snmp_config.communities[community_name] = 'rw'  # default
                
                # Check for authorization read-only or read-write statement
                if 'authorization' in statement:
                    if 'read-only' in statement:
                        self.snmp_config.communities[community_name] = 'ro'
                    elif 'read-write' in statement:
                        self.snmp_config.communities[community_name] = 'rw'
                    else:
                        self.log_unsupported_feature(statement, self.UNSUPPORTED_MSG)
                else:
                    self.log_unsupported_feature(statement, self.UNSUPPORTED_MSG)
            else:
                self.log_unsupported_feature(statement, self.UNSUPPORTED_MSG)
        else:
            self.log_unsupported_feature(statement, self.UNSUPPORTED_MSG)
    
    def _parse_radius_config_junos(self, statement: str, line_num: int):
        """Parse RADIUS server configuration in JunOS format"""
        from base_migrator import RadiusConfig
        
        radius_ip = None
        for path_part in self.current_path:
            if re.match(r'\d+\.\d+\.\d+\.\d+', path_part):
                radius_ip = path_part
                break
        
        if radius_ip:
            if not self.radius_config:
                self.radius_config = RadiusConfig()
            
            self.radius_config.host = radius_ip
            
            if 'timeout' in statement:
                timeout_match = re.search(r'timeout\s+(\d+)', statement)
                if timeout_match:
                    self.radius_config.timeout = int(timeout_match.group(1))
            
            elif 'retry-count' in statement:
                retry_match = re.search(r'retry-count\s+(\d+)', statement)
                if retry_match:
                    self.radius_config.retransmit = int(retry_match.group(1))
    
    def convert_interface_name(self, interface: str) -> str:
        """Convert JunOS QFX interface name to SONiC interface name"""
        # Handle xe-0/0/0 -> Eth 1/0 (FPC 0, slot 0, port 0 -> Eth 1/0)
        # QFX: FPC 0, slot 0, port X -> Eth 1/X
        xe_match = re.match(r'xe-(\d+)/(\d+)/(\d+)', interface, re.IGNORECASE)
        if xe_match:
            fpc = int(xe_match.group(1))
            slot = int(xe_match.group(2))
            port = int(xe_match.group(3))
            # For QFX with FPC 0, slot 0, map directly
            if fpc == 0 and slot == 0:
                return f'Eth 1/{port}'
            else:
                # Calculate port number: (fpc * slots_per_fpc + slot) * ports_per_slot + port
                # Simplified: assume standard mapping
                return f'Eth 1/{port}'
        
        # Handle et-0/0/0 (100G interfaces)
        et_match = re.match(r'et-(\d+)/(\d+)/(\d+)', interface, re.IGNORECASE)
        if et_match:
            fpc = int(et_match.group(1))
            slot = int(et_match.group(2))
            port = int(et_match.group(3))
            if fpc == 0 and slot == 0:
                return f'Eth 1/{port}'
            else:
                return f'Eth 1/{port}'
        
        # Handle ae0 -> PortChannel 0
        ae_match = re.match(r'ae(\d+)', interface, re.IGNORECASE)
        if ae_match:
            ae_id = ae_match.group(1)
            return f'PortChannel {ae_id}'
        
        # Handle lo0, lo1, lo0.0, lo1.0 -> Loopback 0, Loopback 1 (SONiC style with space)
        lo_match = re.match(r'lo(\d+)', interface, re.IGNORECASE)
        if lo_match:
            return f'Loopback {lo_match.group(1)}'
        
        # Handle irb.X -> Vlan X (VLAN SVI in JunOS)
        irb_match = re.match(r'irb\.(\d+)', interface, re.IGNORECASE)
        if irb_match:
            return f'Vlan {irb_match.group(1)}'
        
        # Return as-is if no conversion found
        return interface
