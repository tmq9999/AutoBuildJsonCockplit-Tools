import assert from 'node:assert/strict';

// All pool/key writes reach the real synthetic FastAPI/PG fixture. Paging is
// reduced using the real endpoint's limit query, not fabricated account DTOs.
export async function codexReviewRegressions(context,base){
  const failures=[];
  async function scenario(name,run){
    const page=await context.newPage();page.setDefaultTimeout(8000);
    try{await page.goto(base+'/service/');await page.locator('#workspace').waitFor({state:'visible'});await run(page);console.log('Review regression PASS:',name);}
    catch(error){failures.push(name+': '+error.message);console.log('Review regression FAIL:',name,error.message);}
    finally{await page.close();}
  }
  const get=(page,path)=>page.evaluate(async path=>{const response=await fetch('/api/service/'+path);if(!response.ok)throw Error('fixture GET '+response.status);return response.json();},path);
  const put=(page,path,method,body)=>page.evaluate(async({path,method,body})=>{const session=await(await fetch('/api/session')).json();const response=await fetch('/api/service/'+path,{method,headers:{'Content-Type':'application/json','X-CSRF-Token':session.csrf_token},body:JSON.stringify(body)});if(!response.ok)throw Error('fixture write '+response.status);return response.json();},{path,method,body});
  const poolPath='account-pools/vendor/model:v1';
  async function seedPool(page,policy){const pool=await get(page,poolPath);return put(page,poolPath,'PUT',{version:pool.version,policy});}
  async function openPool(page,paged=false){
    if(paged)await page.route(/\/api\/service\/oauth-accounts(?:\?.*)?$/,async route=>{const url=new URL(route.request().url());url.searchParams.set('limit','1');const response=await route.fetch({url:url.href});await route.fulfill({response});});
    await page.getByRole('button',{name:'Tài khoản Codex',exact:true}).click();await page.locator('#codex-list').waitFor();
    await page.locator('#codex-pool-model').selectOption('vendor/model:v1');await page.locator('#codex-pool-form').waitFor();
  }
  async function savePool(page){assert(await page.locator('#codex-pool-save').isEnabled(),'Valid pool must be saveable');assert(await page.locator('#codex-pool-form').evaluate(f=>f.checkValidity()),'Pool form is valid');const response=page.waitForResponse(r=>r.request().method()==='PUT'&&decodeURIComponent(new URL(r.url()).pathname).endsWith(poolPath));await page.locator('#codex-pool-save').click();assert.equal((await response).status(),200);}
  await scenario('dynamic full pool and eight-account limit persist through real admin API',async page=>{
    await seedPool(page,{members:[]});await openPool(page,true);
    await page.locator('#codex-pool-form [name="mode"]').selectOption('round_robin');
    await page.locator('#codex-pool-form [name="all_accounts"]').check();
    await page.locator('#codex-pool-form [name="retry_limit"]').selectOption('7');await savePool(page);
    const saved=await get(page,poolPath);assert.equal(saved.policy.mode,'round_robin');assert.equal(saved.policy.all_accounts,true);assert.equal(saved.policy.retry_limit,7);assert.deepEqual(saved.policy.members,[]);
    const oldForm=await page.locator('#codex-pool-form').elementHandle();await page.locator('#codex-pool-reload').click();
    await page.waitForFunction(el=>!el.isConnected,oldForm);assert(await page.locator('#codex-pool-form [name="all_accounts"]').isChecked());
    assert(!(await page.locator('#codex-pool').textContent()).includes('Request mới sẽ tạm dừng'));
    await page.locator('#codex-pool-form [name="mode"]').selectOption('single');
    assert(await page.locator('#codex-pool-form [name="all_accounts"]').isDisabled());assert(!await page.locator('#codex-pool-form [name="all_accounts"]').isChecked());assert(await page.locator('#codex-pool-save').isDisabled());
  });
  await scenario('paging preserves a member outside the rendered controls',async page=>{
    const {items}=await get(page,'oauth-accounts');const before=await seedPool(page,{members:[{credential_id:items[0].id,priority:1,weight:2,backup:false},{credential_id:items[1].id,priority:7,weight:8,backup:true}]});
    await openPool(page,true);assert.equal(await page.locator('#codex-pool-form .codex-member').count(),1);
    await page.locator('.codex-pagination[data-position="top"]').getByRole('button',{name:'Trang tiếp theo',exact:true}).click();await page.locator('#codex-list').getByRole('button',{name:items[1].email,exact:true}).waitFor();
    await page.locator('#codex-pool-members summary').click();await page.locator('#codex-pool-form [name="weight-0"]').fill('9');await savePool(page);const saved=await get(page,poolPath);
    assert.deepEqual(saved.policy.members.find(m=>m.credential_id===items[1].id),before.policy.members[1],'Unrendered member must survive paging plus valid CAS save');
    assert.equal(saved.policy.members.find(m=>m.credential_id===items[0].id).weight,9);
  });
  await scenario('empty single selection never chooses the first account',async page=>{
    const {items}=await get(page,'oauth-accounts');await seedPool(page,{members:[{credential_id:items[0].id}]});await openPool(page);
    await page.locator('#codex-pool-form [name="mode"]').selectOption('single');await page.locator('#codex-pool-form [name="single"]').selectOption('');
    assert(await page.locator('#codex-pool-save').isDisabled(),'Empty single must prevent submission rather than implicitly select account[0]');
  });
  await scenario('off-page single selection stays valid after loading that account',async page=>{
    const {items}=await get(page,'oauth-accounts');await seedPool(page,{mode:'single',members:[{credential_id:items[1].id,priority:3,weight:4,backup:false}],single_credential_id:items[1].id});await openPool(page,true);
    await page.locator('.codex-pagination[data-position="top"]').getByRole('button',{name:'Trang tiếp theo',exact:true}).click();await page.locator('#codex-list').getByRole('button',{name:items[1].email,exact:true}).waitFor();
    await savePool(page);const saved=await get(page,poolPath);assert.equal(saved.policy.single_credential_id,items[1].id,'Existing off-page single ID must not become first account');assert.equal(saved.policy.members.length,1);
  });
  await scenario('real 409 reloads keys before a new explicitly confirmed grant',async page=>{
    const [key]=await get(page,'keys');await page.getByRole('button',{name:'Tài khoản Codex',exact:true}).click();await page.locator('[data-codex-tab="keys"]').click();await page.locator('#codex-key').selectOption({label:'Demo key'});
    // Another admin commits an audited zero grant, leaving this form's version stale.
    await put(page,`keys/${key.key_id}/quota-adjust`,'POST',{request_id:crypto.randomUUID(),version:key.version,amount_micro:'0',reason:'Synthetic concurrent admin'});
    await page.locator('#codex-grant-amount').fill('0');await page.locator('#codex-grant-reason').fill('Synthetic retry');await page.locator('#codex-grant-ack').check();
    let keyReloaded=false;const observeReload=async response=>{if(response.url()===base+'/api/service/keys'&&response.status()===200){await response.finished();keyReloaded=true;page.off('response',observeReload);}};page.on('response',observeReload);
    const rejected=page.waitForResponse(r=>r.request().method()==='POST'&&r.url().endsWith('/quota-adjust'));await page.locator('#codex-grant-submit').click();assert.equal((await rejected).status(),409);
    await page.waitForFunction(()=>document.querySelector('#codex-message').textContent.includes('409'));
    assert(keyReloaded,'Definite rejection must finish authoritative key reload before reporting recovery');assert(await page.locator('#codex-grant-ack').isEnabled(),'Authoritative key reload must unlock a definite 409 rejection');
    assert(!await page.locator('#codex-grant-ack').isChecked(),'Recovery requires a new confirmation');
    await page.locator('#codex-grant-amount').fill('0');await page.locator('#codex-grant-reason').fill('Explicit synthetic retry');await page.locator('#codex-grant-ack').check();
    const accepted=page.waitForResponse(r=>r.request().method()==='POST'&&r.url().endsWith('/quota-adjust'));await page.locator('#codex-grant-submit').click();assert.equal((await accepted).status(),200);
    const [updated]=await get(page,'keys');assert.equal(updated.version,key.version+2);assert.equal(updated.balance.total_micro,'100000000000000');
  });
  await scenario('ambiguous grant outcome stays locked after real receipt is lost',async page=>{
    await page.getByRole('button',{name:'Tài khoản Codex',exact:true}).click();await page.locator('[data-codex-tab="keys"]').click();await page.locator('#codex-key').selectOption({label:'Demo key'});
    let posts=0;await page.route(base+'/api/service/keys/*/quota-adjust',async route=>{posts++;const response=await route.fetch();assert.equal(response.status(),200);await route.abort('failed');});
    await page.locator('#codex-grant-amount').fill('0');await page.locator('#codex-grant-reason').fill('Synthetic lost receipt');await page.locator('#codex-grant-ack').check();await page.locator('#codex-grant-submit').click();
    await page.waitForFunction(()=>document.querySelector('#codex-keys').textContent.includes('Kết quả cấp quota chưa rõ'));
    assert(await page.locator('#codex-grant-ack').isDisabled());assert(await page.locator('#codex-grant-submit').isDisabled());assert.equal(posts,1);
  });
  await scenario('unavailable Codex provider does not hide service status',async page=>{
    await page.route(base+'/api/service/codex-service/proxy',route=>route.fulfill({status:404,contentType:'application/json',body:JSON.stringify({error:'not_found'})}));
    await page.getByRole('button',{name:'Tài khoản Codex',exact:true}).click();await page.locator('[data-codex-tab="overview"]').click();
    await page.waitForFunction(()=>document.querySelector('#codex-service-proxy')?.textContent.includes('Chưa có provider Codex'));
    assert.equal(await page.locator('#codex-overview .codex-stat').count(),5);assert.equal(await page.locator('#codex-service-proxy-save').count(),0);
    assert(!((await page.locator('#codex-message').textContent())??'').includes('not_found'));
  });
  await scenario('missing profile never silently falls back to direct and reload fetches profiles',async page=>{
    const profile=(await get(page,'proxies')).find(p=>p.name==='fixed demo');assert(profile);
    const before=await get(page,'codex-service/proxy');await put(page,'codex-service/proxy','PUT',{version:before.version,proxy_profile_id:profile.id});
    let hide=true;await page.route(base+'/api/service/proxies',async route=>{const response=await route.fetch();const rows=await response.json();await route.fulfill({response,json:hide?rows.filter(p=>p.id!==profile.id):rows});});
    await page.getByRole('button',{name:'Tài khoản Codex',exact:true}).click();await page.locator('[data-codex-tab="overview"]').click();await page.locator('#codex-service-proxy-select').waitFor();
    assert.equal(await page.locator('#codex-service-proxy-select').inputValue(),'__missing__');assert(await page.locator('#codex-service-proxy-save').isDisabled());
    assert.equal((await get(page,'codex-service/proxy')).proxy_profile_id,profile.id);
    hide=false;await page.locator('#codex-reload-saved').click();await page.waitForFunction(()=>document.querySelector('#codex-service-proxy-select')?.selectedOptions[0]?.textContent==='fixed · fixed demo');
    assert(await page.locator('#codex-service-proxy-save').isEnabled());const current=await get(page,'codex-service/proxy');await put(page,'codex-service/proxy','PUT',{version:current.version,proxy_profile_id:null});
  });
  await scenario('late proxy save cannot overwrite reopened workspace',async page=>{
    await page.getByRole('button',{name:'Tài khoản Codex',exact:true}).click();await page.locator('[data-codex-tab="overview"]').click();await page.locator('#codex-service-proxy-select').waitFor();
    await page.locator('#codex-service-proxy-select').selectOption({label:'fixed · fixed demo'});
    let release,entered;const gate=new Promise(r=>release=r),started=new Promise(r=>entered=r);
    await page.route(base+'/api/service/codex-service/proxy',async route=>{if(route.request().method()!=='PUT')return route.continue();const response=await route.fetch();entered();await gate;await route.fulfill({response}).catch(()=>{});});
    await page.locator('#codex-service-proxy-save').click();await started;
    await page.locator('[data-nav="models"]').click();await page.locator('#codex-workspace').waitFor({state:'hidden'});
    await page.getByRole('button',{name:'Tài khoản Codex',exact:true}).click();await page.locator('[data-codex-tab="overview"]').click();await page.locator('#codex-service-proxy-select').waitFor();
    assert.equal(await page.locator('#codex-service-proxy-select').locator('option:checked').textContent(),'fixed · fixed demo');
    release();await page.unroute(base+'/api/service/codex-service/proxy');assert(await page.locator('#codex-service-proxy-save').isEnabled());
    const current=await get(page,'codex-service/proxy');await put(page,'codex-service/proxy','PUT',{version:current.version,proxy_profile_id:null});
  });
  await scenario('logout discards a delayed proxy receipt',async page=>{
    const isolated=await context.browser().newContext();let release;
    try{
      const signedIn=await isolated.newPage();signedIn.setDefaultTimeout(8000);
      await isolated.route('**/*',route=>route.request().url().startsWith(base+'/')?route.continue():route.abort());
      await signedIn.goto(base+'/service/');await signedIn.locator('#admin-token').fill('browser-test-admin');await signedIn.locator('#login-form button').click();await signedIn.locator('#workspace').waitFor({state:'visible'});
      await signedIn.getByRole('button',{name:'Tài khoản Codex',exact:true}).click();await signedIn.locator('[data-codex-tab="overview"]').click();await signedIn.locator('#codex-service-proxy-select').waitFor();
      await signedIn.locator('#codex-service-proxy-select').selectOption({label:'fixed · fixed demo'});
      let entered;const gate=new Promise(r=>release=r),started=new Promise(r=>entered=r);
      await signedIn.route(base+'/api/service/codex-service/proxy',async route=>{if(route.request().method()!=='PUT')return route.continue();const response=await route.fetch();entered();await gate;await route.fulfill({response}).catch(()=>{});});
      await signedIn.locator('#codex-service-proxy-save').click();await started;await signedIn.locator('#logout').click();release();await signedIn.locator('#login-panel').waitFor({state:'visible'});
      assert.equal(await signedIn.locator('#codex-workspace').textContent(),'');assert.equal(await signedIn.evaluate(()=>localStorage.length+sessionStorage.length),0);
    }finally{release?.();await isolated.close();}
    const current=await get(page,'codex-service/proxy');await put(page,'codex-service/proxy','PUT',{version:current.version,proxy_profile_id:null});
  });
  await scenario('restore known pool for main smoke',async page=>{await seedPool(page,{members:[]});});
  assert.deepEqual(failures,[],'Review regressions must all pass');
}
