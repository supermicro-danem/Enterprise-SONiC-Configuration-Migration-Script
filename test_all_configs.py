#!/usr/bin/env python3
"""
Test script to validate migration against all source configuration files
"""

import os
import subprocess
import sys
from pathlib import Path

# Test configurations - all source configs in test_configs/ (exclude outputs, README)
# Cisco: sample, test1-4, leaf.cfg | Arista: sample, test1-3, leaf.cfg | Juniper: sample, test1-3, leaf.cfg | Cumulus: test1-3
TEST_CONFIGS = {
    'cisco': [
        'test_configs/cisco_nxos_sample.txt',
        'test_configs/cisco_nxos_test1.txt',
        'test_configs/cisco_nxos_test2.txt',
        'test_configs/cisco_nxos_test3.txt',
        'test_configs/cisco_nxos_test4.txt',
    ],
    'arista': [
        'test_configs/arista_eos_sample.txt',
        'test_configs/arista_eos_test1.txt',
        'test_configs/arista_eos_test2.txt',
        'test_configs/arista_eos_test3.txt',
    ],
    'juniper': [
        'test_configs/juniper_qfx_sample.txt',
        'test_configs/juniper_qfx_test1.txt',
        'test_configs/juniper_qfx_test2.txt',
        'test_configs/juniper_qfx_test3.txt',
    ],
    'cumulus': [
        'test_configs/cumulus_nclu_test1.txt',
        'test_configs/cumulus_nclu_test2.txt',
        'test_configs/cumulus_nclu_test3.txt',
    ]
}

# Standard test inputs (passwords, etc.)
# Arista/Cisco: include MCLAG peer IP (192.168.200.3) when config has MLAG so prompt has enough input
# Cisco: use 3 passwords so configs with 3 users (e.g. admin, operator, guest) have enough lines
TEST_INPUTS = {
    'cisco': 'y\ntestpass123\ntestpass123\ntestpass123\n192.168.200.254/24\n192.168.200.2\n192.168.200.3\ntestkey123\n',
    'arista': 'y\ntestpass123\ntestpass123\n192.168.200.254/24\n192.168.200.2\n192.168.200.3\ntestkey123\n',
    'juniper': 'y\ntestpass123\ntestpass123\n192.168.200.254/24\n192.168.200.2\ntestkey123\n',
    'cumulus': 'y\ntestpass123\nn\nn\ntestkey123\n',  # No NTP prefer, no AAA
}

def run_migration_test(config_file, os_type, output_dir='test_outputs'):
    """Run migration test for a single configuration file"""
    print(f"\n{'='*70}")
    print(f"Testing: {os.path.basename(config_file)} ({os_type.upper()})")
    print(f"{'='*70}")
    
    # Create output directory if it doesn't exist
    os.makedirs(output_dir, exist_ok=True)
    
    # Generate output filenames
    base_name = os.path.splitext(os.path.basename(config_file))[0]
    output_file = os.path.join(output_dir, f'{base_name}_sonic.txt')
    report_file = os.path.join(output_dir, f'{base_name}_sonic.report.txt')
    
    # Prepare command
    cmd = [
        sys.executable,
        'multi_os_to_sonic_migrator.py',
        config_file,
        output_file,
        '--source-os', os_type
    ]
    
    # Get test inputs - provide enough inputs for all possible prompts
    # Format: OS selection, admin password (always needed), 
    #         management IP (if needed), gateway (if needed), 
    #         MCLAG peer IP (if MLAG exists), RADIUS key (if RADIUS exists)
    # We provide extra inputs to handle variable scenarios
    base_input = TEST_INPUTS.get(os_type, TEST_INPUTS['cisco'])
    
    # For Juniper, we need to handle cases with no users, MLAG, and RADIUS
    # Provide enough inputs: admin pass, mgmt IP (with CIDR), gateway, MCLAG peer, RADIUS key
    if os_type == 'juniper':
        test_input = 'y\ntestpass123\n192.168.10.1/24\n192.168.10.254\n192.168.10.2\ntestkey123\n'
    elif os_type == 'cumulus':
        # Cumulus needs: admin pass, NTP prefer (n if single, y/1 if multiple), AAA (n), RADIUS IP/key (if AAA=y)
        # For test1: has MLAG, needs mgmt IP, gateway, peer IP
        # For test2 and test3: no MLAG
        # Check if config has MLAG by looking for clag
        try:
            with open(config_file, 'r', encoding='utf-8') as f:
                config_content = f.read()
                has_clag = 'clag' in config_content.lower()
                has_multiple_ntp = config_content.count('net add time ntp server') > 1
        except:
            has_clag = False
            has_multiple_ntp = False
        
        if has_clag:
            # Has MLAG: admin pass, mgmt IP, gateway, peer IP, NTP prefer (n if single, y/1 if multiple), AAA (y for test1, n for others), RADIUS IP/key (if AAA=y)
            # For test1: has RADIUS documented in comments, so use "y" for AAA
            is_test1 = 'test1' in config_file
            if has_multiple_ntp:
                if is_test1:
                    # Prompt order: admin, mgmt IP, gateway, [no peer - in config], AAA (y), RADIUS IP (10.3.3.1), RADIUS key (radiuskey123), NTP prefer (y), NTP server (1)
                    test_input = 'testpass123\n192.168.10.1/24\n192.168.10.254\ny\n10.3.3.1\nradiuskey123\ny\n1\n'
                else:
                    # admin pass, mgmt IP, gateway, [no peer - in config], NTP prefer (n), AAA (n)
                    test_input = 'testpass123\n192.168.10.1/24\n192.168.10.254\nn\nn\n'
            else:
                if is_test1:
                    # admin pass, mgmt IP, gateway, [no peer - in config], AAA (y), RADIUS IP (10.3.3.1), RADIUS key (radiuskey123)
                    test_input = 'testpass123\n192.168.10.1/24\n192.168.10.254\ny\n10.3.3.1\nradiuskey123\n'
                else:
                    # admin pass, mgmt IP, gateway, [no peer - in config], AAA (n)
                    test_input = 'testpass123\n192.168.10.1/24\n192.168.10.254\nn\n'
        else:
            # No MLAG: admin pass, NTP prefer (n if single, y/1 if multiple), AAA (n), RADIUS IP/key (if AAA=y)
            # Note: No "y" confirmation needed for admin password - it's just the password itself
            if has_multiple_ntp:
                # admin pass, NTP prefer (n), AAA (n)
                test_input = 'testpass123\nn\nn\n'
            else:
                # admin pass, AAA (n)
                test_input = 'testpass123\nn\n'
    else:
        test_input = base_input
    
    # Run migration
    try:
        process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding='utf-8'
        )
        
        stdout, stderr = process.communicate(input=test_input, timeout=60)
        
        # Check results
        success = process.returncode == 0
        output_exists = os.path.exists(output_file)
        report_exists = os.path.exists(report_file)
        
        result = {
            'config_file': config_file,
            'os_type': os_type,
            'success': success,
            'return_code': process.returncode,
            'output_file': output_file,
            'output_exists': output_exists,
            'report_file': report_file,
            'report_exists': report_exists,
            'stdout': stdout,
            'stderr': stderr,
            'error': None
        }
        
        # Check for errors in output
        if not success:
            result['error'] = f"Process returned code {process.returncode}"
            if stderr:
                result['error'] += f": {stderr[:200]}"
        elif not output_exists:
            result['error'] = "Output file was not created"
        elif not report_exists:
            result['error'] = "Report file was not created"
        
        return result
        
    except subprocess.TimeoutExpired:
        process.kill()
        return {
            'config_file': config_file,
            'os_type': os_type,
            'success': False,
            'error': 'Process timed out after 60 seconds'
        }
    except Exception as e:
        return {
            'config_file': config_file,
            'os_type': os_type,
            'success': False,
            'error': f"Exception: {str(e)}"
        }

def main():
    """Run all migration tests"""
    print("="*70)
    print("Multi-OS to Enterprise SONiC Migration - Full Test Suite")
    print("="*70)
    total_configs = sum(len(configs) for configs in TEST_CONFIGS.values())
    print(f"\nTotal configurations to test: {total_configs}")
    print(f"  - Cisco NX-OS: {len(TEST_CONFIGS['cisco'])}")
    print(f"  - Arista EOS: {len(TEST_CONFIGS['arista'])}")
    print(f"  - Juniper JunOS: {len(TEST_CONFIGS['juniper'])}")
    print(f"  - Cumulus Linux: {len(TEST_CONFIGS['cumulus'])}")
    
    results = []
    total_tests = 0
    passed_tests = 0
    failed_tests = 0
    
    # Test all configurations
    for os_type, configs in TEST_CONFIGS.items():
        print(f"\n{'='*70}")
        print(f"Testing {os_type.upper()} configurations ({len(configs)} files)")
        print(f"{'='*70}")
        
        for config_file in configs:
            if not os.path.exists(config_file):
                print(f"WARNING: Config file not found: {config_file}")
                results.append({
                    'config_file': config_file,
                    'os_type': os_type,
                    'success': False,
                    'error': 'Config file not found'
                })
                failed_tests += 1
                continue
            
            total_tests += 1
            result = run_migration_test(config_file, os_type)
            results.append(result)
            
            if result.get('success') and not result.get('error'):
                passed_tests += 1
                print(f"[PASS] {os.path.basename(config_file)}")
            else:
                failed_tests += 1
                print(f"[FAIL] {os.path.basename(config_file)}")
                if result.get('error'):
                    print(f"  Error: {result['error']}")
    
    # Print summary
    print(f"\n{'='*70}")
    print("TEST SUMMARY")
    print(f"{'='*70}")
    print(f"Total tests: {total_tests}")
    print(f"Passed: {passed_tests}")
    print(f"Failed: {failed_tests}")
    print(f"Success rate: {(passed_tests/total_tests*100):.1f}%" if total_tests > 0 else "N/A")
    
    # Print failed tests details
    if failed_tests > 0:
        print(f"\n{'='*70}")
        print("FAILED TESTS DETAILS")
        print(f"{'='*70}")
        for result in results:
            if not result.get('success') or result.get('error'):
                print(f"\n{result['os_type'].upper()}: {os.path.basename(result['config_file'])}")
                if result.get('error'):
                    print(f"  Error: {result['error']}")
                if result.get('stderr'):
                    stderr_preview = result['stderr'][:300]
                    if len(result['stderr']) > 300:
                        stderr_preview += "..."
                    print(f"  Stderr: {stderr_preview}")
    
    # Return exit code based on results
    sys.exit(0 if failed_tests == 0 else 1)

if __name__ == '__main__':
    main()
