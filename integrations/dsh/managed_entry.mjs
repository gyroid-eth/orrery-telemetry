// Explicit profile and process-only launch environment: no project/user .env.
import fs from 'node:fs';
import path from 'node:path';
import {createRequire} from 'node:module';
import {pathToFileURL, fileURLToPath} from 'node:url';
import {createHash} from 'node:crypto';

// A same-version but unpatched TUI can write the real HOME during startup.
export function verifyTuiPatch({tuiRoot, recipeFile, receiptFile}) {
  const hash = file => createHash('sha256').update(fs.readFileSync(file)).digest('hex');
  const recipe = JSON.parse(fs.readFileSync(recipeFile, 'utf8'));
  const receipt = JSON.parse(fs.readFileSync(receiptFile, 'utf8'));
  const expected = [...new Set(recipe.replacements.map(row => row.file))].sort();
  const changes = receipt.changes;
  if (receipt.recipe_sha256 !== hash(recipeFile) || !Array.isArray(changes) ||
      JSON.stringify(changes.map(row => row.file).sort()) !== JSON.stringify(expected))
    throw new Error('DSH_TUI_PATCH_RECEIPT_MISMATCH');
  for (const file of expected) {
    if (path.isAbsolute(file) || file.split('/').includes('..') ||
        hash(path.join(tuiRoot, file)) !== changes.find(row => row.file === file).after)
      throw new Error('DSH_TUI_PATCH_FILE_MISMATCH: ' + file);
  }
}

export async function main() {
const state = process.env.ORRERY_DSH_STATE_ROOT;
const runtime = process.env.ORRERY_DSH_RUNTIME_ROOT;
if (!state || !runtime || !path.isAbsolute(state) || !path.isAbsolute(runtime))
  throw new Error('DSH_MANAGED_ROOTS_REQUIRED');
if (process.env.DSH_HOME !== path.join(state, 'dsh')) throw new Error('DSH_HOME_MISMATCH');
const manifest = JSON.parse(fs.readFileSync(path.join(state, 'attempt.json'), 'utf8'));
if (manifest.phase !== 'spike' || process.env.ORRERY_DSH_OFFLINE !== '1')
  throw new Error('DSH_SPIKE_ONLY_ENTRY');
const here = path.dirname(fileURLToPath(import.meta.url));
verifyTuiPatch({tuiRoot: path.join(runtime, 'node_modules/@deepseek-harness-tui/dsh-tui'),
  recipeFile: path.join(here, 'tui-managed.patch'), receiptFile: process.env.ORRERY_DSH_TUI_PATCH_RECEIPT});
const require = createRequire(path.join(runtime, 'package.json'));
const {createLaunchEnvironmentSnapshot} = await import(pathToFileURL(require.resolve('@deepseek-ai/dsh-launch-environment')));
const {runProfile} = await import(pathToFileURL(require.resolve('@deepseek-ai/dsh/profile-boot')));
await runProfile({
  environment: createLaunchEnvironmentSnapshot([{source: 'process', values: {...process.env}}]),
  profile: 'orrery-managed',
  patchFiles: [path.join(here, 'managed.patch.yml'), path.join(state, 'selected.patch.json')],
  args: [],
});
}

const [major, minor] = process.versions.node.split('.').map(Number);
if (major < 24 || (major === 24 && minor < 2)) throw new Error('DSH_NODE_24_2_REQUIRED');
if (import.meta.main) await main();
