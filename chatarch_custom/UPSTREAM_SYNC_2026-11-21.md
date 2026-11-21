# Community sync checkpoint (2026-11-21)

Status: **partial / review-only**. This branch starts from ChatArch's open sync candidate
`sync/official-20261019` (PR #71), which already merges official `20acba0376` (October 19),
then carries two independently verified November community fixes. It does **not** merge
NousResearch `38fde82474` (November 21). Do not merge it as a completed upstream sync.

## Comparison (fetched on 2026-11-21)

| Reference | Head | Notes |
| --- | --- | --- |
| ChatArch `main` | `6aade6dcdc` (October 21) | Feishu synthetic-topic routing, macOS gateway/status/history retention, launchd wrapper handling after the October 10 sync. |
| ChatArch PR #71 | `fce65c8a6e` (November 2) | Official October 19 snapshot plus local SSH, Feishu cards/media and gateway behavior; CI gate and plugin-catalog workflow still failed at that head. |
| NousResearch `main` | `38fde82474` (November 21) | Rapidly changing community branch; fetch again before final integration. |

`origin/main...upstream/main` at the fetch: 194 fork-only and 24,213
community-only commits, common ancestor `c57331677d` (October 10). Relative to
PR #71's October 19 official merge parent, there are 199 candidate-only and
19,175 community-only commits. These counts include merge histories and are
not the number of independent features.

## What this branch actually ports

- `2ea31938e4` (November 8): Telegram `hermes send` recognizes only supported
  HTML tags. Arbitrary angle-bracketed prose no longer enters Telegram's HTML
  parser; 16 focused tests pass.
- `45e8fb4b59` (November 9): filesystem model-provider discovery honors
  `plugins.disabled` *before* importing a disabled plugin, including flat and
  legacy layouts; 5 focused tests pass. The test conflict preserved the fork's
  bundled Copilot-ACP assertion and the community's OpenRouter assertion.

These are selective backports, not evidence that the other November changes
listed below are present here.

## Major community developments still to integrate

- Prompt-cache lifecycle and idle compaction: cache warming/expiry, Nous Portal
  cache accounting, and archived tool results for compression
  (`16099a4dbb`, `bf6a59f336`, `ae77ded2f9`; November 18).
- Gateway topology and migration: multi-backend migration dry-run/preflight,
  status reporting, and per-receiver report intervals
  (`1ecfc85460`, `fb885484b9`, `c31f28544a`; November 21).
- Model/provider support: GPT-6.2 Astra/Sol, Gemini 4 Neon with xhigh
  thinking, and refreshed provider pickers (`786314cef1`, `845acdd9e9`,
  `4c680fcb2f`; November 17-20). Picker-only cherry-picks require the
  model metadata/pricing prerequisite; do not advertise them alone.
- Setup and plugins: one-question first run with free tier, plugin settings
  edited from the CLI, and install-time configuration prompts
  (`210d4625ba`, `63102706e8`, `595accf662`; November 19-20).
- Desktop and web app: browser-tab desktop plugins, profile-switch feedback,
  and opt-in long-frame metrics (`2b10dd274c`, `f125187d6e`,
  `951efa9c39`; October 27-November 20).
- Release/testing: canary lanes in the Stable Release pipeline and E2E claims
  (`667081be8e`, `8415003fba`; November 17-20).

## Full-merge blocker and next review steps

A no-commit merge of `upstream/main` into PR #71's candidate was attempted in
an isolated worktree and aborted without recording a misleading merge parent.
Git reported **108 unmerged paths**: 30 under `.github`, 32 under `tests`, 10
under `tools`, 9 under `gateway`, and 1 Feishu adapter with 12 conflict
hunks. This includes upstream's `hermes_state.py` replacement, SSH backend
and sandbox-scoping changes, Feishu approval-actor refactor, gateway stream
finalization, deleted workflows and generated prompt fixtures. Choosing
`ours` or `theirs` globally would drop either community security changes or
ChatArch's session/SSH/Feishu safety behavior. A second attempt to cherry-pick
skill-index configurability also hit moved tests and implementation seams;
no partial result was retained.

1. Integrate persistence and profile/SSH isolation with explicit behavior
   tests; review the upstream module moves rather than retaining old facades.
2. Reconcile Feishu approval identity, card callbacks, topic anchors, and
   media delivery; then gateway turn and stream lifecycle.
3. Reconcile deleted CI workflows and regenerate prompt fixtures from the
   resolved code, rather than keeping stale snapshots.
4. Merge the latest fetched official head with its real parent and run
   `scripts/run_tests.sh`, `python scripts/check`, and exact-head CI on
   macOS/Linux/Windows before marking the sync ready. PR #71's existing CI
   failure must also be understood; this branch is not a deployment or release.
