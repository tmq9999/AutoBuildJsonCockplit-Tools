// Real browser + real local API/runner/store. Only provider I/O is replaced.
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { mkdtemp, mkdir } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import net from 'node:net';
import { chromium } from 'playwright-core';

const listener = net.createServer();
await new Promise(resolve => listener.listen(0, '127.0.0.1', resolve));
const port = listener.address().port;
await new Promise(resolve => listener.close(resolve));
const origin = `http://127.0.0.1:${port}`;
const data = await mkdtemp(join(tmpdir(), 'autobuild-browser-'));
const python = process.env.TEST_PYTHON || '.venv/bin/python';
const server = spawn(python, ['-m','tests.ui_server','--port',String(port)], {
  env:{...process.env,AUTOBUILD_TEST_DATA:data}, stdio:['ignore','pipe','pipe'],
});
let serverOutput = '';
server.stderr.on('data', chunk => { serverOutput += chunk; });
const exited = new Promise(resolve => server.on('exit', resolve));
let browser;
const artifacts = '.superpowers/browser-check';
await mkdir(artifacts, {recursive:true});
try {
  let ready = false;
  for (let i=0;i<100;i++) {
    if (server.exitCode !== null) throw new Error('Test server failed: '+serverOutput);
    try { if ((await fetch(origin)).ok) { ready=true; break; } } catch { /* starting */ }
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  assert.ok(ready, 'Local test server starts');
  browser = await chromium.launch({executablePath:process.env.CHROME_PATH || '/usr/bin/google-chrome', headless:true, args:['--no-sandbox']});
  const context = await browser.newContext({viewport:{width:1440,height:1100}, acceptDownloads:true});
  const page = await context.newPage();
  const errors = [], external = [];
  let releaseRestore;
  const restoreGate = new Promise(resolve => { releaseRestore=resolve; });
  let delayedRestore = false;
  page.on('pageerror', error => errors.push(error.message));
  await context.route('**/*', route => {
    const url = route.request().url();
    if (!url.startsWith(origin+'/') && !url.startsWith('blob:')) { external.push(url); return route.abort(); }
    return route.continue();
  });
  await page.route(origin+'/api/session', async route => {
    if (route.request().method()==='GET' && !delayedRestore) {
      delayedRestore=true;
      await restoreGate;
      return route.fulfill({status:401,contentType:'application/json',body:'{"error":"UNAUTHORIZED"}'}).catch(()=>{});
    }
    return route.continue();
  });
  await page.goto(origin);
  await page.locator('#login-form').waitFor({state:'visible'});
  await page.locator('#admin-token').fill('browser-test-admin');
  await page.locator('#login-btn').click();
  await page.locator('#dashboard').waitFor({state:'visible'});
  releaseRestore();
  await page.waitForTimeout(150);
  assert.ok(await page.locator('#dashboard').isVisible(), 'A stale session-restore response must not undo a new login');
  await page.unroute(origin+'/api/session');
  assert.ok(await page.locator('#start-btn').isDisabled());
  const input = 'good@example.com|fake-pass|JBSWY3DPEHPK3PXP\nphone@example.com|fake-pass|JBSWY3DPEHPK3PXP\nerror@example.com|fake-pass|JBSWY3DPEHPK3PXP\nbad|fake-pass|';
  await page.locator('#accounts-file').setInputFiles({name:'accounts.txt',mimeType:'text/plain',buffer:Buffer.from(input)});
  await page.waitForFunction(() => document.querySelector('#accounts-text').value.includes('good@example.com'));
  await page.locator('#validate-btn').click();
  await page.waitForFunction(() => document.querySelector('#validation-summary').textContent.includes('3 hợp lệ'));
  await page.locator('#mode').selectOption('parallel');
  await page.locator('#workers').fill('3');
  await page.locator('#proxies-text').fill('http://proxy.invalid:8080');
  await page.locator('#start-btn').click();
  await page.waitForFunction(() => document.querySelector('#job-status').dataset.status === 'completed');
  assert.equal(await page.locator('#accounts-text').inputValue(), '');
  assert.equal(await page.locator('#proxies-text').inputValue(), '');
  assert.equal(await page.locator('[data-count="success"]').textContent(), '1');
  assert.equal(await page.locator('[data-count="phone_verify"]').textContent(), '1');
  await page.locator('#status-filter').selectOption('phone_verify');
  assert.equal(await page.locator('#results-body tr[data-row]').count(), 1);
  assert.match(await page.locator('#results-body').textContent(), /Phone number verify/);
  await page.locator('#status-filter').selectOption('all');
  const readDownload = async action => {
    const pending = page.waitForEvent('download');
    await action();
    const download = await pending;
    const stream = await download.createReadStream();
    let value=''; for await (const chunk of stream) value += chunk;
    return {name:download.suggestedFilename(), value:JSON.parse(value)};
  };
  for (const [kind,count] of [['success',1],['errors',1],['phone_verify',1],['filtered',1],['cancelled',0]]) {
    const result = await readDownload(() => page.locator(`[data-export="${kind}"]`).click());
    assert.equal(result.name, `${kind}.json`);
    assert.equal(result.value.length, count);
  }
  const body = await page.locator('body').textContent();
  assert.ok(!body.includes('fake-refresh') && !body.includes('fake-pass'));
  await page.screenshot({path:join(artifacts,'desktop.png'),fullPage:true});
  await page.setViewportSize({width:390,height:844});
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
  await page.screenshot({path:join(artifacts,'mobile.png'),fullPage:true});
  await page.setViewportSize({width:1440,height:1100});
  await page.locator('#manual-panel summary').click();
  await page.locator('#manual-email').fill('manual@example.com');
  await page.locator('#generate-link-btn').click();
  await page.waitForFunction(() => document.querySelector('#oauth-link').value.startsWith('https://chatgpt.com/'));
  const desktop = new URL(await page.locator('#oauth-link').inputValue());
  const state = new URL(desktop.searchParams.get('authorize_url')).searchParams.get('state');
  await page.locator('#callback-url').fill(`http://localhost:1455/auth/callback?state=${state}&code=fake`);
  const manual = await readDownload(() => page.locator('#complete-btn').click());
  assert.equal(manual.value[0].email, 'manual@example.com');
  assert.equal(await page.locator('#callback-url').inputValue(), '');
  assert.ok(await page.locator('#complete-btn').isDisabled());
  await page.locator('#accounts-text').fill('slow@example.com|p|JBSWY3DPEHPK3PXP\nnext@example.com|p|JBSWY3DPEHPK3PXP');
  await page.locator('#mode').selectOption('sequential');
  await page.locator('#validate-btn').click();
  await page.waitForFunction(() => !document.querySelector('#start-btn').disabled);
  let historySeen;
  const historyStarted = new Promise(resolve => { historySeen=resolve; });
  let releaseHistory;
  const historyGate = new Promise(resolve => { releaseHistory=resolve; });
  let historyDone;
  const historyFulfilled = new Promise(resolve => { historyDone=resolve; });
  let delayedHistory=false;
  await page.route(origin+'/api/jobs', async route => {
    if (route.request().method()==='GET' && !delayedHistory) {
      delayedHistory=true;
      const stale=await route.fetch();
      const oldBody=await stale.body();
      historySeen(); await historyGate;
      await route.fulfill({status:200,contentType:'application/json',body:oldBody});
      historyDone();
      return;
    }
    return route.continue();
  });
  await page.locator('#refresh-btn').click();
  await historyStarted;
  const started = page.waitForResponse(response=>response.url()===origin+'/api/jobs' && response.request().method()==='POST');
  await page.locator('#start-btn').click();
  const accepted = await started;
  const activeID=(await accepted.json()).id;
  await page.waitForResponse(response=>response.url()===origin+'/api/jobs/'+activeID);
  releaseHistory();
  await historyFulfilled;
  await page.unroute(origin+'/api/jobs');
  await page.waitForFunction(() => document.querySelector('#job-status').dataset.status === 'running');
  await page.waitForTimeout(200);
  assert.ok(await page.locator('#stop-btn').isEnabled(), 'Stale history must not clear a newly accepted active batch');
  await page.waitForResponse(response=>response.url()===origin+'/api/jobs/'+activeID, {timeout:5000});
  await page.locator('#stop-btn').click();
  await page.waitForFunction(() => document.querySelector('#job-status').dataset.status === 'cancelled');
  assert.equal(await page.locator('[data-count="cancelled"]').textContent(), '2');
  await page.reload();
  await page.locator('#dashboard').waitFor({state:'visible'});
  await page.waitForFunction(() => document.querySelector('#job-history').options.length >= 3);
  await page.locator('#logout-btn').click();
  await page.locator('#login-form').waitFor({state:'visible'});
  assert.equal(await page.locator('#admin-token').inputValue(), '');
  assert.equal(await page.evaluate(() => localStorage.length + sessionStorage.length), 0);
  assert.deepEqual(errors, []);
  assert.deepEqual(external, []);
  console.log('Browser PASS: login, import/filter, parallel/proxy batch, five exports, manual callback, Stop, reload/history, mobile, logout; no external requests.');
} finally {
  if (browser) await browser.close();
  server.kill('SIGTERM');
  await exited;
}
