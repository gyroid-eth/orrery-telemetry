// The skills an installed dsh's filesystem provider offers, as dsh-tui's
// preset mounts it (no config: DSH_HOME, DSH_AGENTS_HOME and the project roots
// come from the environment and cwd). Prints [name, source, real path] for the
// winning candidate of each name, ranked as the dsh skill registry does.
// Usage: node dsh_skill_candidates.mjs <dsh-skill-filesystem/lib/index.js> <cwd>
import { realpathSync } from 'node:fs'

const [providerModule, cwd] = process.argv.slice(2)
const { FileSystemSkillProvider } = await import(providerModule)
const ctx = { logger: { warn() {}, info() {}, debug() {}, error() {} }, on() {}, effect() {}, get() { return undefined } }
const provider = new FileSystemSkillProvider(ctx, { invalidate() {}, signal: new AbortController().signal }, { watch: false })
const listed = await provider.list({ cwd })
const candidates = (Array.isArray(listed) ? listed : listed.candidates)
  .map((candidate, order) => ({ candidate, order }))
  .sort((left, right) => left.candidate.rank - right.candidate.rank || left.order - right.order)
const seen = new Set()
const rows = []
for (const { candidate } of candidates) {
  if (seen.has(candidate.name)) continue
  seen.add(candidate.name)
  rows.push([candidate.name, candidate.source, realpathSync(candidate.instructionPath ?? candidate.path)])
}
rows.sort((left, right) => (left[0] < right[0] ? -1 : left[0] > right[0] ? 1 : 0))
console.log(JSON.stringify(rows))
await provider.dispose?.()
process.exit(0)
