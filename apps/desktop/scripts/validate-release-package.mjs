import { createRequire } from 'node:module'
import { closeSync, lstatSync, openSync, readFileSync, readSync, readdirSync } from 'node:fs'
import path from 'node:path'
import os from 'node:os'

const require = createRequire(import.meta.url)

export function validateNativeBinary(file, platform, arch) {
  const header = Buffer.alloc(4096)
  const descriptor = openSync(file, 'r')
  try {
    readSync(descriptor, header, 0, header.length, 0)
  } finally {
    closeSync(descriptor)
  }
  const validators = {
    linux: () =>
      header.subarray(0, 4).equals(Buffer.from([0x7f, 0x45, 0x4c, 0x46])) &&
      header[4] === 2 &&
      header.readUInt16LE(18) === 62 &&
      arch === 'x64',
    win32: () => {
      const offset = header.readUInt32LE(60)
      return (
        header.toString('ascii', 0, 2) === 'MZ' &&
        offset < header.length - 6 &&
        header.readUInt32LE(offset) === 0x4550 &&
        header.readUInt16LE(offset + 4) === 0x8664 &&
        arch === 'x64'
      )
    },
    darwin: () =>
      header.readUInt32LE(0) === 0xfeedfacf && header.readUInt32LE(4) === (arch === 'arm64' ? 0x100000c : 0x1000007)
  }
  if (!validators[platform]?.()) throw new Error('Packaged executable architecture mismatch')
}

export function validatePackagedPaths(paths) {
  for (const name of paths) {
    const parts = name.replaceAll('\\', '/').split('/').filter(Boolean)
    if (
      parts.some(part =>
        /^(\.env(?:\..*)?|\.hermes|profiles?|tokens?\.json|credentials?\.json|\.cache|__pycache__|\.git)$/i.test(part)
      )
    ) {
      throw new Error(`Private state in package: ${name}`)
    }
  }
}

export function validateReleasePackage(resources, metadata, sourceRoot) {
  const asar = require('@electron/asar')
  const archive = path.join(resources, 'app.asar')
  const paths = asar.listPackage(archive)
  validatePackagedPaths(paths)
  const packaged = JSON.parse(asar.extractFile(archive, 'package.json').toString())
  if (
    packaged.version !== metadata.version ||
    packaged.repository?.url !== `https://github.com/${metadata.repository}.git`
  ) {
    throw new Error('Packaged version/repository mismatch')
  }
  const forbidden = [sourceRoot, os.homedir()].flatMap(value => [
    value,
    value.replaceAll('\\', '/'),
    value.replaceAll('\\', '\\\\')
  ])
  const inspect = (name, content) => {
    if (
      /\.(m?js|cjs|json|html|css|map|txt)$/i.test(name) &&
      forbidden.some(value => content.includes(Buffer.from(value)))
    ) {
      throw new Error(`Private build-host path in package: ${name}`)
    }
  }
  for (const name of paths) {
    if (/\.(m?js|cjs|json|html|css|map|txt)$/i.test(name))
      inspect(name, asar.extractFile(archive, name.replace(/^\//, '')))
  }
  const walk = directory => {
    for (const entry of readdirSync(directory, { withFileTypes: true })) {
      const file = path.join(directory, entry.name)
      const relative = path.relative(resources, file)
      validatePackagedPaths([relative])
      if (lstatSync(file).isSymbolicLink()) throw new Error(`Unexpected resource symlink: ${relative}`)
      if (entry.isDirectory()) walk(file)
      else if (entry.name !== 'app.asar') inspect(relative, readFileSync(file))
    }
  }
  walk(resources)
}
