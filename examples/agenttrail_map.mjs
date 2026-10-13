// Lance le serveur Map officiel avec un etat isole, sans installer de hooks.
import fs from 'node:fs'
import path from 'node:path'
import os from 'node:os'
import { pathToFileURL } from 'node:url'

const args = process.argv.slice(2)
const options = {}
for (let i = 0; i < args.length; i += 2) {
  if (!['--package', '--project', '--state-dir', '--port'].includes(args[i]) || !args[i + 1]) throw new Error('argument Map invalide')
  options[args[i].slice(2)] = args[i + 1]
}
for (const key of ['package', 'project', 'state-dir']) if (!options[key]) throw new Error('--' + key + ' requis')
const packageRoot = fs.realpathSync(options.package)
const project = fs.realpathSync(options.project)
const stateDir = path.resolve(options['state-dir'])
const port = Number(options.port || 5330)
if (!Number.isInteger(port) || port < 1024 || port > 65515) throw new Error('port Map invalide')
const pkg = JSON.parse(fs.readFileSync(path.join(packageRoot, 'package.json'), 'utf8'))
if (pkg.name !== 'agenttrail') throw new Error('le paquet doit etre AgentTrail Map')
const entry = path.join(packageRoot, 'bin', 'agenttrail.mjs')
const source = fs.readFileSync(entry, 'utf8')
// Le paquet npm 0.2.0 etiquetait tous les hooks "claude". Utiliser la source
// officielle recente, qui permet ev.agent, pour nommer Codex correctement.
if (!source.includes('if (ev.agent) run.agent') || !source.includes("u.pathname === '/model'")) {
  throw new Error('Map trop ancien : utiliser la source officielle sodiumsun/agenttrail avec ev.agent et /model')
}
fs.mkdirSync(stateDir, { recursive: true })
const observerHome = path.join(stateDir, 'observer-home')
fs.mkdirSync(observerHome, { recursive: true })
// Le daemon importe os par defaut. Seul ce processus voit ce dossier d'etat.
os.homedir = () => observerHome
process.argv = [process.execPath, entry, project, '--port', String(port), '--no-open']
let input = ''
process.stdin.setEncoding('utf8')
process.stdin.on('data', chunk => {
  input = (input + chunk).slice(-1000)
  if (input.split(/\r?\n/).includes('stop')) process.emit('SIGTERM')
})
process.stdin.resume()
process.stdin.unref?.()
await import(pathToFileURL(entry).href)

let checking = false
const registration = setInterval(async () => {
  if (checking) return
  checking = true
  try {
    for (let candidate = port; candidate <= port + 20; candidate++) {
      try {
        const response = await fetch(`http://127.0.0.1:${candidate}/whoami`, { signal: AbortSignal.timeout(300) })
        const identity = await response.json()
        if (identity.repoPath !== project || identity.port !== candidate) continue
        fs.writeFileSync(path.join(stateDir, 'server.json'), JSON.stringify({
          app: 'agenttrail-map', port: candidate, repoPath: project, pid: process.pid,
        }))
        clearInterval(registration)
        break
      } catch {}
    }
  } finally { checking = false }
}, 100)
