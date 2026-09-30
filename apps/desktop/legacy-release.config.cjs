"use strict"

const fs = require('node:fs')
const path = require('node:path')
const { buildLegacyConfig } = require('./scripts/legacy-release-config.cjs')

const source = path.resolve(__dirname, '../..')
const metadata = JSON.parse(fs.readFileSync(path.join(source, 'desktop-release-source.json'), 'utf8'))
if (metadata.commit !== process.env.DESKTOP_SOURCE_COMMIT ||
    metadata.version !== process.env.DESKTOP_RELEASE_VERSION ||
    metadata.repository !== process.env.DESKTOP_SOURCE_REPOSITORY ||
    metadata.tag !== process.env.DESKTOP_RELEASE_TAG) {
  throw new Error('Legacy release identity does not match the verified checkout')
}

module.exports = buildLegacyConfig(require('./electron-builder.config.cjs'), metadata, process.platform, process.env.TARGET_ARCH || process.arch)
