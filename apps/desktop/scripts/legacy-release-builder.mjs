import path from 'node:path'
import { spawnSync } from 'node:child_process'
import { createRequire } from 'node:module'
import { isMain } from './utils.mjs'
import { targets } from './desktop-release.mjs'
import { pinnedPackageRoot } from './prepare-packaging-tools.mjs'

const source = path.resolve(import.meta.dirname, '../../..')
const desktop = path.join(source, 'apps/desktop')

export function legacyBuilderArgs(platform, arch) {
  const target = targets.find(entry => entry.platform === platform && entry.arch === arch)
  if (!target) throw new Error('Unsupported legacy installer target')
  return ['--config', 'legacy-release.config.cjs', `--${target.builder}`, `--${arch}`, '--publish', 'never']
}

export function runLegacyBuilder(platform, arch, spawn = spawnSync) {
  const root = pinnedPackageRoot(source, 'electron-builder')
  pinnedPackageRoot(source, 'app-builder-lib')
  const require = createRequire(path.join(root, 'package.json'))
  const bin = require(path.join(root, 'package.json')).bin['electron-builder']
  const result = spawn(process.execPath, [path.join(root, bin), ...legacyBuilderArgs(platform, arch)], {
    cwd: desktop, stdio: 'inherit', env: { ...process.env, CSC_IDENTITY_AUTO_DISCOVERY: 'false' }
  })
  if (result.error) throw result.error
  return result.status ?? 1
}

if (isMain(import.meta.url)) process.exitCode = runLegacyBuilder(process.argv[2], process.argv[3])
