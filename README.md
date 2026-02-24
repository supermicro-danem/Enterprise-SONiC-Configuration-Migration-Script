# Multi-OS to Enterprise SONiC Configuration Migration Tool

A comprehensive Python tool for migrating network configurations from Cisco NX-OS, Arista EOS, Juniper JunOS (QFX), and Cumulus Linux to Enterprise SONiC format.

## Overview

This migration tool automates the conversion of network switch configurations from multiple vendor-specific formats to Enterprise SONiC CLI commands. It supports parsing complex hierarchical configurations, handles interface ranges, and provides detailed error reporting for unsupported features.

## Features

- **Multi-OS Support**: Migrates configurations from Cisco NX-OS, Arista EOS, Juniper JunOS (QFX), and Cumulus Linux
- **Auto-Detection**: Automatically detects the source OS from configuration content
- **Manual Fallback**: Allows manual OS selection if auto-detection fails
- **Comprehensive Parsing**: Supports:
  - Physical interfaces (individual and ranges)
  - VLAN interfaces
  - Port-Channels / Link Aggregation
  - BGP routing configuration
  - Static routes
  - Loopback interfaces
  - MCLAG/MLAG/VPC configuration
  - Management interface configuration
  - User authentication (local users)
  - RADIUS configuration
  - SNMP configuration
  - Syslog configuration
  - NTP configuration
  - DCBX configuration
  - VRRP configuration
- **Gateway Inference**: Automatically infers management gateway from static routes
- **Error Reporting**: Generates detailed reports of unsupported features with context
- **Partial Migration**: Allows migration to proceed with supported features while documenting unsupported ones

## Requirements

- Python 3.7 or higher
- No external dependencies (uses only Python standard library)

## Installation

1. Clone or download this repository
2. Ensure Python 3.7+ is installed
3. No additional packages need to be installed

## Usage

### Basic Usage

```bash
python3 multi_os_to_sonic_migrator.py <input_config_file> <output_file> [--source-os <os_type>]
```

### Arguments

- `input_config_file`: Path to the source configuration file (Cisco, Arista, Juniper, or Cumulus format)
- `output_file`: Path where the generated SONiC configuration will be written
- `--source-os` (optional): Manually specify the OS type. Options: `cisco`, `arista`, `juniper`, `cumulus`. If not provided, the tool will attempt auto-detection.

### Examples

#### Auto-detect OS
```bash
python3 multi_os_to_sonic_migrator.py cisco_config.txt sonic_output.txt
```

#### Manually specify OS
```bash
python3 multi_os_to_sonic_migrator.py arista_config.txt sonic_output.txt --source-os arista
```

#### Juniper configuration
```bash
python3 multi_os_to_sonic_migrator.py juniper_config.txt sonic_output.txt --source-os juniper
```

#### Cumulus Linux configuration
```bash
python3 multi_os_to_sonic_migrator.py cumulus_config.txt sonic_output.txt --source-os cumulus
```

## Interactive Prompts

During migration, the tool will prompt for:

1. **User Passwords**: For each user found in the source configuration
2. **Admin Password**: Always required (if not found in config)
3. **Management Gateway**: Only if not found in config and cannot be inferred from static routes
4. **MCLAG Peer IP**: If MLAG/MCLAG/VPC configuration is detected
5. **RADIUS Secret Key**: If RADIUS server is configured

### Cumulus Linux-Specific Prompts

For Cumulus Linux configurations, additional prompts are provided:

1. **NTP Preferred Server**: If multiple NTP servers are configured, you'll be asked if one should be preferred (since NCLU doesn't support the `prefer` flag)
2. **AAA/RADIUS Usage**: You'll be asked if AAA/RADIUS authentication is used on the switch (since it's configured via files, not NCLU commands)

### Gateway Auto-Inference

The tool automatically infers the management gateway from static routes when:
- A default route (0.0.0.0/0) exists with a next-hop in the management network
- Any static route has a next-hop in the same network as the management IP

If inferred, you'll see: `Inferred management gateway from static routes: <gateway_ip>`

## Output Files

The tool generates two files:

1. **SONiC Configuration File** (`<output_file>`): Contains the Enterprise SONiC CLI commands
2. **Migration Report** (`<output_file>.report.txt`): Contains:
   - Migration statistics (VLANs, interfaces, routes, etc.)
   - List of unsupported features (if any)
   - Warnings and notes

## Supported Configuration Elements

### Interfaces
- Physical interfaces (Ethernet, xe-, et-)
- Interface ranges (Cisco/Arista format)
- VLAN interfaces (SVI)
- Port-Channel / Link Aggregation
- Loopback interfaces
- Management interface

### Layer 2
- VLANs (access and trunk)
- Native/untagged VLANs
- Trunk allowed VLANs
- Port-channel configuration
- MLAG/MCLAG/VPC conversion

### Layer 3
- IP addresses (with CIDR or subnet mask)
- Static routes
- BGP configuration (ASN, router-id, neighbors, per-neighbor remote-as, description, update-source, ebgp-multihop, route-maps in/out, redistribute)
- VRRP configuration (group ID, virtual IP, priority, preempt)

### Management & Services
- Management interface IP and gateway
- Management VRF (`ip vrf mgmt`)
- User accounts and passwords
- RADIUS authentication
- SNMP communities
- Syslog servers
- NTP servers
- DCBX configuration

## Unsupported Features

The tool tracks and reports unsupported features. Common unsupported features include:
- Complex route-map configurations under BGP neighbors
- Some vendor-specific features not available in SONiC
- Advanced QoS configurations
- Some security features

The migration will proceed with supported features, and unsupported ones will be documented in the report file.

## Project Structure

```
.
├── multi_os_to_sonic_migrator.py  # Main entry point
├── base_migrator.py                # Base class and common utilities
├── sonic_config_generator.py       # SONiC configuration generation
├── cisco_nxos_parser.py            # Cisco NX-OS parser
├── arista_eos_parser.py            # Arista EOS parser
├── juniper_junos_parser.py         # Juniper JunOS parser
├── cumulus_linux_parser.py         # Cumulus Linux parser
├── test_all_configs.py            # Automated test suite
├── test_configs/                  # Sample configuration files
│   ├── cisco_nxos_sample.txt, cisco_nxos_test1-4.txt
│   ├── arista_eos_sample.txt, arista_eos_test1-3.txt
│   ├── juniper_qfx_sample.txt, juniper_qfx_test1-3.txt
│   └── cumulus_nclu_test1.txt, cumulus_nclu_test2.txt, cumulus_nclu_test3.txt
└── test_outputs/                  # Generated SONiC configs and reports
```

## Testing

Run the automated test suite to validate all configurations:

```bash
python3 test_all_configs.py
```

This will test all sample configurations (19 total: 5 Cisco, 4 Arista, 5 Juniper, 3 Cumulus) and generate a summary report.

## Configuration File Formats

### Cisco NX-OS
Standard `show running-config` output or configuration template format.

### Arista EOS
Standard `show running-config` output or configuration template format.

### Juniper JunOS (QFX)
Standard `show configuration` output in set/display format or hierarchical format.

### Cumulus Linux
NCLU (Network Command Line Utility) format with `net add` commands. This is the output from `net show configuration commands` or configuration files that can be applied with `sudo net commit`.

**Note**: Cumulus Linux uses:
- **Bonds** instead of port-channels (e.g., `bond1`, `bond20`, `peerlink`)
- **Bridge-based VLANs** with VLAN-aware mode (`bridge vids`, `bridge pvid`)
- **clag** commands for MLAG (instead of `mlag`)
- **VRR** (Virtual Router Redundancy) instead of VRRP (translated to VRRP in SONiC)
- **Interface naming**: `swp1`, `swp2`, etc. (switched ports)

**Cumulus translation details**:
- **BGP**: `net add bgp neighbor <IP> remote-as <AS>` and sub-commands (description, update-source, ebgp-multihop) are parsed and emitted as SONiC BGP neighbor config. `update-source lo` is mapped to `Loopback0`.
- **Native VLAN on trunk**: `net add interface swpX bridge pvid <vlan>` is translated to `switchport trunk native vlan <vlan>`.
- **VRR → VRRP**: `ip address-virtual <MAC> <VIP>` uses the VRR MAC (`00:00:5e:00:01:XX`) to derive the VRRP group number (last octet hex → decimal). A comment block in the SONiC output explains the active/active (VRR) vs active/standby (VRRP) behavioral difference.
- **Management 0**: When the source has no explicit OOB management config (typical for NCLU), the tool does not create a Management 0 IP that duplicates an SVI address.
- **Peer-link trunk**: The MLAG peer-link PortChannel gets explicit `switchport trunk allowed vlan` from the source bridge VLAN set (`bridge vids`).

**Special Considerations**:
- NTP `prefer` flag is not available in NCLU - you'll be prompted if multiple NTP servers exist
- AAA/RADIUS authentication is configured via files (`/etc/pam_radius_auth.conf`, `/etc/nsswitch.conf`) - you'll be prompted if AAA is used

## Interface Name Conversion

The tool automatically converts vendor-specific interface names to SONiC format:

- **Cisco**: `Ethernet1/1` → `Eth 1/1`
- **Arista**: `Ethernet1` → `Eth 1/1`
- **Juniper**: `xe-0/0/0` → `Eth 1/1`, `et-0/0/0` → `Eth 1/1`
- **Cumulus**: `swp1` → `Eth 1/1`, `swp2` → `Eth 1/2`, etc.

## Notes

- **Juniper Support**: Currently focused on QFX data center switches with ELS (Enhanced Layer 2 Software)
  - **Important**: QFX with ELS requires VLAN members to reference VLAN names, not numeric IDs
  - Example: Use `members [ Server-VLAN Management-VLAN ]` instead of `members [ 100 200 ]`
- **Cumulus Linux Support**: Supports NCLU format configurations
  - **Interface Mapping**: `swp1` → `Eth 1/1`, `swp2` → `Eth 1/2`, etc.
  - **Bond Translation**: `bond1` → `PortChannel 1`, `bond20` → `PortChannel 20`, `peerlink` → MLAG peer-link port-channel
  - **MLAG/clag**: Translates `clag` commands to SONiC MCLAG format; peer-link gets trunk VLANs from bridge vids
  - **VRR to VRRP**: Translates Cumulus VRR (`ip address-virtual`) to SONiC VRRP; VRRP group from VRR MAC last octet; duplicate VRRP lines suppressed; behavioral note (active/active → active/standby) in output
  - **Bridge pvid**: Per-port `bridge pvid <vlan>` → `switchport trunk native vlan <vlan>`
  - **NTP Prefer**: Prompts user if multiple NTP servers exist (NCLU doesn't support `prefer` flag)
  - **AAA/RADIUS**: Prompts user if AAA is used (configured via files, not NCLU)
- **Interface Ranges**: Supports both explicit `interface range` commands and expanded individual interfaces
- **Management VRF**: Automatically configures `ip vrf mgmt` for Management0 interface (placed before interface configuration)
- **Description fields**: All description values in the generated SONiC config are enclosed in double quotes (e.g., `description "My Interface"`).
- **Partial Migration**: The tool will generate a valid SONiC configuration even if some features are unsupported
- **Test Configurations**: All sample configurations have been validated and tested (19 total)

## Troubleshooting

### Auto-detection fails
Use the `--source-os` flag to manually specify the OS type.

### Missing gateway prompt
If the management IP is found but gateway is not, the tool will:
1. Try to infer from static routes
2. If inference fails, prompt for manual entry

### Unsupported features
Check the `.report.txt` file for a detailed list of unsupported features and their context.

## License

This tool is provided as-is for network configuration migration purposes.

## Contributing

When adding support for new features or OS types:
1. Extend the appropriate parser class
2. Add corresponding SONiC generation logic
3. Update test configurations
4. Run the test suite to validate

## Version History

- **v1.2**: Cumulus and generator enhancements
  - Cumulus BGP: full neighbor translation (remote-as, description, update-source, ebgp-multihop)
  - Cumulus: bridge pvid per-port → switchport trunk native vlan
  - VRRP group number derived from VRR MAC (00:00:5e:00:01:XX)
  - Duplicate VRRP command suppression in output
  - Management 0: do not create when source has no explicit OOB and IP would mirror an SVI
  - MCLAG peer-link: explicit trunk VLAN membership from bridge vids
  - VRR→VRRP behavioral note in SONiC output (active/active vs active/standby)
  - Description fields always enclosed in double quotes in generated config
- **v1.1**: Added Cumulus Linux support
  - NCLU command parsing
  - Interface mapping (swp → Eth)
  - Bond to port-channel translation
  - MLAG/clag translation
  - VRR to VRRP translation
  - NTP prefer and AAA/RADIUS prompts
- **v1.0**: Initial release with support for Cisco NX-OS, Arista EOS, and Juniper JunOS (QFX)
  - Gateway inference from static routes
  - Comprehensive error reporting
  - Management VRF support
  - Interface range handling
  - Support for Juniper QFX ELS syntax (VLAN name references)
  - Enhanced static route parsing for gateway inference