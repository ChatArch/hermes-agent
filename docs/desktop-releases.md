# ChatArch desktop releases

`Desktop release` builds **installers**, not just GitHub's automatic source archives.
These are community fork builds of Hermes, originally by Nous Research. They do
not claim Nous Research's signing identity or use its download CDN. The app's
legal attribution is retained and `Hermes-LICENSE.txt` is included in resources.

## Versions and source

Release tags use the existing CalVer convention `vYYYY.M.D[.N]`, without leading
zeroes; the optional positive suffix distinguishes releases on the same day.
The date must be real. The tag's peeled commit must be reachable from the remote
`main` or `release` branch. An unrelated feature-branch tag fails before builds.
Protect those branches and restrict tag creation with repository rulesets.

CalVer identifies the release; **SemVer identifies the software**. The pipeline
requires `pyproject.toml` and `hermes_cli/__init__.py` to agree, and uses that
SemVer as electron-builder's `extraMetadata.version`, the installer version and
the install stamp version. The desktop workspace's local development version is
not bumped or rewritten by this workflow. No version is bumped by adding this
pipeline. Select any desired version changes in a reviewed commit before tagging.

Every platform checks out the same immutable source commit. The packaged stamp
and manifest record its repository and commit. First-run bootstrap downloads
`install.sh` or `install.ps1` from that repository at that commit and passes the
repository and commit to the installer. Release builds use `main` as the initial
clone branch, then fetch the exact commit (including release-branch commits).
Repository-aware installer caches are separate. A failed pinned fetch does not
silently use an installed upstream script. Existing managed checkouts from a
different repository are refused by bootstrap; connect to that backend instead
or choose a separate installation directory. Normal updates follow the managed
checkout's origin, not an upstream URL substituted for the fork. Existing newer
checkouts keep the existing no-downgrade behavior.

## Downloads

All asset names use:

```text
ChatArch-Hermes-<SemVer>-<CalVer-or-preview>-<platform>-<arch>-unsigned.<format>
```

| Native runner | Platform | Architecture | Required formats |
| --- | --- | --- | --- |
| `macos-15` | `darwin` | `arm64` | `.dmg`, `.zip` |
| `macos-15-intel` | `darwin` | `x64` | `.dmg`, `.zip` |
| `windows-2025` | `win32` | `x64` | `.exe` (NSIS), `.msi` |
| `ubuntu-24.04` | `linux` | `x64` | `.AppImage`, `.deb`, `.rpm` |

The runner's Node platform and architecture must match the target. All nine
installers are mandatory; unavailable runners, missing native dependencies or
failed formats block publication rather than shrinking the matrix. Existing
before-build, before-pack, after-pack and native-dependency staging hooks remain
active. Linux installs the RPM tooling needed by electron-builder. The builder
continues to run with `--publish never`; only the separate publisher writes a
GitHub Release.

The final bundle also contains:

- `release-manifest.json`: source, versions, event, per-asset platform,
  architecture, format, byte size, SHA256 and signing status.
- `SHA256SUMS`: checksums for all installers, the manifest and release notes.
- `release-notes.md`: release identity, formats and first-run limitations.

Download the installer matching your CPU and OS. DMG is the usual macOS install
route; ZIP contains the app bundle. Windows offers interactive NSIS or MSI.
AppImage is portable; DEB and RPM integrate with their respective distributions.
Verify the checksum before opening. For example, run `sha256sum -c SHA256SUMS`
on Linux, `shasum -a 256 -c SHA256SUMS` on macOS, or compare
`Get-FileHash -Algorithm SHA256 <installer>` with its entry on Windows.

## Unsigned by design in this first pipeline

Both PR previews and tag builds are explicitly **unsigned** and macOS artifacts
are **not notarized**. macOS may refuse to open the app or show a Gatekeeper
warning; Windows may show SmartScreen or unknown-publisher warnings. Managed
devices may prohibit these builds entirely. Do not disable system-wide security
controls to install them. Signed distributions need a separately reviewed signing
lane and certificates owned by the publishing organization.

No signing secrets are referenced by this workflow, including on tags.
`CSC_IDENTITY_AUTO_DISCOVERY=false` and macOS `identity: null` prevent accidental
use of a runner's signing identity. The existing manual builder supports
`CSC_LINK` and `CSC_KEY_PASSWORD` for a publisher-owned signing certificate; the
existing notarization hook supports `APPLE_API_KEY`, `APPLE_API_KEY_ID` and
`APPLE_API_ISSUER`, or a local `APPLE_NOTARY_PROFILE`. These names describe the
existing hooks, **not enabled secret inputs for this workflow**. Adding repository
secrets alone will not make this pipeline sign. Windows signing also needs an
explicit signing configuration; its current `signAndEditExecutable: false` is
preserved. Never supply official Nous Research credentials or identities.

## First launch and privacy

Only the Electron shell/UI and its staged native dependencies are packaged, not
Python or a full backend environment. First launch requires Internet access to
GitHub and the installer's dependency providers to create a Python backend, or
you can connect to an existing compatible backend. Provider credentials remain a
user setup step. A downloaded installer is not an offline backend distribution.

Packaging runs from a clean checkout, using the existing file allowlist, never a
user's Hermes home. Validation inspects the actual packaged app version,
repository, install stamp and resource names, rejects config/profile/token/cache
paths and embedded build-host paths in text resources, then exports only the
nine expected nonempty regular installer files. Debug directories, raw caches,
user homes and build logs are not release assets. No checks can replace keeping
credentials and private files out of source control.

## PR previews

Relevant `pull_request` events run all four native builds. The preview checks out
the PR head commit and records the PR head repository, rather than stamping a
temporary merge commit as a bootstrap source. Its identity is `preview-pr-N`.
Artifacts are under the Actions run: individual `desktop-<platform>-<arch>`
artifacts and, after all jobs succeed, `desktop-release-bundle`. They expire after
14 days. Cross-repository previews require their source commit to remain publicly
fetchable for first-run bootstrap.

PR jobs have only `contents: read`, no persisted checkout credentials, no signing
or publishing secrets, no release environment and no release/tag writes. This
uses `pull_request`, never `pull_request_target`. Untrusted PR code still executes
in disposable GitHub-hosted runners; do not move these jobs to privileged or
persistent self-hosted machines.

## Maintainer procedure

1. Merge the reviewed source to `main` or `release`. Confirm that the four native
   preview builds and packaging checks passed for the intended changes.
2. Configure the GitHub environment `desktop-release` with required reviewers and
   tag-only deployment restrictions. Restrict release tags with repository
   rulesets. The publishing job needs its scoped `GITHUB_TOKEN` with
   `contents: write`; no PAT, CLI login, npm token or CDN credentials are needed.
3. Review the chosen CalVer tag and existing SemVer. Create an annotated tag at
   the intended commit and push that tag **only when release execution is
   separately authorized**. This implementation does not create or push tags.
4. Wait for source validation, every native build, complete-matrix validation,
   and environment approval. Inspect the manifest/checksums before approving.
5. The publisher rechecks the remote tag, creates a draft, uploads and verifies
   every asset's server-reported SHA256, then makes the draft public. It never
   creates, retargets or force-updates a Git tag. Read back the published release
   and test the installers on real supported hosts.

## Failures and reruns

Build/validation failures cannot publish. Use **Re-run failed jobs** while this
run's artifacts still exist. Do not rerun successful build jobs after partial
publication: timestamps/signatures mean rebuilt binaries need not be identical.
Artifact names are immutable within a run; a full rebuild is not an overwrite
mechanism. If artifacts expired, stop and plan a fresh release identity.

Interrupted publication leaves a draft. Retry the failed publisher with the same
validated bundle: it accepts existing assets only when their server-reported
digest is identical, and uploads only missing files. A published release with
exactly the same source, notes and digests is a no-op on rerun. Differing assets,
unrecognized drafts, changed tags or incomplete published releases are errors;
nothing is deleted or overwritten automatically. A partially uploaded invalid
asset requires explicit maintainer investigation/removal of that draft asset,
or a new release tag; do not casually discard published evidence. The GitHub API
must supply asset SHA256 digests, otherwise the publisher fails closed.

Do not use the legacy `scripts/release.py --publish` path for desktop releases.
This workflow does not change scheduled Install & Update E2E or invent historical
tags for it.
