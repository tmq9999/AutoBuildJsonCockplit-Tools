import {node,action,field,value,checked,lines} from './dom.js';
import {microToDecimal,decimalToMicro} from './api.js';
import {createCodexState,canConsumeReset,quotaLabel,resetLocked,windowDuration,remainingPercent,mergePoolMembers,poolSingleSelection,recoverGrantFailure} from './codex-state.js';
import {createCodexActions} from './codex-actions.js';

const unknown='Không rõ';
const amount=v=>v==null?unknown:BigInt(v)<0n?'-'+microToDecimal((-BigInt(v)).toString()):microToDecimal(v);
const stamp=v=>v?new Date(v).toLocaleString('vi-VN',{timeZone:'UTC'})+' UTC':unknown;
function panel(parent,id,title,eyebrow){const box=node('section','',{id,class:'card codex-panel','aria-labelledby':id+'-heading'});box.append(node('span',eyebrow,{class:'eyebrow'}),node('h3',title,{id:id+'-heading'}));parent.append(box);return box;}
function table(parent,headers,rows){const wrap=node('div','',{class:'codex-table-scroll'}),t=node('table'),head=node('tr');for(const h of headers)head.append(node('th',h,{scope:'col'}));t.append(node('thead'));t.firstChild.append(head);const body=node('tbody');for(const cells of rows){const tr=node('tr');for(const c of cells){const td=node('td');if(c instanceof Node)td.append(c);else td.textContent=c??unknown;tr.append(td);}body.append(tr);}if(!rows.length){const td=node('td','Chưa có dữ liệu.',{colspan:headers.length});const tr=node('tr');tr.append(td);body.append(tr);}t.append(body);wrap.append(t);parent.append(wrap);}
function idField(parent,name,label,options,id){const el=field(parent,name,label,options);el.id=id;return el;}
function freshness(projection){return !projection?.snapshot?unknown:projection.stale?'stale':'fresh';}
function quotaWindow(parent,label,w){const card=node('div','',{class:'codex-metric'}),left=remainingPercent(w?.used_percent);card.append(node('small',`${label} · ${windowDuration(w?.window_seconds)}`),node('strong',left==null?unknown:`${left}% còn lại`),node('small',quotaLabel(w)));if(left!==null)card.append(node('meter','',{min:0,max:100,value:left,'aria-label':`${label} còn lại (%)`}));card.append(node('small','Reset '+stamp(w?.reset_at)));parent.append(card);}

// Only opaque indexes enter DOM controls. Account/operation/key IDs stay in closures.
export function createCodexWorkspace(root,api,onUnauthorized){
  const state=createCodexState();let active=false,epoch=0,accounts=[],next=null,profiles=[],models=[],keys=[],matrix={},selectedKey=null,pool=null,poolModel='',grantPending=new Set(),savedQuota=new Map(),healthFilter='Tất cả';
  const actions=createCodexActions(api,state,()=>{if(active){if(state.accountId&&state.usage)savedQuota.set(state.accountId,state.usage);if(state.accountId&&state.credits)savedCredits.set(state.accountId,state.credits);renderDetail();if(listBox)renderList();}});
  let message,details,poolBox,keyBox,reportsBox,capBox,listBox,importBox,statusBox,overviewBox,tabs,serviceStatus=null,serviceProxy=null;
  let importResult=null,importBusy=false,refreshBusy=false,selectedAccounts=new Set(),savedCredits=new Map(),activeTab='accounts',searchText='',refreshErrors=new Map(),listGeneration=0;
  let proxyBusy=false,proxyNeedsReload=false,proxyState='loading',proxyProfiles=[],statusGeneration=0;
  const tabPanels={overview:'codex-overview',keys:'codex-keys',accounts:'codex-accounts-panel',models:'codex-capabilities',usage:'codex-reports'};
  const current=t=>active&&t===epoch;
  function notify(text,error=false){if(message){message.textContent=text;message.className='notice'+(error?' warning':'');}}
  function switchTab(id){activeTab=id;for(const tab of tabs?.querySelectorAll('button')??[]){tab.classList.toggle('active',tab.dataset.codexTab===id);tab.setAttribute('aria-pressed',String(tab.dataset.codexTab===id));}for(const p of Object.values(tabPanels).map(x=>root.querySelector('#'+x)).filter(Boolean))p.hidden=p.id!==tabPanels[id];if(poolBox)poolBox.hidden=id!=='accounts';}
  async function readProxyConfig(){
    const [proxy,loadedProfiles]=await Promise.all([
      api.request('/api/service/codex-service/proxy').catch(error=>{if(error.status===404)return null;throw error;}),
      api.request('/api/service/proxies')
    ]);
    return {proxy,loadedProfiles};
  }
  function acceptProxyConfig({proxy,loadedProfiles}){serviceProxy=proxy;proxyProfiles=loadedProfiles;proxyState=proxy?'available':'unavailable';proxyNeedsReload=false;}
  async function readStatus(){
    const t=epoch,g=++statusGeneration;
    const [status,proxy]=await Promise.allSettled([api.request('/api/service/codex-service/status'),readProxyConfig()]);
    if(!current(t)||g!==statusGeneration)return;
    if(status.status==='fulfilled'){serviceStatus=status.value;serviceNameStatus(serviceStatus);}
    if(!proxyBusy){
      if(proxy.status==='fulfilled')acceptProxyConfig(proxy.value);
      else{proxyNeedsReload=true;proxyState='failed';}
    }
    renderOverview();
    if(status.status==='rejected')throw status.reason;
    if(proxy.status==='rejected')throw proxy.reason;
  }
  function renderProxyControls(parent){
    const box=node('article','',{id:'codex-service-proxy'});box.append(node('h3','Proxy đầu ra Codex'));parent.append(box);
    if(proxyState==='loading'){box.append(node('p','Đang đọc cấu hình proxy…',{class:'muted'}));return;}
    if(proxyState==='unavailable'){box.append(node('p','Chưa có provider Codex. Nhập tài khoản OAuth trước khi cấu hình proxy.',{class:'muted'}));return;}
    if(proxyState==='failed'){box.append(node('p','Không đọc được cấu hình proxy. Tải lại dữ liệu đã lưu để thử lại.',{class:'notice warning'}));return;}
    const profileOptions=proxyProfiles;
    const selected=serviceProxy.proxy_profile_id?profileOptions.findIndex(p=>p.id===serviceProxy.proxy_profile_id):-1;
    const missing=Boolean(serviceProxy.proxy_profile_id)&&selected<0;
    const choices=[...(missing?[['__missing__','Giữ proxy hiện tại (profile không hiển thị)']]:[]),['','Direct (không proxy)'],...profileOptions.map((p,i)=>[String(i),`${p.config.mode} · ${p.name}`])];
    const select=idField(box,'codex-proxy','Proxy mặc định',{choices,value:missing?'__missing__':selected<0?'':String(selected)},'codex-service-proxy-select');
    const save=action(box,'Lưu proxy mặc định',()=>perform(async()=>{
      if(proxyBusy||proxyNeedsReload||select.value==='__missing__')return;
      const profile=select.value===''?null:profileOptions[Number(select.value)];
      if(select.value!==''&&(!/^\d+$/.test(select.value)||!profile)){notify('Profile không hợp lệ. Tải lại dữ liệu đã lưu.',true);return;}
      const t=epoch,version=serviceProxy.version,id=profile?.id??null;
      proxyBusy=true;++statusGeneration;renderOverview();
      try{
        const result=await api.request('/api/service/codex-service/proxy',{method:'PUT',body:{version,proxy_profile_id:id}});
        if(!current(t))return;
        ++statusGeneration;serviceProxy={...result,profile:profile?{id:profile.id,name:profile.name,mode:profile.config.mode}:null};
        notify(id?'Đã cập nhật proxy mặc định.':'Đã chuyển Codex về direct egress.');
      }catch(error){
        if(!current(t))return;
        proxyNeedsReload=true;
        // A rejected CAS reads the saved state, but never resends the write.
        if(error.status===409){
          const refreshed=await readProxyConfig();
          if(!current(t))return;
          ++statusGeneration;acceptProxyConfig(refreshed);
        }
        throw error;
      }finally{if(current(t)){proxyBusy=false;renderOverview();}}
    }),'primary');
    save.id='codex-service-proxy-save';select.disabled=proxyBusy||proxyNeedsReload;
    const updateDisabled=()=>{save.disabled=proxyBusy||proxyNeedsReload||select.value==='__missing__';};select.onchange=updateDisabled;updateDisabled();
    if(missing)box.append(node('p','Profile hiện tại không có trong danh sách. Cấu hình được giữ nguyên; không tự chuyển sang direct.',{class:'notice warning'}));
    if(proxyNeedsReload)box.append(node('p','Chưa xác nhận được cấu hình đã lưu. Tải lại dữ liệu trước khi lưu tiếp.',{class:'notice warning'}));
    box.append(node('p',`Nguồn: ${serviceProxy.source==='provider'?'provider Codex':'direct'} · proxy riêng của tài khoản vẫn được ưu tiên.`,{class:'muted'}));
  }
  function renderOverview(){
    overviewBox.replaceChildren(node('h3','Tổng quan dịch vụ'));const connection=node('div','',{class:'codex-connection'});overviewBox.append(connection);
    const config=node('article');config.append(node('h3','Cấu hình dịch vụ'));const url=field(config,'base-url','Base URL',{value:serviceStatus?.base_url??''});url.readOnly=true;
    action(config,'Sao chép Base URL',()=>perform(()=>navigator.clipboard.writeText(url.value))).disabled=!url.value;
    config.append(node('p','Khóa máy khách được cấp ở mục API key. Mỗi key có model và quota riêng.',{class:'muted'}));
    connection.append(config);renderProxyControls(connection);const reload=root.querySelector('#codex-reload-saved');if(reload)reload.disabled=proxyBusy;
    const health=node('article');health.append(node('h3','Sức khỏe tài khoản'));const metrics=node('div','',{class:'codex-metrics'});const a=serviceStatus?.accounts??{};
    for(const [label,v]of [['Tài khoản',a.total],['Hoạt động',a.active],['Chưa xác minh',a.unverified],['Cần OAuth lại',a.reauth_required]]){const c=node('div');c.append(node('small',label),node('strong',v??unknown));metrics.append(c);}health.append(metrics,node('p',`${serviceStatus?.keys?.enabled??unknown} khóa máy khách bật · ${a.disabled??unknown} tài khoản tắt`,{class:'muted'}));connection.append(health);
    statusBox=node('div','',{class:'codex-overview-stats'});const u=serviceStatus?.usage??{};for(const [label,val]of [['Yêu cầu',u.requests],['Hoàn tất',u.completed],['Lỗi',u.failed],['Đang chờ',u.pending],['Token đầu vào',u.input_tokens]]){const s=node('div','',{class:'codex-stat'});s.append(node('small',label),node('strong',val==null?unknown:String(val)),node('span','Tổng dữ liệu đã lưu',{class:'muted'}));statusBox.append(s);}overviewBox.append(statusBox);
    overviewBox.append(node('p',`Cache đọc: ${u.cached_read??unknown} · Cache ghi: ${u.cached_write??unknown} · Output: ${u.output_tokens??unknown} · Quota đã trừ: ${amount(u.charged_micro)}`,{class:'muted'}));
    const protocols=node('div','',{class:'codex-protocols'});for(const [name,path]of [['Models','/v1/models'],['OpenAI Chat','/v1/chat/completions'],['Responses','/v1/responses'],['Compact','/v1/responses/compact'],['Images','/v1/images/generations · /v1/images/edits'],['Responses WebSocket','WS /v1/responses']]){const box=node('article');box.append(node('strong',name),node('code',path));protocols.append(box);}overviewBox.append(node('h3','API Codex'),protocols);
  }
  function serviceNameStatus(status){const pill=root.querySelector('.codex-service-name .pill');if(pill){pill.textContent=status?.status??unknown;pill.className='pill '+(status?.status==='configured'||status?.status==='running'?'ok':'warn');}}
  async function perform(fn){const t=epoch;try{await fn();}catch(e){if(!current(t)||e.name==='AbortError'||e.message==='STALE_SESSION')return;if(e.status===401)onUnauthorized();else notify(e.status===409?'409 · Xung đột phiên bản hoặc thao tác. Tải lại dữ liệu đã lưu trước khi thử lại.':e.message,true);}}
  async function readSavedQuota(rows,t){let i=0;await Promise.all(Array.from({length:Math.min(4,rows.length)},async()=>{while(current(t)&&i<rows.length){const row=rows[i++],base='/api/service/oauth-accounts/'+encodeURIComponent(row.id);const [usage,credits]=await Promise.all([api.request(base+'/quota'),api.request(base+'/reset-credits')]);if(!current(t))return;savedQuota.set(row.id,usage);savedCredits.set(row.id,credits);}}));}
  async function selectAccount(row){const t=epoch;state.select(row.id);renderDetail();await actions.read();if(current(t)){renderList();details.scrollIntoView({block:'nearest'});}}
  async function refreshMany(rows,all=false){
    if(refreshBusy||state.busy)return;refreshBusy=true;const t=epoch;let index=0,done=0,failed=0,stop=false;renderList();
    try{if(all){rows=[];let cursor=null;do{const page=await api.request('/api/service/oauth-accounts?limit=200'+(cursor?'&after='+encodeURIComponent(cursor):''));if(!current(t))return;rows.push(...page.items);cursor=page.next_after;}while(cursor);}
    await Promise.all([0,1].map(async()=>{while(current(t)&&!stop&&index<rows.length){const row=rows[index++];if(!row.enabled){failed++;continue;}try{
      const result=await api.request('/api/service/oauth-accounts/'+encodeURIComponent(row.id)+'/quota/refresh',{method:'POST',body:{}});if(!current(t))return;
      savedQuota.set(row.id,result.usage);savedCredits.set(row.id,result.credits);refreshErrors.delete(row.id);if(state.accountId===row.id)state.accept(state.ticket(),result);
      if(result.usage_status==='failed'||result.credits_status==='failed')failed++;else done++;
    }catch(e){if(!current(t))return;failed++;refreshErrors.set(row.id,e.message);if(e.status===401){stop=true;onUnauthorized();return;}if(e.status===429)stop=true;}
    if(current(t)){notify(`Đã xử lý ${done+failed}/${rows.length} · Thành công ${done} · Lỗi ${failed}${stop?' · Tạm dừng do rate limit':''}`,failed>0);renderList();renderDetail();}
    }}));if(current(t))await readStatus();}finally{if(current(t)){refreshBusy=false;renderList();renderDetail();}}
  }
  function renderImport(){
    let form=importBox.querySelector('#codex-batch-import');
    if(!form){
      importBox.replaceChildren(node('h3','Nhập hàng loạt OAuth'),node('p','Nhiều tệp JSON hoặc dán mảng JSON. Tối đa 1000 bản ghi / 32 MiB. Nội dung token không được đưa vào kết quả.',{class:'muted'}));
      form=node('form','',{id:'codex-batch-import',class:'codex-import-form'});
      const files=node('input','',{id:'codex-batch-files',type:'file',multiple:true,accept:'application/json,.json','aria-label':'Tệp OAuth JSON'});form.append(files);
      const profileOptions=profiles;
      const pasted=field(form,'batch-json','Dán mảng JSON',{type:'textarea'}),profile=field(form,'import-profile','Proxy credential',{choices:[['','Kế thừa provider'],...profileOptions.map((p,i)=>[String(i),`${p.config.mode} · ${p.name}`])]});
      form.append(node('button','Nhập JSON',{type:'submit',class:'primary'}));importBox.append(form);
      form.onsubmit=e=>{e.preventDefault();if(importBusy)return;const t=epoch;perform(async()=>{
        const chosen=[...files.files],paste=pasted.value,profileId=profile.value===''?null:profileOptions[Number(profile.value)]?.id;
        const bytes=chosen.reduce((n,f)=>n+f.size,0)+new TextEncoder().encode(paste).length;
        if(bytes>32*1024*1024)throw Error('Tối đa 32 MiB mỗi lần nhập.');
        if(!chosen.length&&!paste.trim())throw Error('Chọn tệp JSON hoặc dán mảng JSON.');
        importBusy=true;renderImport();
        try{
          const records=[];
          for(const f of chosen){let parsed;try{parsed=JSON.parse(await f.text());}catch{throw Error('Tệp JSON không hợp lệ.');}if(!current(t))return;records.push(...(Array.isArray(parsed)?parsed:[parsed]));}
          if(paste.trim()){let parsed;try{parsed=JSON.parse(paste);}catch{throw Error('Mảng JSON không hợp lệ.');}if(!Array.isArray(parsed))throw Error('Nội dung dán phải là mảng JSON.');records.push(...parsed);}
          if(!records.length||records.length>1000)throw Error('Cần từ 1 đến 1000 bản ghi.');
          if(!current(t))return;
          const result=await api.request('/api/service/oauth/import-batch',{method:'POST',body:{records,proxy_profile_id:profileId}});
          if(!current(t))return;
          importResult=result;files.value='';pasted.value='';await readList();
        }finally{if(current(t)){importBusy=false;renderImport();}}
      });};
    }
    // Keep the same live form and FileList during incidental account/quota
    // updates. Only a confirmed import clears it; errors retain the draft.
    for(const control of form.elements)control.disabled=importBusy;
    const resultBox=node('div','',{id:'codex-batch-result','aria-live':'polite'}),previous=importBox.querySelector('#codex-batch-result');
    if(previous)previous.replaceWith(resultBox);else importBox.append(resultBox);
    if(importResult){resultBox.append(node('strong',`Đã nhập ${importResult.imported} · Trùng ${importResult.duplicate} · Lỗi ${importResult.failed}`));const ul=node('ul');for(const r of importResult.results??[])ul.append(node('li',`#${r.index} ${r.status}${r.email?' · '+r.email:''}${r.code?' · '+r.code:''}`));resultBox.append(ul);}
  }
  async function readList(more=false){const t=epoch,g=++listGeneration;const page=await api.request('/api/service/oauth-accounts'+(more&&next?'?after='+encodeURIComponent(next):''));if(!current(t)||g!==listGeneration)return;accounts=more?[...accounts,...page.items]:page.items;next=page.next_after;await readSavedQuota(page.items,t);if(current(t)&&g===listGeneration)renderList();}
  function renderList(){
    if(importBox?.parentElement===listBox){for(const child of [...listBox.children])if(child!==importBox)child.remove();}
    else{listBox.replaceChildren();importBox=node('div','',{class:'codex-import-box'});listBox.append(importBox);}
    listBox.prepend(node('span','CREDENTIAL CATALOG',{class:'eyebrow'}),node('h3','Tài khoản đã lưu · Quota OpenAI · Freshness',{id:'codex-list-heading'}));renderImport();
    const tools=node('div','',{class:'codex-account-tools'});listBox.append(tools);
    const filter=field(tools,'health','Lọc trạng thái',{choices:['Tất cả','active','unverified','reauth_required','refresh_uncertain','disabled'].map(x=>[x,x]),value:healthFilter});
    const search=field(tools,'account-search','Tìm email',{value:searchText});search.type='search';
    const refreshAll=action(tools,'Làm mới quota tất cả',()=>perform(()=>refreshMany([],true)));refreshAll.id='codex-refresh-all';refreshAll.disabled=refreshBusy||Boolean(state.busy);
    const refreshSelected=action(tools,'Làm mới đã chọn',()=>perform(()=>refreshMany(accounts.filter(r=>selectedAccounts.has(r.id)))));refreshSelected.disabled=refreshBusy||Boolean(state.busy)||!selectedAccounts.size;
    const rows=node('div');listBox.append(rows);
    const draw=()=>{rows.replaceChildren();rows.className='codex-account-grid';for(const r of accounts.filter(r=>(filter.value==='Tất cả'||(filter.value==='disabled'?!r.enabled:r.status===filter.value))&&(!searchText||(r.email??'').toLocaleLowerCase().includes(searchText.toLocaleLowerCase())))){
      const b=node('button',r.email??'Chưa có email',{type:'button',class:'row-link','aria-pressed':String(state.accountId===r.id)});b.onclick=()=>perform(()=>selectAccount(r));
      const projection=savedQuota.get(r.id),credits=savedCredits.get(r.id),card=node('article','',{class:'codex-account-card'}),head=node('div','',{class:'codex-account-head'}),check=node('input','',{type:'checkbox','aria-label':`Chọn ${r.email??'tài khoản'}`});
      check.checked=selectedAccounts.has(r.id);check.onchange=()=>{if(check.checked)selectedAccounts.add(r.id);else selectedAccounts.delete(r.id);refreshSelected.disabled=refreshBusy||Boolean(state.busy)||!selectedAccounts.size;};
      head.append(check,b,node('span',projection?.snapshot?.plan_type??r.plan_type??unknown,{class:'plan-badge'}));card.append(head,node('small',`${r.enabled?r.status:'disabled'} · ${r.proxy_profile_name??'Kế thừa provider'}`,{class:'muted'}));
      const creditLabel=`Lượt đặt lại ${credits?.snapshot?.available_count??unknown}`;const creditButton=action(card,creditLabel,()=>perform(async()=>{await selectAccount(r);root.querySelector('#codex-reset')?.scrollIntoView({block:'nearest'});}));creditButton.className='pill';
      const quota=node('div','',{class:'codex-card-quota'});for(const [label,w] of [['Primary',projection?.snapshot?.primary],['Secondary',projection?.snapshot?.secondary]]){const line=node('div','',{class:'quota-line'}),left=remainingPercent(w?.used_percent);line.append(node('strong',w?.window_seconds?windowDuration(w.window_seconds):label),node('span',left==null?unknown:`${left}% còn lại`));if(left!==null)line.append(node('meter','',{min:0,max:100,value:left,low:20,high:50,optimum:100,'aria-label':`${label} còn lại (%)`}));line.append(node('small',`${quotaLabel(w)} · reset ${stamp(w?.reset_at)}`,{class:'muted'}));quota.append(line);}card.append(quota);
      const error=refreshErrors.get(r.id)??projection?.last_error??credits?.last_error;if(error)card.append(node('p',error,{class:'codex-card-error'}));
      const actionsRow=node('div','',{class:'codex-card-actions'});actionsRow.append(node('span',`${freshness(projection)} · ${stamp(projection?.fetched_at)}`,{class:'muted'}));action(actionsRow,'Mở chi tiết',()=>perform(()=>selectAccount(r)));action(actionsRow,'Làm mới',()=>perform(()=>refreshMany([r]))).disabled=refreshBusy||Boolean(state.busy);card.append(actionsRow);rows.append(card);}
    };filter.onchange=()=>{healthFilter=filter.value;draw();};search.oninput=()=>{searchText=search.value;draw();};draw();
    listBox.append(node('p',`Đã tải ${accounts.length} / ${serviceStatus?.accounts?.total??accounts.length} tài khoản`,{class:'muted'}));
    listBox.append(node('p','Tải lại danh sách chỉ đọc dữ liệu đã lưu; không gọi provider.',{class:'muted'}));if(next)action(listBox,'Thêm tài khoản đã lưu',()=>perform(()=>readList(true)));
  }
  function renderDetail(){if(!details)return;const focused=document.activeElement?.id;details.replaceChildren();const row=accounts.find(a=>a.id===state.accountId);
    const account=panel(details,'codex-detail',row?.email??'Chọn tài khoản','CHI TIẾT TÀI KHOẢN');if(!row){account.append(node('p','Chọn tài khoản để xem quota, proxy và lượt đặt lại.'));return;}
    account.append(node('p',`${row.status} · ${state.usage?.snapshot?.plan_type??row.plan_type??unknown}`,{class:'muted'}));const metrics=node('div','',{class:'codex-metrics'});quotaWindow(metrics,'Primary',state.usage?.snapshot?.primary);quotaWindow(metrics,'Secondary',state.usage?.snapshot?.secondary);account.append(metrics);
    account.append(node('p',`${freshness(state.usage)} · cập nhật ${stamp(state.usage?.fetched_at)} · ${state.usage?.last_error??'Snapshot đã lưu'}`,{class:'muted'}));
    const settings=node('form');const profile=field(settings,'profile','Proxy credential',{choices:[['','Kế thừa provider'],...profiles.map((p,i)=>[String(i),`${p.config.mode} · ${p.name}`])],value:row.proxy_profile_id?String(profiles.findIndex(p=>p.id===row.proxy_profile_id)):''});
    const enabled=field(settings,'enabled','Cho phép sử dụng',{type:'checkbox',value:row.enabled});account.append(settings);
    const locked=Boolean(state.busy)||refreshBusy||resetLocked(state.operation);
    const save=action(account,'Lưu tài khoản',()=>perform(async()=>{await actions.saveAccount(row,enabled.checked,profile.value===''?null:profiles[Number(profile.value)]?.id);await readList();renderDetail();}));save.disabled=locked;
    const quota=action(account,'Cập nhật quota',()=>perform(()=>actions.refresh()));quota.id='codex-refresh-quota';quota.disabled=Boolean(state.busy)||refreshBusy;
    const token=action(account,'Refresh token',()=>perform(async()=>{await actions.refreshToken();await readList();await actions.read();}));token.disabled=locked;
    const reset=panel(details,'codex-reset','Xác nhận reset theo credit','RESET QUOTA OPENAI');
    reset.append(node('p','Credit khả dụng: '+(state.credits?.snapshot?.available_count??unknown)),node('span',freshness(state.credits),{id:'codex-credit-freshness',class:'pill'}));
    const expiries=state.credits?.snapshot?.credits?.filter(c=>c.state==='available'&&c.expires_at).map(c=>c.expires_at).sort();reset.append(node('p','Hết hạn gần nhất: '+stamp(expiries?.[0])));
    const operation=state.operation?.state??'Chưa gửi';reset.append(node('p',operation,{id:'codex-reset-state',class:'notice'+(resetLocked(state.operation)?' warning':''),'aria-live':'polite'}));
    if(resetLocked(state.operation))reset.append(node('p',operation==='succeeded_refresh_failed'?'OpenAI đã chấp nhận; Cập nhật quota đầy đủ, fresh sẽ được server xác nhận thành công. Partial refresh vẫn khóa; không gửi lại reset.':'Chưa xác định kết quả cuối cùng. Không tự gửi lại; tải trạng thái đã lưu hoặc đối soát bằng chứng.',{class:'notice warning'}));
    reset.append(node('p','Tiêu thụ 01 provider-granted reset credit. Quota OpenAI (%) độc lập với weighted quota của key khách. Không gửi lại tự động.',{id:'codex-reset-note',class:'muted'}));
    const ack=idField(reset,'ack','Tôi xác nhận tiêu thụ một provider-granted reset credit',{type:'checkbox'},'codex-ack');
    const consume=action(reset,'Đặt lại quota OpenAI',()=>perform(async()=>{actions.confirm();await actions.consume();}),'primary');consume.id='codex-consume';consume.setAttribute('aria-describedby','codex-reset-note');
    const allowed=!state.busy&&!refreshBusy&&canConsumeReset({available_count:state.credits?.snapshot?.available_count,fresh:state.credits?.stale===false,operation_state:state.operation?.state});ack.disabled=!allowed;consume.disabled=true;ack.onchange=()=>{consume.disabled=!(ack.checked&&allowed);};
    const evidence=panel(details,'codex-evidence','Đối soát theo bằng chứng','ADMIN ONLY');const form=node('form');evidence.append(form);
    field(form,'outcome','Outcome',{choices:[['confirmed_applied','Đã áp dụng'],['confirmed_not_applied','Chưa áp dụng']]});field(form,'reason','Lý do và nguồn bằng chứng (không nhập secret)',{type:'textarea',required:true}).maxLength=500;
    const resolve=node('button','Áp dụng outcome',{type:'submit'});resolve.disabled=Boolean(state.busy)||!['unknown','succeeded_refresh_failed'].includes(operation)||state.operation?.version==null;form.append(resolve);form.onsubmit=e=>{e.preventDefault();perform(()=>actions.resolve(value(form,'outcome'),value(form,'reason')));};
    if(focused){const el=document.getElementById(focused);if(el&&!el.disabled)el.focus();}
  }
  async function readPool(){if(!poolModel)return;const t=epoch,model=poolModel;const result=await api.request('/api/service/account-pools/'+model.split('/').map(encodeURIComponent).join('/'));if(current(t)&&model===poolModel){pool=result;renderPool();}}
  function renderPool(){poolBox.replaceChildren(node('span','POOL EDITOR · CATALOG CANONICAL',{class:'eyebrow'}),node('h3','Pool phục vụ',{id:'codex-pool-heading'}));
    const model=idField(poolBox,'model','Canonical model',{choices:[['','Chọn model'],...models.map(m=>[m.model_id,m.model_id])],value:poolModel},'codex-pool-model');model.onchange=()=>{poolModel=model.value;pool=null;perform(readPool);};
    if(!pool)return;poolBox.append(node('p','version '+pool.version,{id:'codex-pool-version'}),node('p','Pool đã cấu hình nhưng không có thành viên eligible: Tạm dừng — không có tài khoản. Không fallback sang mọi tài khoản. Conversation hiện hữu luôn pinned.',{class:'notice warning'}));
    const p=pool.policy??{},editorAccounts=accounts.slice(),form=node('form','',{id:'codex-pool-form'});poolBox.append(form);
    field(form,'mode','Chế độ chọn pool',{choices:['auto','random','single','priority','weight'].map(x=>[x,x]),value:p.mode??'auto'});
    const memberControls=accounts.map((a,i)=>{const member=p.members?.find(m=>m.credential_id===a.id),container=node('div','',{class:'codex-member'});form.append(container);
      const include=field(container,'member-'+i,a.email??'Tài khoản đã ẩn',{type:'checkbox',value:Boolean(member)});
      const weight=field(container,'weight-'+i,'Weight',{type:'number',value:member?.weight??1});weight.min='1';weight.max='10000';
      const priority=field(container,'priority-'+i,'Priority',{type:'number',value:member?.priority??0});priority.min='0';priority.max='10000';
      const backup=field(container,'backup-'+i,'Backup',{type:'checkbox',value:member?.backup??false});container.append(node('small',`${a.enabled?'enabled':'disabled'} · ${a.status} (eligibility do server quyết định)`));return {a,include,weight,priority,backup};});
    const singleIndex=editorAccounts.findIndex(a=>a.id===p.single_credential_id);
    field(form,'single','Tài khoản single',{choices:[['',p.single_credential_id&&singleIndex<0?'Giữ tài khoản đã lưu ngoài trang':'Chọn…'],...editorAccounts.map((a,i)=>[String(i),a.email??'Tài khoản đã ẩn'])],value:singleIndex>=0?String(singleIndex):''});
    field(form,'reserve_enabled','Reserve (mặc định OFF)',{type:'checkbox',value:p.reserve_enabled??false});
    for(const n of ['primary','secondary'])field(form,'min_'+n+'_remaining','Minimum remaining · '+n+' (%)',{value:p['min_'+n+'_remaining']??'0'});
    const age=field(form,'snapshot_max_age_seconds','Freshness max age (giây)',{type:'number',value:p.snapshot_max_age_seconds??120});age.min='30';age.max='3600';
    field(form,'retry_limit','Retry',{choices:[['0','0'],['1','1']],value:p.retry_limit??1});field(form,'session_affinity','Session affinity · TTL 24h tối đa, cố định',{type:'checkbox',value:p.session_affinity??true});field(form,'plan_order','Plan order (mỗi dòng)',{type:'textarea',value:(p.plan_order??[]).join('\n')});field(form,'prefer_expiring','Ưu tiên plan sắp hết hạn',{type:'checkbox',value:p.prefer_expiring??false});
    const save=node('button','Lưu pool (CAS)',{type:'submit',id:'codex-pool-save',class:'primary'});form.append(save);const singleWarning=node('p','Chọn một tài khoản cho single mode; tài khoản ngoài trang hiện tại được giữ nguyên nếu chưa chỉnh sửa.',{class:'notice warning'});singleWarning.hidden=true;form.append(singleWarning);const reload=action(poolBox,'Tải lại pool đã lưu',()=>perform(readPool));reload.id='codex-pool-reload';
    let saving=false;const validatePool=()=>{const selection=poolSingleSelection(value(form,'mode'),value(form,'single'),editorAccounts,p.single_credential_id);singleWarning.hidden=selection.valid||value(form,'mode')!=='single';save.disabled=saving||!selection.valid;return selection;};form.addEventListener('change',validatePool);validatePool();
    form.onsubmit=e=>{e.preventDefault();if(save.disabled)return;const t=epoch,model=poolModel;
      const selection=validatePool();if(!selection.valid)return;const members=mergePoolMembers(p.members??[],memberControls.filter(m=>m.include.checked).map(m=>({credential_id:m.a.id,priority:Number(m.priority.value),weight:Number(m.weight.value),backup:m.backup.checked})),new Set(memberControls.map(m=>m.a.id)));
      const policy={mode:value(form,'mode'),members,single_credential_id:selection.credential_id,session_affinity:checked(form,'session_affinity'),reserve_enabled:checked(form,'reserve_enabled'),min_primary_remaining:value(form,'min_primary_remaining'),min_secondary_remaining:value(form,'min_secondary_remaining'),snapshot_max_age_seconds:Number(value(form,'snapshot_max_age_seconds')),retry_limit:Number(value(form,'retry_limit')),plan_order:lines(value(form,'plan_order')),prefer_expiring:checked(form,'prefer_expiring')};
      saving=true;save.disabled=true;perform(async()=>{try{const result=await actions.savePool(model,{version:pool.version,policy});if(current(t)&&model===poolModel){pool=result;renderPool();}}finally{if(current(t)){saving=false;validatePool();}}});
    };
  }
  function renderKeys(){keyBox.replaceChildren(node('span','CUSTOMER KEY · WEIGHTED QUOTA',{class:'eyebrow'}),node('h3','Key khách hàng & cấp thêm quota',{id:'codex-keys-heading'}));
    action(keyBox,'Tạo hoặc quản lý API key',()=>document.querySelector('[data-nav="keys"]')?.click(),'primary');
    const cards=node('div','',{class:'codex-key-cards'});for(const k of keys){const c=node('article','',{class:'codex-key-card'}),head=node('div','',{class:'actions'});head.append(node('strong',k.name||'API key'),node('code',(k.prefix??'')+' ••••••••'),node('span',k.revoked_at?'Đã thu hồi':k.policy.enabled?'Đã bật':'Đã tắt',{class:'pill'}));c.append(head);
      const b=k.balance??{};c.append(node('p',`Quota: ${b.total_micro==null?'Không giới hạn':amount(b.total_micro)} · Đã dùng: ${amount(b.spent_micro)} · Giữ trước: ${amount(b.held_micro)} · Còn lại: ${b.available_micro==null?'Không giới hạn':amount(b.available_micro)}`,{class:'muted'}));
      c.append(node('p',`Giao thức: ${(k.policy.protocols??[]).join(', ')} · Model: ${k.policy.all_models?'Tất cả':(k.policy.model_ids??[]).join(', ')||'Chưa cấp'}`,{class:'muted'}));action(c,'Cấp thêm quota cho key',()=>{selectedKey=k.key_id;renderKeys();root.querySelector('#codex-balance')?.scrollIntoView({block:'nearest'});});cards.append(c);}keyBox.append(cards);
    const choose=idField(keyBox,'key','Key khách hàng',{choices:[['','Chọn key'],...keys.map((k,i)=>[String(i),k.name||'Key đã ẩn'])],value:selectedKey?String(keys.findIndex(k=>k.key_id===selectedKey)):''},'codex-key');choose.onchange=()=>{selectedKey=choose.value===''?null:keys[Number(choose.value)].key_id;renderKeys();};
    const row=keys.find(k=>k.key_id===selectedKey);if(!row)return;const b=row.balance??{},balance=node('div','',{id:'codex-balance',class:'codex-metrics'});for(const[n,label]of [['total_micro','Tổng quota'],['spent_micro','Đã spent'],['held_micro','Held'],['available_micro','Còn lại']]){const c=node('div','',{class:'codex-metric'});c.append(node('small',label),node('strong',b[n]==null&&['total_micro','available_micro'].includes(n)?'Không giới hạn':amount(b[n])));balance.append(c);}keyBox.append(balance);
    keyBox.append(node('p','Ví dụ: input 1,000 = uncached 300 + cache-read 600 + cache-write 100; output 200. Hệ số 1 / 0.1 / 1.25 / 3 → 1,085 weighted tokens. Mọi hệ số = 1 → 1,200, không phải 1,900.',{class:'formula'}));
    const form=node('form');keyBox.append(form);idField(form,'amount','Weighted tokens cấp thêm',{required:true},'codex-grant-amount');idField(form,'reason','Lý do (bắt buộc, không nhập secret)',{type:'textarea',required:true},'codex-grant-reason').maxLength=500;
    const ack=idField(form,'ack','Tôi xác nhận tăng total limit, không xóa spent, held hoặc lịch sử',{type:'checkbox'},'codex-grant-ack');const send=node('button','Cấp thêm quota',{type:'submit',id:'codex-grant-submit',class:'primary'});send.disabled=true;form.append(send);
    const unavailable=row.revoked_at||b.total_micro==null||grantPending.has(row.key_id);ack.disabled=Boolean(unavailable);ack.onchange=()=>{send.disabled=!ack.checked||Boolean(unavailable);};
    if(grantPending.has(row.key_id))form.append(node('p','Kết quả cấp quota chưa rõ. Không gửi lại; kiểm tra audit/balance trước khi xác nhận mới.',{class:'notice warning'}));
    form.onsubmit=e=>{e.preventDefault();if(send.disabled)return;perform(async()=>{decimalToMicro(value(form,'amount'));const t=epoch;grantPending.add(row.key_id);send.disabled=true;ack.disabled=true;try{const receipt=await actions.grant(row.key_id,{version:row.version,amount:value(form,'amount'),reason:value(form,'reason')});if(!current(t))return;grantPending.delete(row.key_id);const updated=await api.request('/api/service/keys');if(!current(t))return;keys=updated;renderKeys();notify(`Đã cấp ${amount(receipt.amount_micro)} · actor ${receipt.actor} · ${stamp(receipt.created_at)}. Lịch sử giữ nguyên.`);}catch(error){const retain=await recoverGrantFailure(error,async()=>{const updated=await api.request('/api/service/keys');if(current(t)){grantPending.delete(row.key_id);keys=updated;renderKeys();}});if(retain&&current(t))renderKeys();throw error;}});};
  }
  function renderCapabilities(){
    capBox.replaceChildren(node('span','MODELS & CAPABILITIES',{class:'eyebrow'}),node('h3','Mô hình và năng lực',{id:'codex-capabilities-heading'}));
    const list=node('div','',{class:'codex-model-list'}),catalog=matrix.model_catalog??[];
    const entries=[...catalog,...models.filter(m=>!catalog.some(c=>c.id===m.model_id)).map(m=>({id:m.model_id,display_name:m.model_id}))];
    for(const m of entries){
      const saved=models.find(s=>s.model_id===m.id),row=node('article','',{class:'codex-model-row'});
      row.append(node('strong',m.display_name),node('code',m.id),node('span',saved?(saved.enabled?'Đã xuất bản':'Đã tắt'):'Chưa xuất bản',{class:'pill'}));
      row.append(node('small',`Context: ${m.context_window?.toLocaleString()??unknown} · ${(m.capabilities??[]).join(' / ')}`));
      if(m.reasoning_efforts?.length)row.append(node('small','Reasoning: '+m.reasoning_efforts.join(' / ')));
      if(saved)row.append(node('small',`Input ${amount(saved.input_micro)} · Cache đọc ${amount(saved.cache_read_micro??saved.input_micro)} · Output ${amount(saved.output_micro)}`));
      list.append(row);
    }
    capBox.append(list,node('p','Catalog tham khảo; quyền model/ảnh còn phụ thuộc tài khoản upstream. Xuất bản tạo mapping còn thiếu, giữ nguyên giá và cấu hình đã sửa.',{class:'muted'}));
    async function publishCatalog(images=false){
      const imageAccounts=images?[...selectedAccounts]:[];
      if(images&&!imageAccounts.length){notify('Chọn các tài khoản có quyền tạo ảnh ở Nhóm tài khoản trước.',true);return;}
      if(!confirm(images?'Xác nhận các tài khoản đã chọn có quyền tạo ảnh và xuất bản model ảnh?':'Xuất bản model Codex cho các tài khoản đã nhập? Giữ nguyên mapping, trạng thái và hệ số hiện có.'))return;
      const t=epoch,button=root.querySelector('#codex-publish-models');if(button)button.disabled=true;
      try{const result=await api.request('/api/service/codex-models/publish',{method:'POST',body:{model_ids:catalog.filter(m=>Boolean(m.capabilities.includes('images'))===images).map(m=>m.id),image_credential_ids:imageAccounts}});
        if(!current(t))return;models=await api.request('/api/service/models');if(!current(t))return;renderCapabilities();renderPool();notify(`Đã tạo ${result.models_created} model và ${result.bindings_created} mapping. Hệ số cũ giữ nguyên.`);
      }finally{if(current(t)&&button)button.disabled=false;}
    }
    action(capBox,'Xuất bản catalog Codex',()=>perform(()=>publishCatalog()),'primary').id='codex-publish-models';
    action(capBox,'Xuất bản model ảnh cho tài khoản đã chọn',()=>perform(()=>publishCatalog(true)));
    action(capBox,'Quản lý model',()=>document.querySelector('[data-nav="models"]')?.click());action(capBox,'Model mapping',()=>document.querySelector('[data-nav="bindings"]')?.click());action(capBox,'Alias model',()=>document.querySelector('[data-nav="aliases"]')?.click());
    capBox.append(node('h3','Khả năng của adapter Codex'));for(const[n,v]of Object.entries(matrix.codex_oauth??{})){if(typeof v==='boolean')capBox.append(node('span',`${n} · ${v?'hỗ trợ':'chưa hỗ trợ'}`,{class:'pill '+(v?'ok':'muted')}));}capBox.append(node('p','WebSocket Responses theo lượt; chưa hỗ trợ warmup generate:false hoặc nhiều stream đồng thời.',{class:'muted'}));}
  function setupReports(){reportsBox.replaceChildren(node('span','USAGE REPORT · DỮ LIỆU ĐÃ LƯU',{class:'eyebrow'}),node('h3','Charge và chi phí upstream',{id:'codex-reports-heading'}));const form=node('form','',{class:'codex-report-filters'});reportsBox.append(form);
    field(form,'kind','Báo cáo',{choices:[['requests','Từng attempt'],['accounts','Theo tài khoản']]});field(form,'account','Tài khoản',{choices:[['','Tất cả'],...accounts.map((a,i)=>[String(i),a.email??'Tài khoản đã ẩn'])]});field(form,'model','Canonical model',{choices:[['','Tất cả'],...models.map(m=>[m.model_id,m.model_id])]});field(form,'from','Từ (UTC)',{type:'datetime-local'});field(form,'to','Đến (UTC)',{type:'datetime-local'});const submit=node('button','Tải báo cáo đã lưu',{type:'submit'});form.append(submit);const output=node('div');reportsBox.append(output);let page=null,filters={},kind='requests',generation=0;
    async function read(more=false){const g=++generation,t=epoch;const q=new URLSearchParams({...filters,...(more?{from:page.from,to:page.to,after:page.next_after}:{}),limit:'100'});const result=await api.request('/api/service/usage/'+kind+'?'+q);if(!current(t)||g!==generation)return;page=result;output.replaceChildren(node('p',`${stamp(result.from)} — ${stamp(result.to)}`));
      const name=id=>accounts.find(a=>a.id===id)?.email??'Tài khoản đã ẩn / Không rõ';
      if(kind==='requests')table(output,['Tài khoản / model','Attempt / request','Raw input / cache read / cache write / output','Computed / charged / held (weighted tokens)','Chi phí upstream'],result.items.map(r=>[name(r.credential_id)+' · '+r.model_id,r.status+' / '+r.request_state,[r.usage?.input_tokens,r.usage?.cached_read,r.usage?.cached_write,r.usage?.output_tokens].map(v=>v??unknown).join(' / '),[r.computed_micro,r.charged_micro,r.held_micro].map(amount).join(' / '),r.upstream_cost?`${r.upstream_cost.currency} ${r.upstream_cost.amount}`:unknown]));
      else table(output,['Tài khoản','Attempts / completed / unknown usage','Computed / charged / held','Raw input / read / write / output','Chi phí theo tiền tệ / unknown'],result.items.map(r=>[name(r.credential_id),`${r.attempt_count} / ${r.completed_count} / ${r.unknown_usage_count}`,[r.computed_micro,r.charged_micro,r.held_micro].map(amount).join(' / '),[r.input_tokens,r.cached_read,r.cached_write,r.output_tokens].map(v=>v??unknown).join(' / '),r.upstream_costs.map(c=>`${c.currency} ${c.amount}`).join(' · ')+` · Không rõ: ${r.unknown_cost_count}`]));
      if(result.items.some(r=>BigInt(r.held_micro??'0')>0n))output.append(node('p','Pending usage hold cần đối soát; không silent refund.',{class:'notice warning'}));
      output.append(node('p','Failed attempt không charge khách; chỉ serving attempt được quyết toán một lần. Chi phí upstream không quy đổi thành quota; Không rõ không phải 0.',{class:'muted'}));if(result.next_after)action(output,'Trang báo cáo tiếp theo',()=>perform(()=>read(true)));
    }
    form.onsubmit=e=>{e.preventDefault();kind=value(form,'kind');filters={};if(value(form,'account')!=='')filters.credential_id=accounts[Number(value(form,'account'))].id;if(value(form,'model'))filters.model_id=value(form,'model');for(const n of ['from','to'])if(value(form,n))filters[n]=value(form,n)+':00Z';perform(()=>read());};perform(()=>read());
  }
  function build(){root.replaceChildren();
    const heading=node('div','',{class:'codex-page-heading'});heading.append(node('h2','Accounts'),node('span','? ',{class:'codex-help','title':'Quản lý tài khoản OAuth và dịch vụ API Codex'}));root.append(heading);
    const serviceBar=node('section','',{class:'codex-service-bar'});const serviceName=node('div','',{class:'codex-service-name'});serviceName.append(node('span','◎',{class:'codex-service-icon'}),node('strong','Dịch vụ API Codex'),node('span','Đang đọc trạng thái',{class:'pill'}));serviceBar.append(serviceName);
    tabs=node('nav','',{class:'codex-tabs','aria-label':'Codex sections'});const tabDefs=[['overview','Tổng quan dịch vụ'],['keys','Khóa máy khách'],['accounts','Nhóm tài khoản'],['models','Mô hình và năng lực'],['usage','Thống kê và nhật ký']];for(const [id,label] of tabDefs){const tab=node('button',label,{type:'button',class:'codex-tab','data-codex-tab':id,'aria-controls':tabPanels[id]});tab.onclick=()=>switchTab(id);tabs.append(tab);}message=node('p','',{id:'codex-message',class:'notice',role:'status','aria-live':'polite'});const toolbar=node('div','',{class:'actions codex-toolbar'});const reload=action(toolbar,'Tải lại dữ liệu đã lưu',()=>perform(async()=>{await readList();if(state.accountId)await actions.read();await readStatus();}));reload.id='codex-reload-saved';root.append(serviceBar,tabs,message,toolbar);
    overviewBox=panel(root,'codex-overview','Tổng quan dịch vụ','SERVICE OVERVIEW');overviewBox.append(node('p','Số liệu được đọc từ trạng thái dịch vụ riêng tư.',{class:'muted'}));statusBox=node('div','',{class:'codex-overview-stats'});overviewBox.append(statusBox);
    const grid=node('div','',{class:'codex-layout',id:'codex-accounts-panel'});root.append(grid);listBox=panel(grid,'codex-list','Nhóm tài khoản','CREDENTIAL CATALOG');details=node('div','',{class:'codex-details'});grid.append(details);poolBox=panel(root,'codex-pool','Pool phục vụ','POOL EDITOR');keyBox=panel(root,'codex-keys','Khóa máy khách','QUOTA GRANT');reportsBox=panel(root,'codex-reports','Thống kê và nhật ký','USAGE REPORT');capBox=panel(root,'codex-capabilities','Mô hình và năng lực','CAPABILITY STRIP');
    renderList();renderDetail();renderPool();renderKeys();renderCapabilities();setupReports();switchTab('accounts');perform(readStatus);
  }
  return {
    async open(){active=true;const t=++epoch;proxyBusy=false;proxyNeedsReload=false;proxyState='loading';serviceStatus=null;serviceProxy=null;root.hidden=false;root.replaceChildren(node('p','Đang đọc danh mục tài khoản…'));await perform(async()=>{const values=[];for(const p of ['oauth-accounts','proxies','models','keys','capabilities']){if(!current(t))return;values.push(await api.request('/api/service/'+p));}if(!current(t))return;accounts=values[0].items;next=values[0].next_after;[profiles,models,keys,matrix]=values.slice(1);
      // Render the Cockpit-style shell as soon as the catalog is available.
      // Quota snapshots are a storage read for every account and must not
      // block the first paint (226 accounts otherwise looked like a frozen UI).
      if(current(t))build();
      readSavedQuota(accounts,t).then(()=>{if(current(t)){renderList();renderDetail();}}).catch(error=>{if(current(t)){notify('Không đọc được một số snapshot quota đã lưu: '+error.message,true);renderList();}});
    });},
    leave(){active=false;epoch++;listGeneration++;statusGeneration++;state.select(null);importBusy=false;refreshBusy=false;proxyBusy=false;root.hidden=true;root.replaceChildren();details=null;message=null;importBox=null;},
    clear(){this.leave();state.clear();accounts=[];profiles=[];proxyProfiles=[];models=[];keys=[];matrix={};selectedKey=null;pool=null;poolModel='';grantPending=new Set();savedQuota=new Map();savedCredits=new Map();selectedAccounts=new Set();refreshErrors=new Map();importResult=null;serviceStatus=null;serviceProxy=null;searchText='';healthFilter='Tất cả';}
  };
}
