/**
 * Regression #87758: a local desktop pack must never enter electron-builder's
 * publish path, and publish resolution must succeed when something else does
 * enter it.
 *
 * These call the real app-builder-lib resolver rather than asserting on the
 * text of the pack script, so they fail if electron-builder changes the
 * behavior we depend on — not when someone reformats package.json.
 */
import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { afterEach, beforeEach, describe, test, vi } from 'vitest'

const require = createRequire(import.meta.url)
const desktopDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const desktopPkg = require(path.join(desktopDir, 'package.json'))

const { getPublishConfigs, PublishManager } = require('app-builder-lib/out/publish/PublishManager.js')
const { getRepositoryInfo } = require('app-builder-lib/out/util/repositoryInfo.js')
const { createYargs, configureBuildCommand, normalizeOptions } = require('electron-builder/out/builder.js')
const { CancellationToken } = require('builder-util-runtime')
const { getCiTag } = require('electron-publish')
const parser = configureBuildCommand(createYargs())
  .exitProcess(false)
  .fail(message => {
    throw new Error(message)
  })

async function captureWrapper(args, result = { status: 0 }) {
  const originalArgv = process.argv
  const spawn = vi.fn(() => result)
  const exitSignal = new Error('intercepted wrapper exit')
  let exitCode
  const exit = vi.spyOn(process, 'exit').mockImplementation(code => {
    exitCode = code
    throw exitSignal
  })
  const errors = vi.spyOn(console, 'error').mockImplementation(() => {})
  vi.resetModules()
  vi.doMock('node:child_process', () => ({ spawnSync: spawn }))
  process.argv = [process.execPath, fileURLToPath(new URL('./run-electron-builder.mjs', import.meta.url)), ...args]
  try {
    await import('./run-electron-builder.mjs')
  } catch (error) {
    if (error !== exitSignal) {
      assert.equal(spawn.mock.calls.length, 0, 'invalid policy must fail before spawning')
      throw error
    }
  } finally {
    process.argv = originalArgv
    exit.mockRestore()
    errors.mockRestore()
    vi.doUnmock('node:child_process')
    vi.resetModules()
  }
  return { calls: spawn.mock.calls, exitCode }
}

function publishManager(options) {
  return new PublishManager(
    {
      cancellationToken: new CancellationToken(),
      onAfterPack() {},
      onArtifactCreated() {}
    },
    options
  )
}

/**
 * The slice of PlatformPackager that getPublishConfigs actually reads. The
 * repositoryInfo getter mirrors what electron-builder does for real: resolve
 * from THIS package's metadata with projectDir = apps/desktop.
 */
function fakePackager(metadata) {
  const info = {
    get repositoryInfo() {
      return getRepositoryInfo(desktopDir, metadata, null)
    },
    appInfo: { version: '0.0.0', channel: null, updaterCacheDirName: 'hermes' },
    config: {},
    options: {}
  }
  return {
    platformSpecificBuildOptions: {},
    config: {},
    info,
    platform: { name: 'linux' },
    appInfo: info.appInfo,
    expandMacro: value => value
  }
}

const TOKEN_VARS = [
  'GH_TOKEN',
  'GITHUB_TOKEN',
  'GITHUB_RELEASE_TOKEN',
  'GITLAB_TOKEN',
  'KEYGEN_TOKEN',
  'BITBUCKET_TOKEN',
  'BT_TOKEN',
  'DESKTOP_RELEASE_TOKEN',
  'AWS_ACCESS_KEY_ID',
  'AWS_SECRET_ACCESS_KEY',
  'AWS_SESSION_TOKEN'
]
let savedEnv

beforeEach(() => {
  savedEnv = {}
  for (const key of TOKEN_VARS) {
    savedEnv[key] = process.env[key]
    delete process.env[key]
  }
})

afterEach(() => {
  vi.unstubAllEnvs()
  for (const key of TOKEN_VARS) {
    if (savedEnv[key] === undefined) delete process.env[key]
    else process.env[key] = savedEnv[key]
  }
})

describe('local desktop pack stays out of the publish path', () => {
  test('actual wrapper keeps one scalar never policy through the real parser in token-free tag CI', async () => {
    vi.stubEnv('CI', 'true')
    vi.stubEnv('GITHUB_ACTIONS', 'true')
    vi.stubEnv('GITHUB_REF_TYPE', 'tag')
    vi.stubEnv('GITHUB_REF_NAME', 'v2026.9.9.1')
    vi.stubEnv('GITHUB_REF', 'refs/tags/v2026.9.9.1')
    for (const key of [
      'TRAVIS_TAG',
      'APPVEYOR_REPO_TAG_NAME',
      'CIRCLE_TAG',
      'BITRISE_GIT_TAG',
      'CI_BUILD_TAG',
      'CI_COMMIT_TAG',
      'BITBUCKET_TAG',
      'TRAVIS_PULL_REQUEST',
      'CIRCLE_PULL_REQUEST',
      'BITRISE_PULL_REQUEST',
      'APPVEYOR_PULL_REQUEST_NUMBER',
      'GITHUB_BASE_REF',
      'PUBLISH_FOR_PULL_REQUEST'
    ])
      vi.stubEnv(key, undefined)
    assert.equal(getCiTag(), process.env.GITHUB_REF_NAME)
    for (const key of TOKEN_VARS) assert.equal(process.env[key], undefined)
    const duplicated = normalizeOptions(parser.parse(['--publish', 'never', '--publish', 'never']))
    assert.deepEqual(duplicated.publish, ['never', 'never'])
    assert.equal(publishManager(duplicated).isPublish, true)
    assert.equal(publishManager(normalizeOptions(parser.parse(['--publish', 'never']))).isPublish, false)
    const unrelated = [
      '--dir',
      '--config',
      'fixture-config.json',
      '--x64',
      '--prepackaged',
      'fixture-app',
      '-c.electronDist=fixture-electron'
    ]
    for (const flags of [
      [],
      ['--publish', 'never'],
      ['--publish', 'never', '--publish', 'never'],
      ['--publish=never'],
      ['-p', 'never'],
      ['-p=never'],
      ['--p', 'never'],
      ['--p=never'],
      ['--publish=never', '-p', 'never'],
      ['-mp', 'never'],
      ['-mwp=never'],
      ['--', '--publish', 'always']
    ]) {
      const { calls, exitCode } = await captureWrapper([...unrelated, ...flags])
      assert.equal(exitCode, 0)
      assert.equal(calls.length, 1)
      assert.equal(calls[0][0], process.execPath)
      assert.deepEqual(calls[0][2], { stdio: 'inherit' })
      const args = calls[0][1].slice(1)
      assert.ok(
        args.includes(`-c.electronDist=${path.join(path.dirname(require.resolve('electron/package.json')), 'dist')}`)
      )
      assert.deepEqual(args.slice(args.indexOf('--dir'), args.indexOf('--dir') + unrelated.length), unrelated)
      const options = normalizeOptions(parser.parse(args))
      assert.equal(options.publish, 'never')
      assert.equal(publishManager(options).isPublish, false)
    }
  })

  test.each([['-pd', 'fixture-app'], ['-pd=fixture-app']])(
    'wrapper preserves prepackaged alias %s',
    async (...flags) => {
      const { calls, exitCode } = await captureWrapper(['--dir', ...flags])
      assert.equal(exitCode, 0)
      assert.equal(calls.length, 1)
      const args = calls[0][1].slice(1)
      const prepackaged = flags.length === 2 ? ['--prepackaged', flags[1]] : ['--prepackaged=fixture-app']
      assert.deepEqual(args.slice(-prepackaged.length), prepackaged)
      const options = normalizeOptions(parser.parse(args))
      assert.equal(options.prepackaged, 'fixture-app')
      assert.equal(options.publish, 'never')
      assert.equal(publishManager(options).isPublish, false)
    }
  )

  test('wrapper rejects explicit publishing policies before spawning', async () => {
    for (const flags of [
      ['--publish', 'always'],
      ['--publish=onTag'],
      ['-p', 'onTagOrDraft'],
      ['-p=always'],
      ['--p=always'],
      ['--publish', 'never', '--publish', 'always'],
      ['-mp', 'always'],
      ['--publish'],
      ['--no-publish'],
      ['--no-p'],
      ['--publish.never=true'],
      ['-pnever'],
      ['--publish=']
    ]) {
      await assert.rejects(() => captureWrapper(flags), /Only --publish never/)
    }
  })

  test('wrapper preserves child exit, signal and spawn-error failure propagation', async () => {
    for (const [result, expected] of [
      [{ status: 7 }, 7],
      [{ status: null, signal: 'SIGTERM' }, 1],
      [{ error: new Error('fixture spawn failure') }, 1]
    ]) {
      assert.equal((await captureWrapper(['--dir'], result)).exitCode, expected)
    }
  })

  test('the pack script pins an explicit publish policy', () => {
    // electron-builder 26 infers `onTagOrDraft` from CI when --publish is
    // absent, and `hermes desktop` runs the pack with CI=1 (_npm_lifecycle_env).
    // v27 drops the implicit behavior, so being explicit is also forward-safe.
    const pack = desktopPkg.scripts.pack
    assert.match(pack, /--dir\b/)
    assert.match(pack, /--publish\s+never\b/)
  })

  test('publish resolution succeeds with a GitHub token present', async () => {
    // The #87758 failure mode: CI=1 makes isPublish true, a GITHUB_TOKEN in the
    // environment auto-selects the github provider, and the provider needs a
    // repository it cannot find — apps/desktop has no .git/config of its own
    // and app-builder-lib does not walk up to the workspace root.
    process.env.GITHUB_TOKEN = 'x'

    const configs = await getPublishConfigs(fakePackager(desktopPkg), null, null, /* errorIfCannot */ true)

    assert.ok(Array.isArray(configs) && configs.length > 0)
    assert.equal(configs[0].provider, 'github')
    assert.equal(configs[0].owner, 'NousResearch')
    assert.equal(configs[0].repo, 'hermes-agent')
  })

  test('a package without the repository field is what breaks resolution', async () => {
    // Guards the fix itself: proves the assertion above passes because of the
    // repository field, not because the throw is unreachable.
    process.env.GITHUB_TOKEN = 'x'
    const { repository, ...withoutRepository } = desktopPkg
    assert.ok(repository, 'apps/desktop/package.json must declare a repository')

    await assert.rejects(
      () => getPublishConfigs(fakePackager(withoutRepository), null, null, true),
      /Cannot detect repository by \.git\/config/
    )
  })

  test('resolution is quiet when no publish token is configured', async () => {
    const configs = await getPublishConfigs(fakePackager(desktopPkg), null, null, true)
    assert.deepEqual(configs, [])
  })
})
