"use strict"

// Legacy ChatArch installers share the upstream app but keep a separate,
// unsigned NSIS/MSI recipe. The default PM/MSIX recipe is never mutated.
function buildLegacyConfig(base, metadata, platform) {
  if (!base || !['win32', 'darwin', 'linux'].includes(platform) ||
      !/^[0-9]+\.[0-9]+\.[0-9]+$/.test(metadata?.version || '') ||
      !/^[A-Za-z0-9-]+\/[A-Za-z0-9._-]+$/.test(metadata?.repository || '') ||
      !/^(?:v[0-9]{4}\.|preview-pr-)/.test(metadata?.tag || '')) {
    throw new Error('Invalid legacy desktop release identity')
  }
  const { msix, ...config } = base
  return {
    ...config,
    artifactName: `ChatArch-Hermes-${metadata.version}-${metadata.tag}-${platform}-\${arch}-unsigned.\${ext}`,
    extraMetadata: {
      ...base.extraMetadata,
      version: metadata.version,
      homepage: `https://github.com/${metadata.repository}#readme`,
      repository: { type: 'git', url: `https://github.com/${metadata.repository}.git` }
    },
    extraResources: [...(base.extraResources || []), { from: '../../LICENSE', to: 'Hermes-LICENSE.txt' }],
    mac: { ...base.mac, sign: null },
    win: { ...base.win, target: ['nsis', 'msi'], sign: null },
    nsis: { oneClick: false, allowToChangeInstallationDirectory: true,
      perMachine: false, shortcutName: 'Hermes', uninstallDisplayName: 'Hermes',
      warningsAsErrors: false },
    linux: { ...base.linux, target: ['AppImage', 'deb', 'rpm'],
      maintainer: 'ChatArch <noreply@github.com>' }
  }
}

module.exports = { buildLegacyConfig }
