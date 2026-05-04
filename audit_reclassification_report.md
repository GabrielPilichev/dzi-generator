# Reclassification Audit Report

## 0. Current DB state

- Total questions: `819`
- Questions with topic_id: `794`
- Questions with topic_id NULL: `25`
- Total curriculum_topics: `128`

Untagged by source:

```text
aug_2022_v2 | 8
aug_2023 | 1
aug_2023_v2 | 2
aug_2024 | 1
jun_2022 | 4
may_2022 | 3
may_2023_v2 | 3
may_2024 | 2
may_2025_v2 | 1
```

## 1. Override scale + reasoning

Total deterministic/priority override return rules found: `57`

| # | slug returned | override reason in code | active questions caught |
|---:|---|---|---|
| 1 | `access-action-queries` | priority_override: clear Access action query cue | 451, 740 |
| 2 | `cms-security` | priority_override: clear CMS security cue | 501, 793 |
| 3 | `effective-information-search` | priority_override: clear search query cue | 375 |
| 4 | `cms-systems` | priority_override: clear CMS systems cue | 488, 489, 780, 781 |
| 5 | `voip-internet-communication` | keyword_override: clear VoIP / internet voice-video communication cue | 540 |
| 6 | `raster-image-properties` | keyword_override: clear raster image cue | 254 |
| 7 | `peripheral-devices` | keyword_override: clear peripheral device cue | 278 |
| 8 | `device-drivers` | keyword_override: clear device driver cue | 566, 684 |
| 9 | `install-uninstall-software` | keyword_override: clear software install/uninstall cue | 281, 282, 288, 568, 569 |
| 10 | `archive-compression` | keyword_override: clear archive/compression cue | 570, 571, 572, 573, 574, 594 |
| 11 | `social-networks` | keyword_override: clear social network cue | 547, 548, 553, 589, 606, 652 |
| 12 | `effective-information-search` | keyword_override: clear search/operator cue | 551, 554 |
| 13 | `network-topology` | keyword_override: clear network topology cue | 335, 622 |
| 14 | `network-devices` | keyword_override: clear network device cue | 339, 340, 626, 627, 649 |
| 15 | `network-protocols` | keyword_override: clear network protocol cue | 341, 360, 367, 369, 628, 629, 648, 655, 657, 751 |
| 16 | `information-security` | keyword_override: clear information security cue | 664, 666 |
| 17 | `cloud-technologies` | keyword_override: clear cloud technologies cue | 671 |
| 18 | `biometric-identification` | keyword_override: clear biometric identification cue | 689, 690 |
| 19 | `device-manager` | keyword_override: clear Device Manager cue | — |
| 20 | `information-system-feasibility` | keyword_override: clear IS feasibility cue | 722 |
| 21 | `access-tables` | keyword_override: clear Access tables cue | 723 |
| 22 | `access-forms` | keyword_override: clear Access forms cue | 741 |
| 23 | `access-macros` | keyword_override: clear Access macros cue | — |
| 24 | `access-select-queries` | keyword_override: clear select query cue | — |
| 25 | `access-action-queries` | keyword_override: clear action query cue | 451, 740 |
| 26 | `website-planning` | keyword_override: clear website planning cue | 457, 745, 746, 747 |
| 27 | `website-information-architecture` | keyword_override: clear information architecture cue | 756, 757, 759 |
| 28 | `responsive-design` | keyword_override: clear responsive design cue | 762 |
| 29 | `rss-feeds` | keyword_override: clear RSS cue | 775 |
| 30 | `user-interface` | keyword_override: clear user interface cue | 305, 592 |
| 31 | `website-design-process` | keyword_override: clear website design cue | 297, 584 |
| 32 | `cms-systems` | keyword_override: clear CMS systems cue | 488, 489, 780, 781 |
| 33 | `cms-wordpress-installation` | keyword_override: clear WordPress installation cue | 491, 783 |
| 34 | `captcha` | keyword_override: clear CAPTCHA cue | 497 |
| 35 | `network-sniffing` | keyword_override: clear sniffing cue | 500, 792 |
| 36 | `cms-security` | keyword_override: clear CMS security cue | 501, 793 |
| 37 | `web-hosting-bandwidth` | keyword_override: clear hosting bandwidth cue | 503, 795 |
| 38 | `hypertext-hyperlinks` | keyword_override: clear hypertext cue | 512, 804 |
| 39 | `team-roles` | keyword_override: clear team roles cue | 518, 810 |
| 40 | `brainstorming` | keyword_override: clear brainstorming cue | 520, 812 |
| 41 | `ram-memory` | keyword_override: clear RAM cue | 814 |
| 42 | `windows-user-accounts` | keyword_override: clear Windows accounts cue | 817 |
| 43 | `windows-firewall` | keyword_override: clear firewall cue | 526, 818 |
| 44 | `safe-mode` | keyword_override: clear safe mode cue | 532 |
| 45 | `grid-infrastructure` | keyword_override: clear grid infrastructure cue | 614 |
| 46 | `internet-services` | keyword_override: clear internet services cue | — |
| 47 | `network-transmission-media` | keyword_override: clear transmission media cue | 625 |
| 48 | `file-explorer-sharing` | keyword_override: clear File Explorer sharing cue | — |
| 49 | `document-templates` | keyword_override: clear document template cue | — |
| 50 | `task-manager` | keyword_override: clear Task Manager cue | 685 |
| 51 | `software-troubleshooting` | keyword_override: clear software troubleshooting cue | 696 |
| 52 | `analysis-toolpak` | keyword_override: clear Analysis ToolPak cue | 709 |
| 53 | `information-system-development-stages` | keyword_override: clear IS development stage cue | 713 |
| 54 | `access-data-entry-objects` | keyword_override: clear Access data-entry object cue | — |
| 55 | `access-controls-buttons` | keyword_override: clear Access controls/buttons cue | 742 |
| 56 | `access-startup-form` | keyword_override: clear Access startup form cue | 743 |
| 57 | `e-learning-platforms` | keyword_override: clear e-learning cue | — |

Motivation summary:
- Most overrides were added because embeddings/BgGPT chose a nearby but wrong topic when the whitelist was missing or semantically crowded.
- Examples observed during dry-runs: social-network questions going to `web-standards`; MS Word table formatting going to `dsum`; router/network questions going to unrelated hardware/history topics; CMS security going to `archive-compression`; Access action queries going to select-query topics.
- The overrides should be considered high-precision classroom-test rescue rules, not a clean long-term architecture.

## 2. Decision pathway breakdown for classroom reclassified questions

Total tagged classroom questions counted: `579`

| decision pathway | count |
|---|---:|
| pure embedding final decision | `0` |
| embedding shortlist + BgGPT pick | `71` |
| keyword override deterministic | `88` |
| keyword override priority | `9` |
| manual correction / manual final | `20` |
| unknown / no matching log | `0` |

Manual methods:
```text
manual_alt_correction_v1: 10
manual_final_8_12_v1: 3
manual_final_alt_7_v1: 7
```

Note: the script does not use pure embedding as a final assignment method. Embeddings only build/rank the candidate shortlist; BgGPT or deterministic override makes the final choice.

## 3. DB consistency checks

- PRAGMA foreign_key_check rows: `0`
- Duplicate active question_topic_assignments: `0`
- questions.topic_id pointing to non-existent topic: `0`
- topic_section_assignments with invalid topic/section: `0`
- Duplicate topic_aliases.alias_slug rows: `0`

## 4. ДЗИ-relevance flagging

Cannot run exact requested query because curriculum_sections does not have all requested columns.
Available curriculum_sections columns: `class, curriculum_id, display_order, has_section_test, id, is_dzi_relevant, is_entry_check_scope, is_exit_check_scope, module_id, notes, section_slug, section_type, source_type, title_bg`

Authority used for current flags: `NONE / not verified`.
These flags were seeded from the project’s working curriculum assumptions and user-provided distribution, not from an official МОН DZI exam-program source. Treat them as provisional until checked against the official current МОН изпитна програма.

## 5. Per-script impact matrix

| script | status | impact / needed change |
|---|---|---|
| `topic_classifier.py` | b) needs minor migration | Still runs, but should optionally read topic_aliases and write question_topic_assignments instead of only questions.topic_id. |
| `review_export.py` | a) unaffected | No required migration for current workflow; can still read questions.topic_id/curriculum_topics. |
| `review_import.py` | a) unaffected | No required migration for current workflow; quality/approval fields unchanged. |
| `sync_vault.py` | b) needs minor migration | Needs minor migration if new topics should have vault notes/section metadata. Should not delete DB topics missing from vault. |
| `build_worksheet.py` | a) unaffected | Existing topic/area/class worksheet mode still works. Optional future enhancement: --section/--assessment-event. |
| `build_exam.py` | b) needs minor migration | Still works, but DZI generation should eventually use assessment_events / DZI-relevant sections rather than broad random selection. |
| `predict_answers.py` | a) unaffected | Answer prediction tables unchanged. |
| `import_classroom_tests.py` | a) unaffected | Insert path unchanged. Optional future enhancement: invoke reclassifier after import. |

## 6. Vault state

- New topics expected from expansion: `61`
- New topics with vault notes: `61`
- New topics without vault notes: `0`

`sync_vault.py` appears to delete missing curriculum_topics: `False`

Recommendation: generate stub notes for the new topics rather than leaving them invisible in the vault. Do not make sync_vault delete DB topics that do not have notes. Best next step is a `generate_topic_stubs.py` utility that creates minimal `vault/Topics/<slug>.md` files from curriculum_topics.note_path/title_bg/description/classes/section.

## 7. Backup confirmation

- Backup path: `data/questions.backup-after-classroom-reclassification-complete.db`
- Exists: `True`
- Backup total questions: `819`
- Backup with topic_id NOT NULL: `794`
- Backup with topic_id NULL: `25`

Expected backup counts:
- total questions: `819`
- topic_id NOT NULL: `794`
- topic_id NULL: `25`