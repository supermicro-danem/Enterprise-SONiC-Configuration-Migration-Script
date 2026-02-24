# Test Configuration Files

This directory contains sample configuration files from each Network Operating System to be used as test cases for the migration tool.

## Files

### `cisco_nxos_sample.txt`
Sample Cisco NX-OS configuration covering:
- Basic system settings (hostname, users, DNS, NTP, syslog, SNMP, RADIUS)
- VLAN configuration
- Physical interfaces (trunk, access modes)
- **Physical interface ranges** (`interface range ethernet 1/6-10`)
- Port-channels
- **Port-channel ranges** (`interface range port-channel 30-35`)
- VPC (Virtual Port Channel) / MLAG configuration
- BGP routing with neighbors and route-maps
- VLAN interfaces with IP addresses
- Static routes

### `arista_eos_sample.txt`
Sample Arista EOS configuration covering:
- Basic system settings (hostname, users, DNS, NTP, syslog, SNMP, RADIUS, AAA)
- VLAN configuration
- Physical interfaces (trunk, access modes)
- **Physical interface ranges** (`interface range Ethernet6-10`)
- Port-channels
- **Port-channel ranges** (`interface range Port-Channel30-35`)
- MLAG configuration
- BGP routing with neighbors and route-maps
- VLAN interfaces with IP addresses
- Static routes

### `juniper_qfx_sample.txt`
Sample Juniper QFX configuration (hierarchical format) covering:
- System settings (hostname, users, DNS, NTP, syslog, SNMP, RADIUS)
- Physical interfaces (xe-0/0/X format)
- **Physical interface ranges** (xe-0/0/5-7 as sequential interfaces for range testing)
- Aggregated Ethernet (AE) interfaces (port-channels)
- **Port-channel ranges** (ae30-ae32 as sequential AEs for range testing)
- MC-LAG configuration
- BGP routing with groups and neighbors
- VRRP configuration
- VLAN interfaces with IP addresses
- Static routes

### Cumulus Linux (NCLU format)

Cumulus test files use **NCLU** (Network Command Line Utility) format: `net add` / `net show configuration commands` style.

#### `cumulus_nclu_test1.txt`
Data center leaf with MLAG (equivalent to EOS/NX-OS test1):
- System settings (hostname, loopback, DNS, NTP, syslog, SNMP)
- **Bridge** (VLAN-aware): `bridge vids`, `bridge pvid`
- Physical interfaces: `swp1`–`swp8` (uplinks, access, trunk)
- **Bonds**: `peerlink` (MLAG peer-link, swp9+swp10), `bond20` (server LAG with clag id)
- **clag** on peerlink.4094 (backup-ip, peer-ip linklocal, priority, sys-mac)
- VLAN SVIs (10, 20, 30, 40) with IP addresses
- **BGP**: `net add bgp neighbor <IP> remote-as`, description, update-source lo, ebgp-multihop

#### `cumulus_nclu_test2.txt`
Campus access switch (equivalent to EOS/NX-OS test2):
- Bridge vids 100, 200, 300; bridge pvid 1
- **Per-port native VLAN**: `net add interface swp3 bridge pvid 100` with `bridge trunk vlans 100,200` (voice trunk)
- Access ports (VLAN 100, 300), voice trunk ports (100+200, native 100), uplink trunk
- Port-channel (bond1) and standalone uplink
- BGP (AS 65100), loopback, static route

#### `cumulus_nclu_test3.txt`
Access switch with **VRR** (Virtual Router Redundancy) → translated to VRRP:
- Bridge vids 100, 200, 300; bridge pvid 1
- **VRR**: `net add vlan <id> ip address-virtual 00:00:5e:00:01:XX <VIP>/<cidr>` (group from MAC last octet)
- VLAN SVIs with address-virtual for VLANs 100 and 200
- BGP with neighbor description and ebgp-multihop
- No MLAG; single switch

**Cumulus-specific mappings:**
- `swpN` → `Eth 1/N`; `bondN` → `PortChannel N`; `peerlink` → MLAG peer-link PortChannel
- `bridge pvid <vlan>` (per-port) → `switchport trunk native vlan <vlan>`
- `ip address-virtual` (VRR) → VRRP with group ID from VRR MAC `00:00:5e:00:01:XX`
- Peer-link gets `switchport trunk allowed vlan` from bridge vids

## Usage

These files can be used to:
1. Test OS auto-detection
2. Validate parsing logic for each OS
3. Verify output SONiC configuration generation
4. Test error handling for unsupported features
5. Iterative development and validation

## Important: Two Configuration Formats

The test files include **both** configuration formats that users may provide:

### 1. **Run Book / Template Format** (with interface ranges)
- Uses `interface range` commands for efficiency
- Example: `interface range ethernet 1/6-10` (Cisco) or `interface range Ethernet6-10` (Arista)
- Common in templates, run books, and manual configurations
- **Parser must handle range expansion**

### 2. **Show Running-Config Output Format** (expanded individual interfaces)
- Shows each interface individually with its configuration
- Example: Individual `interface Ethernet1/11`, `interface Ethernet1/12`, etc.
- This is what `show running-config` outputs
- **Parser must handle individual interface parsing**

Both formats are included in the test files to ensure the migration tool works with either input type.

## Expected Output

Each configuration should generate a corresponding Enterprise SONiC configuration file with:
- Converted interface names (NX-OS: Ethernet1/1 → Eth 1/1, EOS: Ethernet1 → Eth 1/1, JunOS: xe-0/0/0 → Eth 1/1, **Cumulus: swp1 → Eth 1/1**, bond1 → PortChannel 1)
- Converted VLAN configurations
- Converted port-channel/LAG configurations (Cumulus bonds → PortChannels)
- Converted MLAG/VPC/MC-LAG/clag to MCLAG (Cumulus peerlink → peer-link PortChannel with trunk VLANs from bridge vids)
- Converted BGP configurations (Cumulus: neighbor remote-as, description, update-source Loopback0, ebgp-multihop)
- VRR (Cumulus) → VRRP with group from VRR MAC; trunk native vlan from bridge pvid
- Management interface configuration (Cumulus: no Management 0 IP when source has no explicit OOB and it would mirror an SVI)
- User accounts
- System services (NTP, syslog, SNMP, RADIUS)

## Notes

- These are representative samples and may not cover all edge cases
- Some features (like route-maps) may be logged as unsupported depending on implementation
- Interface numbering mappings may need adjustment based on actual hardware
- **Cumulus**: Test files are in NCLU format (`net add` commands). Use `net show configuration commands` output or equivalent; OOB management is not in NCLU, so Management 0 IP is not generated when it would duplicate an SVI address
