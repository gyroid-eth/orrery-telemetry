// Explicit profile and process-only launch environment: no project/user .env.
import fs from 'node:fs';
import path from 'node:path';
import {createRequire} from 'node:module';
import {pathToFileURL, fileURLToPath} from 'node:url';

const state = process.env.ORRERY_DSH_STATE_ROOT;
const runtime = process.env.ORRERY_DSH_RUNTIME_ROOT;
if (!state || !runtime || !path.isAbsolute(state) || !path.isAbsolute(runtime))
  throw new Error('DSH_MANAGED_ROOTS_REQUIRED');
if (process.env.DSH_HOME !== path.join(state, 'dsh')) throw new Error('DSH_HOME_MISMATCH');
const manifest = JSON.parse(fs.readFileSync(path.join(state, 'attempt.json'), 'utf8'));
if (manifest.phase !== 'spike' || process.env.ORRERY_DSH_OFFLINE !== '1')
  throw new Error('DSH_SPIKE_ONLY_ENTRY');
const require = createRequire(path.join(runtime, 'package.json'));
const {createLaunchEnvironmentSnapshot} = await import(pathToFileURL(require.resolve('@deepseek-ai/dsh-launch-environment')));
const {runProfile} = await import(pathToFileURL(require.resolve('@deepseek-ai/dsh/profile-boot')));
const here = path.dirname(fileURLToPath(import.meta.url));
await runProfile({
  environment: createLaunchEnvironmentSnapshot([{source: 'process', values: {...process.env}}]),
  profile: 'orrery-managed',
  patchFiles: [path.join(here, 'managed.patch.yml'), path.join(state, 'selected.patch.json')],
  args: [],
});
