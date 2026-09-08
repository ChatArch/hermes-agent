import { execFileSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import { mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import path from 'node:path'
import { describe, expect, test } from 'vitest'
import yaml from 'js-yaml'
import { assetName, collectAssets, targets, validTag, validateBundle, validateSource } from './desktop-release.mjs'
import { publishRelease } from './publish-desktop-release.mjs'
import { validateNativeBinary, validatePackagedPaths } from './validate-release-package.mjs'

const temp = () => mkdtempSync(path.join(process.env.TMPDIR || process.cwd(), 'desktop-release-test-'))
const metadata = {
  schemaVersion: 1,
  tag: 'v2026.9.8',
  version: '1.2.3',
  repository: 'ChatArch/hermes-agent',
  commit: 'a'.repeat(40),
  event: 'push',
  signing: 'unsigned'
}

async function fixture(root) {
  const merged = path.join(root, 'merged')
  mkdirSync(merged)
  for (const target of targets) {
    const input = path.join(root, `${target.platform}-${target.arch}-input`)
    const output = path.join(root, `${target.platform}-${target.arch}-output`)
    mkdirSync(input)
    for (const format of target.formats)
      writeFileSync(path.join(input, assetName(metadata, target, format)), `fixture ${format} ${target.arch}`)
    await collectAssets(metadata, target, input, output, { ...metadata, dirty: false })
    const { readdirSync, copyFileSync } = await import('node:fs')
    for (const name of readdirSync(output)) copyFileSync(path.join(output, name), path.join(merged, name))
  }
  return merged
}

describe('desktop release contracts', () => {
  test('native executable headers must identify the requested architecture', () => {
    const root = temp()
    const file = path.join(root, 'executable')
    try {
      for (const target of targets) {
        const header = Buffer.alloc(512)
        if (target.platform === 'darwin') {
          header.writeUInt32LE(0xfeedfacf, 0)
          header.writeUInt32LE(target.arch === 'arm64' ? 0x100000c : 0x1000007, 4)
        } else if (target.platform === 'linux') {
          header.set([0x7f, 0x45, 0x4c, 0x46, 2])
          header.writeUInt16LE(62, 18)
        } else {
          header.write('MZ')
          header.writeUInt32LE(128, 60)
          header.writeUInt32LE(0x4550, 128)
          header.writeUInt16LE(0x8664, 132)
        }
        writeFileSync(file, header)
        expect(() => validateNativeBinary(file, target.platform, target.arch)).not.toThrow()
        header.fill(0)
        writeFileSync(file, header)
        expect(() => validateNativeBinary(file, target.platform, target.arch)).toThrow('architecture')
      }
    } finally {
      rmSync(root, { recursive: true, force: true })
    }
  })
  test('only valid calendar tags on approved ancestry and matching versions resolve', () => {
    const root = temp()
    const git = (...args) =>
      execFileSync('git', args, { cwd: root, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }).trim()
    try {
      git('init', '-b', 'main')
      git('config', 'user.name', 'Release Fixture')
      git('config', 'user.email', 'fixture@example.invalid')
      mkdirSync(path.join(root, 'hermes_cli'))
      writeFileSync(path.join(root, 'pyproject.toml'), '[project]\nversion = "1.2.3"\n')
      writeFileSync(path.join(root, 'hermes_cli/__init__.py'), '__version__ = "1.2.3"\n')
      git('add', '.')
      git('commit', '-m', 'fixture source')
      git('update-ref', 'refs/remotes/origin/main', 'HEAD')
      git('tag', '-a', metadata.tag, '-m', 'fixture annotated tag')
      const source = { ...metadata, commit: git('rev-parse', 'HEAD') }
      expect(validateSource(root, source)).toEqual(source)
      expect(validTag('v2024.2.29.2')).toBe(true)
      for (const tag of ['v2026.2.29', 'v2026.09.8', 'v2026.9.8.0', 'v1.2.3', 'v2026.9.8/evil'])
        expect(validTag(tag)).toBe(false)
      git('checkout', '-b', 'unrelated-feature')
      git('commit', '--allow-empty', '-m', 'unreviewed work')
      git('tag', 'v2026.9.8.1')
      expect(() => validateSource(root, { ...source, tag: 'v2026.9.8.1', commit: git('rev-parse', 'HEAD') })).toThrow(
        'ancestry'
      )
      expect(() => validateSource(root, source)).toThrow('commit mismatch')
      expect(() =>
        validateSource(root, {
          ...source,
          event: 'pull_request',
          tag: 'preview-pr-7',
          commit: git('rev-parse', 'HEAD')
        })
      ).not.toThrow()
      writeFileSync(path.join(root, 'pyproject.toml'), 'version = "1.2.4"\n')
      expect(() =>
        validateSource(root, {
          ...source,
          event: 'pull_request',
          tag: 'preview-pr-7',
          commit: git('rev-parse', 'HEAD')
        })
      ).toThrow('SemVer')
    } finally {
      rmSync(root, { recursive: true, force: true })
    }
  })

  test('complete assets verify; missing, tampered and mismatched sources fail closed', async () => {
    expect(() => assetName({ ...metadata, version: '../../private' }, targets[0], 'dmg')).toThrow('Unsafe')
    expect(() => validatePackagedPaths(['/dist/main.mjs', '/assets/icon.png'])).not.toThrow()
    for (const name of ['/dist/.env', '/profiles/work/config.yaml', '/assets/.cache/private', '/tokens.json'])
      expect(() => validatePackagedPaths([name])).toThrow('Private state')
    const root = temp()
    try {
      const merged = await fixture(root)
      const bundle = path.join(root, 'bundle')
      const assets = await validateBundle(metadata, merged, bundle)
      expect(assets.map(asset => asset.name).sort()).toEqual(
        targets.flatMap(target => target.formats.map(format => assetName(metadata, target, format))).sort()
      )
      expect(readFileSync(path.join(bundle, 'SHA256SUMS'), 'utf8')).toContain('release-manifest.json')
      await expect(
        validateBundle({ ...metadata, commit: 'b'.repeat(40) }, merged, path.join(root, 'wrong-source'))
      ).rejects.toThrow('commit')
      const first = assets[0]
      writeFileSync(path.join(merged, first.name), 'tampered')
      await expect(validateBundle(metadata, merged, path.join(root, 'tampered'))).rejects.toThrow('Checksum')
      rmSync(path.join(merged, first.name))
      await expect(validateBundle(metadata, merged, path.join(root, 'missing'))).rejects.toThrow()
    } finally {
      rmSync(root, { recursive: true, force: true })
    }
  })

  test('publisher never writes on PRs, resumes only identical drafts and never overwrites', async () => {
    const root = temp()
    try {
      const directory = path.join(root, 'bundle')
      await validateBundle(metadata, await fixture(root), directory)
      let release = null
      let assets = []
      let writes = 0
      let failUpload = true
      let hideByTag = false
      let pages = null
      let mutateReadback = value => value
      let mutateAssets = value => value
      let mutatePatch = value => value
      let finalReadback = false
      const calls = []
      const releaseUrl = `https://github.com/${metadata.repository}/releases/tag/${metadata.tag}`
      const request = async (method, endpoint, data) => {
        calls.push([method, endpoint])
        if (method !== 'GET') writes++
        if (endpoint.includes('/git/ref/')) return { object: { type: 'commit', sha: metadata.commit } }
        if (method === 'GET' && endpoint.includes('/releases/tags/')) {
          finalReadback = false
          return hideByTag ? null : release
        }
        if (method === 'GET' && endpoint.includes('/releases?')) {
          const page = Number(new URL(`https://api.github.com${endpoint}`).searchParams.get('page'))
          return pages ? pages[page - 1] : release ? [release] : []
        }
        if (method === 'GET' && endpoint.endsWith('/releases/1')) {
          finalReadback = true
          return mutateReadback(release)
        }
        if (method === 'GET' && endpoint.includes('/assets?') && finalReadback) return mutateAssets(assets)
        if (method === 'GET') return assets
        if (method === 'POST')
          return (release = {
            ...data,
            id: 1,
            html_url: releaseUrl,
            upload_url: 'https://uploads.github.com/repos/ChatArch/hermes-agent/releases/1/assets{?name}'
          })
        if (method === 'UPLOAD') {
          if (failUpload && assets.length === 1) throw new Error('fixture interrupted upload')
          const asset = {
            name: new URL(endpoint).searchParams.get('name'),
            state: 'uploaded',
            digest: `sha256:${createHash('sha256')
              .update(Buffer.from(await data.arrayBuffer()))
              .digest('hex')}`
          }
          assets.push(asset)
          return asset
        }
        release = { ...release, ...data }
        return mutatePatch(release)
      }
      const options = { directory, ...metadata, request }
      await expect(publishRelease({ ...options, event: 'pull_request' })).rejects.toThrow('tag-only')
      expect(writes).toBe(0)
      await expect(publishRelease(options)).rejects.toThrow('interrupted')
      expect(release.draft).toBe(true)
      failUpload = false
      hideByTag = true
      pages = [Array.from({ length: 100 }, (_, index) => ({ tag_name: `other-${index}` })), [release]]
      expect(await publishRelease(options)).toBe(releaseUrl)
      expect(calls.slice(-2)).toEqual([
        ['GET', `/repos/${metadata.repository}/releases/1`],
        ['GET', `/repos/${metadata.repository}/releases/1/assets?per_page=100`]
      ])
      expect(
        calls.some(([method, endpoint]) => method === 'GET' && endpoint.endsWith('/releases?per_page=100&page=2'))
      ).toBe(true)
      expect(calls.filter(([method]) => method === 'POST')).toHaveLength(1)
      hideByTag = false
      expect(release.draft).toBe(false)
      const before = writes
      expect(await publishRelease(options)).toBe(releaseUrl)
      expect(writes).toBe(before)
      expect(calls.slice(-2)).toEqual([
        ['GET', `/repos/${metadata.repository}/releases/1`],
        ['GET', `/repos/${metadata.repository}/releases/1/assets?per_page=100`]
      ])
      for (const change of [
        { draft: true },
        { target_commitish: 'b'.repeat(40) },
        { tag_name: 'v2026.9.9' },
        { body: 'changed' },
        { id: 2 },
        { html_url: 'https://example.invalid/' }
      ]) {
        mutateReadback = value => ({ ...value, ...change })
        await expect(publishRelease(options)).rejects.toThrow()
      }
      mutateReadback = value => value
      for (const mutation of [
        value => value.slice(1),
        value => [...value.slice(1), value[1]],
        value => value.map((asset, index) => (index ? asset : { ...asset, name: 'unexpected.zip' })),
        value => value.map((asset, index) => (index ? asset : { ...asset, digest: 'sha256:wrong' })),
        value => value.map((asset, index) => (index ? asset : { ...asset, state: 'new' }))
      ]) {
        mutateAssets = mutation
        await expect(publishRelease(options)).rejects.toThrow('asset readback')
      }
      mutateAssets = value => value
      expect(writes).toBe(before)
      for (const change of [{ draft: true }, { target_commitish: 'b'.repeat(40) }]) {
        release = { ...release, draft: true }
        mutatePatch = value => ({ ...value, ...change })
        await expect(publishRelease(options)).rejects.toThrow()
      }
      mutatePatch = value => value
      release = { ...release, draft: true }
      mutateAssets = value => value.slice(1)
      await expect(publishRelease(options)).rejects.toThrow('asset readback')
      expect(release.draft).toBe(false)
      mutateAssets = value => value
      hideByTag = true
      pages = [[release, { ...release, id: 2 }]]
      const afterPatchChecks = writes
      await expect(publishRelease(options)).rejects.toThrow('Ambiguous')
      pages = Array.from({ length: 10 }, () => Array.from({ length: 100 }, () => ({ tag_name: 'other' })))
      await expect(publishRelease(options)).rejects.toThrow('pagination limit')
      pages = [[{ ...release, body: 'unowned draft' }]]
      await expect(publishRelease(options)).rejects.toThrow('not owned')
      expect(writes).toBe(afterPatchChecks)
      hideByTag = false
      assets[0].digest = 'sha256:wrong'
      await expect(publishRelease(options)).rejects.toThrow('never overwritten')
      expect(writes).toBe(afterPatchChecks)
    } finally {
      rmSync(root, { recursive: true, force: true })
    }
  })

  test('workflow isolates publication from read-only native PR builds', () => {
    const workflow = yaml.load(
      readFileSync(new URL('../../../.github/workflows/desktop-release.yml', import.meta.url), 'utf8')
    )
    expect(workflow.permissions).toEqual({ contents: 'read' })
    expect(workflow.on.pull_request).toBeTruthy()
    expect(workflow.on.pull_request_target).toBeUndefined()
    expect(workflow.concurrency['cancel-in-progress']).toBe("${{ github.event_name == 'pull_request' }}")
    expect(workflow.jobs.publish.if).toContain("github.event_name == 'push'")
    expect(workflow.jobs.publish.needs).toContain('validate')
    expect(workflow.jobs.publish.permissions).toEqual({ contents: 'write' })
    for (const [name, job] of Object.entries(workflow.jobs)) {
      if (name !== 'publish') expect(job.permissions?.contents).not.toBe('write')
      for (const step of job.steps) {
        expect(JSON.stringify(step)).not.toContain('secrets.')
        if (step.uses) expect(step.uses).toMatch(/@[a-f0-9]{40}$/)
      }
    }
    expect(workflow.jobs.build.strategy['fail-fast']).toBe(false)
  })
})
