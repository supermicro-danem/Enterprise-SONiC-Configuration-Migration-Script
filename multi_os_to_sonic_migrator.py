#!/usr/bin/env python3
"""
Multi-OS to Enterprise SONiC Configuration Migration Tool

This script migrates network configurations from Cisco NX-OS, Arista EOS,
Juniper JunOS (QFX), and Cumulus Linux to Enterprise SONiC configurations.
"""

import sys
import os
import argparse
import io
from typing import Optional, Dict
from base_migrator import BaseMigrator

# Set UTF-8 encoding for stdout/stderr on Windows
if sys.platform == 'win32':
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
    except AttributeError:
        # If buffer doesn't exist, try setting encoding directly
        pass
from cisco_nxos_parser import CiscoNXOSMigrator
from arista_eos_parser import AristaEOSMigrator
from juniper_junos_parser import JuniperJunOSMigrator
from cumulus_linux_parser import CumulusLinuxMigrator
from sonic_config_generator import SonicConfigGenerator


def detect_os(config_content: str) -> Optional[str]:
    """Auto-detect the source OS from configuration content"""
    config_lower = config_content.lower()
    
    # Cisco NX-OS detection
    nxos_indicators = [
        '!command: show running-config',
        'version 9.',
        'version 8.',
        'version 7.',
        'feature ',
        'vpc domain',
        'interface ethernet',
        'interface port-channel'
    ]
    nxos_score = sum(1 for indicator in nxos_indicators if indicator in config_lower)
    
    # Arista EOS detection
    eos_indicators = [
        '! device:',
        'service routing protocols model multi-agent',
        'interface ethernet',
        'interface port-channel',
        'mlag configuration'
    ]
    eos_score = sum(1 for indicator in eos_indicators if indicator in config_lower)
    
    # Juniper JunOS detection
    junos_indicators = [
        'version "',
        'system {',
        'interfaces {',
        'protocols {',
        'xe-0/0/',
        'et-0/0/',
        'ae0',
        'vlans {'
    ]
    junos_score = sum(1 for indicator in junos_indicators if indicator in config_lower)
    
    # Cumulus Linux detection
    cumulus_indicators = [
        'net add',
        'cumulus linux',
        'swp1',
        'swp',
        'bond',
        'clag',
        'bridge vlan-aware',
        'peerlink'
    ]
    cumulus_score = sum(1 for indicator in cumulus_indicators if indicator in config_lower)
    
    # Determine OS based on scores
    if cumulus_score >= 2 and cumulus_score > nxos_score and cumulus_score > eos_score and cumulus_score > junos_score:
        return 'cumulus'
    elif nxos_score >= 2 and nxos_score > eos_score and nxos_score > junos_score:
        return 'cisco'
    elif eos_score >= 2 and eos_score > junos_score:
        return 'arista'
    elif junos_score >= 2:
        return 'juniper'
    
    return None


def get_migrator(os_type: str) -> BaseMigrator:
    """Get the appropriate migrator for the OS type"""
    if os_type == 'cisco':
        return CiscoNXOSMigrator()
    elif os_type == 'arista':
        return AristaEOSMigrator()
    elif os_type == 'juniper':
        return JuniperJunOSMigrator()
    elif os_type == 'cumulus':
        return CumulusLinuxMigrator()
    else:
        raise ValueError(f"Unknown OS type: {os_type}")


def prompt_for_os() -> str:
    """Prompt user to manually select the OS"""
    print("\nUnable to auto-detect source OS. Please select manually:")
    print("1. Cisco NX-OS")
    print("2. Arista EOS")
    print("3. Juniper JunOS (QFX)")
    print("4. Cumulus Linux")
    
    while True:
        choice = input("Enter choice (1-4): ").strip()
        if choice == '1':
            return 'cisco'
        elif choice == '2':
            return 'arista'
        elif choice == '3':
            return 'juniper'
        elif choice == '4':
            return 'cumulus'
        else:
            print("Invalid choice. Please enter 1, 2, 3, or 4.")


def get_user_inputs(migrator: BaseMigrator, os_type: Optional[str] = None) -> Dict[str, str]:
    """Get user inputs for migration"""
    print("=== Multi-OS to Enterprise Advanced SONiC Migration ===")
    print("Please provide the following information for the migration:\n")
    
    inputs = {}
    
    # Get passwords for users
    for username in migrator.users.keys():
        password = input(f"Enter password for user '{username}': ").strip()
        while not password:
            print(f"Password for '{username}' cannot be empty!")
            password = input(f"Enter password for user '{username}': ").strip()
        inputs[f'{username.lower()}_password'] = password
    
    # Ensure admin password
    if 'admin_password' not in inputs:
        admin_password = input("Enter password for user 'admin': ").strip()
        while not admin_password:
            print("Password for 'admin' cannot be empty!")
            admin_password = input("Enter password for user 'admin': ").strip()
        inputs['admin_password'] = admin_password
    
    # Check if MLAG configuration exists and needs management IP
    has_mlag = bool(migrator.mlag_config)
    needs_mgmt_ip = has_mlag and (migrator.management_ip == 'dhcp' or not migrator.management_ip)
    
    if needs_mgmt_ip:
        print(f"\nManagement IP configuration needed for MCLAG source-ip:")
        if migrator.management_ip == 'dhcp':
            print("Current config uses DHCP, but MCLAG requires static IP for source-ip.")
        else:
            print("No management IP found in config, but MCLAG requires source-ip.")
        
        mgmt_ip = input("Enter management interface IP address with prefix (e.g., 192.168.1.10/24): ").strip()
        while not mgmt_ip or '/' not in mgmt_ip:
            print("Please enter a valid IP address with CIDR prefix (e.g., 192.168.1.10/24)")
            mgmt_ip = input("Enter management interface IP address with prefix: ").strip()
        
        inputs['management_ip_cidr'] = mgmt_ip
        
        # Get management gateway
        gateway = input("Enter management interface default gateway: ").strip()
        while not gateway:
            print("Gateway is required for static IP configuration!")
            gateway = input("Enter management interface default gateway: ").strip()
        inputs['management_gateway'] = gateway
    else:
        # Get management gateway for existing static IP (required) - only if not already parsed
        if migrator.management_ip and migrator.management_ip != 'dhcp':
            if migrator.management_gateway:
                # Use existing gateway from config
                inputs['management_gateway'] = migrator.management_gateway
            else:
                # Try to infer gateway from static routes
                inferred_gateway = migrator._find_gateway_from_static_routes(
                    migrator.management_ip,
                    migrator.management_mask
                )
                if inferred_gateway:
                    print(f"\nInferred management gateway from static routes: {inferred_gateway}")
                    inputs['management_gateway'] = inferred_gateway
                else:
                    # Prompt for gateway if none exists in config and couldn't be inferred
                    gateway = input("Enter management interface default gateway: ").strip()
                    while not gateway:
                        print("Gateway is required for static IP configuration!")
                        gateway = input("Enter management interface default gateway: ").strip()
                    inputs['management_gateway'] = gateway
    
    # MCLAG peer-ip: always prompt (VPC/ICP peering in source may not use Management0 / Juniper equivalent)
    if has_mlag:
        default_peer = migrator.mlag_config.get('peer_address', '') or ''
        print("MCLAG/VPC peer IP: Source config's peer-address may be a VLAN SVI or other interface, not Management0.")
        if default_peer:
            prompt = f"Enter MCLAG peer IP (peer's Management0 recommended) [{default_peer}]: "
        else:
            prompt = "Enter MCLAG peer IP (peer's Management0): "
        peer_ip = input(prompt).strip()
        if peer_ip:
            inputs['mclag_peer_ip'] = peer_ip
        elif default_peer:
            inputs['mclag_peer_ip'] = default_peer
        else:
            while not peer_ip:
                peer_ip = input("Enter MCLAG peer IP: ").strip()
            inputs['mclag_peer_ip'] = peer_ip
    
    # Get RADIUS secret key if RADIUS is configured
    # Note: Source config keys are encrypted, so we always need to prompt for the plaintext key
    if migrator.radius_config and migrator.radius_config.host:
        print(f"\nRADIUS server configuration detected for host: {migrator.radius_config.host}")
        print("Note: The RADIUS key in the source configuration is encrypted and cannot be used directly.")
        radius_key = input("Enter RADIUS secret key (plaintext): ").strip()
        while not radius_key:
            print("RADIUS secret key is required for RADIUS authentication!")
            radius_key = input("Enter RADIUS secret key (plaintext): ").strip()
        inputs['radius_key'] = radius_key
    
    # Cumulus-specific prompts (AAA/RADIUS first, then NTP prefer - order is fixed for consistent test input)
    if os_type == 'cumulus':
        # AAA/RADIUS prompt (before NTP so piped test input lines align correctly)
        print("\n" + "=" * 70)
        print("Cumulus Linux AAA/RADIUS authentication is configured via files")
        print("(/etc/pam_radius_auth.conf, /etc/nsswitch.conf) and not via NCLU.")
        print("These configurations cannot be auto-detected from NCLU commands.")
        use_aaa = input("Is AAA/RADIUS authentication used on this Cumulus switch? (y/n): ").strip().lower()
        if use_aaa == 'y':
            radius_host = input("Enter RADIUS server IP address: ").strip()
            while not radius_host:
                print("RADIUS server IP address is required!")
                radius_host = input("Enter RADIUS server IP address: ").strip()
            
            radius_key = input("Enter RADIUS secret key (plaintext): ").strip()
            while not radius_key:
                print("RADIUS secret key is required!")
                radius_key = input("Enter RADIUS secret key (plaintext): ").strip()
            
            if not migrator.radius_config:
                from base_migrator import RadiusConfig
                migrator.radius_config = RadiusConfig()
            migrator.radius_config.host = radius_host
            migrator.radius_config.timeout = 10
            migrator.radius_config.retransmit = 3
            inputs['radius_key'] = radius_key
        
        # NTP prefer flag prompt (if multiple NTP servers)
        ntp_servers = migrator.global_settings.get('ntp_servers', [])
        if len(ntp_servers) > 1:
            print("\n" + "=" * 70)
            print("Cumulus Linux NCLU does not support NTP 'prefer' flag.")
            print("NTP servers found:", ', '.join(ntp_servers))
            prefer_ntp = input("Should one of these NTP servers be preferred? (y/n): ").strip().lower()
            if prefer_ntp == 'y':
                print("\nAvailable NTP servers:")
                for i, server in enumerate(ntp_servers, 1):
                    print(f"  {i}. {server}")
                while True:
                    try:
                        choice = input("Enter server number to prefer (1-{}): ".format(len(ntp_servers))).strip()
                        choice_num = int(choice)
                        if 1 <= choice_num <= len(ntp_servers):
                            inputs['ntp_preferred_server'] = ntp_servers[choice_num - 1]
                            break
                        else:
                            print(f"Please enter a number between 1 and {len(ntp_servers)}")
                    except ValueError:
                        print("Please enter a valid number")
    
    return inputs


def generate_migration_report(migrator: BaseMigrator, input_file: str, output_file: str) -> str:
    """Generate migration report file"""
    report_lines = [
        "=" * 70,
        "Migration Report",
        "=" * 70,
        f"Input file: {input_file}",
        f"Output file: {output_file}",
        f"Hostname: {migrator.hostname}",
        "",
    ]
    
    # Statistics
    stats = {
        'vlans': len(migrator.vlans),
        'interfaces': len(migrator.physical_interfaces),
        'port_channels': len(migrator.port_channels),
        'loopbacks': len(migrator.loopbacks),
        'static_routes': len(migrator.static_routes),
        'bgp_configured': 'asn' in migrator.bgp_config,
        'mlag_configured': bool(migrator.mlag_config),
    }
    
    report_lines.extend([
        "Statistics:",
        "-" * 70,
        f"VLANs: {stats['vlans']}",
        f"Physical Interfaces: {stats['interfaces']}",
        f"Port-Channels: {stats['port_channels']}",
        f"Loopback Interfaces: {stats['loopbacks']}",
        f"Static Routes: {stats['static_routes']}",
        f"BGP Configured: {stats['bgp_configured']}",
        f"MCLAG Configured: {stats['mlag_configured']}",
        "",
    ])
    
    # Unsupported features
    if migrator.has_unsupported_features():
        report_lines.extend([
            migrator.get_unsupported_report(),
            "",
        ])
    
    # Warnings
    if migrator.warnings:
        report_lines.extend([
            "Warnings:",
            "-" * 70,
        ])
        for warning in migrator.warnings:
            context_str = " > ".join(warning.context_stack) if warning.context_stack else "global"
            report_lines.append(f"{context_str}: {warning.message} (line {warning.line_number})")
        report_lines.append("")
    
    # Notes (e.g. MLAG-only features with no non-MLAG equivalent)
    if migrator.report_notes:
        report_lines.extend([
            "Notes:",
            "-" * 70,
        ])
        for note in migrator.report_notes:
            context_str = " > ".join(note.context_stack) if note.context_stack else "global"
            report_lines.append(f"{context_str}: {note.line} (line {note.line_number})")
            report_lines.append(f"  {note.message}")
        report_lines.append("")
    
    # Recommendations
    if migrator.has_unsupported_features():
        report_lines.extend([
            "Recommendations:",
            "-" * 70,
            "Please review the unsupported features listed above and configure them manually",
            "in the SONiC device after applying the generated configuration.",
            "",
        ])
    
    report_lines.append("=" * 70)
    
    return "\n".join(report_lines)


def process_file(input_file_path: str, output_file_path: Optional[str] = None, source_os: Optional[str] = None) -> None:
    """Process a configuration file and generate SONiC configuration"""
    try:
        # Read input configuration
        with open(input_file_path, 'r', encoding='utf-8') as file:
            config_content = file.read()
        
        # Detect or get OS type
        if source_os:
            os_type = source_os.lower()
            if os_type not in ['cisco', 'arista', 'juniper', 'cumulus']:
                print(f"Error: Invalid OS type '{source_os}'. Must be 'cisco', 'arista', 'juniper', or 'cumulus'.")
                sys.exit(1)
        else:
            os_type = detect_os(config_content)
            if not os_type:
                os_type = prompt_for_os()
        
        print(f"Detected/Selected OS: {os_type.capitalize()}")
        
        # Get appropriate migrator
        migrator = get_migrator(os_type)
        
        # Parse configuration
        print("Parsing configuration...")
        migrator.parse_config(config_content)
        
        # Check for unsupported features
        if migrator.has_unsupported_features():
            print("\n" + "=" * 70)
            print("WARNING: Some configurations could not be migrated.")
            print("=" * 70)
            try:
                report = migrator.get_unsupported_report()
                print(report.encode('utf-8', errors='replace').decode('utf-8', errors='replace'))
            except UnicodeEncodeError:
                # Fallback for Windows console encoding issues
                report = migrator.get_unsupported_report()
                print(report.encode('ascii', errors='replace').decode('ascii', errors='replace'))
            print("\n")
            
            response = input("Continue with supported features only? (y/n): ").strip().lower()
            if response != 'y':
                print("Migration cancelled by user.")
                sys.exit(0)
        
        # Get user inputs
        user_inputs = get_user_inputs(migrator, os_type)
        
        # Generate SONiC configuration
        print("Generating SONiC configuration...")
        generator = SonicConfigGenerator(migrator)
        sonic_config = generator.generate_sonic_config(user_inputs)
        
        # Determine output file path
        if not output_file_path:
            base_name = os.path.splitext(input_file_path)[0]
            output_file_path = f"{base_name}_sonic.txt"
        
        # Write output configuration
        with open(output_file_path, 'w', encoding='utf-8') as file:
            file.write(sonic_config)
        
        # Generate migration report
        report_file_path = f"{os.path.splitext(output_file_path)[0]}.report.txt"
        report_content = generate_migration_report(migrator, input_file_path, output_file_path)
        with open(report_file_path, 'w', encoding='utf-8') as file:
            file.write(report_content)
        
        print(f"\n=== Migration Complete ===")
        print(f"Input file: {input_file_path}")
        print(f"Output file: {output_file_path}")
        print(f"Report file: {report_file_path}")
        print(f"Hostname: {migrator.hostname}")
        if migrator.has_unsupported_features():
            print(f"Unsupported features: {len(migrator.unsupported_features)}")
        print("\nEnterprise Advanced SONiC configuration has been generated successfully!")
        
    except FileNotFoundError:
        print(f"Error: Input file '{input_file_path}' not found.")
        sys.exit(1)
    except Exception as e:
        print(f"Error during migration: {str(e)}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


def main():
    """Main function to run the migration script"""
    parser = argparse.ArgumentParser(
        description='Migrate network configurations from Cisco NX-OS, Arista EOS, Juniper JunOS, or Cumulus Linux to Enterprise SONiC'
    )
    parser.add_argument('input_file', help='Input configuration file path')
    parser.add_argument('output_file', nargs='?', help='Output SONiC configuration file path (optional)')
    parser.add_argument('--source-os', choices=['cisco', 'arista', 'juniper', 'cumulus'],
                       help='Manually specify source OS (cisco, arista, juniper, or cumulus)')
    
    args = parser.parse_args()
    
    process_file(args.input_file, args.output_file, args.source_os)


if __name__ == "__main__":
    main()
