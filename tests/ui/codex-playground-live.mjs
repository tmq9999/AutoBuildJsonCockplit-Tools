// Explicit opt-in only: real local gateway, real Codex inference, no mocks.
// Creates temporary admin records; revokes/disables them while retaining audit.
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {chromium,request} from 'playwright-core';

if(process.env.AUTOBUILD_LIVE_VERIFY!=='1')throw Error('Set AUTOBUILD_LIVE_VERIFY=1 to permit one real inference request.');
const base='http://127.0.0.1:8787';
const api=await request.newContext({baseURL:base,extraHTTPHeaders:{Origin:base},timeout:180000});
let csrf,browser,customer,key,published;
async function call(path,method='GET',data){
  const response=await api.fetch(path,{method,data,headers:csrf?{'X-CSRF-Token':csrf}:{}});
  assert(response.ok(),`${method} ${path}: HTTP ${response.status()}`);
  return response.json();
}
try{
  const config=JSON.parse(await readFile('data/local-gateway/admin-data/local-config.json','utf8'));
  ({csrf_token:csrf}=await call('/api/session','POST',{token:config.admin_token}));
  browser=await chromium.launch({executablePath:process.env.CHROME_PATH||'/usr/bin/google-chrome',headless:true,args:['--no-sandbox']});
  const context=await browser.newContext({storageState:await api.storageState()});
  const page=await context.newPage(),errors=[];
  page.on('pageerror',error=>errors.push(error.name));
  await page.goto(base+'/service/',{waitUntil:'networkidle'});
  await page.locator('#codex-workspace').waitFor({state:'visible'});

  // Read the real paginated account list and submit a report filter after
  // changing pages. No quota refresh, OAuth, or account edits are performed.
  await page.waitForFunction(()=>document.querySelector('#codex-list')?.getAttribute('aria-busy')==='false');
  await page.locator('[data-codex-tab="usage"]').click();
  const accountSelector=page.locator('.codex-report-filters [name="account"]');
  const firstPageIds=await accountSelector.locator('option').evaluateAll(items=>items.map(item=>item.value).filter(Boolean));
  assert(firstPageIds.length>0);
  const selectedAccount=firstPageIds[0];
  assert.match(selectedAccount,/^[0-9a-f-]{36}$/i);
  await accountSelector.selectOption(selectedAccount);
  await page.locator('[data-codex-tab="accounts"]').click();
  const pager=page.locator('.codex-pagination[data-position="top"]');
  await pager.getByRole('button',{name:'Trang tiếp theo',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('.codex-pagination[data-position="top"]')?.dataset.page==='2'
    &&document.querySelector('#codex-list')?.getAttribute('aria-busy')==='false');
  await page.locator('[data-codex-tab="usage"]').click();
  assert.equal(await accountSelector.inputValue(),selectedAccount);
  const mergedIds=await accountSelector.locator('option').evaluateAll(items=>items.map(item=>item.value).filter(Boolean));
  assert(mergedIds.some(id=>!firstPageIds.includes(id)),'Visited account page must be merged into report options');
  const reportResponse=page.waitForResponse(response=>{
    const url=new URL(response.url());
    return url.pathname==='/api/service/usage/requests'&&url.searchParams.get('credential_id')===selectedAccount;
  });
  await page.locator('.codex-report-filters').getByRole('button',{name:'Tải báo cáo đã lưu',exact:true}).click();
  const report=await reportResponse;assert.equal(report.status(),200);
  assert((await report.json()).items.every(row=>row.credential_id===selectedAccount));
  console.log(JSON.stringify({check:'real_browser_paginated_account_filter',pages:2,selection_preserved:true}));

  // A second admin publication must appear on the very first Playground entry.
  // No binding is created, so this temporary model never grants upstream access.
  const id='verify-playground-'+crypto.randomUUID();
  await call('/api/service/models','POST',{model_id:id,identity:'gpt-6-astra',enabled:true});
  published=id;
  await page.locator('[data-nav="playground"]').click();
  await page.waitForFunction(()=>!document.getElementById('new-item').disabled);
  const choices=await page.locator('#field-model option').evaluateAll(options=>options.map(option=>option.value));
  assert(choices.includes(id),'First Playground entry must include the freshly published model without clicking New again');

  customer=await call('/api/service/customers','POST',{name:'Live browser acceptance'});
  key=await call('/api/service/keys','POST',{customer_id:customer.id,name:'Temporary live browser key',policy:{
    model_ids:['gpt-6-astra'],protocols:['openai'],total_micro:'100000000000000',
    expires_at:new Date(Date.now()+600000).toISOString(),
  }});
  await page.locator('#field-client_key').fill(key.secret);
  await page.locator('#field-model').selectOption('gpt-6-astra');
  await page.locator('#field-thinking').selectOption('high');
  const resultPromise=page.waitForResponse(response=>response.url()===base+'/api/service/playground'
    &&response.request().method()==='POST',{timeout:180000});
  await page.getByRole('button',{name:'Gửi request thật',exact:true}).click();
  const response=await resultPromise;
  assert.equal(response.status(),200);
  const result=await response.json();
  const answer=result.output.filter(item=>item.type==='message').flatMap(item=>item.content)
    .filter(item=>item.type==='output_text').map(item=>item.text).join('');
  assert.match(answer,/^API_OK\.?$/);
  assert(result.usage.input_tokens>0&&result.usage.output_tokens>0);
  await page.locator('#playground-result').waitFor();
  console.log(JSON.stringify({check:'real_browser_playground',http:200,answer,usage:result.usage}));

  await page.locator('[data-nav="codex-accounts"]').click();
  await page.locator('[data-codex-tab="overview"]').click();
  await page.locator('[data-usage="total_tokens"] strong').waitFor();
  const total=await page.locator('[data-usage="total_tokens"] strong').innerText();
  const status=await call('/api/service/codex-service/status');
  assert.equal(total,BigInt(status.usage.total_tokens).toLocaleString('vi-VN'));
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({check:'fresh_models_and_dashboard',total,js_errors:0}));
}finally{
  // Independent cleanup actions: one failure must not prevent key revocation.
  const cleanup=await Promise.allSettled([
    browser?.close(),
    key?call(`/api/service/keys/${key.key_id}/revoke`,'POST',{version:key.version}):undefined,
    customer?call(`/api/service/customers/${customer.id}`,'DELETE',{version:1}):undefined,
    published?call(`/api/service/models/${published}`,'DELETE',{version:1}):undefined,
  ]);
  await api.dispose();
  assert(cleanup.every(result=>result.status==='fulfilled'),'Temporary verification record cleanup failed');
}
