#!/usr/bin/env node
import crypto from 'node:crypto';
import {constants} from 'node:fs';
import fs from 'node:fs/promises';
import path from 'node:path';
import process from 'node:process';
import puppeteer from 'puppeteer';

async function launch() { return puppeteer.launch({headless: true}); }
if (process.argv.includes('--preflight')) {
  const browser = await launch();
  const page = await browser.newPage();
  await page.goto('about:blank');
  await browser.close();
  process.exit(0);
}
function argument(name) {
  const index = process.argv.indexOf(name);
  if (index < 0 || index + 1 >= process.argv.length) throw new Error('invalid browser smoke arguments');
  return process.argv[index + 1];
}
async function privateInput(file) {
  const handle = await fs.open(file, constants.O_RDONLY | (constants.O_NOFOLLOW || 0));
  try {
    const info = await handle.stat();
    if (!info.isFile() || info.size > 64 * 1024 || (info.mode & 0o077) !== 0 || (process.getuid && info.uid !== process.getuid())) throw new Error('unsafe browser smoke input');
    return await handle.readFile();
  } finally { await handle.close(); }
}
async function atomicPrivateOutput(file, value) {
  const directory = await fs.lstat(path.dirname(file));
  if (!directory.isDirectory() || directory.isSymbolicLink() || (directory.mode & 0o077) !== 0 || (process.getuid && directory.uid !== process.getuid())) throw new Error('unsafe browser smoke output directory');
  const temporary = path.join(path.dirname(file), `.${path.basename(file)}.${process.pid}.${crypto.randomBytes(8).toString('hex')}`);
  const handle = await fs.open(temporary, 'wx', 0o600);
  try {
    await handle.writeFile(JSON.stringify(value));
    await handle.sync();
  } finally { await handle.close(); }
  await fs.rename(temporary, file);
  await fs.chmod(file, 0o600);
}
const inputPath = argument('--input');
const outputPath = argument('--output');
let browser;
try {
  const bytes = await privateInput(inputPath);
  const inputHash = crypto.createHash('sha256').update(bytes).digest('hex');
  const input = JSON.parse(bytes);
  if (input.version !== 1 || !['node', 'coordinator'].includes(input.kind)) throw new Error('invalid browser smoke request');
  browser = await launch();
  const context = await browser.createBrowserContext();
  const page = await context.newPage();
  await page.setViewport({width: 390, height: 844, isMobile: true, hasTouch: true});
  const url = new URL(input.url);
  await page.setCookie({name: input.cookieName, value: input.credential, domain: url.hostname, path: '/', secure: url.protocol === 'https:', httpOnly: true, sameSite: 'Strict'});
  await page.setRequestInterception(true);
  let forbiddenMutation = false;
  page.on('request', request => {
    const target = new URL(request.url());
    const allowedAccess = input.kind === 'coordinator' && request.method() === 'POST'
      && target.origin === url.origin && target.pathname === '/api/fleet/access' && request.postData() === '{}';
    if (!['GET', 'HEAD', 'OPTIONS'].includes(request.method()) && !allowedAccess) {
      forbiddenMutation = true;
      request.abort('blockedbyclient');
    } else request.continue();
  });
  const response = await page.goto(input.url, {waitUntil: 'domcontentloaded', timeout: 30000});
  if (!response?.ok()) throw new Error('dashboard did not load successfully');
  await page.waitForFunction(({kind, machineIds}) => {
    if (typeof ompDeckApi === 'undefined' || !ompDeckApi.authenticated()) return false;
    if (!document.getElementById('app') || document.getElementById('app').hidden) return false;
    const machines = ompDeckApi.machines();
    if (kind !== 'coordinator') return machines.some(machine => machine.local && machine.status === 'live');
    return machineIds.every(id => machines.some(machine => machine.id === id && machine.status === 'live'));
  }, {timeout: 30000}, {kind: input.kind, machineIds: input.machineIds});
  const check = await page.evaluate(async kind => {
    const get = route => fetch(route).then(r => r.ok ? r.json() : Promise.reject(new Error('read failed')));
    const [health, config, authenticated] = await Promise.all([get('/healthz'), get('/api/client-config'), get(kind === 'coordinator' ? '/api/fleet' : '/api/sessions')]);
    const machines = typeof ompDeckApi === 'undefined' ? [] : ompDeckApi.machines();
    return {health, config, authenticated, machines, overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth};
  }, input.kind);
  if (forbiddenMutation || check.overflow || check.health?.ok !== true) throw new Error('dashboard health verification failed');
  if (check.config?.build?.version !== input.expectedBuild.version || check.config?.build?.revision !== input.expectedBuild.revision) throw new Error('dashboard build identity mismatch');
  if (input.kind === 'coordinator' && !input.machineIds.every(id => check.machines.some(machine => machine.id === id && machine.status === 'live'))) throw new Error('expected coordinator machine is not live');
  const result = {version: 1, transaction: input.transaction, inputSha256: inputHash, build: input.expectedBuild, ok: true, checks: ['viewport', 'read-only', 'healthy']};
  await atomicPrivateOutput(outputPath, result);
  await context.close();
} catch {
  process.exitCode = 1;
} finally {
  if (browser) await browser.close();
}
