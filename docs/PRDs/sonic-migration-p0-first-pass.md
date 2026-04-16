# PRD: Enterprise SONiC Configuration Migration Script - P0 First Pass

**Author:** Dane McMichael
**Status:** Draft - Pending Review
**Date:** 2026-04-16
**Version:** 1.1

---

## 1. Problem Statement

The Enterprise SONiC Configuration Migration Script (`supermicro-danem/Enterprise-SONiC-Configuration-Migration-Script`, HEAD `aff44e9`) ingests vendor switch configurations (Cisco NX-OS, Arista EOS, Juniper JunOS, Cumulus Linux NCLU) and emits best-effort Enterprise Advanced SONiC IS-CLI configuration plus a migration report. A full code audit (see "Enterprise SONiC Configuration Migration Script - Code Analysis", dated 2026-04-16) identified six P0 defects in the generator and per-parser logic that produce invalid IS-CLI at apply time, plus a testing gap where no output is ever validated against a known-good baseline.

Representative failures in the current sample output (`test_outputs/cisco_nxos_sample_sonic.txt`):
- Line 132: `interface ethernet 1/6-10` - source-vendor range syntax leaked into SONiC output; command will not apply.
- Line 171: `redistribute direct route-map DIRECT-TO-BGP` - FRR/SONiC uses the `connected` keyword, not `direct`.
- Header block (lines 1-16): mid-paste `write memory` issued from inside `configure terminal` and a re-entry into `sonic-cli` that kicks the user back to the Linux shell mid-session.
- Port-channel range emitted in addition to individual PortChannel declarations (duplicate configuration).
- Migration report lists every interface/VLAN/port-channel header as "unsupported" despite successful translation, destroying report usefulness.

Downstream impact: FAEs cannot ship the output from this tool to a customer without manual post-edit. The gap is also silent: the existing test harness (`test_all_configs.py`) only checks that the process exits 0 and files are created; it never inspects the emitted text, so a future regression that produces syntactically invalid output still passes CI.

This PRD scopes the minimum set of fixes required to ship a version of the generator whose sample output applies cleanly to an SSE-T8196 running Enterprise Advanced SONiC, plus a golden-file regression baseline so subsequent refactors (out of scope here) cannot silently regress behavior.

## 2. Solution

Correct six specific defects in `sonic_config_generator.py` and the Cisco/Arista parsers so that every line the generator emits is canonical Enterprise Advanced SONiC IS-CLI syntax validated against the 4.5.0 user guide and confirmed by applying the sample outputs to an SSE-T8196. Concurrently, capture a byte-exact snapshot of all 16 sample outputs as golden files checked into the repo, and extend `test_all_configs.py` with a diff mode that fails the run on any unexpected deviation from the golden. After this work, the sample outputs are reviewer-approved canonical configurations, and any future change that alters them requires intentional golden-file regeneration as part of a reviewed PR.

## 3. Non-Functional Requirements

**Architecture:** Python 3.7+ CLI utility, no backend, no persistence, no new external dependencies. This matches the existing tool architecture; no change is justified for this scope.

**Rationale for keeping the existing architecture:**
- The tool is one-shot: a user runs it on a laptop, pastes the output into a switch, and discards it. No need for a server, database, or daemon.
- The existing codebase is self-contained (standard library only, per `README.md`). Introducing new dependencies for a P0 fix expands the change surface without a matching requirement.
- P1 architectural cleanups (CiscoLikeParser extraction, type hints, `logging` module migration) are explicitly out of scope per the first-pass directive.

**Platform constraints:**
- Must continue to run on Windows and Linux without code changes (the existing UTF-8 stdio reconfig in `multi_os_to_sonic_migrator.py:17-23` stays as-is for this pass - it is P1).
- Output must be Enterprise Advanced SONiC IS-CLI, copy-paste friendly into a `sonic#` or `sonic(config)#` session.
- No change to the interactive prompt flow or CLI arguments.

**Deployment model:** Unchanged - `git clone` + `python3 multi_os_to_sonic_migrator.py ...`.

**Test execution model:** Unchanged - `python3 test_all_configs.py` runs locally. A new invocation mode (`--update-goldens`) regenerates the snapshot files. CI is explicitly out of scope.

## 4. Functional Requirements

All P1 tier requirements below are independently testable. Priority tier for this PRD is P0-only; P1 and P2 rows in the source analysis are deferred.

### 4.1 P0 fixes (must ship)

**FR-1 (maps to analysis P0-1): Eliminate false-positive "unsupported feature" entries in the migration report.**
In `cisco_nxos_parser.py` and `arista_eos_parser.py`, every section-header line (interface, vlan, port-channel) currently falls through to the catch-all `log_unsupported_feature()` call after the FSM has already consumed it. The fix: in each parser's main line loop, after a successful state transition or header consumption, `continue` to the next line so the catch-all never sees the consumed line.
- **Acceptance:** the regenerated `cisco_nxos_sample_sonic.report.txt` and the equivalent Arista report contain zero entries for lines that were successfully translated to an `interface ...`, `vlan ...`, or PortChannel declaration in the corresponding `_sonic.txt`.
- **Testability:** diff the pre-fix and post-fix `.report.txt` line counts; the post-fix count must drop by at least 100 lines for the Cisco sample.

**FR-2 (maps to analysis P0-2): Emit a single, atomic enter-configure-write-exit sequence in the output header, with a prescribed two-block split for DCBX paths.**
Replace the current header block in `SonicConfigGenerator.generate_sonic_config()` (lines ~38-85 of `sonic_config_generator.py`) with a single-entry sequence: `sonic-cli` -> `configure terminal` -> (configuration body) -> `end` -> `write memory`. The current double-entry (`sonic-cli` -> `configure terminal` -> `write memory` inside config mode -> `exit` -> `sonic-cli` again) must be removed.

For the DCBX path the generator MUST emit exactly two IS-CLI blocks separated by a clearly-delimited marker. The first block is the full pre-reboot configuration ending with `end` and `write memory`. The marker `! --- PASTE AFTER REBOOT ---` appears exactly once between the blocks. The second block contains only the `buffer init lossless` command plus any post-reboot DCBX commands required by the configuration, wrapped in its own `sonic-cli` -> `configure terminal` -> body -> `end` -> `write memory` envelope. No re-entry into `sonic-cli` appears between the first block's `write memory` and the marker.

- **Canonical form, non-DCBX** (userguide citation: `Bundle_Getting_Started.md` lines 91, 113-123, 179-187):
  ```
  sonic-cli
  configure terminal
  ! ... configuration body ...
  end
  write memory
  ```
  `write memory` is only valid in EXEC mode (`sonic#`), not config mode.
- **Canonical form, DCBX-present:**
  ```
  sonic-cli
  configure terminal
  ! ... pre-reboot configuration body ...
  end
  write memory
  ! --- PASTE AFTER REBOOT ---
  sonic-cli
  configure terminal
  buffer init lossless
  ! ... any additional post-reboot DCBX commands ...
  end
  write memory
  ```
- **Acceptance:**
  - In non-DCBX sample outputs: the string `sonic-cli` appears exactly once; `write memory` appears exactly once (at the end, after `end`); no `write memory` appears between `configure terminal` and `end`; the `! --- PASTE AFTER REBOOT ---` marker appears zero times.
  - In DCBX-present sample outputs: the `! --- PASTE AFTER REBOOT ---` marker appears exactly once; `sonic-cli` appears exactly twice (once per block); `write memory` appears exactly twice (once at the end of each block); the `buffer init lossless` line appears only in the second block, after the marker.
- **Testability:** grep counts on the golden files for `sonic-cli`, `write memory`, `buffer init lossless`, and the literal marker string `! --- PASTE AFTER REBOOT ---`.

**FR-3 (maps to analysis P0-3): Emit `ip vrf mgmt` before any management interface block.**
Confirmed by Dane McMichael on 2026-04-16: `mgmt` is NOT a built-in VRF on Enterprise Advanced SONiC. Only `default` is built in. `ip vrf mgmt` MUST be emitted to create the management VRF before any `interface Management 0` block references it. The global CLAUDE.md Section 2 has been updated to reflect this. This resolves the prior userguide-vs-CLAUDE.md conflict: the userguide was correct.
- **Disposition:** RESOLVED. Non-negotiable.
- **Canonical form:** the generator emits `ip vrf mgmt` exactly once per output, positioned before any `interface Management 0` block. The line is omitted if (and only if) no management interface is configured in the source configuration.
  ```
  sonic(config)# ip vrf mgmt
  sonic(config)# interface Management 0
  sonic(config-if-Management0)#  ip address 10.0.0.1/24
  ```
- **Acceptance:**
  - The generator emits `ip vrf mgmt` exactly once per output when a management interface is configured; zero times when no management interface is configured.
  - The `ip vrf mgmt` line precedes the first `interface Management 0` block in emission order.
- **Testability:** grep count of `^ip vrf mgmt$` (or the exact emitted form) in each golden file; must equal 1 for management-interface-present samples and 0 otherwise.
- **Hardware validation in Section 7 is confirmatory, not deciding:** validate that `ip vrf mgmt` applies without error on SSE-T8196 at 172.18.0.110. Any hardware anomaly is escalated to Dane; it does not alter the emission rule defined here.

**FR-4 (maps to analysis P0-4): Map Cisco/Arista `redistribute direct` to EAS `redistribute connected`.**
The generator at `sonic_config_generator.py:737` emits each redistribute entry verbatim from the parser's `bgp_config['redistribute']` list. Cisco NX-OS and Arista EOS source configs use `redistribute direct` to pull in connected-interface routes; EAS FRR uses `redistribute connected`. Fix in the generator (not the parser): when emitting a redistribute line whose first token is `direct`, substitute `connected`. Preserve the route-map suffix untouched.
- **Canonical form** (userguide citation: `Bundle_Layer3_Features.md` line 1410: `sonic(config-router-bgp-af)# redistribute connected`):
  ```
   address-family ipv4 unicast
    redistribute connected route-map DIRECT-TO-BGP
   exit
  ```
- **Acceptance:** the generator never emits `redistribute direct` in any sample output. Every occurrence of `direct` following `redistribute` in the current goldens is replaced with `connected`. The route-map suffix is preserved.
- **Testability:** grep `redistribute direct` across all 16 new golden files - expected count: 0.

**FR-5 (maps to analysis P0-5): Emit either a range form OR individual PortChannel declarations per a prescriptive rule; never both.**
The generator's port-channel section currently emits individual `interface PortChannel N` blocks for each PortChannel, and when a PortChannel name contains a `-` character (range marker from the parser), it additionally emits an `interface PortChannel M-N` range block. Result: PortChannels 30-35 appear six times in the sample output.

**Prescribed rule:** the generator MUST use the range form `interface PortChannel M-N` if and only if the range covers three or more consecutive PortChannels AND all PortChannels in the range share identical sub-command settings (same member ports count, same mode, same VLAN membership, same MTU, same description-prefix logic, etc.). Otherwise the generator MUST emit individual `interface PortChannel N` blocks, one per PortChannel, and MUST NOT emit any range line. The two forms are mutually exclusive per PortChannel: a given PortChannel number appears in exactly one declaration across the output.

- **Canonical form, range (>= 3 consecutive PortChannels with identical settings):**
  ```
  interface PortChannel 30-35
   mtu 9216
   switchport mode trunk
   switchport trunk allowed Vlan 100,200
  exit
  ```
- **Canonical form, individual (< 3 PortChannels, or any setting differs):**
  ```
  interface PortChannel 30
   mtu 9216
  exit
  interface PortChannel 31
   mtu 1500
  exit
  ```
- **Acceptance:** across the 16 golden files, for every PortChannel number, exactly one `interface PortChannel N` or one range block covering N exists. Duplicate declarations (same PortChannel number emitted under both an individual block and a range block) are a test failure. When the prescribed rule selects range form, no individual blocks for PortChannels in that range appear in the output.
- **Testability:** a Python check on each golden file that extracts all PortChannel numbers covered by any `interface PortChannel ...` line and verifies no number appears twice; and that range blocks only appear when the rule (>= 3 consecutive, identical settings) is satisfied.

**FR-6 (maps to analysis P0-6): Replace source-vendor range syntax (`interface ethernet 1/6-10`) with canonical EAS range syntax `interface range Eth 1/6-1/10`.**
The Cisco parser's range handling stores range specs in `range_configs` with the source-form key (e.g., `ethernet 1/6-10`); the generator's `_get_sonic_range_name()` helper attempts to convert this but the conversion fails for the bare `ethernet N/M-K` form and emits the source text literally.

**Prescribed form:** the generator MUST emit `interface range Eth 1/6-1/10` (with the explicit slot on both endpoints, per userguide `Bundle_Interfaces.md` line 453). Per-port expansion is NOT an implementation option. Per-port expansion is a fallback reserved for the case where hardware validation on SSE-T8196 shows `interface range Eth` fails for a specific sample; in that event, the engineer MUST escalate to Dane McMichael before switching to per-port expansion. The engineer does not choose the fallback unilaterally.

- **Canonical form** (userguide citation: `Bundle_Interfaces.md` lines 453, 463):
  ```
  interface range Eth 1/6-1/10
    mtu 1500
    speed 1000
    description "Server-Port-Range"
    switchport access vlan 100
    no shutdown
  exit
  ```
- **Acceptance:**
  - The string `interface ethernet ` (lowercase, space before slot) does not appear in any golden file.
  - Every range declaration in the goldens starts with `interface range Eth ` and uses the explicit slot-on-both-endpoints form (`Eth 1/6-1/10`, not `Eth 1/6-10`).
  - Hardware validation confirms `interface range Eth` applies without error on SSE-T8196. If it fails, the engineer escalates to Dane rather than silently switching forms.
- **Testability:** grep `^interface ethernet ` across all 16 golden files - expected count: 0. Grep `^interface range Eth ` appears for every range block; every match is followed by a `N/M-N/K` slot-explicit range spec.

### 4.2 Golden-file regression baseline

**FR-7: Snapshot all 16 sample outputs as golden files.**
After the P0 fixes are applied and reviewed, regenerate every sample output in `test_outputs/` by running `test_all_configs.py`. Move (or copy) each `*_sonic.txt` and `*_sonic.report.txt` into a new `test_goldens/` directory. These become the canonical reference outputs. Subsequent runs of the test harness diff against these goldens and fail on any unexpected change.
- **Acceptance:** `test_goldens/` contains exactly 32 files (16 `_sonic.txt` + 16 `_sonic.report.txt`). Every file is a byte-exact copy of the corresponding `test_outputs/` file generated immediately after FR-1 through FR-6 are merged.
- **Testability:** file count check + `cmp` byte comparison for each pair.

**FR-8: Extend `test_all_configs.py` with golden-file diff mode.**
Add a default-on diff step after each successful migration run: the newly generated `test_outputs/<name>_sonic.txt` and `*.report.txt` are compared against the corresponding `test_goldens/<name>_sonic.txt` and `*.report.txt`. Any byte-level difference is a test failure; the test output must show the unified diff (first 30 lines) for each failing case. Add an opt-in `--update-goldens` flag that, when set, copies the newly generated outputs over the existing goldens (used only when the engineer has intentionally changed output and has human-reviewed the diff).
- **Acceptance criteria:**
 - `python3 test_all_configs.py` (no flag): passes when every output matches its golden byte-for-byte; fails with unified diff on any mismatch.
 - `python3 test_all_configs.py --update-goldens`: overwrites golden files with current outputs; prints a list of changed files; exits 0 even on changes.
 - Test harness return code: 0 if all goldens match; 1 if any mismatch in default mode.
- **Testability:** introduce a deliberate one-character change to a golden, rerun the test harness, confirm it fails with a visible diff. Revert the change, rerun, confirm it passes.

**FR-9: Document the golden-file workflow in the repo README.**
Add a "Testing" section to `README.md` describing: how to run the tests, what a clean run looks like, what a golden-file diff failure means, and how to intentionally regenerate goldens after an approved output change. Text only - no new diagrams or deployment instructions.
- **Acceptance:** the new README section names `test_goldens/`, the `--update-goldens` flag, and states explicitly that unexpected diffs at PR-review time must be resolved by either (a) fixing the code or (b) obtaining a reviewer sign-off before regenerating goldens.

## 5. Data Model

No new data model. Existing `BaseMigrator` dataclasses (`VlanConfig`, `PortChannelConfig`, `PhysicalInterfaceConfig`, `LoopbackConfig`, `StaticRouteConfig`, etc.) remain unchanged. No schema field additions or removals. One new filesystem artifact (`test_goldens/` directory) is introduced; it is byte-exact mirror of `test_outputs/` at the P0-fix milestone and has no internal schema.

## 6. Integration Points

- **Enterprise Advanced SONiC 4.5.0 user guide bundles** (`~/projects/sse-portfolio/docs/strategy/enterprise-advanced-sonic-userguide/`): source of truth for canonical IS-CLI syntax on P0-2, P0-4, P0-5, P0-6. Cited inline per fix.
- **CLAUDE.md Section 2** (global): IS-CLI prompt prefix (`config-` not `conf-`), VRF naming convention, interface naming modes. CLAUDE.md Section 2 was updated 2026-04-16 to reflect that only `default` is a built-in VRF; `mgmt` must be created. This resolves the prior P0-3 userguide-vs-CLAUDE.md conflict; see FR-3.
- **SSE-T8196 at 172.18.0.110** (admin:supermicro): hardware target for validation phase. See Section 7.
- **Existing `test_all_configs.py`**: must continue to pass unchanged after the P0 fixes. Golden-file diff extension is additive.

## 7. Hardware Validation Plan

**Target:** SSE-T8196 at 172.18.0.110, credentials admin:supermicro. Running Enterprise Advanced SONiC (version documented at validation time in the PR).

**Sequence:**

1. Engineer regenerates all 16 sample outputs after FR-1 through FR-6 are implemented.
2. For each of the four source NOS families (Cisco, Arista, Juniper, Cumulus), engineer picks the `*_sample_sonic.txt` output (not test1/test2/test3 variants - sample is representative). Four target configurations.
3. For each target configuration:
    - SSH to SSE-T8196 as admin.
    - Capture `show running-configuration | no-more` and save as `<vendor>_pre.txt` in the PR artifact set.
    - Enter `sonic-cli`, then `configure terminal`.
    - **Pre-validate ambiguous lines with the `?` technique before pasting.** When in doubt about a specific keyword or syntax form (particularly for FR-3 `ip vrf mgmt`, FR-5 range forms, and FR-6 `interface range Eth`), type the partial command followed by `?` — the CLI parser will list valid continuations or reject the token without saving anything to running-config. No Enter is pressed, so nothing is committed. Use this to confirm canonical form on the live switch before the actual paste.
    - Paste the generator output in chunks (avoid terminal buffer overflow; 50 lines at a time is safe).
    - Capture every CLI response, including errors, warnings, and `%Error` messages. Any error aborts the validation for that configuration; engineer records the failing command and the switch's response verbatim.
    - After a clean paste, exit to EXEC mode (`end`), run `write memory`, capture the output.
    - Capture `show running-configuration | no-more` again as `<vendor>_post.txt`.
    - Produce a unified diff `<vendor>_pre.txt` -> `<vendor>_post.txt` showing the intended configuration state took effect.
    - Restore the switch to pre-test state (`copy <vendor>_pre.txt running-configuration overwrite` or equivalent) before moving to the next vendor test.
4. Attach the following to the PR as evidence:
    - Four `<vendor>_post.txt` diffs showing the migration's intended state reached the switch.
    - A summary table: vendor, lines emitted, errors encountered (should be zero), final running-config block for the migrated section (e.g., the new VLAN interfaces and BGP neighbors).
    - Confirmation that `ip vrf mgmt` (FR-3) applied without error on SSE-T8196, with the running-config evidence showing the `mgmt` VRF was created by the line. Any anomaly is escalated to Dane; the emission rule is not altered on engineer authority.

**Acceptance for validation phase:**
- Zero `%Error` messages during paste on all four sample outputs.
- Post-paste `show running-configuration` contains every intended IS-CLI element in the generator output (modulo EAS-canonical normalization - e.g., `vlan 100` entered may render as `Vlan100` in running config; this is expected).
- `write memory` completes without error and a reboot or `copy running-config startup-config` round-trip preserves the applied state.

## 8. Open Items

| # | Item | Proposed Resolution |
|---|------|---------------------|
| O2 | FR-5 (PortChannel dedup): when to prefer range vs individual declarations? | RESOLVED in FR-5: range form is prescribed if and only if the range covers 3 or more consecutive PortChannels AND all PortChannels in the range share identical sub-command settings; individual form otherwise. Not an engineer choice. |
| O3 | FR-6 range syntax: `interface range Eth` or per-port expansion? | RESOLVED in FR-6: `interface range Eth 1/6-1/10` is prescribed per userguide. Per-port expansion is NOT an implementation option; it is a hardware-failure fallback that the engineer MUST escalate to Dane before adopting. |
| O4 | DCBX path in FR-2: does the reboot-mid-paste flow need a documented user-facing comment, or should DCBX entirely be emitted as a separate "phase 2" file? | RESOLVED in FR-2: emit two IS-CLI blocks separated by exactly one `! --- PASTE AFTER REBOOT ---` marker. Phase 2 file is out of scope for this PRD. |
| O5 | `update-source loopback0` vs `update-source Loopback0` (analysis item C9). | Out of scope for this PRD (not a P0). Will be addressed in the P1 pass. |
| O6 | Config-mode prompt prefix: userguide examples use both `config-router-bgp` and `conf-router-bgp`. CLAUDE.md mandates `config-`. | Script does not emit prompts (only commands). No action required; flag applies only to documentation. |

Note: O1 (`ip vrf mgmt` disposition) was removed in v1.1. Dane confirmed on 2026-04-16 that `mgmt` is not a built-in VRF on Enterprise Advanced SONiC; the line must be emitted. See FR-3.

## 9. Agent Workflow

Once this PRD is approved by Dane:

1. **Engineer** (sub-agent, branch `agent/engineer/sonic-migration-p0-first-pass`): implements FR-1 through FR-9 per the acceptance criteria above. Produces updated code, regenerated goldens, and extended `test_all_configs.py`. Opens a PR on the engineer branch targeting `main`.
2. **QA Reviewer** (sub-agent): adversarial review against every FR's acceptance criteria. Must score confidence 95 or above per CLAUDE.md Section 6 orchestrator merge gate. Below 95 routes back to Engineer.
3. **Security Reviewer** (sub-agent): dedicated security audit of any input-handling or subprocess-dispatch code paths touched by the fix. Returns a risk score (0-100).
4. **Hardware validation** (Dane or Engineer-led session): executes the plan in Section 7 on SSE-T8196. Attaches evidence to the PR.
5. **Tech Writer** (sub-agent, optional): only if the README update from FR-9 needs substantive rewrite. A minimal section addition can stay in the Engineer's PR without Tech Writer dispatch.
6. **Orchestrator merge:** gated on QA >=95, Security-reviewer sign-off, hardware validation evidence attached, and Dane's explicit merge approval per CLAUDE.md Section 5.

## 10. Success Criteria

Binary pass/fail for the whole PRD:

- [ ] **SC-1:** All six P0 defects (P0-1 through P0-6) have a code change that addresses them per the acceptance criteria in Section 4.1.
- [ ] **SC-2:** `python3 test_all_configs.py` exits 0 against the new goldens with no diffs. Intentional one-character mutation of a golden causes exit 1 with a visible unified diff.
- [ ] **SC-3:** `test_goldens/` contains exactly 32 byte-exact snapshot files; every file in `test_outputs/` has a peer in `test_goldens/`.
- [ ] **SC-4:** Grep counts on goldens: `redistribute direct` = 0; `^interface ethernet ` = 0; `sonic-cli` occurrences = 1 for non-DCBX outputs and exactly 2 for DCBX-present outputs; `! --- PASTE AFTER REBOOT ---` = 0 in non-DCBX outputs and exactly 1 in DCBX-present outputs; `^ip vrf mgmt` = 1 in every output that configures a management interface.
- [ ] **SC-5:** Hardware validation evidence attached to the PR for all four vendor sample outputs, with zero `%Error` responses.
- [ ] **SC-6:** QA confidence score 95 or above; Security-reviewer sign-off present.
- [ ] **SC-7:** FR-3 (`ip vrf mgmt`) emission is confirmed by hardware validation on SSE-T8196 (`ip vrf mgmt` applies without error, management VRF present in running-config). Emission rule is not alterable on engineer authority; any hardware anomaly is escalated to Dane.
- [ ] **SC-8:** README has a Testing section naming `test_goldens/` and the `--update-goldens` flag.
- [ ] **SC-9:** The false-positive count in the Cisco sample report has dropped by at least 100 lines (FR-1 acceptance).

## 11. Out of Scope

Explicitly deferred to subsequent PRDs or sprints:

- All P1 items from the source analysis: `arista_eos_parser` syslog keyword normalization (C7/P1-1), MCLAG `peer-link` spacing variants (P1-2), bare `except:` in `_mask_to_cidr` (P1-3), broad exception swallowing in `multi_os_to_sonic_migrator.py` (P1-4), UTF-8 stdio reconfig side-effect (P1-5), Cumulus `peerlink` hardcoded bond (P1-6), Juniper FPC slot collapse (P1-7), idempotency markers (P1-8), timeout stdout capture (P1-9).
- All P2 items: CiscoLikeParser extraction (P2-1), Juniper VLAN resolution consolidation (P2-2), regex library (P2-3), section-emit helper refactor (P2-4), Cumulus BGP normalization cleanup (P2-5), detect_os tie-break logging (P2-6), type hints (P2-7), logging module migration (P2-8), README caveat section (P2-9).
- Full test harness rebuild (Step 3 of the analysis §7 recommendation) beyond the golden-file snapshot added here: unit tests per parser, negative tests, pytest migration, CI integration are all out of scope.
- OSPF, QoS, ACL, NAT, multicast - still out of tool scope as they were before.
- Any parser logic changes beyond the minimum required to support FR-1 (false-positive elimination) and FR-4 (redistribute keyword mapping).
- Any change to interactive prompts, CLI arguments, auto-detection heuristics, or the report text format beyond what FR-1 produces as a side-effect.

---

## Self-review confidence score

**Overall: 97/100** (v1.1, post-correction pass)

- Requirement completeness (all user-facing behaviors covered): 24/25. FR-1 through FR-9 name the exact file:line changes or the canonical form to emit; acceptance criteria are grep-testable or byte-diffable. FR-2's DCBX branch is now prescribed with exact block structure and a counted `! --- PASTE AFTER REBOOT ---` marker. One point held back for FR-9's README section: the prose content is described by acceptance criteria but not templated verbatim.
- Data model precision: 25/25. No schema change; existing dataclasses explicitly preserved. `test_goldens/` is a byte-exact mirror with no internal schema.
- Constraint compliance (pricing, platform, CLAUDE.md rules): 25/25. Pricing absent. Hardware validation on SSE-T8196 specified. FR-3 is RESOLVED per Dane's 2026-04-16 confirmation; the global CLAUDE.md Section 2 reflects the corrected VRF rule. No remaining UNVERIFIED items.
- Open items resolved or clearly flagged: 23/25. O1 removed. O2, O3, O4 have prescriptive resolutions inlined in the respective FRs. O5 and O6 are explicit out-of-scope flags. Two points held back because O5 (`update-source loopback0` capitalization) and O6 (prompt prefix) are acknowledged rather than fixed - acceptable for a P0-scoped PRD but still deferred.

Revision history:
- **v1.0 (2026-04-16):** initial draft; scored 92/100.
- **v1.1 (2026-04-16):** FR-3 resolved (Dane confirmed `mgmt` is not built-in; emit `ip vrf mgmt`); FR-2 DCBX block structure prescribed; FR-5 range-vs-individual rule prescribed (>= 3 consecutive + identical settings); FR-6 per-port expansion downgraded from fallback-option to escalation-only; O1 removed; SC-4 and SC-7 tightened. Re-scored 97/100.

All four categories now score at or above 23/25; delivery threshold met.
