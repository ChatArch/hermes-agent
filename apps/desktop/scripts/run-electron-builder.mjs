// Resolve electronDist at runtime (#38673, #47917): electron-builder 26.8.x can
// re-unpack a broken Electron.app; reusing the installed dist dodges that.
// npm workspace hoisting is non-deterministic — require.resolve finds electron
// wherever it landed. Dist present → -c.electronDist=<abs>/dist; absent → let
// electron-builder fetch via @electron/get (electronVersion + ELECTRON_MIRROR).

import fs from "node:fs"
import path from "node:path"
import { spawnSync } from "node:child_process"
import { createRequire } from "node:module"

const require = createRequire(import.meta.url)

function electronDistDir() {
  try {
    return path.join(path.dirname(require.resolve("electron/package.json")), "dist")
  } catch {
    return null
  }
}

function distBinary(dist) {
  if (process.platform === "darwin") {
    return path.join(dist, "Electron.app", "Contents", "MacOS", "Electron")
  }
  if (process.platform === "win32") {
    return path.join(dist, "electron.exe")
  }
  return path.join(dist, "electron")
}

function electronBuilderCli() {
  const pkgJson = require.resolve("electron-builder/package.json")
  const bin = require(pkgJson).bin
  const rel = typeof bin === "string" ? bin : bin["electron-builder"]
  return path.join(path.dirname(pkgJson), rel)
}

function withoutPublishFlags(callerArgs) {
  const args = []
  for (let index = 0; index < callerArgs.length; index++) {
    const argument = callerArgs[index]
    if (argument === "--") {
      args.push(...callerArgs.slice(index))
      break
    }
    if (argument === "-pd" || argument.startsWith("-pd=")) {
      args.push(argument === "-pd" ? "--prepackaged" : `--prepackaged=${argument.slice(4)}`)
      continue
    }
    const publish = /^(?:--publish|--p|-p)(?:=(.*))?$/.exec(argument)
    const platformPublish = /^-([mwl]+)p(?:=(.*))?$/.exec(argument)
    if (publish || platformPublish) {
      const inlineValue = publish ? publish[1] : platformPublish[2]
      const policy = inlineValue === undefined ? callerArgs[++index] : inlineValue
      if (policy !== "never") {
        throw new Error("Only --publish never is allowed; publishing belongs to the separate release job")
      }
      if (platformPublish) args.push(`-${platformPublish[1]}`)
      continue
    }
    if (/^--(?:no-)?(?:publish|p)(?:[.=]|$)/.test(argument) || /^-[A-Za-z]*p[A-Za-z]*(?:=|$)/.test(argument)) {
      throw new Error("Only --publish never is allowed; unsupported publish flag")
    }
    args.push(argument)
  }
  return args
}

const callerArgs = withoutPublishFlags(process.argv.slice(2))
const dist = electronDistDir()
// Local `hermes desktop` builds only ever package (--dir or dist), never
// publish a GitHub release — CI publication is a separate job. But the npm
// lifecycle env sets CI=1 (so esbuild's postinstall doesn't try interactive
// animations), and electron-builder treats CI=1 as a signal to implicitly
// resolve a publish target. That resolution reads <projectDir>/.git/config
// directly — projectDir here is apps/desktop, which has no .git of its own
// (only the repo root does) and no "repository" field in its package.json —
// so it fails with "Cannot detect repository by .git/config". Pin publish to
// "never" so electron-builder skips that lookup entirely.
const args = ["--publish", "never"]
if (dist && fs.existsSync(distBinary(dist))) {
  args.push(`-c.electronDist=${dist}`)
} else {
  console.warn(
    "[run-electron-builder] no local electron dist; electron-builder will fetch " +
      "via @electron/get (electronVersion + ELECTRON_MIRROR)."
  )
}
args.push(...callerArgs)

const result = spawnSync(process.execPath, [electronBuilderCli(), ...args], {
  stdio: "inherit",
})
if (result.error) {
  console.error(`[run-electron-builder] spawn failed: ${result.error.message}`)
  process.exit(1)
}
process.exit(result.status == null ? 1 : result.status)
