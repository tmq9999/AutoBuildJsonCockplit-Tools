// Real private FastAPI/PG fixture. Only provider HTTP is synthetic.
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {mkdtemp,mkdir} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import net from 'node:net';
import {execFileSync} from 'node:child_process';
import {chromium} from 'playwright-core';
import {codexReviewRegressions} from './codex-review-regressions.mjs';
const socket=net.createServer();await new Promise(r=>socket.listen(0,'127.0.0.1',r));const port=socket.address().port;await new Promise(r=>socket.close(r));
const base=`http://127.0.0.1:${port}`,data=await mkdtemp(join(tmpdir(),'codex-ui-'));
const server=spawn('.venv/bin/python',['-m','tests.gateway_ui_server','--port',String(port),'--codex'],{env:{...process.env,AUTOBUILD_TEST_DATA:data},stdio:['ignore','pipe','pipe']});
let output='',browser;server.stderr.on('data',c=>output+=c);server.stdout.on('data',c=>output+=c);
const artifacts='.superpowers/sdd/2026-09-25-codex-api-service/task-11-screenshots';
try{
  let ready=false;for(let i=0;i<300;i++){if(server.exitCode!==null)break;try{if((await fetch(base)).ok){ready=true;break;}}catch{}await new Promise(r=>setTimeout(r,50));}assert(ready,output);
  browser=await chromium.launch({executablePath:process.env.CHROME_PATH||'/usr/bin/google-chrome',headless:true,args:['--no-sandbox']});
  const context=await browser.newContext({viewport:{width:1500,height:1100}}),page=await context.newPage();page.setDefaultTimeout(8000);
  const errors=[],external=[],commands=[];
  page.on('pageerror',e=>errors.push(e.message));page.on('request',r=>{if(r.method()!=='GET')commands.push({path:new URL(r.url()).pathname,body:r.postDataJSON()});});
  await context.route('**/*',r=>{if(!r.request().url().startsWith(base+'/')){external.push(r.request().url());return r.abort();}return r.continue();});
  if(process.env.CODEX_REVIEW_BASELINE){
    const baselineView=execFileSync('git',['show','cb867df:autobuild_json/gateway/admin/static/codex-view.js'],{encoding:'utf8'});
    await context.route(base+'/service-assets/codex-view.js',async route=>{const response=await route.fetch();await route.fulfill({response,body:baselineView});});
  }
  await page.goto(base+'/service/');await page.locator('#admin-token').fill('browser-test-admin');await page.locator('#login-form button').click();
  await page.locator('#workspace').waitFor({state:'visible'});
  await codexReviewRegressions(context,base);
  if(!process.env.CODEX_REVIEW_BASELINE){
  assert.equal(await page.getByRole('button',{name:'Tài khoản Codex',exact:true}).count(),1,'Admin must expose the approved account workspace');
  await page.getByRole('button',{name:'Tài khoản Codex',exact:true}).click();
  await page.locator('#codex-list').waitFor();
  assert(await page.locator('#codex-accounts-panel').isVisible());assert(await page.locator('#codex-keys').isHidden());
  assert.equal(await page.evaluate(()=>getComputedStyle(document.documentElement).colorScheme),'light');
  assert.match(await page.locator('#codex-list .plan-badge').first().textContent(),/Không rõ/);
  assert.equal(await page.locator('#codex-list .row-link').first().evaluate(el=>getComputedStyle(el).whiteSpace),'normal','Private full email must wrap, not ellipsize');
  assert.equal(await page.locator('#codex-list [name="health"]').inputValue(),'Tất cả','Real select uses first choice');
  await page.getByRole('button',{name:'alpha@example.invalid',exact:true}).click();
  assert.match(await page.locator('#codex-detail').textContent(),/Không rõ/);
  assert(await page.locator('#codex-consume').isDisabled());
  const initialCommands=commands.length;await page.getByRole('button',{name:'Tải lại dữ liệu đã lưu',exact:true}).click();
  await page.locator('#codex-refresh-quota').waitFor();assert.equal(commands.length,initialCommands,'Reload is storage GET only');
  await page.locator('#codex-refresh-quota').click();await page.waitForFunction(()=>document.querySelector('#codex-detail').textContent.includes('58%'));
  assert.equal(await page.locator('#codex-detail meter').count(),2);assert.equal(await page.locator('#codex-detail meter').first().getAttribute('value'),'58');
  assert.match(await page.locator('#codex-detail').textContent(),/5 giờ/);assert.match(await page.locator('#codex-detail').textContent(),/7 ngày/);assert.match(await page.locator('#codex-detail').textContent(),/58% còn lại/);
  // Make the cached credit version stale through a real explicit admin refresh.
  await page.evaluate(async()=>{const s=await(await fetch('/api/session')).json();const accounts=await(await fetch('/api/service/oauth-accounts')).json();const row=accounts.items.find(a=>a.email==='alpha@example.invalid');const r=await fetch(`/api/service/oauth-accounts/${row.id}/quota/refresh`,{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':s.csrf_token},body:'{}'});if(!r.ok)throw Error('fixture refresh failed');});
  const rejected=page.waitForResponse(r=>r.url().endsWith('/consume')&&r.request().method()==='POST');const creditsReload=page.waitForResponse(r=>r.url().endsWith('/reset-credits')&&r.request().method()==='GET');
  await page.locator('#codex-ack').check();await page.locator('#codex-consume').click();assert.equal((await rejected).status(),409);await creditsReload;
  await page.waitForFunction(()=>!document.querySelector('#codex-ack').disabled);
  assert.equal(commands.filter(c=>c.path.endsWith('/consume')).length,1);assert(await page.locator('#codex-consume').isDisabled());assert(!await page.locator('#codex-ack').isChecked());
  await page.locator('#codex-ack').check();assert(await page.locator('#codex-consume').isEnabled());
  await page.locator('#codex-consume').click();await page.waitForFunction(()=>document.querySelector('#codex-reset-state').textContent.includes('succeeded_refresh_failed'));
  assert(await page.locator('#codex-consume').isDisabled());
  const recovery=page.waitForResponse(r=>r.url().endsWith('/quota/refresh')&&r.request().method()==='POST');
  await page.locator('#codex-refresh-quota').click();assert.equal((await recovery).status(),200);await page.waitForFunction(()=>document.querySelector('#codex-credit-freshness').textContent==='fresh'&&!document.querySelector('#codex-refresh-quota').disabled);
  assert.equal(commands.filter(c=>c.path.endsWith('/consume')).length,2);assert(await page.locator('#codex-consume').isDisabled());
  assert.equal(await page.locator('#codex-reset-state').textContent(),'succeeded','Server finalizes confirmed success after complete explicit refresh');
  let releaseOverview,overviewEntered;const overviewGate=new Promise(r=>releaseOverview=r),overviewStarted=new Promise(r=>overviewEntered=r);
  await page.route(base+'/api/service/overview',async route=>{const response=await route.fetch();overviewEntered();await overviewGate;await route.fulfill({response});});
  await page.reload();await overviewStarted;await page.getByRole('button',{name:'Tài khoản Codex',exact:true}).click();await page.getByRole('button',{name:'alpha@example.invalid',exact:true}).waitFor();
  releaseOverview();await page.waitForResponse(r=>r.url()===base+'/api/service/overview');await page.unroute(base+'/api/service/overview');
  await page.getByRole('button',{name:'alpha@example.invalid',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('#codex-reset-state').textContent==='succeeded');assert(await page.locator('#codex-consume').isDisabled());
  await page.locator('#codex-ack').check();assert(await page.locator('#codex-consume').isEnabled());await page.locator('#codex-ack').uncheck();
  // Pool CAS: another admin updates the real store after this UI loads v0.
  await page.locator('#codex-pool-model').selectOption('vendor/model:v1');await page.waitForFunction(()=>document.querySelector('#codex-pool-form'));
  await page.locator('#codex-pool-form [name="mode"]').selectOption('single');assert(await page.locator('#codex-pool-save').isDisabled(),'Single mode without an explicit account must not choose the first account');await page.locator('#codex-pool-form [name="mode"]').selectOption('auto');
  const poolVersion=await page.evaluate(async()=>{const s=await(await fetch('/api/session')).json();const p=await(await fetch('/api/service/account-pools/vendor/model:v1')).json();const r=await fetch('/api/service/account-pools/vendor/model:v1',{method:'PUT',headers:{'Content-Type':'application/json','X-CSRF-Token':s.csrf_token},body:JSON.stringify({version:p.version,policy:{members:[]}})});if(!r.ok)throw Error('fixture CAS');return (await r.json()).version;});
  await page.locator('#codex-pool-save').click();await page.waitForFunction(()=>document.querySelector('#codex-message').textContent.includes('409'));
  assert.match(await page.locator('#codex-message').textContent(),/Tải lại/);
  await page.locator('#codex-pool-reload').click();await page.waitForFunction(v=>document.querySelector('#codex-pool-version').textContent==='version '+v,poolVersion);
  await page.locator('#codex-pool-save').click();await page.waitForFunction(v=>document.querySelector('#codex-pool-version').textContent==='version '+v,poolVersion+1);
  // Exact balances and append-only grant from real ledger.
  await page.locator('[data-codex-tab="keys"]').click();await page.locator('#codex-key').selectOption({label:'Demo key'});await page.waitForFunction(()=>document.querySelector('#codex-balance').textContent.includes('99998915'));
  await page.locator('#codex-grant-amount').fill('1085');await page.locator('#codex-grant-reason').fill('Synthetic reconciliation');
  await page.locator('#codex-grant-ack').check();await page.locator('#codex-grant-submit').click();
  await page.waitForFunction(()=>document.querySelector('#codex-balance').textContent.includes('100001085'));
  const balance=await page.locator('#codex-balance').textContent();assert.match(balance,/100000000/);assert.match(balance,/1085/);
  await page.locator('[data-codex-tab="usage"]').click();assert(await page.locator('#codex-reports').isVisible());assert(await page.locator('#codex-accounts-panel').isHidden());
  assert.match(await page.locator('#codex-reports').textContent(),/1085/);assert.match(await page.locator('#codex-reports').textContent(),/Không rõ/);
  await page.locator('[data-codex-tab="models"]').click();assert(await page.locator('#codex-capabilities').isVisible());assert(await page.locator('#codex-reports').isHidden());
  assert.match(await page.locator('#codex-capabilities').textContent(),/websocket.*hỗ trợ/);
  assert.equal(await page.locator('#codex-capabilities input').count(),0);
  await page.locator('[data-codex-tab="overview"]').click();await page.locator('#codex-overview .codex-stat').first().waitFor();
  assert.doesNotMatch(await page.locator('#codex-overview').textContent(),/3\.4K|713\.3M|1462\.05|15\.81s/,'Overview must never contain copied screenshot metrics');
  const status=await page.evaluate(async()=> (await(await fetch('/api/service/codex-service/status')).json()));
  assert.match(await page.locator('#codex-overview').textContent(),new RegExp(String(status.usage.requests)));
  // The private service selector round-trips the provider's profile with an
  // optimistic version.  Profile UUIDs are used only in closures/fetches and
  // never rendered as option values or text.
  const proxyBefore=await page.evaluate(async()=> (await(await fetch('/api/service/codex-service/proxy')).json()));
  const proxyProfiles=await page.evaluate(async()=> (await(await fetch('/api/service/proxies')).json()));
  const fixedProfile=proxyProfiles.find(p=>p.name==='fixed demo');assert(fixedProfile,'fixture must expose a fixed profile');
  const proxySelect=page.locator('#codex-service-proxy-select');await proxySelect.waitFor();assert.equal(await proxySelect.inputValue(),'');
  await proxySelect.selectOption({label:'fixed · fixed demo'});assert(await page.locator('#codex-service-proxy-save').isEnabled());
  let releaseProxy,enteredProxy;const proxyGate=new Promise(r=>releaseProxy=r),proxyStarted=new Promise(r=>enteredProxy=r);
  await page.route(base+'/api/service/codex-service/proxy',async route=>{if(route.request().method()!=='PUT')return route.continue();const response=await route.fetch();enteredProxy();await proxyGate;await route.fulfill({response});});
  const savedProxyResponse=page.waitForResponse(r=>r.url().endsWith('/api/service/codex-service/proxy')&&r.request().method()==='PUT');await page.locator('#codex-service-proxy-save').click();await proxyStarted;
  assert(await page.locator('#codex-service-proxy-save').isDisabled());assert(await proxySelect.isDisabled());assert(await page.locator('#codex-reload-saved').isDisabled());releaseProxy();assert.equal((await savedProxyResponse).status(),200);await page.unroute(base+'/api/service/codex-service/proxy');
  await page.waitForFunction(()=>document.querySelector('#codex-service-proxy-select')?.selectedOptions[0]?.textContent==='fixed · fixed demo');
  const proxyAfter=await page.evaluate(async()=> (await(await fetch('/api/service/codex-service/proxy')).json()));assert.equal(proxyAfter.proxy_profile_id,fixedProfile.id);assert.equal(proxyAfter.version,proxyBefore.version+1);assert(!((await page.locator('#codex-service-proxy').textContent())??'').includes(fixedProfile.id));
  // Make the UI's version stale.  A 409 reloads authoritative config but
  // never repeats the user's PUT, and requires a fresh explicit selection.
  const staleVersion=proxyAfter.version;await page.evaluate(async({version})=>{const s=await(await fetch('/api/session')).json();const r=await fetch('/api/service/codex-service/proxy',{method:'PUT',headers:{'Content-Type':'application/json','X-CSRF-Token':s.csrf_token},body:JSON.stringify({version,proxy_profile_id:null})});if(!r.ok)throw Error('fixture proxy CAS seed '+r.status);},{version:staleVersion});
  const proxyWritesBeforeConflict=commands.filter(c=>c.path.endsWith('/codex-service/proxy')).length;
  await proxySelect.selectOption({label:'fixed · fixed demo'});const conflictPut=page.waitForResponse(r=>r.url().endsWith('/api/service/codex-service/proxy')&&r.request().method()==='PUT');await page.locator('#codex-service-proxy-save').click();assert.equal((await conflictPut).status(),409);
  await page.waitForFunction(()=>document.querySelector('#codex-message').textContent.includes('409'));assert.equal(await page.locator('#codex-service-proxy-select').inputValue(),'');assert(await page.locator('#codex-service-proxy-save').isEnabled());
  assert.equal(commands.filter(c=>c.path.endsWith('/codex-service/proxy')).length,proxyWritesBeforeConflict+1,'Conflict must not automatically resend the proxy PUT');
  const proxyHtml=await page.locator('#codex-service-proxy').innerHTML();for(const profile of proxyProfiles)assert(!proxyHtml.includes(profile.id));
  await page.locator('[data-codex-tab="accounts"]').click();
  // Quota and freshness columns use only storage projections, never refresh POST.
  assert.match(await page.locator('#codex-list').textContent(),/Quota OpenAI/);
  assert.match(await page.locator('#codex-list').textContent(),/Freshness/);assert.match(await page.locator('#codex-list').textContent(),/58%/);
  // Confirmed-success recovery needs no evidence mutation or extra consume.
  assert(await page.locator('#codex-evidence button').isDisabled());
  assert.equal(commands.filter(c=>c.path.endsWith('/resolve')).length,0);
  // Settings select profile references without emitting them in DOM values.
  await page.locator('[data-codex-tab="accounts"]').click();await page.locator('#codex-detail [name="profile"]').selectOption({label:'direct · direct demo'});
  await page.getByRole('button',{name:'Lưu tài khoản',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('#codex-list').textContent.includes('direct demo'));
  const accounts=await page.evaluate(async()=> (await(await fetch('/api/service/oauth-accounts')).json()).items);
  const html=await page.locator('#codex-workspace').innerHTML();for(const row of accounts)assert(!html.includes(row.id),'Account IDs must not appear in DOM, including values');
  for(const sentinel of ['SYNTHETIC-ACCOUNT','SYNTHETIC-ACCESS','SYNTHETIC-REFRESH','SYNTHETIC-ID','SYNTHETIC-CREDIT','SYNTHETIC-SECRET'])assert(!html.includes(sentinel));
  await page.locator('#codex-refresh-quota').focus();assert.equal(await page.evaluate(()=>document.activeElement.id),'codex-refresh-quota');await page.keyboard.press('Tab');assert.notEqual(await page.evaluate(()=>document.activeElement.tagName),'BODY');
  assert.equal(await page.locator('#codex-message').getAttribute('role'),'status');
  await mkdir(artifacts,{recursive:true});await page.screenshot({path:join(artifacts,'desktop.png'),fullPage:true});await page.setViewportSize({width:390,height:844});
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));await page.screenshot({path:join(artifacts,'mobile.png'),fullPage:true});
  // Model and key cache controls round-trip null inheritance versus explicit 0.
  await page.locator('[data-nav="models"]').click();await page.getByRole('button',{name:'vendor/model:v1',exact:true}).click();
  await page.locator('[name="cache_read"]').fill('0');await page.locator('[name="cache_write"]').fill('');await page.locator('#editor-form [type="submit"]').click();await page.getByRole('button',{name:'vendor/model:v1',exact:true}).click();
  assert.equal(await page.locator('[name="cache_read"]').inputValue(),'0');assert.equal(await page.locator('[name="cache_write"]').inputValue(),'');
  await page.locator('[data-nav="keys"]').click();await page.getByRole('button',{name:'Demo key',exact:true}).click();
  await page.locator('[name="overrides"]').fill('vendor/model:v1|1|3|0|');await page.locator('[name="model_prefix"]').fill('team/');await page.locator('[name="excluded_model_ids"]').fill('legacy');await page.locator('#editor-form [type="submit"]').click();await page.getByRole('button',{name:'Demo key',exact:true}).click();
  assert.equal(await page.locator('[name="overrides"]').inputValue(),'vendor/model:v1|1|3|0|');assert.equal(await page.locator('[name="model_prefix"]').inputValue(),'team/');assert.equal(await page.locator('[name="excluded_model_ids"]').inputValue(),'legacy');
  await page.locator('[data-nav="audit"]').click();assert.match(await page.locator('#rows').textContent(),/quota.*adjust|quota.*grant/);
  // Delay real quota GET: switching account must reject late response; returning must not stick busy.
  await page.getByRole('button',{name:'Tài khoản Codex',exact:true}).click();await page.locator('#codex-list').waitFor();
  let release,entered;const gate=new Promise(r=>release=r),started=new Promise(r=>entered=r);let delayed=false;
  const quotaPattern=base+'/api/service/oauth-accounts/*/quota';
  await page.route(quotaPattern,async route=>{if(delayed)return route.continue();delayed=true;const response=await route.fetch();entered();await gate;await route.fulfill({response});});
  await page.getByRole('button',{name:'alpha@example.invalid',exact:true}).click();await started;
  await page.getByRole('button',{name:'beta@example.invalid',exact:true}).click();release();
  await page.waitForFunction(()=>document.querySelector('#codex-detail h3').textContent==='beta@example.invalid'&&!document.querySelector('#codex-refresh-quota').disabled);
  assert.match(await page.locator('#codex-detail').textContent(),/Không rõ/);await page.unroute(quotaPattern);
  await page.getByRole('button',{name:'alpha@example.invalid',exact:true}).click();await page.waitForFunction(()=>!document.querySelector('#codex-refresh-quota').disabled);
  // Real FastAPI multi-file bulk import: 226 new records plus duplicate and malformed rows.
  const record=n=>({id:crypto.randomUUID(),email:`batch-${n}@example.invalid`,account:{id:`SYNTHETIC-BATCH-${n}`},tokens:{id_token:'SYNTHETIC-ID',access_token:'SYNTHETIC-ACCESS',refresh_token:'SYNTHETIC-REFRESH'}});
  const bulk=Array.from({length:226},(_,i)=>record(i));
  await page.locator('[data-nav="oauth"]').click();await page.locator('#codex-batch-files').waitFor();
  assert.equal(await page.locator('#codex-batch-files').getAttribute('accept'),'application/json,.json');assert(await page.locator('#codex-batch-files').evaluate(el=>el.multiple));
  // An incidental list/quota redraw must not destroy a draft being typed.
  let releaseDraft,enteredDraft;const draftGate=new Promise(r=>releaseDraft=r),draftStarted=new Promise(r=>enteredDraft=r);let draftDelayed=false;
  await page.route(quotaPattern,async route=>{if(draftDelayed)return route.continue();draftDelayed=true;const response=await route.fetch();enteredDraft();await draftGate;await route.fulfill({response});});
  await page.locator('#codex-reload-saved').click();await draftStarted;
  const draft=JSON.stringify([record('draft')]);await page.locator('#codex-batch-import [name="batch-json"]').fill(draft);assert.equal(await page.locator('#codex-batch-import [name="batch-json"]').inputValue(),draft);releaseDraft();
  await page.waitForFunction(value=>document.querySelector('#codex-batch-import [name="batch-json"]')?.value===value,draft);await page.unroute(quotaPattern);await page.locator('#codex-batch-import [name="batch-json"]').fill('');
  await page.locator('#codex-batch-files').setInputFiles([{name:'first.json',mimeType:'application/json',buffer:Buffer.from(JSON.stringify(bulk.slice(0,113)))},{name:'second.json',mimeType:'application/json',buffer:Buffer.from(JSON.stringify([...bulk.slice(113),bulk[0],{email:'invalid@example.invalid'},null]))}]);
  let releaseImport,enteredImport;const importGate=new Promise(r=>releaseImport=r),importStarted=new Promise(r=>enteredImport=r);
  await page.route(base+'/api/service/oauth/import-batch',async route=>{const response=await route.fetch();enteredImport();await importGate;await route.fulfill({response});});
  const importedResponse=page.waitForResponse(r=>r.url().endsWith('/oauth/import-batch')&&r.request().method()==='POST');await page.locator('#codex-batch-import [type="submit"]').click();await importStarted;
  assert(await page.locator('#codex-batch-import [type="submit"]').isDisabled(),'A running batch must not be submitted twice');releaseImport();const imported=await importedResponse;assert.equal(imported.status(),200);const result=await imported.json();assert.equal(result.imported,226);assert.equal(result.duplicate,1);assert.equal(result.failed,2);assert.equal(result.results.length,229);
  await page.waitForFunction(()=>document.querySelector('#codex-batch-result').textContent.includes('Đã nhập 226')&&!document.querySelector('#codex-batch-import [type="submit"]').disabled,{},{timeout:60000});
  assert.equal(await page.locator('#codex-batch-result li').count(),229);assert.equal(await page.locator('#codex-batch-files').inputValue(),'');
  await page.unroute(base+'/api/service/oauth/import-batch');const priorResult=await page.locator('#codex-batch-result').elementHandle();await page.getByRole('button',{name:'Tải lại dữ liệu đã lưu',exact:true}).click();await page.waitForFunction(el=>!el.isConnected,priorResult,{timeout:60000});await page.waitForFunction(()=>document.querySelector('#codex-batch-result').textContent.includes('Đã nhập 226'));
  assert.match(await page.locator('#codex-batch-result').textContent(),/Trùng 1.*Lỗi 2/);
  await page.locator('#codex-batch-import [name="batch-json"]').fill(JSON.stringify([bulk[0],record('paste'),{tokens:{access_token:'SYNTHETIC-ACCESS'}}]));
  const pastedResponse=page.waitForResponse(r=>r.url().endsWith('/oauth/import-batch')&&r.request().method()==='POST');await page.locator('#codex-batch-import [type="submit"]').click();const pasted=await (await pastedResponse).json();assert.equal(pasted.imported,1);assert.equal(pasted.duplicate,1);assert.equal(pasted.failed,1);
  await page.waitForFunction(()=>document.querySelector('#codex-batch-result').textContent.includes('Đã nhập 1')&&!document.querySelector('#codex-batch-import [type="submit"]').disabled,{},{timeout:60000});
  assert.equal(await page.locator('#codex-batch-import [name="batch-json"]').inputValue(),'');assert.equal(await page.locator('#codex-batch-result li').count(),3);
  for(const sentinel of ['SYNTHETIC-ACCESS','SYNTHETIC-REFRESH','SYNTHETIC-ID'])assert(!(await page.locator('#codex-workspace').innerHTML()).includes(sentinel),'Import results must not render secrets');
  let releaseLogout,enteredLogout;const logoutGate=new Promise(r=>releaseLogout=r),logoutStarted=new Promise(r=>enteredLogout=r);
  await page.route(quotaPattern,async route=>{const response=await route.fetch();enteredLogout();await logoutGate;await route.fulfill({response}).catch(()=>{});});
  await page.getByRole('button',{name:'Tải lại dữ liệu đã lưu',exact:true}).click();await logoutStarted;
  await page.locator('#logout').click();releaseLogout();await page.locator('#login-panel').waitFor({state:'visible'});assert.equal(await page.locator('#codex-workspace').textContent(),'');
  assert.equal(await page.evaluate(()=>localStorage.length+sessionStorage.length),0);
  assert.deepEqual(errors,[]);assert.deepEqual(external,[]);assert.equal(commands.filter(c=>c.path.endsWith('/consume')).length,2);
  const bodies=commands.filter(c=>c.path.endsWith('/consume')).map(c=>c.body);assert.notEqual(bodies[0].request_id,bodies[1].request_id);
  console.log('Codex browser PASS: real PG/session, storage-only reads, reset warning/reload/recovery without resend, CAS, exact grant/audit, cache rates, redaction, keyboard/ARIA, desktop/mobile390, no external network.');}
}finally{await browser?.close();server.kill('SIGTERM');await new Promise(r=>{if(server.exitCode!==null)r();else server.once('exit',r);});}
