// Real private API and temporary PostgreSQL; 1000 synthetic OAuth accounts.
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {mkdtemp} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import net from 'node:net';
import {chromium} from 'playwright-core';
const socket=net.createServer();await new Promise(r=>socket.listen(0,'127.0.0.1',r));const port=socket.address().port;await new Promise(r=>socket.close(r));
const base=`http://127.0.0.1:${port}`,data=await mkdtemp(join(tmpdir(),'codex-pagination-'));
const server=spawn('.venv/bin/python',['-m','tests.gateway_ui_server','--port',String(port),'--codex'],{env:{...process.env,AUTOBUILD_TEST_DATA:data},stdio:['ignore','pipe','pipe']});
let logs='',browser;server.stderr.on('data',c=>logs+=c);server.stdout.on('data',c=>logs+=c);
try{
  let ready=false;for(let i=0;i<300;i++){if(server.exitCode!==null)break;try{if((await fetch(base)).ok){ready=true;break;}}catch{}await new Promise(r=>setTimeout(r,50));}assert(ready,logs);
  browser=await chromium.launch({executablePath:process.env.CHROME_PATH||'/usr/bin/google-chrome',headless:true,args:['--no-sandbox']});
  const context=await browser.newContext({viewport:{width:1500,height:1000}}),page=await context.newPage();page.setDefaultTimeout(15000);
  const errors=[],external=[],writes=[],snapshots=[];page.on('pageerror',e=>errors.push(e.message));page.on('request',r=>{if(r.method()!=='GET')writes.push(new URL(r.url()).pathname);if(/oauth-accounts\/[^/]+\/(quota|reset-credits)$/.test(r.url()))snapshots.push(r.url());});
  await context.route('**/*',r=>{if(!r.request().url().startsWith(base+'/')){external.push(r.request().url());return r.abort();}return r.continue();});
  await page.goto(base+'/service/');await page.locator('#admin-token').fill('browser-test-admin');await page.locator('#login-form button').click();await page.locator('#codex-list').waitFor();
  const imported=await page.evaluate(async()=>{
    const session=await(await fetch('/api/session')).json();
    const records=Array.from({length:997},(_,n)=>({id:crypto.randomUUID(),email:`paging-${n}@example.invalid`,account:{id:`SYNTHETIC-PAGING-${n}`},tokens:{access_token:'SYNTHETIC-ACCESS',refresh_token:'SYNTHETIC-REFRESH',id_token:'SYNTHETIC-ID'}}));
    const response=await fetch('/api/service/oauth/import-batch',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':session.csrf_token},body:JSON.stringify({records})});if(!response.ok)throw Error('Fixture import failed '+response.status);return (await response.json()).imported;
  });assert.equal(imported,997);
  snapshots.length=0;await page.locator('#codex-reload-saved').click();
  const pager=page.locator('.codex-pagination[data-position="top"]');
  const waitPage=async(n,count,total=1000)=>{await page.waitForFunction(({n,count,total})=>document.querySelector('.page-summary')?.textContent.includes(` / ${total} tài khoản · Trang ${n}/`)&&document.querySelectorAll('.codex-account-card').length===count&&document.querySelector('#codex-list').getAttribute('aria-busy')==='false',{n,count,total});};
  await waitPage(1,48);assert(snapshots.length<=96,'Only current-page snapshots may be requested');
  const first=await page.locator('.codex-account-head .row-link').allTextContents();
  await pager.getByRole('button',{name:'Trang cuối',exact:true}).click();await waitPage(21,40);assert(await pager.getByRole('button',{name:'Trang tiếp theo',exact:true}).isDisabled());
  const last=await page.locator('.codex-account-head .row-link').allTextContents();assert(last.every(email=>!first.includes(email)));
  await pager.getByRole('button',{name:'Trang trước',exact:true}).click();await waitPage(20,48);
  await page.locator('#codex-page-jump').fill('10');await pager.getByRole('button',{name:'Đi',exact:true}).click();await waitPage(10,48);
  await page.locator('#codex-page-size').selectOption('24');await waitPage(1,24);assert.match(await pager.textContent(),/Trang 1\/42/);
  await page.locator('#codex-page-size').selectOption('96');await waitPage(1,96);assert.match(await pager.textContent(),/Trang 1\/11/);
  const target=last[0],search=page.locator('#codex-list [name="account-search"]');await search.fill(target.toUpperCase());await waitPage(1,1,1);assert.equal(await page.locator('.codex-account-head .row-link').textContent(),target);
  await page.locator('#codex-list [name="health"]').selectOption('disabled');await waitPage(1,0,0);assert(await pager.getByRole('button',{name:'Trang cuối',exact:true}).isDisabled());
  await page.locator('#codex-list [name="health"]').selectOption('Tất cả');await waitPage(1,1,1);await search.fill('');await waitPage(1,96);
  // Failed page loads must keep the current page and allow a new explicit retry.
  const listPattern=/\/api\/service\/oauth-accounts(?:\?.*)?$/;
  await page.route(listPattern,route=>new URL(route.request().url()).searchParams.get('page')==='2'?route.fulfill({status:503,contentType:'application/json',body:'{"error":"STORAGE_UNAVAILABLE"}'}):route.continue());
  const failed=page.waitForResponse(r=>r.url().includes('/oauth-accounts?')&&r.status()===503);await pager.getByRole('button',{name:'Trang tiếp theo',exact:true}).click();await failed;await waitPage(1,96);await page.unroute(listPattern);
  await pager.getByRole('button',{name:'Trang tiếp theo',exact:true}).click();await waitPage(2,96);
  // A late page response cannot replace newer global search results.
  let entered,release;const started=new Promise(r=>entered=r),gate=new Promise(r=>release=r);
  await page.route(listPattern,async route=>{if(new URL(route.request().url()).searchParams.get('page')!=='3')return route.continue();const response=await route.fetch();entered();await gate;await route.fulfill({response});});
  await pager.getByRole('button',{name:'Trang tiếp theo',exact:true}).click();await started;
  await search.fill(target);await waitPage(1,1,1);const late=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/service/oauth-accounts'&&new URL(r.url()).searchParams.get('page')==='3');release();await late;await page.unroute(listPattern);
  await waitPage(1,1,1);assert.equal(await page.locator('.codex-account-head .row-link').textContent(),target);
  await search.fill('');await waitPage(1,96);await page.setViewportSize({width:390,height:844});assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
  assert.deepEqual(writes.filter(p=>p!=='/api/session'&&p!=='/api/service/oauth/import-batch'),[],'Paging/filtering never writes or calls providers');assert.deepEqual(errors,[]);assert.deepEqual(external,[]);
  console.log('Pagination PASS: 1000 accounts, 24/48/96 limits, first/previous/next/last/jump, global search/status, failed-load recovery, late-response protection, mobile, no provider writes.');
}finally{await browser?.close();server.kill('SIGTERM');await new Promise(r=>{if(server.exitCode!==null)r();else server.once('exit',r);});}
