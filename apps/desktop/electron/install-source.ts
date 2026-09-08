export function installRepository(stamp: { repository?: string } | null | undefined): string {
  const repository = stamp?.repository ?? 'NousResearch/hermes-agent'

  if (!/^[A-Za-z0-9][A-Za-z0-9-]*\/[A-Za-z0-9][A-Za-z0-9._-]*$/.test(repository)) {
    throw new Error('Invalid desktop source repository')
  }

  return repository
}

export function assertInstallOrigin(repository: string, origin: string): void {
  const canonical = origin
    .trim()
    .replace(/^git@github\.com:/, '')
    .replace(/^(https:\/\/|ssh:\/\/git@)github\.com\//, '')
    .replace(/\.git\/?$/, '')
    .replace(/\/$/, '')

  if (canonical.toLowerCase() !== repository.toLowerCase()) {
    throw new Error(
      'Desktop and managed backend repositories differ. Connect to the existing backend or use a separate install directory.'
    )
  }
}
