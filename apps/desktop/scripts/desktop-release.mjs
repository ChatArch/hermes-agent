import { execFileSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import { createReadStream, copyFileSync, lstatSync, mkdirSync, readFileSync, readdirSync, writeFileSync } from 'node:fs'
import path from 'node:path'
import { isMain } from './utils.mjs'
import { validateNativeBinary, validateReleasePackage } from './validate-release-package.mjs'

export const targets = [
  { platform: 'darwin', arch: 'arm64', runner: 'macos-15', formats: ['dmg', 'zip'], builder: 'mac' },
  { platform: 'darwin', arch: 'x64', runner: 'macos-15-intel', formats: ['dmg', 'zip'], builder: 'mac' },
  { platform: 'win32', arch: 'x64', runner: 'windows-2025', formats: ['exe', 'msi'], builder: 'win' },
  { platform: 'linux', arch: 'x64', runner: 'ubuntu-24.04', formats: ['AppImage', 'deb', 'rpm'], builder: 'linux' }
]
const readJson = file => JSON.parse(readFileSync(file, 'utf8'))
const saveJson = (file, data) => writeFileSync(file, JSON.stringify(data, null, 2) + '\n')
const git = (root, ...args) =>
  execFileSync('git', args, { cwd: root, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }).trim()
const requireThat = (condition, message) => {
  if (!condition) throw new Error(message)
}

export function validTag(tag) {
  const match = /^v(\d{4})\.([1-9]|1[0-2])\.([1-9]|[12]\d|3[01])(?:\.([1-9]\d*))?$/.exec(tag)
  if (!match) return false
  const date = new Date(Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3])))
  return date.getUTCMonth() === Number(match[2]) - 1 && date.getUTCDate() === Number(match[3])
}

export function validateSource(root, { event, tag, commit, repository }) {
  requireThat(/^[A-Za-z0-9][A-Za-z0-9-]*\/[A-Za-z0-9][A-Za-z0-9._-]*$/.test(repository), 'Invalid repository')
  requireThat(/^[a-f0-9]{40}$/.test(commit) && git(root, 'rev-parse', 'HEAD') === commit, 'Source commit mismatch')
  requireThat(['push', 'pull_request'].includes(event), 'Unsupported release event')
  if (event === 'push') {
    requireThat(repository === 'ChatArch/hermes-agent' && validTag(tag), 'Invalid release repository or CalVer tag')
    requireThat(git(root, 'rev-parse', `refs/tags/${tag}^{commit}`) === commit, 'Tag does not identify source')
    const allowed = ['main', 'release'].some(branch => {
      try {
        git(root, 'merge-base', '--is-ancestor', commit, `refs/remotes/origin/${branch}`)
        return true
      } catch {
        return false
      }
    })
    requireThat(allowed, 'Tag is not on main or release ancestry')
  } else {
    requireThat(/^preview-pr-[1-9]\d*$/.test(tag), 'Invalid preview identity')
  }
  const version = /^version\s*=\s*"([^"]+)"/m.exec(readFileSync(path.join(root, 'pyproject.toml'), 'utf8'))?.[1]
  const cliVersion = /^__version__\s*=\s*"([^"]+)"/m.exec(
    readFileSync(path.join(root, 'hermes_cli/__init__.py'), 'utf8')
  )?.[1]
  requireThat(/^\d+\.\d+\.\d+$/.test(version) && version === cliVersion, 'Backend SemVer mismatch')
  requireThat(git(root, 'status', '--porcelain', '-uno') === '', 'Release source has tracked modifications')
  return { schemaVersion: 1, tag, version, repository, commit, event, signing: 'unsigned' }
}

export function assetName(metadata, target, format) {
  requireThat(
    /^\d+\.\d+\.\d+$/.test(metadata.version) && (validTag(metadata.tag) || /^preview-pr-[1-9]\d*$/.test(metadata.tag)),
    'Unsafe asset identity'
  )
  requireThat(
    targets.some(
      entry =>
        entry.platform === target.platform &&
        entry.arch === target.arch &&
        (entry.formats.includes(format) || format === '${ext}')
    ),
    'Unsafe asset target'
  )
  return `ChatArch-Hermes-${metadata.version}-${metadata.tag}-${target.platform}-${target.arch}-unsigned.${format}`
}

export async function hashFile(file) {
  const hash = createHash('sha256')
  for await (const chunk of createReadStream(file)) hash.update(chunk)
  return hash.digest('hex')
}

function regularNonempty(file) {
  const stat = lstatSync(file)
  requireThat(
    stat.isFile() && !stat.isSymbolicLink() && stat.size > 0,
    `Not a nonempty regular file: ${path.basename(file)}`
  )
  return stat.size
}

export async function collectAssets(metadata, target, input, output, stamp) {
  requireThat(
    stamp.commit === metadata.commit &&
      stamp.repository === metadata.repository &&
      stamp.version === metadata.version &&
      !stamp.dirty,
    'Packaged source stamp mismatch'
  )
  mkdirSync(output, { recursive: true })
  requireThat(readdirSync(output).length === 0, 'Asset output must be empty')
  const assets = []
  for (const format of target.formats) {
    const name = assetName(metadata, target, format)
    const file = path.join(input, name)
    const size = regularNonempty(file)
    copyFileSync(file, path.join(output, name))
    assets.push({
      name,
      size,
      sha256: await hashFile(file),
      platform: target.platform,
      arch: target.arch,
      format,
      signing: 'unsigned'
    })
  }
  saveJson(path.join(output, `${target.platform}-${target.arch}.json`), { ...metadata, assets })
}

export async function validateBundle(metadata, input, output) {
  mkdirSync(output, { recursive: true })
  requireThat(readdirSync(output).length === 0, 'Bundle output must be empty')
  const expectedFiles = new Set()
  const assets = []
  for (const target of targets) {
    const manifestName = `${target.platform}-${target.arch}.json`
    expectedFiles.add(manifestName)
    const manifest = readJson(path.join(input, manifestName))
    for (const key of Object.keys(metadata)) requireThat(manifest[key] === metadata[key], `Inconsistent ${key}`)
    requireThat(manifest.assets.length === target.formats.length, 'Incomplete platform assets')
    for (const format of target.formats) {
      const name = assetName(metadata, target, format)
      const asset = manifest.assets.find(entry => entry.name === name)
      requireThat(
        asset &&
          asset.platform === target.platform &&
          asset.arch === target.arch &&
          asset.format === format &&
          asset.signing === 'unsigned',
        `Invalid asset ${name}`
      )
      const file = path.join(input, name)
      requireThat(
        regularNonempty(file) === asset.size && (await hashFile(file)) === asset.sha256,
        `Checksum mismatch: ${name}`
      )
      expectedFiles.add(name)
      copyFileSync(file, path.join(output, name))
      assets.push(asset)
    }
  }
  requireThat(
    readdirSync(input).every(name => expectedFiles.has(name)),
    'Unexpected artifact files'
  )
  saveJson(path.join(output, 'release-manifest.json'), { ...metadata, assets })
  const notes = `# ChatArch Hermes ${metadata.tag}\n\nSoftware SemVer: ${metadata.version}\nSource: ${metadata.repository}@${metadata.commit}\n\nCommunity fork build, not an official Nous Research signed distribution. All installers are unsigned and macOS builds are not notarized. Hermes attribution and licenses are preserved.\n\nmacOS: DMG and ZIP for arm64 and x64. Windows x64: EXE (NSIS) and MSI. Linux x64: AppImage, DEB and RPM.\n\nOnly the Electron shell/UI is included. First launch needs network access to install the Python backend from the source commit above, or connect to an existing backend. No user configuration or credentials are included. See docs/desktop-releases.md in the tagged source for verification and first-run details.\n`
  writeFileSync(path.join(output, 'release-notes.md'), notes)
  const names = [...assets.map(asset => asset.name), 'release-manifest.json', 'release-notes.md'].sort()
  const sums = await Promise.all(names.map(async name => `${await hashFile(path.join(output, name))}  ${name}\n`))
  writeFileSync(path.join(output, 'SHA256SUMS'), sums.join(''))
  return assets
}

function prepare(root) {
  const metadata = validateSource(root, {
    event: process.env.GITHUB_EVENT_NAME,
    tag: process.env.DESKTOP_RELEASE_TAG,
    commit: process.env.DESKTOP_SOURCE_COMMIT,
    repository: process.env.DESKTOP_SOURCE_REPOSITORY
  })
  saveJson(path.join(root, 'desktop-release-source.json'), metadata)
  if (process.env.GITHUB_OUTPUT)
    writeFileSync(
      process.env.GITHUB_OUTPUT,
      `matrix=${JSON.stringify({ include: targets })}\nversion=${metadata.version}\n`,
      { flag: 'a' }
    )
  return metadata
}

async function main() {
  const [command, platform, arch] = process.argv.slice(2)
  const root = process.cwd()
  if (command === 'prepare') return prepare(root)
  const metadata = readJson(path.join(root, 'desktop-release-source.json'))
  if (command === 'bundle')
    return validateBundle(metadata, path.join(root, 'desktop-assets'), path.join(root, 'desktop-bundle'))
  const target = targets.find(entry => entry.platform === platform && entry.arch === arch)
  requireThat(target && process.platform === platform && process.arch === arch, 'Native runner architecture mismatch')
  const desktop = path.join(root, 'apps/desktop')
  if (command === 'configure') {
    const base = readJson(path.join(desktop, 'package.json')).build
    const config = {
      ...base,
      appId: 'org.chatarch.hermes',
      extraResources: [...base.extraResources, { from: '../../LICENSE', to: 'Hermes-LICENSE.txt' }],
      artifactName: assetName(metadata, target, '${ext}'),
      extraMetadata: {
        version: metadata.version,
        homepage: `https://github.com/${metadata.repository}#readme`,
        repository: { type: 'git', url: `https://github.com/${metadata.repository}.git` }
      },
      mac: { ...base.mac, identity: null },
      linux: { ...base.linux, maintainer: 'ChatArch <noreply@github.com>' }
    }
    saveJson(path.join(desktop, 'desktop-release-config.json'), config)
  } else if (command === 'collect') {
    const resourceRoot =
      platform === 'darwin'
        ? path.join(desktop, 'release', arch === 'arm64' ? 'mac-arm64' : 'mac', 'Hermes.app/Contents/Resources')
        : path.join(desktop, 'release', platform === 'win32' ? 'win-unpacked' : 'linux-unpacked', 'resources')
    const executable =
      platform === 'darwin'
        ? path.join(resourceRoot, '../MacOS/Hermes')
        : path.join(resourceRoot, '..', platform === 'win32' ? 'Hermes.exe' : 'Hermes')
    validateNativeBinary(executable, platform, arch)
    validateReleasePackage(resourceRoot, metadata, root)
    await collectAssets(
      metadata,
      target,
      path.join(desktop, 'release'),
      path.join(root, 'desktop-assets'),
      readJson(path.join(resourceRoot, 'install-stamp.json'))
    )
  } else throw new Error('Unknown release command')
}

if (isMain(import.meta.url)) await main()
