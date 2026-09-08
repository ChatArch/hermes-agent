import { execFileSync } from 'node:child_process'
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import path from 'node:path'

import { expect, test } from 'vitest'

import { buildPinArgs, buildPosixPinArgs, resolveInstallScript } from './bootstrap-runner'
import { assertInstallOrigin, installRepository } from './install-source'

// Allow the 30s native manifest probe plus bounded assertions and cleanup.
test(
  'repository identity reaches download, cache and both native installers without upstream fallback',
  { timeout: 40_000 },
  async () => {
    const home = mkdtempSync(path.join(process.env.TMPDIR || process.cwd(), 'desktop-source-test-'))
    const stamp = { repository: 'ChatArch/hermes-agent', commit: 'c'.repeat(40), branch: 'main' }

    try {
      const script = path.resolve('../../scripts', process.platform === 'win32' ? 'install.ps1' : 'install.sh')
      const command = process.platform === 'win32' ? 'powershell.exe' : 'bash'

      const args =
        process.platform === 'win32'
          ? [
              '-NoProfile',
              '-ExecutionPolicy',
              'Bypass',
              '-File',
              script,
              '-Manifest',
              '-HermesHome',
              home,
              '-InstallDir',
              path.join(home, 'backend'),
              ...buildPinArgs(stamp)
            ]
          : [
              script,
              '--manifest',
              ...buildPosixPinArgs({ installStamp: stamp, activeRoot: path.join(home, 'backend'), hermesHome: home })
            ]

      const output = execFileSync(command, args, {
        env: { ...process.env, HOME: home, HERMES_HOME: home },
        encoding: 'utf8',
        timeout: 30_000
      })

      const manifest = JSON.parse(output.trim().split(/\r?\n/).at(-1)!)
      expect(manifest.stages.some((stage: { name: string }) => stage.name === 'repository')).toBe(true)
      expect(buildPinArgs(stamp)).toEqual([
        '-Repository',
        stamp.repository,
        '-Commit',
        stamp.commit,
        '-Branch',
        stamp.branch
      ])
      expect(buildPosixPinArgs({ installStamp: stamp, activeRoot: home, hermesHome: home })).toContain(stamp.repository)

      const result = await resolveInstallScript({
        installStamp: stamp,
        sourceRepoRoot: null,
        hermesHome: home,
        emit: () => {},
        _download: async (ref, destination, repository) => {
          expect(repository).toBe(stamp.repository)
          expect(ref).toBe(stamp.commit)
          expect(destination).toContain('ChatArch_hermes-agent')
          mkdirSync(path.dirname(destination), { recursive: true })
          writeFileSync(destination, 'fixture installer')
        }
      })

      expect(result.source).toBe('download')
      mkdirSync(path.join(home, 'hermes-agent/scripts'), { recursive: true })
      writeFileSync(path.join(home, 'hermes-agent/scripts/install.sh'), 'wrong source fixture')
      writeFileSync(path.join(home, 'hermes-agent/scripts/install.ps1'), 'wrong source fixture')
      await expect(
        resolveInstallScript({
          installStamp: { ...stamp, commit: 'd'.repeat(40) },
          sourceRepoRoot: null,
          hermesHome: home,
          emit: () => {},
          _download: async () => {
            throw new Error('source unavailable')
          }
        })
      ).rejects.toThrow('source unavailable')
    } finally {
      rmSync(home, { recursive: true, force: true })
    }
  }
)

test('source identities reject injection and refuse to retarget existing upstream installs', () => {
  for (const repository of ['../other/repo', 'ChatArch/repo?token=secret', 'https://github.com/a/b']) {
    expect(() => installRepository({ repository })).toThrow('Invalid')
  }

  expect(() => assertInstallOrigin('ChatArch/hermes-agent', 'git@github.com:ChatArch/hermes-agent.git')).not.toThrow()
  expect(() =>
    assertInstallOrigin('ChatArch/hermes-agent', 'https://github.com/NousResearch/hermes-agent.git')
  ).toThrow('differ')
})
