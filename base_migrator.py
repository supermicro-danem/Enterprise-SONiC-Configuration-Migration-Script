#!/usr/bin/env python3
"""
Base Migrator Class for Multi-OS to Enterprise SONiC Configuration Migration

This module provides the abstract base class and common data structures
for migrating network configurations from various OSes to Enterprise SONiC.
"""

import re
import ipaddress
from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field


def sanitize_for_output(value):
    """Strip newline/carriage-return characters from a parsed config field
    before it is stored in a dataclass. Prevents newline-injection attacks
    where a crafted source config embeds \\n or \\r in a field value,
    which would otherwise leak extra IS-CLI lines into the generated output.

    Part of defense-in-depth; per-field allowlist validation is tracked
    separately (see backlog issue #5).
    """
    if value is None:
        return value
    if isinstance(value, str):
        return value.replace('\r\n', '').replace('\n', '').replace('\r', '')
    return value


@dataclass
class VlanConfig:
    """Represents VLAN configuration"""
    vlan_id: str
    name: str = ""
    description: str = ""
    ports_untagged: List[str] = field(default_factory=list)
    ports_tagged: List[str] = field(default_factory=list)
    ip_address: str = ""
    subnet_mask: str = ""
    mtu: int = 1500
    mtu_configured: bool = False  # True only when source had explicit "mtu X"
    vrrp_configs: List[Dict] = field(default_factory=list)


@dataclass
class PortChannelConfig:
    """Represents Port-Channel configuration"""
    po_id: str
    description: str = ""
    mtu: int = 9000
    mtu_configured: bool = False  # True only when source had explicit "mtu X"
    speed: str = ""
    mode: str = ""  # trunk or access
    allowed_vlans: List[str] = field(default_factory=list)
    access_vlan: str = ""
    native_vlan: str = ""  # trunk native vlan
    mlag_enabled: bool = False
    spanning_tree_disable: bool = False
    # L3 routed (no switchport) with IP
    l3_routed: bool = False
    ip_address: str = ""
    subnet_mask: str = ""


@dataclass
class PhysicalInterfaceConfig:
    """Represents Physical Interface configuration"""
    interface: str
    speed: str = ""
    fec: str = "auto"  # auto, no fec, etc.
    mtu: int = 9000
    mtu_configured: bool = False  # True only when source had explicit "mtu X"
    negotiation: bool = True
    channel_group: str = ""
    shutdown: bool = False
    description: str = ""
    lldp_settings: List[str] = field(default_factory=list)
    # Switchport configuration
    switchport_mode: str = ""  # access, trunk, or empty
    access_vlan: str = ""
    allowed_vlans: List[str] = field(default_factory=list)
    native_vlan: str = ""  # trunk native vlan
    # L3 routed (no switchport) with IP
    l3_routed: bool = False
    ip_address: str = ""
    subnet_mask: str = ""
    # DCBX specific fields
    cee_map: str = ""
    dcbx_enabled: bool = False
    dcbx_lldp_settings: List[str] = field(default_factory=list)


@dataclass
class LoopbackConfig:
    """Represents Loopback interface configuration"""
    interface: str
    ip_address: str = ""
    subnet_mask: str = ""
    description: str = ""


@dataclass
class StaticRouteConfig:
    """Represents Static Route configuration"""
    network: str = ""
    mask: str = ""
    next_hop: str = ""
    interface: str = ""


@dataclass
class DCBXConfig:
    """Represents DCBX CEE-MAP configuration"""
    map_id: str
    pri2pg: str = ""
    pfc_priorities: List[str] = field(default_factory=list)
    group_bandwidth: str = ""
    pfc_groups_disable: List[str] = field(default_factory=list)
    pfc_groups_enable: List[str] = field(default_factory=list)
    group_descriptions: Dict[str, str] = field(default_factory=dict)


@dataclass
class SyslogConfig:
    """Represents Syslog server configuration"""
    servers: List[str] = field(default_factory=list)


@dataclass
class RadiusConfig:
    """Represents RADIUS server configuration"""
    host: str = ""
    timeout: int = 15
    retransmit: int = 5
    key: str = ""  # Will be prompted for in SONiC


@dataclass
class SnmpConfig:
    """Represents SNMP configuration"""
    communities: Dict[str, str] = field(default_factory=dict)  # community_name -> permission (ro/rw)
    views: List[str] = field(default_factory=list)


@dataclass
class PrefixListEntry:
    """Single entry in an IP prefix-list (Enterprise SONiC: ip prefix-list <name> seq <n> permit|deny <prefix> [ge] [le])"""
    list_name: str
    seq: int
    action: str  # permit | deny
    prefix: str  # e.g. 192.168.100.0/24
    ge: Optional[int] = None
    le: Optional[int] = None


@dataclass
class RouteMapEntry:
    """Single sequence in a route-map (Enterprise SONiC: route-map <name> permit|deny <seq> then match/set)"""
    map_name: str
    seq: int
    action: str  # permit | deny
    matches: List[str] = field(default_factory=list)  # e.g. "match ip address prefix-list X", "match interface Eth1/2"
    sets: List[str] = field(default_factory=list)    # e.g. "set metric 100", "set local-preference 10000"


@dataclass
class UnsupportedFeature:
    """Represents an unsupported feature with context"""
    line: str
    line_number: int
    context_stack: List[str]
    reason: str


@dataclass
class Warning:
    """Represents a warning with context"""
    line: str
    line_number: int
    context_stack: List[str]
    message: str


@dataclass
class ReportNote:
    """Represents a note to flag in the report (e.g. MLAG-only features with no non-MLAG equivalent)"""
    line: str
    line_number: int
    context_stack: List[str]
    message: str


class BaseMigrator(ABC):
    """Abstract base class for OS-specific configuration migrators"""
    
    def __init__(self):
        """Initialize the base migrator"""
        # Error tracking system
        self.unsupported_features: List[UnsupportedFeature] = []
        self.warnings: List[Warning] = []
        self.report_notes: List[ReportNote] = []
        self.current_context: List[str] = []  # Context stack for hierarchical tracking
        
        # Configuration state tracking
        self.reset_state()
    
    def reset_state(self):
        """Reset parser state for new configuration"""
        self.hostname = ""
        # HW-1/HW-7: spanning-tree mode as it appeared in the source config
        # (free-form lowercase string, e.g. 'rstp', 'mstp', 'rapid-pvst',
        # 'mst', 'pvst', or '' when not explicitly set). The generator
        # normalizes this to the EAS-accepted set ('rapid-pvst' | 'mst' |
        # 'pvst') and defaults to 'rapid-pvst' when no mode was parsed.
        # EAS does NOT accept 'rstp' or 'mstp' as keywords.
        self.stp_mode: str = ""
        self.management_ip = ""
        self.management_mask = ""
        self.management_gateway = ""
        self.users: Dict[str, Dict[str, str]] = {}
        self.vlans: Dict[str, VlanConfig] = {}
        self.port_channels: Dict[str, PortChannelConfig] = {}
        self.physical_interfaces: Dict[str, PhysicalInterfaceConfig] = {}
        self.loopbacks: Dict[str, LoopbackConfig] = {}
        self.static_routes: List[StaticRouteConfig] = []
        self.mlag_config: Dict = {}
        self.bgp_config: Dict = {}
        self.vrrp_config: Dict = {}
        self.has_explicit_management_config: bool = True  # False when source has no OOB mgmt (e.g. Cumulus)
        self.global_settings: Dict = {}
        self.dcbx_configs: Dict[str, DCBXConfig] = {}
        self.syslog_config = SyslogConfig()
        self.radius_config: Optional[RadiusConfig] = None
        self.snmp_config = SnmpConfig()
        self.prefix_lists: Dict[str, List[PrefixListEntry]] = {}
        self.route_maps: Dict[str, List[RouteMapEntry]] = {}
        self.report_notes = []
        
        # Parser state
        self.current_section = "global"
        self.current_vlan: Optional[str] = None
        self.current_interface: Optional[str] = None
        self.current_po: Optional[str] = None
        self.current_dcbx: Optional[str] = None
        self.current_line_number = 0
        # Note: current_neighbor is set in OS-specific parsers' __init__ methods
    
    # Context management methods
    def push_context(self, context_name: str):
        """Push a context onto the context stack"""
        self.current_context.append(context_name)
    
    def pop_context(self) -> Optional[str]:
        """Pop a context from the context stack"""
        if self.current_context:
            return self.current_context.pop()
        return None
    
    def get_context_string(self) -> str:
        """Get the current context as a string (for display)"""
        return " > ".join(self.current_context) if self.current_context else "global"
    
    # Generic message for any configuration not translated (used by catch-all in parsers)
    UNSUPPORTED_MSG = "Configuration not translated; configure manually on SONiC if needed."
    
    # Error tracking methods
    def log_unsupported_feature(self, line: str, reason: str, line_number: Optional[int] = None):
        """Log an unsupported feature with current context"""
        if line_number is None:
            line_number = self.current_line_number
        
        feature = UnsupportedFeature(
            line=line.strip(),
            line_number=line_number,
            context_stack=self.current_context.copy(),
            reason=reason
        )
        self.unsupported_features.append(feature)
    
    def log_warning(self, line: str, message: str, line_number: Optional[int] = None):
        """Log a warning with current context"""
        if line_number is None:
            line_number = self.current_line_number
        
        warning = Warning(
            line=line.strip(),
            line_number=line_number,
            context_stack=self.current_context.copy(),
            message=message
        )
        self.warnings.append(warning)
    
    def log_report_note(self, line: str, message: str, line_number: Optional[int] = None):
        """Log a note to be flagged in the report (e.g. MLAG-only features with no non-MLAG equivalent)"""
        if line_number is None:
            line_number = self.current_line_number
        
        note = ReportNote(
            line=line.strip(),
            line_number=line_number,
            context_stack=self.current_context.copy(),
            message=message
        )
        self.report_notes.append(note)
    
    def get_unsupported_report(self) -> str:
        """Generate a hierarchical report of unsupported features"""
        if not self.unsupported_features:
            return ""
        
        # Build report - show only the immediate relevant context for each feature
        report_lines = ["Unsupported Features:", "=" * 50, ""]
        
        for feature in self.unsupported_features:
            # Show only the immediate parent context (last 1-2 levels) for clarity
            # This avoids showing the entire path which makes it look like everything is unsupported
            if feature.context_stack:
                # Get the last context level (most specific) - this is where the unsupported feature actually is
                # The last context is the most relevant (e.g., "neighbor 10.10.10.3" for route-map)
                last_context = feature.context_stack[-1]
                report_lines.append(f"{last_context}")
                # Use ASCII-safe characters for Windows compatibility
                report_lines.append(f"  - {feature.line} (line {feature.line_number}) - {feature.reason}")
            else:
                report_lines.append(f"  - {feature.line} (line {feature.line_number}) - {feature.reason}")
            report_lines.append("")
        
        return "\n".join(report_lines)
    
    def has_unsupported_features(self) -> bool:
        """Check if there are any unsupported features"""
        return len(self.unsupported_features) > 0
    
    # Abstract methods to be implemented by subclasses
    @abstractmethod
    def parse_config(self, config: str):
        """Parse the configuration file into structured data"""
        pass
    
    @abstractmethod
    def convert_interface_name(self, interface: str) -> str:
        """Convert source OS interface name to SONiC interface name"""
        pass
    
    # Common utility methods
    def _mask_to_cidr(self, mask: str) -> int:
        """Convert subnet mask to CIDR notation"""
        try:
            return sum([bin(int(x)).count('1') for x in mask.split('.')])
        except:
            return 24  # Default /24
    
    def _find_gateway_from_static_routes(self, management_ip: str, management_mask: str = "") -> Optional[str]:
        """Find default gateway from static routes matching the management IP network
        
        Args:
            management_ip: Management IP address
            management_mask: Subnet mask (optional, will be calculated if not provided)
            
        Returns:
            Gateway IP address if found, None otherwise
        """
        if not management_ip or not self.static_routes:
            return None
        
        # Calculate management network
        try:
            if management_mask:
                # Convert mask to CIDR if needed
                if '/' in management_mask:
                    cidr = int(management_mask.split('/')[1])
                else:
                    cidr = self._mask_to_cidr(management_mask)
                mgmt_network = ipaddress.IPv4Network(f'{management_ip}/{cidr}', strict=False)
            else:
                # Try to infer from static routes or use /24 as default
                mgmt_network = ipaddress.IPv4Network(f'{management_ip}/24', strict=False)
        except (ValueError, ipaddress.AddressValueError):
            return None
        
        # Look for default route (0.0.0.0/0) first
        for route in self.static_routes:
            if route.network == '0.0.0.0' and (route.mask == '0.0.0.0' or route.mask == '' or route.network == '0.0.0.0/0'):
                if route.next_hop:
                    # Check if next-hop is in the same network as management IP
                    try:
                        next_hop_ip = ipaddress.IPv4Address(route.next_hop)
                        if next_hop_ip in mgmt_network:
                            return route.next_hop
                    except (ValueError, ipaddress.AddressValueError):
                        pass
                    # Even if not in same network, default route next-hop is likely the gateway
                    return route.next_hop
        
        # Look for routes in the same network as management IP
        for route in self.static_routes:
            if route.next_hop:
                try:
                    # Check if route network matches management network
                    if route.network and route.mask:
                        route_network = ipaddress.IPv4Network(f'{route.network}/{self._mask_to_cidr(route.mask)}', strict=False)
                        if route_network == mgmt_network or route_network.overlaps(mgmt_network):
                            next_hop_ip = ipaddress.IPv4Address(route.next_hop)
                            if next_hop_ip in mgmt_network:
                                return route.next_hop
                    # Check if next-hop is in management network
                    next_hop_ip = ipaddress.IPv4Address(route.next_hop)
                    if next_hop_ip in mgmt_network:
                        return route.next_hop
                except (ValueError, ipaddress.AddressValueError):
                    continue
        
        return None
    
    def _cidr_to_mask(self, cidr: int) -> str:
        """Convert CIDR notation to subnet mask"""
        mask = (0xffffffff >> (32 - cidr)) << (32 - cidr)
        return '.'.join([str((mask >> (8 * (3 - i))) & 0xff) for i in range(4)])
    
    def _extract_port_number(self, interface: str) -> int:
        """Extract port number for sorting (handles x/y format properly)"""
        # Look for pattern like "Ethernet1/15" or "Eth 1/25" and extract the final number
        match = re.search(r'(\d+)/(\d+)$', interface)
        if match:
            return int(match.group(2))  # Return the y part of x/y
        
        # Fallback for other formats
        match = re.search(r'(\d+)$', interface)
        if match:
            return int(match.group(1))
        return 999
    
    def _extract_sonic_port_number(self, sonic_interface: str) -> int:
        """Extract port number from SONiC interface for proper sorting"""
        # Extract from "Eth 1/XX" format
        match = re.search(r'Eth 1/(\d+)$', sonic_interface)
        if match:
            return int(match.group(1))
        
        # Fallback
        match = re.search(r'(\d+)$', sonic_interface)
        if match:
            return int(match.group(1))
        return 999
    
    def _ensure_vlan1_exists(self):
        """Ensure VLAN 1 exists for interfaces that need it"""
        if '1' not in self.vlans:
            self.vlans['1'] = VlanConfig(vlan_id='1', name='default')
    
    def _transfer_mtu_to_port_channels(self):
        """Transfer MTU configurations from interfaces to their PortChannels"""
        for interface, intf_config in self.physical_interfaces.items():
            if intf_config.channel_group and intf_config.mtu != 9000:
                po_id = intf_config.channel_group
                if po_id not in self.port_channels:
                    self.port_channels[po_id] = PortChannelConfig(po_id=po_id)
                # Set MTU on the PortChannel to match the interface
                self.port_channels[po_id].mtu = intf_config.mtu
    
    def _interface_needs_vlan_assignment(self, intf_config: PhysicalInterfaceConfig) -> bool:
        """Check if interface needs VLAN 1 assignment"""
        # Skip if interface is shutdown
        if intf_config.shutdown:
            return False
        
        # If interface has switchport config, it doesn't need auto-assignment
        if intf_config.switchport_mode or intf_config.access_vlan or intf_config.allowed_vlans:
            return False
        
        # If interface has channel-group, check the corresponding PortChannel
        if intf_config.channel_group:
            po_config = self.port_channels.get(intf_config.channel_group)
            if po_config and (po_config.mode or po_config.access_vlan or po_config.allowed_vlans):
                return False
        
        # Interface needs VLAN 1 assignment
        return True
    
    def _apply_vlan1_assignments(self):
        """Apply VLAN 1 assignments to interfaces that need it"""
        needs_vlan1 = False
        
        # Check physical interfaces
        for interface, intf_config in self.physical_interfaces.items():
            if self._interface_needs_vlan_assignment(intf_config):
                if intf_config.channel_group:
                    # Assign to the corresponding PortChannel instead
                    po_config = self.port_channels.get(intf_config.channel_group)
                    if po_config and not po_config.mode and not po_config.access_vlan:
                        po_config.mode = 'access'
                        po_config.access_vlan = '1'
                        needs_vlan1 = True
                else:
                    # Assign directly to the interface
                    intf_config.switchport_mode = 'access'
                    intf_config.access_vlan = '1'
                    needs_vlan1 = True
        
        # Check PortChannels directly (not just through channel-group assignments)
        for po_id, po_config in self.port_channels.items():
            # Check if PortChannel needs VLAN assignment (no access_vlan or allowed_vlans)
            if not po_config.access_vlan and not po_config.allowed_vlans:
                po_config.mode = 'access'
                po_config.access_vlan = '1'
                needs_vlan1 = True
        
        # Create VLAN 1 if needed
        if needs_vlan1:
            self._ensure_vlan1_exists()
