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
  await scenario('paging preserves a member outside the rendered controls',async page=>{
    const {items}=await get(page,'oauth-accounts');const before=await seedPool(page,{members:[{credential_id:items[0].id,priority:1,weight:2,backup:false},{credential_id:items[1].id,priority:7,weight:8,backup:true}]});
    await openPool(page,true);assert.equal(await page.locator('#codex-pool-form .codex-member').count(),1);
    await page.getByRole('button',{name:'Thêm tài khoản đã lưu',exact:true}).click();await page.locator('#codex-list').getByRole('button',{name:items[1].email,exact:true}).waitFor();
    await page.locator('#codex-pool-form [name="weight-0"]').fill('9');await savePool(page);const saved=await get(page,poolPath);
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
    await page.getByRole('button',{name:'Thêm tài khoản đã lưu',exact:true}).click();await page.locator('#codex-list').getByRole('button',{name:items[1].email,exact:true}).waitFor();
    await savePool(page);const saved=await get(page,poolPath);assert.equal(saved.policy.single_credential_id,items[1].id,'Existing off-page single ID must not become first account');assert.equal(saved.policy.members.length,1);
  });
  await scenario('real 409 reloads keys before a new explicitly confirmed grant',async page=>{
    const [key]=await get(page,'keys');await page.getByRole('button',{name:'Tài khoản Codex',exact:true}).click();await page.locator('#codex-key').selectOption({label:'Demo key'});
    // Another admin commits an audited zero grant, leaving this form's version stale.
    await put(page,`keys/${key.key_id}/quota-adjust`,'POST',{request_id:crypto.randomUUID(),version:key.version,amount_micro:'0',reason:'Synthetic concurrent admin'});
    await page.locator('#codex-grant-amount').fill('0');await page.locator('#codex-grant-reason').fill('Synthetic retry');await page.locator('#codex-grant-ack').check();
    let keyReloaded=false;page.on('response',async response=>{if(response.url()===base+'/api/service/keys'&&response.status()===200){await response.finished();keyReloaded=true;}});
    const rejected=page.waitForResponse(r=>r.request().method()==='POST'&&r.url().endsWith('/quota-adjust'));await page.locator('#codex-grant-submit').click();assert.equal((await rejected).status(),409);
    await page.waitForFunction(()=>document.querySelector('#codex-message').textContent.includes('409'));
    assert(keyReloaded,'Definite rejection must finish authoritative key reload before reporting recovery');assert(await page.locator('#codex-grant-ack').isEnabled(),'Authoritative key reload must unlock a definite 409 rejection');
    assert(!await page.locator('#codex-grant-ack').isChecked(),'Recovery requires a new confirmation');
    await page.locator('#codex-grant-amount').fill('0');await page.locator('#codex-grant-reason').fill('Explicit synthetic retry');await page.locator('#codex-grant-ack').check();
    const accepted=page.waitForResponse(r=>r.request().method()==='POST'&&r.url().endsWith('/quota-adjust'));await page.locator('#codex-grant-submit').click();assert.equal((await accepted).status(),200);
    const [updated]=await get(page,'keys');assert.equal(updated.version,key.version+2);assert.equal(updated.balance.total_micro,'100000000000000');
  });
  await scenario('ambiguous grant outcome stays locked after real receipt is lost',async page=>{
    await page.getByRole('button',{name:'Tài khoản Codex',exact:true}).click();await page.locator('#codex-key').selectOption({label:'Demo key'});
    let posts=0;await page.route(base+'/api/service/keys/*/quota-adjust',async route=>{posts++;const response=await route.fetch();assert.equal(response.status(),200);await route.abort('failed');});
    await page.locator('#codex-grant-amount').fill('0');await page.locator('#codex-grant-reason').fill('Synthetic lost receipt');await page.locator('#codex-grant-ack').check();await page.locator('#codex-grant-submit').click();
    await page.waitForFunction(()=>document.querySelector('#codex-keys').textContent.includes('Kết quả cấp quota chưa rõ'));
    assert(await page.locator('#codex-grant-ack').isDisabled());assert(await page.locator('#codex-grant-submit').isDisabled());assert.equal(posts,1);
  });
  await scenario('restore known pool for main smoke',async page=>{await seedPool(page,{members:[]});});
  assert.deepEqual(failures,[],'Review regressions must all pass');
}
