#!/usr/bin/env python3
"""
Test script to validate migration against all source configuration files.

Golden-file diff mode (FR-8):
  Default behavior: after running every migration, compare the newly generated
  output in test_outputs/ against the byte-exact snapshot in test_goldens/.
  Any mismatch is a test failure; the first 30 lines of a unified diff are
  printed for each failing file. Use --update-goldens to overwrite the
  goldens with the current outputs (intended for intentional output changes
  reviewed by a human).
"""

import argparse
import difflib
import os
import shutil
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
_TEST_PASSWORD = os.environ.get('SONIC_MIGRATION_TEST_PASSWORD', 'CHANGE_ME_PLACEHOLDER')
_TEST_RADIUS_KEY = os.environ.get('SONIC_MIGRATION_TEST_RADIUS_KEY', 'CHANGE_ME_PLACEHOLDER')
TEST_INPUTS = {
    'cisco': f'y\n{_TEST_PASSWORD}\n{_TEST_PASSWORD}\n{_TEST_PASSWORD}\n192.168.200.254/24\n192.168.200.2\n192.168.200.3\n{_TEST_RADIUS_KEY}\n',
    'arista': f'y\n{_TEST_PASSWORD}\n{_TEST_PASSWORD}\n192.168.200.254/24\n192.168.200.2\n192.168.200.3\n{_TEST_RADIUS_KEY}\n',
    'juniper': f'y\n{_TEST_PASSWORD}\n{_TEST_PASSWORD}\n192.168.200.254/24\n192.168.200.2\n{_TEST_RADIUS_KEY}\n',
    'cumulus': f'y\n{_TEST_PASSWORD}\nn\nn\n{_TEST_RADIUS_KEY}\n',  # No NTP prefer, no AAA
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
        test_input = f'y\n{_TEST_PASSWORD}\n192.168.10.1/24\n192.168.10.254\n192.168.10.2\n{_TEST_RADIUS_KEY}\n'
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
                    # Prompt order: admin, mgmt IP, gateway, [no peer - in config], AAA (y), RADIUS IP (10.3.3.1), RADIUS key, NTP prefer (y), NTP server (1)
                    test_input = f'{_TEST_PASSWORD}\n192.168.10.1/24\n192.168.10.254\ny\n10.3.3.1\n{_TEST_RADIUS_KEY}\ny\n1\n'
                else:
                    # admin pass, mgmt IP, gateway, [no peer - in config], NTP prefer (n), AAA (n)
                    test_input = f'{_TEST_PASSWORD}\n192.168.10.1/24\n192.168.10.254\nn\nn\n'
            else:
                if is_test1:
                    # admin pass, mgmt IP, gateway, [no peer - in config], AAA (y), RADIUS IP (10.3.3.1), RADIUS key
                    test_input = f'{_TEST_PASSWORD}\n192.168.10.1/24\n192.168.10.254\ny\n10.3.3.1\n{_TEST_RADIUS_KEY}\n'
                else:
                    # admin pass, mgmt IP, gateway, [no peer - in config], AAA (n)
                    test_input = f'{_TEST_PASSWORD}\n192.168.10.1/24\n192.168.10.254\nn\n'
        else:
            # No MLAG: admin pass, NTP prefer (n if single, y/1 if multiple), AAA (n), RADIUS IP/key (if AAA=y)
            # Note: No "y" confirmation needed for admin password - it's just the password itself
            if has_multiple_ntp:
                # admin pass, NTP prefer (n), AAA (n)
                test_input = f'{_TEST_PASSWORD}\nn\nn\n'
            else:
                # admin pass, AAA (n)
                test_input = f'{_TEST_PASSWORD}\nn\n'
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

GOLDENS_DIR = 'test_goldens'
OUTPUT_DIR = 'test_outputs'


def _compare_output_to_golden(output_path: str, golden_path: str):
    """Return (is_match: bool, diff_preview: str) for a single output/golden pair.

    Byte-for-byte comparison. When the two differ, diff_preview is the first
    30 lines of a unified diff; when they match, diff_preview is empty.
    """
    try:
        with open(output_path, 'rb') as f:
            out_bytes = f.read()
    except FileNotFoundError:
        return False, f"output missing: {output_path}"
    try:
        with open(golden_path, 'rb') as f:
            gold_bytes = f.read()
    except FileNotFoundError:
        return False, f"golden missing: {golden_path} (run with --update-goldens to create)"

    if out_bytes == gold_bytes:
        return True, ''

    out_lines = out_bytes.decode('utf-8', errors='replace').splitlines(keepends=True)
    gold_lines = gold_bytes.decode('utf-8', errors='replace').splitlines(keepends=True)
    diff = list(difflib.unified_diff(
        gold_lines, out_lines,
        fromfile=golden_path, tofile=output_path, n=3
    ))
    return False, ''.join(diff[:30])


def run_hw_regression_assertions(output_dir: str):
    """Hardware-validated IS-CLI regression assertions.

    HW-3/HW-4: every 'interface range ...' line emitted by the generator
    must use the 'Eth' keyword (not 'Ethernet'). On EAS with
    'interface-naming standard', the range form is 'interface range Eth
    <slot/port-slot/port>'; 'Ethernet' is rejected by the parser and
    poisons the entire paste session.

    HW-7: 'spanning-tree mode' must use one of the EAS-accepted keywords
    (mst | pvst | rapid-pvst). EAS rejects 'rstp' and 'mstp'.

    HW-9: every 'channel-group <N>' line must be preceded (within the
    same interface block) by a matching 'mtu <pc_mtu>' line whenever the
    referenced PortChannel block declared an explicit MTU. EAS rejects
    'channel-group' when member-port MTU does not equal PortChannel MTU.

    HW-10: every 'update-source' line emitted on a BGP neighbor must be
    followed by either the keyword 'interface' (for interface-name
    sources) or an IP literal. A bare interface token is rejected by EAS.

    Returns a list of (file_path, line_number, line_text) violations.
    See hw-validation/HW_VALIDATION_REPORT.md and
    hw-validation/HW_VALIDATION_REPORT_V2.md.
    """
    import re as _re
    violations = []
    bad_range = _re.compile(r'^\s*interface\s+range\s+Ethernet\b', _re.IGNORECASE)
    bad_stp = _re.compile(r'^\s*spanning-tree\s+mode\s+(rstp|mstp)\b', _re.IGNORECASE)
    update_source = _re.compile(r'^\s*update-source\s+(\S+)(?:\s+(\S+))?')
    ip_literal = _re.compile(r'^(?:\d{1,3}(?:\.\d{1,3}){3}|[0-9A-Fa-f:]*:[0-9A-Fa-f:]*)$')
    pc_block_start = _re.compile(r'^\s*interface\s+PortChannel\s+(\S+)', _re.IGNORECASE)
    eth_block_start = _re.compile(r'^\s*interface\s+(?:Eth|Ethernet)\b', _re.IGNORECASE)
    mtu_line = _re.compile(r'^\s*mtu\s+(\d+)\s*$', _re.IGNORECASE)
    cg_line = _re.compile(r'^\s*channel-group\s+(\S+)\s*$', _re.IGNORECASE)
    block_end = _re.compile(r'^\s*exit\s*$', _re.IGNORECASE)

    for fname in sorted(os.listdir(output_dir)):
        if not fname.endswith('_sonic.txt'):
            continue
        fpath = os.path.join(output_dir, fname)
        try:
            with open(fpath, 'r', encoding='utf-8') as f:
                lines = f.readlines()
        except OSError:
            continue

        # Pass 1: collect PortChannel ID -> explicit MTU map (None when no explicit MTU).
        pc_mtu = {}
        in_pc = None
        in_pc_mtu = None
        for ln in lines:
            m = pc_block_start.match(ln)
            if m:
                in_pc = m.group(1)
                in_pc_mtu = None
                continue
            if in_pc is not None:
                if block_end.match(ln):
                    pc_mtu[in_pc] = in_pc_mtu
                    in_pc = None
                    in_pc_mtu = None
                    continue
                mm = mtu_line.match(ln)
                if mm:
                    try:
                        in_pc_mtu = int(mm.group(1))
                    except ValueError:
                        pass

        # Pass 2: per-line and per-block checks.
        in_eth_block = False
        eth_block_mtu = None
        for i, line in enumerate(lines, start=1):
            if bad_range.match(line):
                violations.append((fpath, i, line.rstrip('\n')))
            if bad_stp.match(line):
                violations.append((fpath, i, line.rstrip('\n')))
            us = update_source.match(line)
            if us:
                first = us.group(1)
                # HW-10: first token must be 'interface' OR an IP literal.
                if first.lower() != 'interface' and not ip_literal.match(first):
                    violations.append((fpath, i, line.rstrip('\n')))
            if eth_block_start.match(line):
                in_eth_block = True
                eth_block_mtu = None
                continue
            if in_eth_block:
                if block_end.match(line):
                    in_eth_block = False
                    eth_block_mtu = None
                    continue
                mm = mtu_line.match(line)
                if mm:
                    try:
                        eth_block_mtu = int(mm.group(1))
                    except ValueError:
                        pass
                cg = cg_line.match(line)
                if cg:
                    pc_id = cg.group(1)
                    expected = pc_mtu.get(pc_id)
                    # HW-9: if the parent PortChannel had an explicit MTU,
                    # the member-port block must emit a matching MTU line
                    # before 'channel-group'.
                    if expected is not None and eth_block_mtu != expected:
                        violations.append((fpath, i, line.rstrip('\n')))
    return violations


def run_golden_diff(output_dir: str, goldens_dir: str):
    """Compare every *_sonic.txt and *_sonic.report.txt in output_dir against
    goldens_dir. Returns (matches: int, mismatches: list of tuples)."""
    os.makedirs(goldens_dir, exist_ok=True)
    pairs = []
    for fname in sorted(os.listdir(output_dir)):
        if fname.endswith('_sonic.txt') or fname.endswith('_sonic.report.txt'):
            pairs.append((os.path.join(output_dir, fname), os.path.join(goldens_dir, fname)))
    matches = 0
    mismatches = []
    for out_path, gold_path in pairs:
        ok, diff_preview = _compare_output_to_golden(out_path, gold_path)
        if ok:
            matches += 1
        else:
            mismatches.append((out_path, gold_path, diff_preview))
    return matches, mismatches


def update_goldens(output_dir: str, goldens_dir: str):
    """Copy every output file into goldens_dir, overwriting existing goldens.
    Returns the list of changed file paths."""
    os.makedirs(goldens_dir, exist_ok=True)
    changed = []
    for fname in sorted(os.listdir(output_dir)):
        if not (fname.endswith('_sonic.txt') or fname.endswith('_sonic.report.txt')):
            continue
        out_path = os.path.join(output_dir, fname)
        gold_path = os.path.join(goldens_dir, fname)
        # Only report a file as changed if its bytes differ from the existing golden.
        try:
            with open(out_path, 'rb') as f:
                out_bytes = f.read()
            try:
                with open(gold_path, 'rb') as f:
                    gold_bytes = f.read()
            except FileNotFoundError:
                gold_bytes = None
            if gold_bytes != out_bytes:
                changed.append(gold_path)
        except OSError:
            pass
        shutil.copyfile(out_path, gold_path)
    return changed


def main():
    """Run all migration tests"""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--update-goldens', action='store_true',
                        help='After migrations run, overwrite test_goldens/ with the current '
                             'test_outputs/ files. Use this only when the engineer has '
                             'intentionally changed output and has human-reviewed the diff.')
    args = parser.parse_args()

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

    # Hardware-validated IS-CLI regression checks (HW-3/HW-4 range keyword;
    # HW-7 spanning-tree mode; HW-9 LAG member MTU; HW-10 update-source
    # interface keyword). Runs before the golden diff so a regression is
    # flagged distinctly from a golden-drift.
    print(f"\n{'='*70}")
    print("HW REGRESSION ASSERTIONS (HW-3/HW-4/HW-7/HW-9/HW-10)")
    print(f"{'='*70}")
    hw_violations = run_hw_regression_assertions(OUTPUT_DIR)
    if hw_violations:
        print(f"FAIL: {len(hw_violations)} hardware-syntax violation(s) found:")
        for fpath, lineno, text in hw_violations:
            print(f"  {fpath}:{lineno}: {text}")
        print("HW regression check: FAIL")
        sys.exit(1)
    print("No hardware-syntax violations found.")
    print("HW regression check: PASS")

    # FR-8: golden-file diff. Either overwrite or verify.
    if args.update_goldens:
        print(f"\n{'='*70}")
        print("GOLDEN-FILE UPDATE (--update-goldens)")
        print(f"{'='*70}")
        changed = update_goldens(OUTPUT_DIR, GOLDENS_DIR)
        if changed:
            print(f"Overwrote {len(changed)} golden file(s) with current outputs:")
            for path in changed:
                print(f"  {path}")
        else:
            print("No golden files needed to be changed (outputs already match).")
        sys.exit(0)

    print(f"\n{'='*70}")
    print("GOLDEN-FILE DIFF")
    print(f"{'='*70}")
    matches, mismatches = run_golden_diff(OUTPUT_DIR, GOLDENS_DIR)
    print(f"Matches: {matches}")
    print(f"Mismatches: {len(mismatches)}")

    if mismatches:
        print(f"\n{'='*70}")
        print("GOLDEN-FILE MISMATCHES")
        print(f"{'='*70}")
        for out_path, gold_path, diff_preview in mismatches:
            print(f"\n--- mismatch: {os.path.basename(out_path)} ---")
            if diff_preview:
                print(diff_preview)
            else:
                print('(no diff preview available)')
        print(f"\n{len(mismatches)} golden-file mismatch(es) detected.")
        print("Either fix the code, or re-run with --update-goldens after human review.")
        print("Golden diff check: FAIL")
        sys.exit(1)

    print("Golden diff check: PASS")
    # Return exit code based on migration results.
    sys.exit(0 if failed_tests == 0 else 1)

if __name__ == '__main__':
    main()
