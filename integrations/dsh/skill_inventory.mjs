// Read the same local skill declarations as dsh, without starting its CLI.
import { basename, dirname } from 'node:path'
import { pathToFileURL } from 'node:url'

const [modulePath, personalRoot, productRoot] = process.argv.slice(2)
const { FileSystemSkillProvider } = await import(pathToFileURL(modulePath).href)
const ctx = {
    logger: { warn() {} },
    get() { return undefined },
}
const provider = new FileSystemSkillProvider(ctx, {
    invalidate() {},
    signal: new AbortController().signal,
}, {
    includeDefaultRoots: false,
    dshHome: productRoot,
    agentsHome: personalRoot,
    customSkillDirs: [personalRoot],
    bundledSkillDir: productRoot,
    watch: false,
})

try {
    const candidates = await provider.list({})
    if (!Array.isArray(candidates)) throw new Error('Incomplete skill inventory')
    const personalNames = new Set(candidates.filter(skill => skill.source === 'custom').map(skill => skill.name))
    const shadowed = candidates
        .filter(skill => skill.source === 'bundled' && personalNames.has(skill.name))
        .map(({ locator }) => dirname(locator.path))
        .filter(directory => dirname(directory) === productRoot)
        .map(directory => basename(directory))
    console.log(JSON.stringify(shadowed))
} finally {
    await provider.dispose()
}
