import assert from 'node:assert/strict'
import { test } from 'vitest'

import {
  FALLBACK_BRANCH,
  FALLBACK_COMMIT,
  fromCI,
  fromFallback,
  fromLocalGit,
  isFallbackCommit,
  resolveStamp,
  resolveRepository
} from './write-build-stamp.mjs'

test('source stamp uses the PR head identity and local fork remote instead of upstream defaults', () => {
  const env = { DESKTOP_SOURCE_REPOSITORY: 'Fork/hermes-agent', DESKTOP_SOURCE_COMMIT: 'b'.repeat(40), GITHUB_REPOSITORY: 'ChatArch/hermes-agent', GITHUB_SHA: 'a'.repeat(40) }
  assert.equal(fromCI(env).commit, env.DESKTOP_SOURCE_COMMIT)
  assert.equal(resolveRepository({ env, execFn: () => null }), env.DESKTOP_SOURCE_REPOSITORY)
  assert.equal(resolveRepository({ env: {}, execFn: () => 'git@github.com:Fork/hermes-agent.git' }), 'Fork/hermes-agent')
  assert.throws(() => resolveRepository({ env: { DESKTOP_SOURCE_REPOSITORY: '../invalid/repository' }, execFn: () => null }))
})

test('fromCI reads GITHUB_SHA / GITHUB_REF_NAME', () => {
  assert.deepEqual(
    fromCI({ GITHUB_SHA: 'a'.repeat(40), GITHUB_REF_NAME: 'release' }),
    { commit: 'a'.repeat(40), branch: 'release', dirty: false, source: 'ci' }
  )
  assert.equal(fromCI({}), null)
})

test('fromLocalGit reads HEAD + branch + dirty status', () => {
  const calls = []
  const execFn = (argv) => {
    const cmd = argv.join(' ')
    calls.push(cmd)
    if (cmd === 'git rev-parse HEAD') return 'b'.repeat(40)
    if (cmd === 'git rev-parse --abbrev-ref HEAD') return 'main'
    if (cmd === 'git status --porcelain -uno') return ' M apps/desktop/package.json'
    return null
  }
  assert.deepEqual(fromLocalGit('/repo', execFn), {
    commit: 'b'.repeat(40),
    branch: 'main',
    dirty: true,
    source: 'local'
  })
  assert.ok(calls.includes('git rev-parse HEAD'))
})

test('fromFallback uses the all-zero placeholder commit', () => {
  assert.deepEqual(fromFallback(), {
    commit: FALLBACK_COMMIT,
    branch: FALLBACK_BRANCH,
    dirty: false,
    source: 'fallback'
  })
  assert.equal(isFallbackCommit(FALLBACK_COMMIT), true)
  assert.equal(isFallbackCommit('a'.repeat(40)), false)
})

test('resolveStamp prefers CI over local git over fallback', () => {
  const ci = resolveStamp({
    env: { GITHUB_SHA: 'c'.repeat(40), GITHUB_REF_NAME: 'main' },
    execFn: () => 'should-not-run'
  })
  assert.equal(ci.source, 'ci')
  assert.equal(ci.commit, 'c'.repeat(40))

  const local = resolveStamp({
    env: {},
    execFn: (argv) => {
      const cmd = argv.join(' ')
      if (cmd === 'git rev-parse HEAD') return 'd'.repeat(40)
      if (cmd === 'git rev-parse --abbrev-ref HEAD') return 'main'
      if (cmd === 'git status --porcelain -uno') return ''
      return null
    }
  })
  assert.equal(local.source, 'local')
  assert.equal(local.commit, 'd'.repeat(40))
  assert.equal(local.dirty, false)
})

test('resolveStamp falls back when neither CI nor git is available', () => {
  const stamp = resolveStamp({ env: {}, execFn: () => null })
  assert.deepEqual(stamp, {
    commit: FALLBACK_COMMIT,
    branch: FALLBACK_BRANCH,
    dirty: false,
    source: 'fallback'
  })
})

// ── buildStampPayload — the staged-desktop full schema ─────────────────────

import { buildStampPayload } from './write-build-stamp.mjs'

const baseStamp = {
  commit: 'c'.repeat(40),
  branch: 'main',
  builtAt: '2026-08-14T00:00:00Z',
  dirty: false,
  source: 'ci'
}

const runtime = {
  repoDir: 'app', toolsDir: 'tools', storePython: 'tools/python/bin/python3',
  sitePackages: 'venv/lib/python3.14/site-packages', commands: { hermes: 'bin/hermes' }
}

test('bundled stamp carries the builder contract and refuses an unstaged payload', () => {
  const env = { HERMES_DESKTOP_VARIANT: 'bundled' }
  assert.throws(() => buildStampPayload(baseStamp, env, 'darwin'), /payload/)
  assert.deepEqual(buildStampPayload(baseStamp, env, 'darwin', { runtime }).runtime, runtime)
  assert.equal(buildStampPayload(baseStamp, { HERMES_DESKTOP_VARIANT: 'light' }, 'darwin', { runtime }).runtime, undefined)
})

test('source builds declare the same artifact schema, without a payload', () => {
  const stamp = buildStampPayload(baseStamp, {})
  assert.equal(stamp.payload, 'bootstrap')
  assert.equal(stamp.distribution, 'desktop-app')
  assert.equal(stamp.runtime, undefined)
  assert.equal(stamp.tag, null)
  assert.equal(stamp.commit, baseStamp.commit)
})

test('buildStampPayload keeps schemaVersion + provenance in the staged shape', () => {
  const payload = buildStampPayload(baseStamp, {
    HERMES_DESKTOP_VARIANT: 'bundled'
  }, 'win32', { runtime })
  assert.equal(payload.schemaVersion, 1)
  assert.equal(payload.commit, baseStamp.commit)
  assert.equal(payload.source, 'ci')
  assert.equal(payload.tag, null)
})

test('commit builds retain exact provenance without entering an update channel', () => {
  for (const platform of ['win32', 'darwin']) {
    const env = { HERMES_DESKTOP_VARIANT: 'bundled', HERMES_BUILD_COMMIT: baseStamp.commit }
    const payload = buildStampPayload(baseStamp, env, platform, { runtime })
    assert.equal(payload.commit, baseStamp.commit)
    assert.equal(payload.source, 'commit-build')
    assert.equal(payload.branch, null)
    assert.equal(payload.tag, null)
    assert.equal(payload.updateMechanism, 'external')
    assert.throws(() => buildStampPayload(baseStamp, { ...env, HERMES_BUILD_COMMIT: 'a'.repeat(40) }, platform, { runtime }), /commit/i)
    assert.throws(() => buildStampPayload(baseStamp, { ...env, HERMES_PAYLOAD_TAG: 'v1.2.3' }, platform, { runtime }), /tag/i)
  }
})


test('legacy Windows 10 installer stamp keeps its source branch and declared release version', () => {
  const stamp = buildStampPayload({ ...baseStamp, branch: 'sync/official-candidate' },
    { DESKTOP_RELEASE_VERSION: '0.21.1' }, 'win32')
  assert.equal(stamp.payload, 'bootstrap')
  assert.equal(stamp.updateMechanism, 'self')
  assert.equal(stamp.version, '0.21.1')
  assert.equal(stamp.baseVersion, '0.21.1')
  assert.equal(stamp.branch, 'sync/official-candidate')
  assert.equal(stamp.tag, null)
  assert.throws(() => buildStampPayload(baseStamp, { DESKTOP_RELEASE_VERSION: '../bad' }, 'win32'), /version/i)
})
