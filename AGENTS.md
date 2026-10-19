# Fork maintenance

This repository maintains a minimal downstream patch set, not a separate editor.
These instructions apply to patches, workflows, documentation, verification, and
release maintenance.

## Scope and branches

- Keep two permanent branches: `main` is an unmodified mirror of upstream `main`;
  `downstream` contains the ordered patches, release workflow, and fork
  documentation. Keep `downstream` as the default branch so scheduled maintenance
  runs from it. Update the mirror only by an ordinary fast-forward.
- Native GitHub/Codex review and repair use temporary `review-base/`, `review/`
  and `repair/` PR branches, as in the other maintained downstreams. Their exact
  commits are review evidence, not additional product branches or installation
  sources. Close the PR after verified publication or selective adoption; never
  merge repair-context files into `downstream`. Do not create release/backup
  branches or a queue of intermediate upstream integrations. Reconstruct sources
  in disposable detached worktrees.
- Preserve the requested downstream behavior, necessary fixes, compatibility, and
  regression tests. Do not add unsolicited features or develop the fork for its
  own sake.
- Prefer a suitable upstream artifact. When it lacks required behavior or fixes,
  maintain the smallest necessary patch set and build a verified artifact in
  GitHub Actions. Source builds, development tools, SDKs, and compilers are allowed
  when needed; an unavailable upstream artifact is not a reason to return routine
  maintenance to a human.
- Use one complete supported upstream snapshot: a stable release, or an exact
  commit of the selected upstream branch when release tags lag. Do not build a
  queue of integrations and releases for every intermediate upstream commit.
- When upstream fixes a downstream issue, run its regression against upstream
  without the local fix. Remove the redundant change only after that passes,
  retaining useful regression coverage.

## Maintenance cycle

### Review first; one final validation and build

- Finish source selection, patch application, all review findings and ordinary
  repairs before starting any tests, compilation, packaging or other expensive
  validation. During preparation use source inspection and bounded mechanical
  checks only; do not run speculative or intermediate test/build jobs.
- For each candidate request two independent native Codex reviews: (1) the new
  or modified downstream patches relative to the last shipped patch set and
  (2) the complete assembled project, including the entire downstream diff and
  its integration with upstream. Bind both requests and completions to the exact
  candidate and their respective bases. Ask each reviewer for the complete list
  of findings in one response. Wait for both reviews, consolidate every finding,
  and repair the whole batch before requesting another pair. Carry unresolved
  findings forward; an omitted finding is not evidence of a fix.
- Freeze one complete candidate after both reviews and every known finding have
  been resolved or explicitly disproved. Run the mandatory policy and application
  tests once in the final validation sequence, then build and inspect that same
  candidate once, then publish its verified artifacts. Collect all selected runtime
  regression failures in that final attempt before reporting the complete batch;
  any required failure blocks the bundle and publication. Bound individual test
  processes and their child processes so one hang cannot hide the remaining cases.
  Never rebuild just because
  a repair was adopted or a callback/schedule repeated. Reuse verified artifacts
  only when their complete source identity matches.
- In that final sequence, check proposed release policy in the credential-free
  sandbox before adopting it or executing it with write credentials. Verify that
  adoption preserves every reviewed product-file blob and the regression
  contract, then continue Rust tests, build and publication in the same run.
  Do not dispatch a second validation cycle after a mechanical adoption.
- Credentialed orchestration imports only the exact checked controller pinned in
  the native repository variable `ZED_TRUSTED_POLICY`; candidate files are input
  data until their final sandbox checks pass. Advance that pin only after those
  checks. A missing or broken pin uses the existing read-only policy bootstrap:
  both exact-source reviews first, then one isolated policy validation, then
  activation and the remaining final sequence. Agents supply the bootstrap
  inputs and perform initialization without returning routine setup to a human.
- A failed final check blocks publication and stops automatic expensive retries;
  retain its diagnostics and the working release. Do not hide a failure, skip a
  required check, or silently rebuild a changed candidate under an old approval.
  An additional heavy attempt needs an explicit owner decision recorded on the
  existing source-review PR for the exact application/configuration and latest
  stopped run. Agents must never create that authorization without the owner
  granting it. The receipt is consumed by the ordinary final-candidate guard;
  changing policy alone never refunds a spent attempt. Changing the application
  SHA, selected upstream, patch digest or review PR never erases the latest failed
  heavy attempt either: retain native receipts across source-review PRs and require
  the exact owner grant for the latest stopped run before another heavy attempt.
  Missing or incomplete native history blocks the attempt. An older trusted pin
  without this guard must use the existing reviewed read-only bootstrap before
  preparing any draft or final-attempt reservation.
- Use the compiler parameters and Cargo configuration of the exact selected
  upstream snapshot for each stage: upstream CI test parameters for regressions,
  upstream macOS bundle parameters for the shipped application. Do not invent
  profile, LTO, codegen-unit, debug-info, debug-assertion, incremental or target
  overrides. Test selection and downstream update identity remain fork inputs.
  Verify recipe parity in executable policy before compilation; a note or review
  alone is insufficient. If upstream changes its recipe, agents port the recipe
  before the final attempt. Preserve all regressions and real selector coverage.
- Keep only one active candidate and one repair task for the selected upstream.
  Reuse existing review/repair context and close superseded PRs after preserving
  their exact commits and findings. Bound retained branches and artifacts; never
  create an integration or review queue for intermediate commits.

1. Use the existing GitHub Actions workflow to discover the supported upstream
   snapshot, pin its exact revision, and apply logical patches in filename order.
   Source selection, validation, builds, and publication belong in CI wherever
   practical, not in an installation assembled from a mutable working copy.
2. Coding agents handle application conflicts, ordinary CI failures, necessary
   refactoring, and substantive review findings. Preserve new upstream behavior
   and the required downstream result. Do not replace whole new files with old
   versions, skip a patch, or expand the task to make integration easier.
3. Deliver the actual fix as an applicable patch or commit, with a regression
   where appropriate. Review and run the relevant checks against the new exact
   SHA. Test the intended success and the defined failure behavior. An agent
   report, reaction, previous approval, or test of another revision is not proof.
   For changed Rust fields or APIs, inspect every constructor, struct literal and
   caller, including test fixtures and feature/target-gated code, before the final
   compilation. Resolve every added import and qualified symbol against the
   actual crate exports and module visibility in that snapshot, including test
   fixtures; prefer the existing public constructors and conversions. Keep
   existing regression fixtures and their assertions intact.
   A policy correction with unchanged application code may validate again only
   when native metadata across every run attempt proves all earlier macOS jobs
   were skipped. A changed
   configuration or adoption SHA must never refund a started heavy application
   attempt; callbacks of the same complete candidate remain deduplicated.
   Upstream contribution procedures requiring a README banner and manual author
   confirmation do not apply to this downstream cycle. Do not copy those banners
   into patches: independent exact-SHA Codex review is the required review gate.
4. Build the verified candidate in GitHub Actions. Verify the version, platform,
   exact upstream and patch revisions, artifact checksums, and relevant platform
   identifiers. Match debug symbols to the shipped binary, not merely to a
   similarly named file.
5. Publish one verified release and use the established installer or updater to
   deliver that artifact. Build, publication, installation, and validation of the
   running version are separate stages; report each truthfully. A published
   release or running schedule alone does not prove delivery or a closed
   maintenance cycle.
6. Repair ordinary failures and repeat the affected checks after the fix. Bound
   attempts, elapsed time, disk use, and other resources. Blind or unbounded
   reruns are not repair. Block an unverified candidate while retaining the last
   known-good release and recovery path; an unrelated task must not wait for a
   broken candidate.

## Verification and operational safety

- Mandatory tests, review, integrity checks, and repository access policies must
  not be weakened to obtain a release. A failed mandatory stage must remain
  failed, not be reported as green, skipped, or indefinitely pending.
- Optional optimization and diagnostic checks do not become correctness
  requirements without an established contract. Investigate applicability and
  practical impact, and disclose unresolved findings without presenting a
  hypothesis as a confirmed cause.
- Preserve user data, credentials, settings, repositories, terminal sessions,
  remote sessions, active work, and applicable rollback arrangements. Respect
  recorded intent and existing access restrictions. Routine maintenance authority
  does not authorize data loss, secret disclosure, or bypassing required checks.
- Treat the editor as an active control surface for other tools and systems.
  Do not close, replace, restart, or disrupt a running instance merely to test an
  artifact. Use isolated artifact inspection and test instances where suitable;
  coordinate any necessary live transition within the granted scope.
- Distinguish the installed package from the running process and an existing
  window. Command-line metadata, a successful launch command, or another window
  does not by itself establish which revision is running or that a live workflow
  works.
- Keep committed instructions and operational material portable and public-safe.
  Do not include personal identifiers, private hostnames, home-directory paths,
  credentials, or private environment details.

## Simplicity and cleanup

- Choose the simplest correct solution that satisfies the whole task and
  preserves existing contracts. Start with the working recipe, native GitHub
  facilities, and existing agent capabilities. Do not add a controller, queue,
  notification service, abstraction, dependency, or speculative feature unless
  the existing mechanism is demonstrably insufficient.
- Keep patches and reviewable changes focused. Refactor only to fix a defect or
  simplify the required maintenance, not to grow a separate product.
- Use bounded, disposable workspaces and caches. Retain concise provenance and
  evidence needed to reproduce the result; do not commit worker transcripts,
  local builds, downloaded releases, temporary source copies, or private archives.
- Before cleanup, identify ownership, active use, and any unique uncommitted
  changes. Preserve necessary history and evidence in a deliberate recovery
  archive outside the repository. Remove obsolete artifacts only within granted
  authority; do not delete shared dependencies or active build inputs blindly.
- Once the requested result is verified and necessary evidence is retained,
  clean up the task's temporary artifacts and stop. Do not add another layer or
  polish beyond the task.

## Autonomy and escalation

Agents perform routine version selection, patch porting, conflict resolution,
bug fixing, necessary refactoring, review, verification, and reruns within existing
access and authorization, without repeatedly asking maintainers to do that work.

Escalate only after checking that no simple correct solution exists within those
constraints, or when a real access boundary or change of maintainer intent is
required. Preserve the working release and open or update a GitHub issue or pull
request through the existing notification mechanism. Include the precise blocker,
verified facts, attempts, viable alternatives, and their tradeoffs. Do not conceal
a dead end with endless retries or a green status, and do not build a separate
notification system.

These requirements define the maintenance contract. Their presence, or the
presence of a workflow, is not evidence that every stage is already automated.
