// Source-aligned layout, modal/key interactions and screenshots against real PG.
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {mkdtemp,mkdir} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import net from 'node:net';
import {chromium} from 'playwright-core';
const socket=net.createServer();await new Promise(r=>socket.listen(0,'127.0.0.1',r));const port=socket.address().port;await new Promise(r=>socket.close(r));
const base=`http://127.0.0.1:${port}`,data=await mkdtemp(join(tmpdir(),'codex-layout-'));
const server=spawn('.venv/bin/python',['-m','tests.gateway_ui_server','--port',String(port),'--codex'],{env:{...process.env,AUTOBUILD_TEST_DATA:data},stdio:['ignore','pipe','pipe']});
let output='',browser;server.stderr.on('data',c=>output+=c);server.stdout.on('data',c=>output+=c);
try{
  let ready=false;for(let i=0;i<300;i++){if(server.exitCode!==null)break;try{if((await fetch(base)).ok){ready=true;break;}}catch{}await new Promise(r=>setTimeout(r,50));}assert(ready,output);
  browser=await chromium.launch({executablePath:process.env.CHROME_PATH||'/usr/bin/google-chrome',headless:true,args:['--no-sandbox']});
  const context=await browser.newContext({viewport:{width:1850,height:1000}}),page=await context.newPage();page.setDefaultTimeout(10000);
  const errors=[],external=[],writes=[];page.on('pageerror',e=>errors.push(e.message));page.on('request',r=>{if(r.method()!=='GET')writes.push(new URL(r.url()).pathname);});
  await context.route('**/*',r=>{if(!r.request().url().startsWith(base+'/')){external.push(r.request().url());return r.abort();}return r.continue();});
  await page.goto(base+'/service/');await page.locator('#admin-token').fill('browser-test-admin');await page.locator('#login-form button').click();await page.locator('#codex-list').waitFor();
  assert.equal(await page.locator('#codex-refresh-all').count(),1,'Accounts tab must expose the server-side all-account refresh');
  assert.equal(await page.locator('#codex-refresh-all-toolbar').count(),1,'Service toolbar must expose all-account refresh');
  assert.equal(await page.locator('#codex-auto-refresh-minutes').inputValue(),'0','Automatic quota refresh is opt-in');
  assert.match(await page.locator('#codex-quota-refresh-schedule').textContent(),/toàn bộ tài khoản/i);
  await page.locator('.service-status').getByText('Đã cấu hình',{exact:true}).waitFor();
  const positions=await page.evaluate(()=>{const r=s=>document.querySelector(s).getBoundingClientRect();return {tabs:r('.codex-tabs').bottom,hero:r('.codex-service-bar').top,list:r('#codex-list').right,pool:r('#codex-pool').left};});
  assert(positions.tabs<positions.hero);assert(positions.list<positions.pool,'Routing must sit beside the account panel');
  assert(await page.locator('#codex-import-dialog').isHidden());assert(await page.locator('.codex-details').isHidden());
  assert.equal(await page.locator('#codex-pool [name="wait"]').count(),0,'No unsupported fake settings');
  const artifacts='.superpowers/cockpit-ui';await mkdir(artifacts,{recursive:true});
  for(const tab of ['accounts','overview','keys','models','usage']){
    await page.locator(`[data-codex-tab="${tab}"]`).click();
    if(tab==='usage')await page.locator('#codex-report-log table').waitFor();
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
    await page.screenshot({path:join(artifacts,`desktop-${tab}.png`),fullPage:true});
  }
  assert(!writes.some(p=>p.endsWith('/quota/refresh')||p.endsWith('/consume')),'Viewing tabs must never consume upstream actions');
  await page.locator('[data-codex-tab="keys"]').click();
  await page.getByRole('button',{name:'Thêm khóa',exact:true}).click();const form=page.locator('#codex-key-editor-form');await form.waitFor();
  await form.locator('[name="name"]').fill('Layout test key');await form.locator('[name="customer_id"]').selectOption({index:1});await form.locator('[name="total"]').fill('100m');await form.locator('[type="submit"]').click();
  const secretDialog=page.locator('#codex-key-secret-dialog');await secretDialog.waitFor();const secret=await secretDialog.locator('textarea').inputValue();assert(secret.startsWith('sk-'));await secretDialog.getByRole('button',{name:'Đóng',exact:true}).click();await secretDialog.waitFor({state:'detached'});assert.equal(await page.locator('#codex-key-secret-dialog').count(),0);
  let card=page.locator('.codex-key-card').filter({has:page.locator('.key-name',{hasText:'Layout test key'})});await card.waitFor();
  await card.getByRole('button',{name:'Chỉnh sửa Layout test key',exact:true}).click();await form.waitFor();assert.equal(await form.locator('[name="total"]').inputValue(),'100000000');await form.locator('[name="name"]').fill('Layout renamed');await form.locator('[type="submit"]').click();
  card=page.locator('.codex-key-card').filter({has:page.locator('.key-name',{hasText:'Layout renamed'})});await card.waitFor();
  await card.getByRole('button',{name:'Tắt key',exact:true}).click();await card.getByText('Đã tắt',{exact:true}).waitFor();await card.getByRole('button',{name:'Bật key',exact:true}).click();await card.getByText('Đã bật',{exact:true}).waitFor();
  page.once('dialog',d=>d.accept());await card.getByRole('button',{name:'Đổi key',exact:true}).click();await secretDialog.waitFor();assert.notEqual(await secretDialog.locator('textarea').inputValue(),secret);await secretDialog.getByRole('button',{name:'Đóng',exact:true}).click();
  page.once('dialog',d=>d.accept());await card.getByRole('button',{name:'Xóa key',exact:true}).click();await card.getByText('Đã thu hồi',{exact:true}).waitFor();
  assert(!(await page.locator('body').innerHTML()).includes(secret));
  await page.locator('[data-codex-tab="accounts"]').click();await page.getByRole('button',{name:'Thêm tài khoản',exact:true}).click();await page.locator('#codex-import-dialog').waitFor();await page.locator('#codex-batch-import [name="batch-json"]').fill('[draft]');await page.keyboard.press('Escape');await page.getByRole('button',{name:'Thêm tài khoản',exact:true}).click();assert.equal(await page.locator('#codex-batch-import [name="batch-json"]').inputValue(),'[draft]');await page.keyboard.press('Escape');
  await page.setViewportSize({width:390,height:844});
  for(const tab of ['accounts','overview','keys','models','usage']){await page.locator(`[data-codex-tab="${tab}"]`).click();assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),`Mobile overflow: ${tab}`);await page.screenshot({path:join(artifacts,`mobile-${tab}.png`),fullPage:true});}
  assert.deepEqual(errors,[]);assert.deepEqual(external,[]);
  console.log('Codex layout PASS: five tabs desktop/mobile, key modal create/edit/toggle/rotate/delete, one-time secret cleanup, import draft, no provider writes.');
}finally{await browser?.close();server.kill('SIGTERM');await new Promise(r=>{if(server.exitCode!==null)r();else server.once('exit',r);});}
