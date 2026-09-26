// Opt-in: real gateway HTTP and real Codex inference. Never prints credentials.
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {request} from 'playwright-core';

if(process.env.AUTOBUILD_LIVE_VERIFY!=='1')throw Error('Set AUTOBUILD_LIVE_VERIFY=1 to permit four real inference requests.');
const adminBase='http://127.0.0.1:8787',publicBase='http://127.0.0.1:8788';
const admin=await request.newContext({baseURL:adminBase,extraHTTPHeaders:{Origin:adminBase},timeout:180000});
const publicApi=await request.newContext({baseURL:publicBase,timeout:180000});
let csrf,customer,key;
const receipts=[],since=new Date().toISOString(),model='gpt-6-astra';
async function control(path,method='GET',data){
  const response=await admin.fetch(path,{method,data,headers:csrf?{'X-CSRF-Token':csrf}:{}});
  assert(response.ok(),`Admin ${method} ${path}: HTTP ${response.status()}`);
  return response.json();
}
async function infer(path,body){
  const response=await publicApi.post(path,{data:body,headers:{Authorization:'Bearer '+key.secret}});
  assert.equal(response.status(),200,`${path}: HTTP ${response.status()}`);
  assert.equal(response.headers()['cache-control'],'no-store');
  return response;
}
function record(label,usage,{cacheDetails=true}={}){
  assert(Number.isSafeInteger(usage.input_tokens)&&usage.input_tokens>0);
  assert(Number.isSafeInteger(usage.output_tokens)&&usage.output_tokens>0);
  const cached=cacheDetails?(usage.input_tokens_details?.cached_tokens??0):null;
  const write=cacheDetails?(usage.input_tokens_details?.cache_write_tokens??0):null;
  const reasoning=usage.output_tokens_details?.reasoning_tokens??null;
  const charge=cacheDetails?BigInt(usage.input_tokens-cached-write)*1000000n+BigInt(cached)*100000n
    +BigInt(write)*1000000n+BigInt(usage.output_tokens)*2000000n:null;
  receipts.push({label,input:usage.input_tokens,output:usage.output_tokens,cached,write,reasoning,charge});
  console.log(JSON.stringify({check:label,http:200,input:usage.input_tokens,output:usage.output_tokens,cached,reasoning,charged_micro:charge===null?null:String(charge)}));
}
try{
  const config=JSON.parse(await readFile('data/local-gateway/admin-data/local-config.json','utf8'));
  ({csrf_token:csrf}=await control('/api/session','POST',{token:config.admin_token}));
  customer=await control('/api/service/customers','POST',{name:'Gateway review live verification'});
  key=await control('/api/service/keys','POST',{customer_id:customer.id,name:'Temporary review verification',policy:{
    model_ids:[model],protocols:['openai','anthropic','gemini','ollama'],total_micro:'100000000000000',
    expires_at:new Date(Date.now()+600000).toISOString(),model_overrides:[{model_id:model,
      input_micro:'1000000',output_micro:'2000000',cache_read_micro:'100000',cache_write_micro:'1000000'}],
  }});
  const auth={Authorization:'Bearer '+key.secret};
  for(const path of ['/v1/models','/models?client_version=1','/v1beta/models','/api/tags','/api/version']){
    const result=await publicApi.get(path,{headers:auth});
    assert.equal(result.status(),200);assert.equal(result.headers()['cache-control'],'no-store');
  }
  const invalid=await publicApi.post('/api/show',{data:'{',headers:{...auth,'Content-Type':'application/json'}});
  assert.equal(invalid.status(),400);assert.deepEqual(await invalid.json(),{error:'invalid_request'});

  const tools=[{type:'function',name:'report',description:'Record the supplied sum',
    parameters:{type:'object',properties:{total:{type:'integer'}},required:['total'],additionalProperties:false}}];
  const body={model,input:'Call report with total=12.',tools,tool_choice:'required',parallel_tool_calls:false,reasoning:{effort:'low'}};
  const plain=await (await infer('/v1/responses',body)).json();
  assert.equal(plain.tool_choice,'required');assert.equal(plain.parallel_tool_calls,false);
  assert.equal(plain.tools[0]?.name,'report');assert(plain.output.some(item=>item.type==='function_call'));
  record('responses_tools',plain.usage);
  const stream=await (await infer('/v1/responses',{...body,stream:true})).text();
  const frames=stream.split('\n').filter(line=>line.startsWith('data: ')).map(line=>JSON.parse(line.slice(6)));
  const created=frames.find(frame=>frame.type==='response.created')?.response;
  const completed=frames.find(frame=>frame.type==='response.completed')?.response;
  for(const value of [created,completed]){
    assert(value);assert.equal(value.tool_choice,'required');assert.equal(value.parallel_tool_calls,false);
    assert.equal(value.tools[0]?.name,'report');
  }
  record('responses_sse_tools',completed.usage);

  const parameters={type:'object',properties:{a:{type:'integer'},b:{type:'integer'}},required:['a','b']};
  const gemini=await (await infer(`/v1beta/models/${model}:generateContent`,{
    contents:[{role:'user',parts:[{text:'Add 1+2 and 4+5 with add, then return only the sum of the two results.'}]},
      {role:'model',parts:[{functionCall:{id:'call_review_1',name:'add',args:{a:1,b:2}}},
        {functionCall:{id:'call_review_2',name:'add',args:{a:4,b:5}}}]},
      {role:'user',parts:[{functionResponse:{name:'add',response:{result:3}}},
        {functionResponse:{name:'add',response:{result:9}}}]}],
    tools:[{functionDeclarations:[{name:'add',description:'Add integers',parameters}]}],
  })).json();
  const geminiText=gemini.candidates[0].content.parts.map(part=>part.text??'').join('');
  assert.match(geminiText,/12/);
  record('gemini_repeated_tool_history',{input_tokens:gemini.usageMetadata.promptTokenCount,
    output_tokens:gemini.usageMetadata.candidatesTokenCount+(gemini.usageMetadata.thoughtsTokenCount??0),
    input_tokens_details:{cached_tokens:gemini.usageMetadata.cachedContentTokenCount??0},
    output_tokens_details:{reasoning_tokens:gemini.usageMetadata.thoughtsTokenCount??0}});

  const ollama=await (await infer('/api/chat',{model,stream:false,tools:[{type:'function',function:{name:'add',parameters}}],
    messages:[{role:'user',content:'Add 1+2 and 4+5 with add, then return only the sum of the two results.'},
      {role:'assistant',content:'',tool_calls:[{function:{name:'add',arguments:{a:1,b:2}}},
        {function:{name:'add',arguments:{a:4,b:5}}}]},
      {role:'tool',tool_name:'add',content:'3'},{role:'tool',tool_name:'add',content:'9'}]})).json();
  assert.match(ollama.message.content,/12/);
  record('ollama_repeated_tool_history',{input_tokens:ollama.prompt_eval_count,output_tokens:ollama.eval_count},{cacheDetails:false});

  const report=await control('/api/service/usage/requests?'+new URLSearchParams({from:since,limit:'100'}));
  const completedRows=report.items.filter(row=>row.key_id===key.key_id&&row.status==='completed');
  assert.equal(completedRows.length,4);
  // Ollama has no cache breakdown on its wire. Do not invent a zero cache hit:
  // compare its visible totals and use the saved provider receipt for subsets.
  const ollamaRows=completedRows.filter(row=>row.protocol==='ollama');
  assert.equal(ollamaRows.length,1);
  const stored=ollamaRows[0].usage,receipt=receipts.find(row=>row.label==='ollama_repeated_tool_history');
  assert.equal(stored.input_tokens,String(receipt.input));assert.equal(stored.output_tokens,String(receipt.output));
  assert.notEqual(stored.cached_read,null);assert.notEqual(stored.cached_write,null);
  receipt.cached=Number(stored.cached_read);receipt.write=Number(stored.cached_write);
  receipt.charge=BigInt(receipt.input-receipt.cached-receipt.write)*1000000n
    +BigInt(receipt.cached)*100000n+BigInt(receipt.write)*1000000n+BigInt(receipt.output)*2000000n;
  console.log(JSON.stringify({check:'ollama_saved_cache_details',cached:receipt.cached,write:receipt.write,charged_micro:String(receipt.charge)}));
  const charged=receipts.reduce((sum,row)=>sum+row.charge,0n);
  assert.equal(completedRows.reduce((sum,row)=>sum+BigInt(row.charged_micro),0n),charged);
  for(const row of completedRows){assert.equal(row.request_state,'completed');assert.equal(row.charged_micro,row.computed_micro);assert.equal(row.held_micro,'0');}
  const saved=(await control('/api/service/keys')).find(row=>row.key_id===key.key_id);
  assert.equal(BigInt(saved.balance.spent_micro),charged);assert.equal(saved.balance.held_micro,'0');
  assert.equal(BigInt(saved.balance.available_micro),100000000000000n-charged);
  console.log(JSON.stringify({check:'exact_weighted_ledger',requests:4,charged_micro:String(charged),held_micro:'0',remaining_micro:saved.balance.available_micro}));
}finally{
  const cleanup=await Promise.allSettled([
    key?control(`/api/service/keys/${key.key_id}/revoke`,'POST',{version:key.version}):undefined,
    customer?control(`/api/service/customers/${customer.id}`,'DELETE',{version:1}):undefined,
  ]);
  await admin.dispose();await publicApi.dispose();
  assert(cleanup.every(result=>result.status==='fulfilled'),'Verification credential cleanup failed');
}
