# Launch checklist (index)

An index of existing requirements, not a procedure: it authorizes no operation, activates nothing and records no live state. A source link or merged implementation is not evidence of a live check. Thresholds and failure criteria live at source. Plan: [ROADMAP](REFACTOR_ACCOUNTS_ROADMAP.md#current-roadmap-update--2026-11-18).

**Legend.** Status starts `Pending`. Evidence: reference to a dated record (no logs or secrets); `—` = none, live state not inspected. Sign-off: `—` = unsigned; default accountable owner Gabriel.

## 1. Release and evidence

A repository baseline is not the deployed or intended release. Nothing below is calculated here.

|Item|Status|Evidence|Sign-off|
|---|---|---|---|
|R1 Deployed commit (unknown)|Pending|—|—|
|R2 Exact intended release commit (unknown)|Pending|—|—|
|R3 Migration range and pending-migration evidence (not calculated)|Pending|—|—|
|R4 Dated evidence record link|Pending|—|—|
|R5 Final owner sign-off|Pending|—|—|

## 2. Deploy with every gate off

Separate from activation; deploying needs its own authority. Sources: [DEP§20](DEPLOYMENT.md#20-deploy--update-procedure) with [DEP§9](DEPLOYMENT.md#9-schema--pre-start-step) (migration safety), [DEP§16](DEPLOYMENT.md#16-backup) (complete snapshot) and [DEP§21](DEPLOYMENT.md#21-rollback-considerations) (rollback), plus each release's own section among [DEP§22–38](DEPLOYMENT.md#22-account-management-cli) (deployment order, rollback). Which apply is unknown until R1 is recorded; none is marked applicable or skipped.

|Item|Status|Evidence|Sign-off|
|---|---|---|---|
|D1 §20 with §9/§16/§21: dry run, snapshot, rollback path|Pending|—|—|
|D2 Release sections 22–38 for the R3 range|Pending|—|—|
|D3 Deployed build keeps all five gates `False`: `registration.PRODUCTION_ACTIVATION`, `registration.TEACHER_ACTIVATION`, `profiles.PRODUCTION_ACTIVATION`, `recovery.PRODUCTION_ACTIVATION`, `google_oidc.PRODUCTION_ACTIVATION` (base-commit values are source facts, not deployment evidence)|Pending|—|—|
|D4 Health and smoke result|Pending|—|—|
|D5 Deploy-stage sign-off|Pending|—|—|

## 3. Registration evidence ([REG§0.3](reviews/EMAIL_REGISTRATION_DESIGN.md#03-activation-prerequisites-evidence-not-design-decisions))

Registration and recovery share one budget; unmet capacity criteria mean activation waits. SMTP acceptance is not mailbox delivery.

|Item|Status|Evidence|Sign-off|
|---|---|---|---|
|G1 Bounded mail [REG§4.2](reviews/EMAIL_REGISTRATION_DESIGN.md#42-the-sender-process-and-its-deadline)|Pending|—|—|
|G2 Single-worker capacity [REG§3.5](reviews/EMAIL_REGISTRATION_DESIGN.md#35-work-budget-and-the-mail-circuit)|Pending|—|—|
|G3 Transport security|Pending|—|—|
|G4 Provider/sender and mailbox delivery|Pending|—|—|
|G5 Nginx and trusted client IP|Pending|—|—|
|G6 Shared NAT and total cap [REG§3.6](reviews/EMAIL_REGISTRATION_DESIGN.md#36-shared-school-networks)|Pending|—|—|
|G7 Code and notice timing [REG§3.7](reviews/EMAIL_REGISTRATION_DESIGN.md#37-enumeration)|Pending|—|—|
|G8 Cleanup and restore [REG§6.5](reviews/EMAIL_REGISTRATION_DESIGN.md#65-retention-and-cleanup)|Pending|—|—|
|G9 Privacy [REG§7.2](reviews/EMAIL_REGISTRATION_DESIGN.md#72-privacy-and-minors-activation-prerequisites-not-designed-here)|Pending|—|—|
|G10 Teacher-form quotas (deferred)|Pending|—|—|
|G11 Recovery before public opening|Pending|—|—|
|G12 Google (deferred, own prerequisites)|Pending|—|—|

## 4. Recovery evidence ([REC§9](reviews/PASSWORD_RECOVERY_DESIGN.md#9-activation-prerequisites-evidence-none-done), [DEP§37](DEPLOYMENT.md#37-password-recovery-storage-and-restore-credentials), [DEP§38](DEPLOYMENT.md#still-outstanding-before-any-activation))

The shared mail, capacity, Nginx and Linux evidence of [REC§9](reviews/PASSWORD_RECOVERY_DESIGN.md#9-activation-prerequisites-evidence-none-done) is G1–G8; V1–V7 add to it.

|Item|Status|Evidence|Sign-off|
|---|---|---|---|
|V1 Cleanup on a quiet server; staging restore and snapshot with pending recovery|Pending|—|—|
|V2 RD18 credential-repair rehearsal [REC§6.6](reviews/PASSWORD_RECOVERY_DESIGN.md#66-restore-and-credentials-rd18): mandatory maintenance isolation and postponement rule (not covered by V1)|Pending|—|—|
|V3 Recovery code and neutral-notice timing (no equal timing claimed)|Pending|—|—|
|V4 Password-writer lock duration|Pending|—|—|
|V5 Real Nginx, header and mixed-class checks|Pending|—|—|
|V6 RD15, D16, D17 decisions|Pending|—|—|
|V7 Independent security review of every scope|Pending|—|—|

## 5. Later activation (outside this checklist)

Each pilot feature needs its own separately authorized activation PR under existing prerequisites; no gate changes or new ordering here. Recovery must be ready before public opening. Teacher accounts use the existing operator CLI. Teacher self-registration (G10) and Google (G12) are deferred, outside the classroom pilot, not passed and not prerequisites for student activation.

## 6. Open inputs

D16, final Bulgarian copy including RD15, controller/contact facts, sender domain/configuration and operational evidence. Brevo SMTP is the planning choice, Amazon SES EU the fallback. Live accounts, credentials, DNS, deployment and delivery are uninspected and unknown, not proven absent; older source prose about no live resources is historical. No legal conclusions are drawn. The production vault-note switch is separately scoped, pending and not done.

## 7. Content

Schema migration is not canonical-content synchronization. Any new content's production update is separate B4 work under the [stable-id contract](CANONICAL_DATA.md#stable-id-contract); no replacement database or update procedure is defined here.
