import { openAsBlob, readFileSync, readdirSync } from 'node:fs'
import path from 'node:path'
import { assetName, hashFile, targets, validTag } from './desktop-release.mjs'
import { isMain } from './utils.mjs'

export async function publishRelease({ directory, event, repository, tag, commit, request }) {
  if (event !== 'push' || repository !== 'ChatArch/hermes-agent' || !validTag(tag))
    throw new Error('Publishing is tag-only in ChatArch/hermes-agent')
  const manifest = JSON.parse(readFileSync(path.join(directory, 'release-manifest.json'), 'utf8'))
  if (
    manifest.repository !== repository ||
    manifest.tag !== tag ||
    manifest.commit !== commit ||
    manifest.event !== event ||
    manifest.signing !== 'unsigned'
  )
    throw new Error('Release source mismatch')
  const names = targets.flatMap(target => target.formats.map(format => assetName(manifest, target, format)))
  if (
    manifest.assets.length !== names.length ||
    names.some(name => !manifest.assets.some(asset => asset.name === name))
  )
    throw new Error('Incomplete release')
  for (const asset of manifest.assets) {
    if ((await hashFile(path.join(directory, asset.name))) !== asset.sha256)
      throw new Error('Release checksum mismatch')
  }
  names.push('release-manifest.json', 'release-notes.md')
  const sums = await Promise.all(
    [...names].sort().map(async name => `${await hashFile(path.join(directory, name))}  ${name}\n`)
  )
  if (readFileSync(path.join(directory, 'SHA256SUMS'), 'utf8') !== sums.join('')) throw new Error('Invalid SHA256SUMS')
  names.push('SHA256SUMS')
  if (readdirSync(directory).sort().join('\n') !== [...names].sort().join('\n'))
    throw new Error('Unexpected release files')
  const base = `/repos/${repository}`
  const verifyTag = async () => {
    let object = (await request('GET', `${base}/git/ref/tags/${tag}`)).object
    for (let depth = 0; object.type === 'tag' && depth < 5; depth++)
      object = (await request('GET', `${base}/git/tags/${object.sha}`)).object
    if (object.type !== 'commit' || object.sha !== commit) throw new Error('Remote tag changed')
  }
  await verifyTag()
  const marker = `<!-- desktop-release:${repository}:${tag}:${commit} -->`
  const body = `${marker}\n${readFileSync(path.join(directory, 'release-notes.md'), 'utf8')}`
  let release = await request('GET', `${base}/releases/tags/${tag}`, undefined, true)
  if (release && (release.body !== body || release.target_commitish !== commit))
    throw new Error('Existing release is not owned by this source run')
  if (!release)
    release = await request('POST', `${base}/releases`, {
      tag_name: tag,
      target_commitish: commit,
      name: `ChatArch Hermes ${tag}`,
      body,
      draft: true,
      prerelease: false
    })
  const existing = await request('GET', `${base}/releases/${release.id}/assets?per_page=100`)
  if (existing.some(asset => !names.includes(asset.name)))
    throw new Error('Existing release contains unexpected assets')
  for (const name of names) {
    const file = path.join(directory, name)
    const digest = `sha256:${await hashFile(file)}`
    const asset = existing.find(entry => entry.name === name)
    if (asset) {
      if (asset.state !== 'uploaded' || asset.digest !== digest)
        throw new Error(`Existing asset differs: ${name}; never overwritten`)
    } else {
      if (!release.draft) throw new Error('Published release is incomplete; refusing to modify it')
      const uploaded = await request(
        'UPLOAD',
        `${release.upload_url.split('{')[0]}?name=${encodeURIComponent(name)}`,
        await openAsBlob(file)
      )
      if (uploaded.state !== 'uploaded' || uploaded.digest !== digest)
        throw new Error(`Upload verification failed: ${name}`)
    }
  }
  if (release.draft) {
    await verifyTag()
    await request('PATCH', `${base}/releases/${release.id}`, { draft: false })
  }
}

async function apiRequest(method, endpoint, data, allowMissing = false) {
  const url = endpoint.startsWith('/') ? `https://api.github.com${endpoint}` : endpoint
  if (!['api.github.com', 'uploads.github.com'].includes(new URL(url).hostname)) throw new Error('Unexpected API host')
  const upload = method === 'UPLOAD'
  const response = await fetch(url, {
    method: upload ? 'POST' : method,
    headers: {
      Authorization: `Bearer ${process.env.DESKTOP_RELEASE_TOKEN}`,
      Accept: 'application/vnd.github+json',
      'X-GitHub-Api-Version': '2022-11-28',
      ...(data ? { 'Content-Type': upload ? 'application/octet-stream' : 'application/json' } : {})
    },
    body: data ? (upload ? data : JSON.stringify(data)) : undefined,
    redirect: 'error'
  })
  if (allowMissing && response.status === 404) return null
  if (!response.ok) throw new Error(`GitHub API ${method} failed: HTTP ${response.status}`)
  return response.json()
}

if (isMain(import.meta.url))
  await publishRelease({
    directory: path.resolve('desktop-bundle'),
    event: process.env.GITHUB_EVENT_NAME,
    repository: process.env.GITHUB_REPOSITORY,
    tag: process.env.GITHUB_REF_NAME,
    commit: process.env.GITHUB_SHA,
    request: apiRequest
  })
