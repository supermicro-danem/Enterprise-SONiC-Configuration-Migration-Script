# Multi-OS to Enterprise SONiC Configuration Migration Tool

A Python tool for migrating network configurations from Cisco NX-OS, Arista EOS, Juniper JunOS (QFX), and Cumulus Linux to Enterprise Advanced SONiC (EAS) IS-CLI format.

## Overview

This migration tool automates the conversion of network switch configurations from multiple vendor-specific formats to Enterprise Advanced SONiC CLI commands. It supports parsing complex hierarchical configurations, handles interface ranges, and provides detailed error reporting for unsupported features.

The generated IS-CLI output has been validated on live Supermicro SSE-T8164 hardware running Enterprise Advanced SONiC; see the Hardware Validation section for the tested behaviors and known hardware-target limitations.

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

1. **SONiC Configuration File** (`<output_file>`): Contains the Enterprise Advanced SONiC IS-CLI commands. The file is organized for readability with `!` separators between blocks. The output opens with an atomic `sonic-cli` + `configure terminal` envelope. After `hostname` and `interface-naming standard`, the script emits `end` + `exit` followed by a **paste boundary** comment block instructing the operator to exit `sonic-cli` and re-enter before continuing (the `interface-naming standard` mode change is only honored by a fresh `sonic-cli` session; confirmed on live EAS hardware). The second `sonic-cli` + `configure terminal` envelope follows, then the remainder of the configuration. MCLAG member PortChannels appear after the MCLAG domain block. Management0 and other interface blocks use indented sub-commands; native/untagged VLANs are emitted as `switchport access vlan <id>`. When DCBX (`buffer init lossless`) is present, a third envelope is emitted after a reboot marker for the post-reboot commands.
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
├── test_outputs/                  # Generated SONiC configs and reports
└── test_goldens/                  # Committed baseline for regression diff
```

## Testing

Run the automated test suite to validate all configurations:

```bash
python3 test_all_configs.py
```

This migrates every sample under `test_configs/` (16 total: 5 Cisco, 4 Arista, 4 Juniper, 3 Cumulus) into `test_outputs/`, prints a per-configuration summary, and then compares each generated `_sonic.txt` and `_sonic.report.txt` byte-for-byte against the matching committed baseline in `test_goldens/`.

### Clean run

A clean run ends with the line `Golden diff check: PASS` and exit code 0. All migrations completed and all outputs matched their committed goldens exactly.

### Golden-diff failure

If any generated output differs from its golden file, the harness prints a unified diff (truncated to the first 30 lines per file) and exits with code 1. A non-zero exit indicates that code changes have altered the generated SONiC configuration for one or more inputs. This is always intentional or a regression; it must never be ignored.

When a golden diff fails at PR-review time, it must be resolved in one of two ways before the goldens are regenerated:

1. Fix the code so that the generated output returns to matching the committed golden, or
2. Obtain an explicit sign-off from the PR reviewer that the new output is the correct behavior for this change.

Regenerating goldens without one of those two outcomes is not acceptable and will mask regressions.

### Regenerating goldens

Once a diff has been approved (option 2 above), refresh the committed baseline with:

```bash
python3 test_all_configs.py --update-goldens
```

The `--update-goldens` flag re-runs all migrations and then copies every `_sonic.txt` and `_sonic.report.txt` from `test_outputs/` into `test_goldens/`, overwriting the previous baseline. Commit the updated `test_goldens/` files together with the code change that produced them, in the same PR, so that reviewers see both halves of the contract.

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
- **Native VLAN on trunk**: `net add interface swpX bridge pvid <vlan>` is translated to `switchport access vlan <vlan>`.
- **VRR → VRRP**: `ip address-virtual <MAC> <VIP>` uses the VRR MAC (`00:00:5e:00:01:XX`) to derive the VRRP group number (last octet hex → decimal). A comment block in the SONiC output explains the active/active (VRR) vs active/standby (VRRP) behavioral difference.
- **Management 0**: NCLU has no explicit OOB management in config; the tool uses the same prompt logic as other NOSes (e.g. when MCLAG needs a management IP). When the user supplies management IP at prompt, Management0 is created. When the source has explicit management and the IP would duplicate an SVI, the tool does not create Management0 for that case.
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
  - **Bridge pvid**: Per-port `bridge pvid <vlan>` → `switchport access vlan <vlan>`
  - **NTP Prefer**: Prompts user if multiple NTP servers exist (NCLU doesn't support `prefer` flag)
  - **AAA/RADIUS**: Prompts user if AAA is used (configured via files, not NCLU)
- **Interface Ranges**: Supports both explicit `interface range` commands and expanded individual interfaces
- **Management VRF**: Automatically configures `ip vrf mgmt` for Management0 interface (placed before interface configuration)
- **Description fields**: All description values in the generated SONiC config are enclosed in double quotes (e.g., `description "My Interface"`).
- **Partial Migration**: The tool will generate a valid SONiC configuration even if some features are unsupported
- **Test Configurations**: All sample configurations have been validated and tested (16 total)

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

## Hardware Validation

This tool has been validated against live Supermicro SSE-T8164 running Enterprise Advanced SONiC. Three rounds of paste-based hardware validation on `test_goldens/cisco_nxos_sample_sonic.txt` confirmed the following EAS-specific behaviors are emitted correctly:

- `spanning-tree mode` uses valid EAS keywords (`mst`, `pvst`, `rapid-pvst`); `rstp` source keyword is normalized to `rapid-pvst`, `mstp` to `mst`. `spanning-tree enable` is never emitted (not a valid EAS command).
- `snmp-server community <name> ro|rw` is emitted without the `ro|rw` suffix (EAS rejects the suffix).
- `interface range` uses the `Eth` keyword, never `Ethernet` (EAS range-mode parser requires the short form in standard interface-naming mode).
- BGP `update-source` is emitted with the explicit `interface` keyword before an interface name (e.g., `update-source interface Loopback0`); IP-literal sources pass through unchanged.
- LAG member ports emit a matching `mtu <value>` line before `channel-group N` when the target PortChannel has an explicit MTU (EAS rejects `channel-group` when port MTU differs from PortChannel MTU; NX-OS inheritance is not available).
- The paste-boundary comment block is emitted between the `hostname` / `interface-naming standard` block and the remaining configuration (fresh `sonic-cli` session required for the naming mode change to take effect).
- `ip vrf mgmt` is auto-emitted before Management0 when an OOB management interface is configured.

**Hardware-target limitations (not generator defects):**
- **Speed keywords**: SSE-T8164 is a 100G/400G-only platform; sample fixtures that target 1G/10G access-switch speeds will see `%Error: Unsupported speed` on the applicable interfaces. This is a hardware/source-config mismatch.
- **Undefined route-maps**: If a source configuration references a route-map under BGP `redistribute` that it does not define, EAS rejects the command with `%Error: No instance found`. The source config must provide the referenced route-map definitions.

## Version History

- **v1.4**: Hardware-validated IS-CLI first pass
  - **Paste boundary**: `end` + `exit` + comment block + re-entry emitted after `hostname` / `interface-naming standard` (EAS requires a fresh `sonic-cli` session before the mode change is honored)
  - **Spanning-tree**: source STP mode recorded verbatim by parsers; generator normalizes to `mst`, `pvst`, or `rapid-pvst` (defaults to `rapid-pvst`); `spanning-tree enable` removed (HW-1 / HW-7)
  - **SNMP**: `ro`/`rw` suffix dropped from `snmp-server community` emission (HW-2)
  - **Interface range**: canonical `Eth` keyword replaces `Ethernet` in range specs (HW-3 / HW-4)
  - **BGP `update-source`**: explicit `interface` keyword inserted before interface names on both peer-group and individual neighbor paths (HW-5 / HW-10)
  - **LAG member MTU**: matching `mtu <value>` emitted before `channel-group N` on member ports whose PortChannel carries an explicit MTU; `mtu_configured` flag is now propagated via `_transfer_mtu_to_port_channels()` (HW-9)
  - **`ip vrf mgmt`**: auto-emitted before Management0 when an OOB management interface is configured (FR-3)
  - **BGP `redistribute`**: canonical form with optional `route-map` (FR-4)
  - **PortChannel / interface range**: canonical form with space separator (FR-5 / FR-6)
  - **Security**: credential input sanitized to reject embedded newlines and shell metacharacters; test suite hardened against credential-injection sample fixtures
  - **Test harness**: `test_goldens/` committed baseline; `test_all_configs.py` runs every fixture through the migrator and diffs byte-for-byte against the baseline; `--update-goldens` refreshes after sign-off
- **v1.3**: Output configuration organization
  - Header flow: after hostname and interface-naming standard, emit exit → write memory → exit (then re-enter sonic-cli to continue)
  - MCLAG: domain block (with space-prefixed sub-commands and trailing exit) emitted before MCLAG member PortChannels
  - Comment separators (`!`) between interface blocks (VLAN, physical, PortChannel, loopback, range) and between `ip vrf mgmt` and Management0
  - Management0: `  ip address` sub-command; NCLU creates Management0 when user supplies management IP (same prompt logic as other NOSes)
  - Output uses `switchport access vlan <id>` instead of `switchport trunk native vlan` for native/untagged VLANs
- **v1.2**: Cumulus and generator enhancements
  - Cumulus BGP: full neighbor translation (remote-as, description, update-source, ebgp-multihop)
  - Cumulus: bridge pvid per-port → switchport access vlan
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