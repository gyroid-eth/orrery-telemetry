// A normal dsh credentials service, backed by one Keychain lookup per process.
// Credential leaks have high impact: selected refs never resolve via env/file
// and native set/unset must not persist the memory value.
import {execFile} from 'node:child_process';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
export const name = 'orrery-keychain-credentials';

export function secretServiceLookup(config) {
  const argv = config.reader_command;
  // Check command/ID shape here; the Python reader owns declaration/kind/ref checks.
  if (process.platform !== 'linux' || !Array.isArray(argv) || argv.length !== 8 ||
      !argv.every(value => typeof value === 'string') ||
      !path.isAbsolute(argv[0]) || argv[1] !== '-I' || argv[2] !== '-B' ||
      !path.isAbsolute(argv[3]) || path.basename(argv[3]) !== 'llm_secret_reader.py' ||
      argv[4] !== '--manifest' || !path.isAbsolute(argv[5]) || argv[6] !== '--backend' ||
      !/^[a-z][a-z0-9_-]{0,63}$/.test(argv[7]))
    return Promise.reject(new Error('DSH_CREDENTIAL_REFERENCE_INVALID'));
  return new Promise((resolve, reject) => {
    const env = Object.fromEntries(['HOME', 'PATH', 'LANG', 'DBUS_SESSION_BUS_ADDRESS', 'XDG_RUNTIME_DIR']
      .filter(key => typeof process.env[key] === 'string').map(key => [key, process.env[key]]));
    execFile(argv[0], argv.slice(1), {encoding: 'utf8', timeout: 8000, maxBuffer: 65536, env}, (error, stdout) => {
      if (error || !stdout.trim()) reject(new Error('DSH_CREDENTIAL_UNAVAILABLE'));
      else resolve(stdout.replace(/\r?\n$/, ''));
    });
  });
}

export function keychainLookup({service, account}) {
  if (process.platform !== 'darwin') return Promise.reject(new Error('DSH_KEYCHAIN_UNSUPPORTED'));
  return new Promise((resolve, reject) => {
    execFile('/usr/bin/security', ['find-generic-password', '-s', service, '-a', account, '-w'],
      {encoding: 'utf8', timeout: 15000, maxBuffer: 65536}, (error, stdout) => {
        if (error || !stdout.trim()) reject(new Error('DSH_KEYCHAIN_UNAVAILABLE'));
        else resolve(stdout.replace(/\r?\n$/, ''));
      });
  });
}

export function providerClass(Local, Service, config, lookup) {
  return class KeychainCredentials extends Local {
    constructor(ctx) {
      super(ctx, {dshHome: config.dshHome});
      // Cordis traces services through proxy receivers, so keep the secret in
      // this constructor's closure rather than a JavaScript private field.
      const key = Promise.resolve().then(() => lookup(config)).then(value => {
        if (typeof value !== 'string' || !value.trim()) throw new Error('DSH_KEYCHAIN_UNAVAILABLE');
        return value;
      }, () => {throw new Error('DSH_KEYCHAIN_UNAVAILABLE');});
      key.catch(() => {});
      this[Service.init] = async function* () {
        await key;
        yield* Local.prototype[Service.init].call(this);
      };
      this.resolve = ref => ref === config.ref ? key.then(value => ({value, source: 'memory'})) : super.resolve(ref);
      this.describe = ref => ref === config.ref ? key.then(() => ({configured: true, source: 'memory', writable: false})) : super.describe(ref);
      this.write = async (ref, value) => {
        if (ref === config.ref) throw new Error('DSH_KEYCHAIN_READ_ONLY');
        return super.write(ref, value);
      };
    }
  };
}

export async function install(ctx, config, lookup = keychainLookup) {
  if (config.reader_command && ('service' in config || 'account' in config))
    throw new Error('DSH_CREDENTIAL_REFERENCE_CONFLICT');
  if (!path.isAbsolute(config.runtime) || !path.isAbsolute(config.dshHome) ||
      !['ref', ...(config.reader_command ? [] : ['service', 'account'])].every(k => typeof config[k] === 'string' && config[k]))
    throw new Error('DSH_KEYCHAIN_CONFIG_INVALID');
  const root = path.join(config.runtime, 'node_modules/@deepseek-ai');
  const [{LocalCredentialProvider}, {Service}] = await Promise.all([
    import(pathToFileURL(path.join(root, 'dsh-credentials-local/lib/index.js'))),
    import(pathToFileURL(path.join(root, 'cordis/lib/index.js'))),
  ]);
  await ctx.plugin(providerClass(LocalCredentialProvider, Service, config, lookup));
}

export async function apply(ctx, config) {
  await install(ctx, config, config.reader_command ? secretServiceLookup : keychainLookup);
}
