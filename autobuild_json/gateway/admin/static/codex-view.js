import {node,action,field,value,checked,lines} from './dom.js';
import {microToDecimal,decimalToMicro} from './api.js';
import {createCodexState,canConsumeReset,quotaLabel,resetLocked,windowDuration,remainingPercent,mergePoolMembers,poolSingleSelection,recoverGrantFailure} from './codex-state.js';
import {createCodexActions} from './codex-actions.js';
import {icon,decorate,panelHead,modal} from './codex-ui.js';
import {keyForm,keyPayload} from './keys.js';
import {requestTrend} from './codex-report.js';
import {ACCOUNT_PAGE_SIZES,pageNumbers,accountPagePath} from './codex-pagination.js';
import {additionalQuotaGroups} from './codex-quota.js';

const unknown='Không rõ';
const amount=v=>v==null?unknown:BigInt(v)<0n?'-'+microToDecimal((-BigInt(v)).toString()):microToDecimal(v);
const stamp=v=>v?new Date(v).toLocaleString('vi-VN',{timeZone:'UTC'})+' UTC':unknown;
function panel(parent,id,title){const box=node('section','',{id,class:'card codex-panel','aria-label':title});panelHead(box,title,id+'-heading');parent.append(box);return box;}
function table(parent,headers,rows){const wrap=node('div','',{class:'codex-table-scroll'}),t=node('table'),head=node('tr');for(const h of headers)head.append(node('th',h,{scope:'col'}));t.append(node('thead'));t.firstChild.append(head);const body=node('tbody');for(const cells of rows){const tr=node('tr');for(const c of cells){const td=node('td');if(c instanceof Node)td.append(c);else td.textContent=c??unknown;tr.append(td);}body.append(tr);}if(!rows.length){const td=node('td','Chưa có dữ liệu.',{colspan:headers.length});const tr=node('tr');tr.append(td);body.append(tr);}t.append(body);wrap.append(t);parent.append(wrap);}
function idField(parent,name,label,options,id){const el=field(parent,name,label,options);el.id=id;return el;}
function freshness(projection){return !projection?.snapshot?unknown:projection.stale?'stale':'fresh';}
function quotaWindow(parent,label,w){const card=node('div','',{class:'codex-metric'}),left=remainingPercent(w?.used_percent);card.append(node('small',`${label} · ${windowDuration(w?.window_seconds)}`),node('strong',left==null?unknown:`${left}% còn lại`),node('small',quotaLabel(w)));if(left!==null)card.append(node('meter','',{min:0,max:100,value:left,'aria-label':`${label} còn lại (%)`}));card.append(node('small','Reset '+stamp(w?.reset_at)));parent.append(card);}
function additionalQuota(parent,projection,detail=false){
  const box=node('div','',{class:'codex-additional-quota'});
  for(const group of additionalQuotaGroups(projection)){
    const section=node('section','',{class:'codex-model-quota','aria-label':group.label+' quota'});
    section.append(node('strong',group.label),node('small',group.status,{class:'muted'}));
    const windows=node('div','',{class:detail?'codex-metrics':'codex-card-quota'});
    for(const [label,w] of [['Primary',group.primary],['Secondary',group.secondary]]){
      if(!w)continue;
      if(detail){quotaWindow(windows,group.label+' · '+label,w);continue;}
      const line=node('div','',{class:'quota-line'}),left=remainingPercent(w.used_percent);
      line.append(node('strong',w.window_seconds?windowDuration(w.window_seconds):label),node('span',left==null?unknown:`${left}% còn lại`));
      if(left!==null)line.append(node('meter','',{min:0,max:100,value:left,low:20,high:50,optimum:100,'aria-label':`${group.label} ${label} còn lại (%)`}));
      line.append(node('small',`${quotaLabel(w)} · reset ${stamp(w.reset_at)}`,{class:'muted'}));windows.append(line);
    }
    if(!group.primary&&!group.secondary)windows.append(node('small','Quota: Không rõ (không có cửa sổ quota)',{class:'muted'}));
    section.append(windows);box.append(section);
  }
  parent.append(box);
}

export function mergeReportAccountOptions(options,rows){
  const merged=new Map(options);
  for(const row of rows)merged.set(row.id,row.email??'Tài khoản đã ẩn');
  return merged;
}
export function reportAccountFilters(credentialId){return credentialId?{credential_id:credentialId}:{};}

// Most IDs stay in closures. Report account values use stable credential UUIDs
// because the visible account page can change while the report form remains live.
export function createCodexWorkspace(root,api,onUnauthorized){
  const state=createCodexState();let active=false,epoch=0,accounts=[],pageSize=48,pageIndex=0,pageTotal=0,pageCount=1,listBusy=false,searchTimer=null,loadedSearch='',loadedHealth='Tất cả',profiles=[],models=[],keys=[],matrix={},selectedKey=null,pool=null,poolModel='',grantPending=new Set(),savedQuota=new Map(),healthFilter='Tất cả';
  const actions=createCodexActions(api,state,()=>{if(active){if(state.accountId&&state.usage)savedQuota.set(state.accountId,state.usage);if(state.accountId&&state.credits)savedCredits.set(state.accountId,state.credits);renderDetail();if(listBox)renderList();}});
  let message,details,poolBox,keyBox,reportsBox,capBox,listBox,importBox,importDialog,statusBox,overviewBox,tabs,serviceStatus=null,serviceProxy=null,keyDialog=null,selectedModel=null;
  let importResult=null,importBusy=false,refreshBusy=false,selectedAccounts=new Set(),savedCredits=new Map(),activeTab='accounts',searchText='',refreshErrors=new Map(),listGeneration=0;
  let quotaRefresh={settings:null,job:null,concurrency:2},quotaRefreshTimer=null,quotaRefreshBusy=false,quotaRefreshGeneration=0,quotaRefreshControls=null,quotaRefreshDraft=false,quotaRefreshPending=null,quotaSnapshotAt=0;
  let proxyBusy=false,proxyNeedsReload=false,proxyState='loading',proxyProfiles=[],statusGeneration=0;
  let statsTimer=null,statsBusy=false,usageNote=null;
  let reportAccounts=new Map(),reportAccountSelect=null;
  const tabPanels={overview:'codex-overview',keys:'codex-keys',accounts:'codex-accounts-panel',models:'codex-capabilities',usage:'codex-reports'};
  const current=t=>active&&t===epoch;
  function notify(text,error=false){if(message){message.textContent=text;message.className='notice'+(error?' warning':'');}for(const dialog of root.querySelectorAll('dialog[open]')){let note=dialog.querySelector('.dialog-notice');if(!note){note=node('p','',{class:'dialog-notice',role:'status'});dialog.prepend(note);}note.textContent=text;note.className='dialog-notice notice'+(error?' warning':'');}}
  function switchTab(id){activeTab=id;clearTimeout(statsTimer);for(const tab of tabs?.querySelectorAll('button')??[]){tab.classList.toggle('active',tab.dataset.codexTab===id);tab.setAttribute('aria-pressed',String(tab.dataset.codexTab===id));}for(const p of Object.values(tabPanels).map(x=>root.querySelector('#'+x)).filter(Boolean))p.hidden=p.id!==tabPanels[id];if(poolBox)poolBox.hidden=id!=='accounts';if(id==='overview')perform(refreshStats);}
  async function refreshStats(){
    if(statsBusy||!active||activeTab!=='overview')return;
    const t=epoch;statsBusy=true;
    try{const next=await api.request('/api/service/codex-service/status');if(current(t)){serviceStatus=next;renderUsageStats();}}
    finally{if(current(t)){statsBusy=false;if(activeTab==='overview')statsTimer=setTimeout(()=>perform(refreshStats),10000);}}
  }
  function renderUsageStats(){
    if(!statusBox?.isConnected)return;
    const u=serviceStatus?.usage??{};statusBox.replaceChildren();
    for(const [label,key]of [['Tổng token','total_tokens'],['Input token','input_tokens'],['Cache đọc','cached_read'],['Cache ghi','cached_write'],['Output token','output_tokens'],['Reasoning (trong output)','reasoning'],['Yêu cầu','requests'],['Hoàn tất','completed'],['Lỗi','failed'],['Chờ đối soát','pending']]){
      const val=u[key],s=node('div','',{class:'codex-stat','data-usage':key});s.append(node('small',label),node('strong',val==null?unknown:BigInt(val).toLocaleString('vi-VN')),node('span','Toàn gateway · đã ghi nhận',{class:'muted'}));statusBox.append(s);
    }
    if(usageNote)usageNote.textContent=`Tổng = input + output. Cache nằm trong input; reasoning nằm trong output, không cộng lần nữa. Quota đã trừ: ${amount(u.charged_micro)}. ${u.pending??0} request chưa chốt usage; ${u.unknown_usage_requests??0} hoàn tất thiếu usage, ${u.unknown_detail_requests??0} thiếu chi tiết. Các số trên chỉ cộng usage có bằng chứng. Tự cập nhật mỗi 10 giây.`;
  }
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
  function refreshJobActive(){return ['queued','running'].includes(quotaRefresh.job?.state);}
  function scheduleQuotaRefreshPoll(){
    clearTimeout(quotaRefreshTimer);quotaRefreshTimer=null;
    if(!active)return;
    quotaRefreshTimer=setTimeout(()=>perform(readQuotaRefresh),refreshJobActive()?1000:10000);
  }
  async function readQuotaRefresh(){
    if(quotaRefreshBusy){scheduleQuotaRefreshPoll();return;}
    const t=epoch,g=++quotaRefreshGeneration;
    try{
      const next=await api.request('/api/service/codex-service/quota-refresh');
      if(!current(t)||g!==quotaRefreshGeneration)return;
      const changed=JSON.stringify(quotaRefresh.job)!==JSON.stringify(next.job);
      quotaRefresh=next;updateQuotaRefreshControls();
      if(changed){
        renderList();renderDetail();
        if(next.job&&(!refreshJobActive()||Date.now()-quotaSnapshotAt>=5000)&&!listBusy){
          quotaSnapshotAt=Date.now();const listTicket=listGeneration;
          await readSavedQuota(accounts,t,listTicket);
          if(current(t)&&listTicket===listGeneration){renderList();if(state.accountId&&!state.busy)await actions.read();}
        }
      }
    }finally{if(current(t))scheduleQuotaRefreshPoll();}
  }
  async function startQuotaRefresh(){
    if(quotaRefreshBusy||refreshBusy||state.busy||refreshJobActive())return;
    const t=epoch;quotaRefreshBusy=true;++quotaRefreshGeneration;quotaRefreshPending??=crypto.randomUUID();renderList();renderDetail();
    try{
      const result=await api.request('/api/service/codex-service/quota-refresh',{method:'POST',body:{request_id:quotaRefreshPending}});
      if(!current(t))return;quotaRefresh=result;quotaRefreshPending=null;notify('Đã xếp hàng refresh quota tất cả tài khoản, không giới hạn trang hoặc bộ lọc.');
    }finally{if(current(t)){quotaRefreshBusy=false;renderList();renderDetail();scheduleQuotaRefreshPoll();}}
  }
  async function stopQuotaRefresh(){
    if(quotaRefreshBusy||!refreshJobActive())return;
    const t=epoch;quotaRefreshBusy=true;++quotaRefreshGeneration;renderList();
    try{
      const result=await api.request('/api/service/codex-service/quota-refresh/stop',{method:'POST',body:{request_id:quotaRefresh.job.id}});
      if(!current(t))return;quotaRefresh=result;
      notify('Đã yêu cầu dừng refresh quota; lượt đang chạy sẽ kết thúc an toàn.');
    }finally{if(current(t)){quotaRefreshBusy=false;renderList();scheduleQuotaRefreshPoll();}}
  }
  async function saveQuotaRefreshInterval(input){
    if(quotaRefreshBusy||!quotaRefresh.settings||!input.reportValidity())return;
    const minutes=Number(input.value);
    if(!Number.isInteger(minutes)||minutes<0||minutes>999){notify('Chu kỳ tự động phải từ 0 đến 999 phút.',true);return;}
    const t=epoch;quotaRefreshBusy=true;++quotaRefreshGeneration;renderList();
    try{
      const result=await api.request('/api/service/codex-service/quota-refresh/settings',{method:'PUT',body:{version:quotaRefresh.settings.version,interval_minutes:minutes}});
      if(!current(t))return;quotaRefresh=result;quotaRefreshDraft=false;
      notify(minutes?'Đã lưu tự động refresh mỗi '+minutes+' phút.':'Đã tắt tự động refresh quota.');
    }catch(error){
      if(current(t)&&error.status===409){const latest=await api.request('/api/service/codex-service/quota-refresh');if(current(t))quotaRefresh=latest;}
      throw error;
    }finally{if(current(t)){quotaRefreshBusy=false;renderList();scheduleQuotaRefreshPoll();}}
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
    overviewBox.replaceChildren();const connection=node('div','',{class:'codex-connection'});overviewBox.append(connection);
    const config=node('article');config.append(node('h3','Cấu hình dịch vụ'));const address=node('div','',{class:'codex-config-row'}),url=serviceStatus?.base_url??'';address.append(node('span','Base URL'),node('code',url||unknown));decorate(action(address,'Sao chép Base URL',()=>perform(()=>navigator.clipboard.writeText(url))),'copy',true).disabled=!url;config.append(address);
    const access=node('div','',{class:'codex-config-row'});access.append(node('span','Khóa máy khách'),node('code','Bearer <API_KEY>'));decorate(action(access,'Quản lý khóa',()=>switchTab('keys')),'key',true);config.append(access);
    connection.append(config);renderProxyControls(config);decorate(action(config,'Quản lý proxy / KiotProxy',()=>document.querySelector('[data-nav="proxies"]')?.click()),'route');const reload=root.querySelector('#codex-reload-saved');if(reload)reload.disabled=proxyBusy;
    const health=node('article');health.append(node('h3','Sức khỏe dịch vụ'));const metrics=node('div','',{class:'health-grid'});const a=serviceStatus?.accounts??{};
    for(const [label,v]of [['Tài khoản hoạt động',a.total==null?unknown:`${a.active}/${a.total}`],['Chưa xác minh',a.unverified],['Cần OAuth lại',a.reauth_required],['Khóa máy khách',serviceStatus?.keys?.enabled]]){const c=node('div');c.append(node('small',label),node('strong',v??unknown));metrics.append(c);}health.append(metrics,node('p',`${a.disabled??unknown} tài khoản tắt · ${a.refresh_uncertain??unknown} tài khoản cần đối soát refresh`,{class:'muted'}),node('p','Trạng thái và quota lấy từ dữ liệu đã lưu, không phải phép thử kết nối upstream.',{class:'muted'}));connection.append(health);
    statusBox=node('div','',{class:'codex-overview-stats'});usageNote=node('p','',{class:'muted'});overviewBox.append(statusBox,usageNote);renderUsageStats();
    const compat=panel(overviewBox,'codex-protocol-panel','Tương thích giao thức'),protocols=node('div','',{class:'codex-protocols'});
    for(const [name,path,note]of [['OpenAI Chat','/v1/chat/completions','OPENAI_BASE_URL'],['Responses','/v1/responses','OPENAI_BASE_URL'],['Compact','/v1/responses/compact','OPENAI_BASE_URL'],['Images','/v1/images/generations','OPENAI_BASE_URL'],['Images edits','/v1/images/edits','OPENAI_BASE_URL'],['Responses WebSocket','WS /v1/responses','WS /v1/responses']]){const box=node('article');box.append(node('strong',name),node('code',path),node('pre',note.startsWith('WS')?'Authorization: Bearer <API_KEY>\n'+note:`${note}=${url||'<BASE_URL>'}\nOPENAI_API_KEY=<API_KEY>`));protocols.append(box);}compat.append(protocols,node('p','Danh mục mô hình  /v1/models · Chỉ model đã xuất bản và được cấp quyền cho key mới được trả về.',{class:'muted'}));
  }
  function serviceNameStatus(status){const pill=root.querySelector('.codex-service-name .service-status');if(pill){pill.textContent=({configured:'Đã cấu hình',running:'Đang chạy',unavailable:'Chưa khả dụng'})[status?.status]??unknown;pill.className='pill service-status '+(status?.status==='configured'||status?.status==='running'?'ok':'warn');}}
  async function perform(fn){const t=epoch;try{await fn();}catch(e){if(!current(t)||e.name==='AbortError'||e.message==='STALE_SESSION')return;if(e.status===401)onUnauthorized();else notify(e.status===409?'409 · Xung đột phiên bản hoặc thao tác. Tải lại dữ liệu đã lưu trước khi thử lại.':e.message,true);}}
  async function readSavedQuota(rows,t,g=listGeneration){let i=0;await Promise.all(Array.from({length:Math.min(4,rows.length)},async()=>{while(current(t)&&g===listGeneration&&i<rows.length){const row=rows[i++],base='/api/service/oauth-accounts/'+encodeURIComponent(row.id);const [usage,credits]=await Promise.all([api.request(base+'/quota'),api.request(base+'/reset-credits')]);if(!current(t)||g!==listGeneration)return;savedQuota.set(row.id,usage);savedCredits.set(row.id,credits);}}));}
  async function selectAccount(row){const t=epoch;state.select(row.id);renderDetail();await actions.read();if(current(t)&&state.accountId===row.id){renderList();details.scrollIntoView({block:'nearest'});}}
  async function refreshMany(rows){
    if(refreshBusy||state.busy||quotaRefreshBusy||refreshJobActive())return;refreshBusy=true;const t=epoch;let index=0,done=0,failed=0,stop=false;renderList();
    try{
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
          importResult=result;files.value='';pasted.value='';await readList(1);await readStatus();
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
  function acceptAccountPage(page,search,status){
    const changed=pageIndex!==page.page-1||pageSize!==page.page_size||loadedSearch!==search||loadedHealth!==status;
    accounts=page.items;pageIndex=page.page-1;pageSize=page.page_size;pageTotal=page.total;pageCount=page.total_pages;loadedSearch=search;loadedHealth=status;
    reportAccounts=mergeReportAccountOptions(reportAccounts,accounts);
    if(reportAccountSelect?.isConnected){const selected=reportAccountSelect.value;reportAccountSelect.replaceChildren(node('option','Tất cả',{value:''}),...[...reportAccounts].map(([id,label])=>node('option',label,{value:id})));reportAccountSelect.value=selected;}
    if(changed){selectedAccounts.clear();savedQuota.clear();savedCredits.clear();refreshErrors.clear();}
    else selectedAccounts=new Set([...selectedAccounts].filter(id=>accounts.some(row=>row.id===id)));
    if(state.accountId&&!accounts.some(row=>row.id===state.accountId))state.select(null);
  }
  async function readList(target=pageIndex+1,size=pageSize){
    clearTimeout(searchTimer);searchTimer=null;
    const t=epoch,g=++listGeneration,search=searchText.trim(),status=healthFilter;
    listBusy=true;if(listBox)renderList();
    try{
      const page=await api.request(accountPagePath(target,size,search,status));
      if(!current(t)||g!==listGeneration)return;
      acceptAccountPage(page,search,status);listBusy=false;renderList();renderDetail();
      try{await readSavedQuota(page.items,t,g);}catch(error){if(!current(t)||g!==listGeneration)return;if(error.status===401)throw error;notify('Không đọc được một số snapshot quota đã lưu: '+error.message,true);}
      if(current(t)&&g===listGeneration){renderList();renderDetail();}
    }catch(error){if(current(t)&&g===listGeneration){searchText=loadedSearch;healthFilter=loadedHealth;throw error;}}
    finally{if(current(t)&&g===listGeneration){listBusy=false;renderList();}}
  }
  function changeAccountPage(target){
    if(listBusy||refreshBusy||state.busy||!Number.isInteger(target)||target<1||target>pageCount)return;
    perform(()=>readList(target));
  }
  function renderPagination(parent,position){
    const footer=node('div','',{class:'codex-pagination','data-page':pageIndex+1,'data-position':position});
    const first=pageTotal?pageIndex*pageSize+1:0,last=pageTotal?first+accounts.length-1:0;
    footer.append(node('span',`${first}–${last} / ${pageTotal} tài khoản · Trang ${pageIndex+1}/${pageCount}`,{class:'page-summary',role:'status'}));
    const pager=node('nav','',{class:'account-page-buttons','aria-label':`Phân trang tài khoản ${position==='top'?'trên':'dưới'}`}),locked=listBusy||refreshBusy||Boolean(state.busy);
    const button=(label,target,disabled=false)=>{const b=action(pager,label,()=>changeAccountPage(target));b.disabled=locked||disabled;return b;};
    button('Đầu',1,pageIndex===0).setAttribute('aria-label','Trang đầu');button('Trước',pageIndex,pageIndex===0).setAttribute('aria-label','Trang trước');
    for(const n of pageNumbers(pageIndex+1,pageCount)){
      if(n===null){pager.append(node('span','…',{class:'page-ellipsis','aria-hidden':'true'}));continue;}
      const b=button(String(n),n,n===pageIndex+1);b.setAttribute('aria-label','Trang '+n);if(n===pageIndex+1){b.classList.add('active');b.setAttribute('aria-current','page');}
    }
    button('Sau',pageIndex+2,pageIndex+1>=pageCount).setAttribute('aria-label','Trang tiếp theo');button('Cuối',pageCount,pageIndex+1>=pageCount).setAttribute('aria-label','Trang cuối');
    footer.append(pager);
    if(position==='top'){
      const size=field(footer,'page-size','Mỗi trang',{choices:ACCOUNT_PAGE_SIZES.map(n=>[String(n),String(n)]),value:String(pageSize)});size.id='codex-page-size';size.disabled=locked;size.onchange=()=>perform(()=>readList(1,Number(size.value)));
      const jump=node('form','',{class:'account-page-jump'}),input=field(jump,'page-jump','Đến trang',{type:'number',value:pageIndex+1});input.id='codex-page-jump';input.min='1';input.max=String(pageCount);input.step='1';input.required=true;input.disabled=locked||pageCount===1;
      const go=node('button','Đi',{type:'submit'});go.disabled=input.disabled;jump.append(go);jump.onsubmit=e=>{e.preventDefault();changeAccountPage(Number(input.value));};footer.append(jump);
    }
    parent.append(footer);
  }
  function renderQuotaRefreshControls(parent){
    if(!quotaRefreshControls){
      quotaRefreshControls=node('div','',{class:'codex-quota-refresh-controls'});
      decorate(action(quotaRefreshControls,'Refresh quota tất cả tài khoản',()=>perform(startQuotaRefresh),'primary'),'refresh').id='codex-refresh-all';
      action(quotaRefreshControls,'Dừng',()=>perform(stopQuotaRefresh)).id='codex-stop-refresh-all';
      const form=node('form','',{class:'codex-quota-refresh-form'}),presets=field(form,'refresh-preset','Tự động refresh',{choices:[['0','Tắt'],['2','2 phút'],['5','5 phút'],['10','10 phút'],['15','15 phút'],['custom','Tùy chỉnh…']],value:'0'});presets.id='codex-auto-refresh-preset';
      const interval=field(form,'refresh-minutes','Số phút',{type:'number',value:0,required:true});interval.id='codex-auto-refresh-minutes';interval.min='0';interval.max='999';interval.step='1';
      const save=node('button','Lưu chu kỳ',{id:'codex-auto-refresh-save',type:'submit'});form.append(save);
      interval.oninput=()=>{quotaRefreshDraft=true;presets.value='custom';};presets.onchange=()=>{quotaRefreshDraft=true;if(presets.value!=='custom')interval.value=presets.value;else interval.focus();};
      form.onsubmit=e=>{e.preventDefault();perform(()=>saveQuotaRefreshInterval(interval));};
      quotaRefreshControls.append(form,node('span','',{class:'codex-quota-refresh-status',role:'status'}),node('progress','',{id:'codex-quota-refresh-progress','aria-label':'Tiến độ refresh quota tất cả'}),node('small','',{id:'codex-quota-refresh-schedule'}));
      const errors=node('details','',{id:'codex-quota-refresh-errors'});errors.append(node('summary','Lỗi trong lượt refresh'),node('ul'));quotaRefreshControls.append(errors);
    }
    parent.append(quotaRefreshControls);updateQuotaRefreshControls();
  }
  function updateQuotaRefreshControls(){
    if(!quotaRefreshControls)return;
    const box=quotaRefreshControls,job=quotaRefresh.job,settings=quotaRefresh.settings,locked=refreshBusy||quotaRefreshBusy||Boolean(state.busy)||refreshJobActive();
    for(const button of [box.querySelector('#codex-refresh-all'),root.querySelector('#codex-refresh-all-toolbar')].filter(Boolean))button.disabled=locked||!settings;
    const stop=box.querySelector('#codex-stop-refresh-all');stop.hidden=!refreshJobActive();stop.disabled=quotaRefreshBusy||job?.stop_requested;
    const status=box.querySelector('.codex-quota-refresh-status'),processed=(job?.succeeded??0)+(job?.failed??0)+(job?.skipped??0);
    const labels={queued:'Đang xếp hàng',running:'Đang chạy',completed:'Đã hoàn tất',stopped:'Đã dừng',failed:'Thất bại',interrupted:'Bị gián đoạn'};
    status.textContent=job?`${labels[job.state]??job.state} · ${processed}/${job.total} · Thành công ${job.succeeded} · Lỗi ${job.failed} · Bỏ qua ${job.skipped}${job.stop_requested&&refreshJobActive()?' · Đang dừng…':''}${job.result_code?' · '+job.result_code:''}`:settings?'Chưa chạy refresh tất cả.':'Đang đọc cấu hình refresh…';
    const progress=box.querySelector('progress');progress.hidden=!job;progress.max=job?.total||1;progress.value=processed;
    const interval=box.querySelector('#codex-auto-refresh-minutes'),presets=box.querySelector('#codex-auto-refresh-preset');
    if(!quotaRefreshDraft){interval.value=String(settings?.interval_minutes??0);presets.value=['0','2','5','10','15'].includes(interval.value)?interval.value:'custom';}
    interval.disabled=presets.disabled=box.querySelector('#codex-auto-refresh-save').disabled=quotaRefreshBusy||!settings;
    box.querySelector('#codex-quota-refresh-schedule').textContent=`${settings?.interval_minutes?'Đã lưu: mỗi '+settings.interval_minutes+' phút'+(settings.next_run_at?' · Lần tới '+stamp(settings.next_run_at):' · Tính từ khi lượt hiện tại kết thúc'):'Tự động đang tắt · 0 = tắt'}. Áp dụng toàn bộ tài khoản đang bật, không phụ thuộc trang/bộ lọc. Chạy tại server kể cả khi đóng trang; dùng proxy đã cấu hình. Refresh chỉ cập nhật số liệu, không reset quota.`;
    const errors=box.querySelector('#codex-quota-refresh-errors');errors.hidden=!job?.errors?.length;const list=errors.querySelector('ul');list.replaceChildren();for(const error of job?.errors??[])list.append(node('li',`${error.email??accounts.find(a=>a.id===error.credential_id)?.email??'Tài khoản ngoài trang'} · ${error.code}`));if(job?.failed>100)list.append(node('li','Chỉ hiển thị 100 lỗi đầu.'));
  }
  function renderList(){
    const focused=document.activeElement,searchFocus=focused?.name==='account-search'&&listBox.contains(focused),caret=searchFocus?[focused.selectionStart,focused.selectionEnd]:null;
    const refreshFocus=quotaRefreshControls?.contains(focused)?focused:null;
    listBox.replaceChildren();
    listBox.setAttribute('aria-busy',String(listBusy));
    const head=panelHead(listBox,'Theo tài khoản','codex-list-heading');
    decorate(action(head,'Thêm tài khoản',()=>importDialog.showModal(),'primary'),'plus');
    decorate(action(head,'Bản đồ mô hình',()=>document.querySelector('[data-nav="bindings"]')?.click()),'route');
    decorate(action(head,'Quản lý thành viên',()=>{const members=root.querySelector('#codex-pool-members');if(members)members.open=true;root.querySelector('#codex-pool-model')?.focus();}),'users');
    renderImport();
    const tools=node('div','',{class:'codex-account-tools'});listBox.append(tools);
    const filter=field(tools,'health','Lọc trạng thái',{choices:['Tất cả','active','unverified','reauth_required','refresh_uncertain','disabled'].map(x=>[x,x]),value:healthFilter});
    const search=field(tools,'account-search','Tìm email (toàn bộ tài khoản)',{value:searchText});search.type='search';search.maxLength=320;search.placeholder='Tìm trên tất cả các trang…';
    renderQuotaRefreshControls(tools);
    const refreshSelected=action(tools,'Làm mới đã chọn (trang này)',()=>perform(()=>refreshMany(accounts.filter(r=>selectedAccounts.has(r.id)))));refreshSelected.disabled=listBusy||refreshBusy||quotaRefreshBusy||refreshJobActive()||Boolean(state.busy)||!selectedAccounts.size;
    renderPagination(listBox,'top');
    const rows=node('div');listBox.append(rows);
    const draw=()=>{rows.replaceChildren();rows.className='codex-account-grid';for(const r of accounts){
      const b=node('button',r.email??'Chưa có email',{type:'button',class:'row-link','aria-pressed':String(state.accountId===r.id)});b.style.whiteSpace='normal';b.style.overflowWrap='anywhere';b.style.textOverflow='clip';b.disabled=listBusy;b.onclick=()=>perform(()=>selectAccount(r));
      const projection=savedQuota.get(r.id),credits=savedCredits.get(r.id),card=node('article','',{class:'codex-account-card'}),head=node('div','',{class:'codex-account-head'}),check=node('input','',{type:'checkbox','aria-label':`Chọn ${r.email??'tài khoản'}`});
      check.checked=selectedAccounts.has(r.id);check.disabled=listBusy;check.onchange=()=>{if(check.checked)selectedAccounts.add(r.id);else selectedAccounts.delete(r.id);refreshSelected.disabled=listBusy||refreshBusy||quotaRefreshBusy||refreshJobActive()||Boolean(state.busy)||!selectedAccounts.size;};
      const plan=projection?.snapshot?.plan_type??r.plan_type;
      head.append(check,b,node('span',plan??unknown,{class:'plan-badge'+(!plan?' unknown':'')}));card.append(head,node('small',`${r.enabled?r.status:'disabled'} · ${r.proxy_profile_name??'Kế thừa provider'}`,{class:'muted'}));
      const creditLabel=`Lượt đặt lại ${credits?.snapshot?.available_count??unknown}`;const creditButton=action(card,creditLabel,()=>perform(async()=>{await selectAccount(r);root.querySelector('#codex-reset')?.scrollIntoView({block:'nearest'});}));creditButton.className='pill';creditButton.disabled=listBusy;
      const quota=node('div','',{class:'codex-card-quota'});for(const [label,w] of [['Primary',projection?.snapshot?.primary],['Secondary',projection?.snapshot?.secondary]]){const line=node('div','',{class:'quota-line'}),left=remainingPercent(w?.used_percent);line.append(node('strong',w?.window_seconds?windowDuration(w.window_seconds):label),node('span',left==null?unknown:`${left}% còn lại`));if(left!==null)line.append(node('meter','',{min:0,max:100,value:left,low:20,high:50,optimum:100,'aria-label':`${label} còn lại (%)`}));line.append(node('small',`${quotaLabel(w)} · reset ${stamp(w?.reset_at)}`,{class:'muted'}));quota.append(line);}card.append(quota);
      const error=refreshErrors.get(r.id)??projection?.last_error??credits?.last_error;if(error)card.append(node('p',error,{class:'codex-card-error'}));
      additionalQuota(card,projection);
      const actionsRow=node('div','',{class:'codex-card-actions'});actionsRow.append(node('span',`${freshness(projection)} · ${stamp(projection?.fetched_at)}`,{class:'muted'}));decorate(action(actionsRow,'Mở chi tiết',()=>perform(()=>selectAccount(r))),'settings',true).disabled=listBusy;decorate(action(actionsRow,'Làm mới',()=>perform(()=>refreshMany([r]))),'refresh',true).disabled=listBusy||refreshBusy||quotaRefreshBusy||refreshJobActive()||Boolean(state.busy);card.append(actionsRow);rows.append(card);}
      if(!rows.childElementCount)rows.append(node('p','Chưa có tài khoản phù hợp. Thêm tài khoản hoặc đổi bộ lọc.',{class:'empty'}));
    };filter.onchange=()=>{healthFilter=filter.value;perform(()=>readList(1));};search.oninput=()=>{searchText=search.value;clearTimeout(searchTimer);++listGeneration;listBusy=true;renderList();searchTimer=setTimeout(()=>{searchTimer=null;perform(()=>readList(1));},250);};draw();
    renderPagination(listBox,'bottom');
    listBox.append(node('p','Quota OpenAI · Freshness lấy từ snapshot đã lưu; chỉ đọc trang đang xem. Chọn tài khoản áp dụng trong trang hiện tại.',{class:'muted'}));
    if(searchFocus){search.focus({preventScroll:true});search.setSelectionRange(...caret);}
    if(refreshFocus&&!refreshFocus.disabled)refreshFocus.focus({preventScroll:true});
  }
  function renderDetail(){if(!details)return;const focused=document.activeElement?.id;details.replaceChildren();const row=accounts.find(a=>a.id===state.accountId);
    details.hidden=!row;if(!row)return;
    const account=panel(details,'codex-detail',row.email??'Chi tiết tài khoản');
    decorate(action(account.querySelector('.panel-head .actions'),'Đóng chi tiết',()=>{state.select(null);renderDetail();renderList();}),'close',true);
    account.append(node('p',`${row.status} · ${state.usage?.snapshot?.plan_type??row.plan_type??unknown}`,{class:'muted'}));const metrics=node('div','',{class:'codex-metrics'});quotaWindow(metrics,'Primary',state.usage?.snapshot?.primary);quotaWindow(metrics,'Secondary',state.usage?.snapshot?.secondary);account.append(metrics);additionalQuota(account,state.usage,true);
    account.append(node('p',`${freshness(state.usage)} · cập nhật ${stamp(state.usage?.fetched_at)} · ${state.usage?.last_error??'Snapshot đã lưu'}`,{class:'muted'}));
    const settings=node('form');const profile=field(settings,'profile','Proxy credential',{choices:[['','Kế thừa provider'],...profiles.map((p,i)=>[String(i),`${p.config.mode} · ${p.name}`])],value:row.proxy_profile_id?String(profiles.findIndex(p=>p.id===row.proxy_profile_id)):''});
    const enabled=field(settings,'enabled','Cho phép sử dụng',{type:'checkbox',value:row.enabled});account.append(settings);
    const bulkLocked=quotaRefreshBusy||refreshJobActive(),locked=Boolean(state.busy)||refreshBusy||bulkLocked||resetLocked(state.operation);
    const save=action(account,'Lưu tài khoản',()=>perform(async()=>{await actions.saveAccount(row,enabled.checked,profile.value===''?null:profiles[Number(profile.value)]?.id);await readList();renderDetail();}));save.disabled=locked;
    const quota=action(account,'Cập nhật quota',()=>perform(()=>actions.refresh()));quota.id='codex-refresh-quota';quota.disabled=Boolean(state.busy)||refreshBusy||bulkLocked;
    const token=action(account,'Refresh token',()=>perform(async()=>{await actions.refreshToken();await readList();await actions.read();}));token.disabled=locked;
    const reset=panel(details,'codex-reset','Xác nhận reset theo credit','RESET QUOTA OPENAI');
    reset.append(node('p','Credit khả dụng: '+(state.credits?.snapshot?.available_count??unknown)),node('span',freshness(state.credits),{id:'codex-credit-freshness',class:'pill'}));
    const expiries=state.credits?.snapshot?.credits?.filter(c=>c.state==='available'&&c.expires_at).map(c=>c.expires_at).sort();reset.append(node('p','Hết hạn gần nhất: '+stamp(expiries?.[0])));
    const operation=state.operation?.state??'Chưa gửi';reset.append(node('p',operation,{id:'codex-reset-state',class:'notice'+(resetLocked(state.operation)?' warning':''),'aria-live':'polite'}));
    if(resetLocked(state.operation))reset.append(node('p',operation==='succeeded_refresh_failed'?'OpenAI đã chấp nhận; Cập nhật quota đầy đủ, fresh sẽ được server xác nhận thành công. Partial refresh vẫn khóa; không gửi lại reset.':'Chưa xác định kết quả cuối cùng. Không tự gửi lại; tải trạng thái đã lưu hoặc đối soát bằng chứng.',{class:'notice warning'}));
    reset.append(node('p','Tiêu thụ 01 provider-granted reset credit. Quota OpenAI (%) độc lập với weighted quota của key khách. Không gửi lại tự động.',{id:'codex-reset-note',class:'muted'}));
    const ack=idField(reset,'ack','Tôi xác nhận tiêu thụ một provider-granted reset credit',{type:'checkbox'},'codex-ack');
    const consume=action(reset,'Đặt lại quota OpenAI',()=>perform(async()=>{actions.confirm();await actions.consume();}),'primary');consume.id='codex-consume';consume.setAttribute('aria-describedby','codex-reset-note');
    const allowed=!state.busy&&!refreshBusy&&!bulkLocked&&canConsumeReset({available_count:state.credits?.snapshot?.available_count,fresh:state.credits?.stale===false,operation_state:state.operation?.state});ack.disabled=!allowed;consume.disabled=true;ack.onchange=()=>{consume.disabled=!(ack.checked&&allowed);};
    const evidence=panel(details,'codex-evidence','Đối soát theo bằng chứng','ADMIN ONLY');const form=node('form');evidence.append(form);
    field(form,'outcome','Outcome',{choices:[['confirmed_applied','Đã áp dụng'],['confirmed_not_applied','Chưa áp dụng']]});field(form,'reason','Lý do và nguồn bằng chứng (không nhập secret)',{type:'textarea',required:true}).maxLength=500;
    const resolve=node('button','Áp dụng outcome',{type:'submit'});resolve.disabled=Boolean(state.busy)||!['unknown','succeeded_refresh_failed'].includes(operation)||state.operation?.version==null;form.append(resolve);form.onsubmit=e=>{e.preventDefault();perform(()=>actions.resolve(value(form,'outcome'),value(form,'reason')));};
    if(focused){const el=document.getElementById(focused);if(el&&!el.disabled)el.focus();}
  }
  async function readPool(){if(!poolModel)return;const t=epoch,model=poolModel;const result=await api.request('/api/service/account-pools/'+model.split('/').map(encodeURIComponent).join('/'));if(current(t)&&model===poolModel){pool=result;renderPool();}}
  function renderPool(){poolBox.replaceChildren();panelHead(poolBox,'Tùy chọn định tuyến','codex-pool-heading');
    const model=idField(poolBox,'model','Canonical model',{choices:[['','Chọn model'],...models.map(m=>[m.model_id,m.model_id])],value:poolModel},'codex-pool-model');model.onchange=()=>{poolModel=model.value;pool=null;perform(readPool);};
    if(!pool){poolBox.append(node('p',models.length?'Chọn model để đọc và chỉnh sửa nhóm tài khoản phục vụ.':'Chưa có model được xuất bản. Xuất bản tại Mô hình và năng lực để cấu hình định tuyến.',{class:'muted'}));decorate(action(poolBox,'Mô hình và năng lực',()=>switchTab('models')),'image');return;}
    poolBox.append(node('p','version '+pool.version,{id:'codex-pool-version'}));
    if(pool.policy&&!pool.policy.all_accounts&&!pool.policy.members?.length)poolBox.append(node('p','Pool đã cấu hình nhưng chưa có thành viên. Request mới sẽ tạm dừng; không tự chuyển sang toàn bộ tài khoản.',{class:'notice warning'}));
    const p=pool.policy??{},editorAccounts=accounts.slice(),form=node('form','',{id:'codex-pool-form'});poolBox.append(form);
    field(form,'mode','Chế độ chọn pool',{choices:['auto','round_robin','random','single','priority','weight'].map(x=>[x,x]),value:p.mode??'auto'});
    field(form,'all_accounts','Toàn bộ tài khoản đã liên kết model (tự cập nhật khi thêm tài khoản)',{type:'checkbox',value:p.all_accounts??false});
    form.append(node('p','round_robin xoay tuần tự qua pool. Chỉ tài khoản enabled, OAuth active và đủ điều kiện được phục vụ; tài khoản chưa xác minh cần Refresh quota/OAuth. Danh sách dưới dùng để chọn thủ công hoặc đặt ưu tiên riêng.',{class:'muted'}));
    const members=node('details','',{id:'codex-pool-members',class:'pool-members'});members.append(node('summary','Thành viên · '+(p.members?.length??0)+' tài khoản'));form.append(members);
    const memberControls=accounts.map((a,i)=>{const member=p.members?.find(m=>m.credential_id===a.id),container=node('div','',{class:'codex-member'});members.append(container);
      const include=field(container,'member-'+i,a.email??'Tài khoản đã ẩn',{type:'checkbox',value:Boolean(member)});
      const weight=field(container,'weight-'+i,'Weight',{type:'number',value:member?.weight??1});weight.min='1';weight.max='10000';
      const priority=field(container,'priority-'+i,'Priority',{type:'number',value:member?.priority??0});priority.min='0';priority.max='10000';
      const backup=field(container,'backup-'+i,'Backup',{type:'checkbox',value:member?.backup??false});container.append(node('small',`${a.enabled?'enabled':'disabled'} · ${a.status} (eligibility do server quyết định)`));return {a,include,weight,priority,backup};});
    const singleIndex=editorAccounts.findIndex(a=>a.id===p.single_credential_id);
    field(form,'single','Tài khoản single',{choices:[['',p.single_credential_id&&singleIndex<0?'Giữ tài khoản đã lưu ngoài trang':'Chọn…'],...editorAccounts.map((a,i)=>[String(i),a.email??'Tài khoản đã ẩn'])],value:singleIndex>=0?String(singleIndex):''});
    field(form,'reserve_enabled','Giữ lại quota tối thiểu (không phải GPT Reserve)',{type:'checkbox',value:p.reserve_enabled??false});
    for(const n of ['primary','secondary'])field(form,'min_'+n+'_remaining','Minimum remaining · '+n+' (%)',{value:p['min_'+n+'_remaining']??'0'});
    const age=field(form,'snapshot_max_age_seconds','Freshness max age (giây)',{type:'number',value:p.snapshot_max_age_seconds??120});age.min='30';age.max='3600';
    field(form,'retry_limit','Số lần chuyển tài khoản tối đa / request',{choices:Array.from({length:8},(_,n)=>[String(n),`${n} · tối đa ${n+1} tài khoản`]),value:p.retry_limit??7});field(form,'session_affinity','Session affinity · TTL 24h tối đa, cố định',{type:'checkbox',value:p.session_affinity??true});field(form,'plan_order','Plan order (mỗi dòng)',{type:'textarea',value:(p.plan_order??[]).join('\n')});field(form,'prefer_expiring','Ưu tiên plan sắp hết hạn',{type:'checkbox',value:p.prefer_expiring??false});
    const save=node('button','Lưu pool (CAS)',{type:'submit',id:'codex-pool-save',class:'primary'});form.append(save);const singleWarning=node('p','Chọn một tài khoản cho single mode; tài khoản ngoài trang hiện tại được giữ nguyên nếu chưa chỉnh sửa.',{class:'notice warning'});singleWarning.hidden=true;form.append(singleWarning);const reload=action(poolBox,'Tải lại pool đã lưu',()=>perform(readPool));reload.id='codex-pool-reload';
    let saving=false;const validatePool=()=>{const all=form.elements.namedItem('all_accounts'),single=value(form,'mode')==='single';all.disabled=single;if(single)all.checked=false;const selection=poolSingleSelection(value(form,'mode'),value(form,'single'),editorAccounts,p.single_credential_id);singleWarning.hidden=selection.valid||!single;save.disabled=saving||!selection.valid;return selection;};form.addEventListener('change',validatePool);validatePool();
    form.onsubmit=e=>{e.preventDefault();if(save.disabled)return;const t=epoch,model=poolModel;
      const selection=validatePool();if(!selection.valid)return;const members=mergePoolMembers(p.members??[],memberControls.filter(m=>m.include.checked).map(m=>({credential_id:m.a.id,priority:Number(m.priority.value),weight:Number(m.weight.value),backup:m.backup.checked})),new Set(memberControls.map(m=>m.a.id)));
      const mode=value(form,'mode');const policy={mode,all_accounts:mode==='single'?false:checked(form,'all_accounts'),members,single_credential_id:selection.credential_id,session_affinity:checked(form,'session_affinity'),reserve_enabled:checked(form,'reserve_enabled'),min_primary_remaining:value(form,'min_primary_remaining'),min_secondary_remaining:value(form,'min_secondary_remaining'),snapshot_max_age_seconds:Number(value(form,'snapshot_max_age_seconds')),retry_limit:Number(value(form,'retry_limit')),plan_order:lines(value(form,'plan_order')),prefer_expiring:checked(form,'prefer_expiring')};
      saving=true;save.disabled=true;perform(async()=>{try{const result=await actions.savePool(model,{version:pool.version,policy});if(current(t)&&model===poolModel){pool=result;renderPool();}}finally{if(current(t)){saving=false;validatePool();}}});
    };
  }
  async function reloadKeys(){const t=epoch,updated=await api.request('/api/service/keys');if(current(t)){keys=updated;renderKeys();}}
  function revealKey(secret){
    const {dialog,body}=modal(root,'API key · chỉ hiển thị một lần','codex-key-secret-dialog');body.append(node('p','Lưu key vào nơi an toàn. Đóng cửa sổ sẽ xóa key khỏi giao diện.',{class:'muted'}));
    const input=node('textarea','',{readonly:true,'aria-label':'API key mới',spellcheck:false});input.value=secret;body.append(input);decorate(action(body,'Sao chép',()=>perform(()=>navigator.clipboard.writeText(input.value))),'copy');
    dialog.addEventListener('close',()=>{input.value='';dialog.remove();},{once:true});dialog.showModal();
  }
  async function editKey(row=null){
    const t=epoch,customers=await api.request('/api/service/customers');if(!current(t))return;
    keyDialog?.close();const {dialog,body}=modal(root,row?'Chỉnh sửa khóa máy khách':'Thêm khóa máy khách','codex-key-editor');keyDialog=dialog;
    const form=node('form','',{id:'codex-key-editor-form',autocomplete:'off'});keyForm(form,row,{customers});body.append(form);
    const submit=node('button','Lưu khóa',{type:'submit',class:'primary'});form.append(submit);let pending=false;
    if(!customers.length)body.prepend(node('p','Chưa có khách hàng. Tạo khách hàng trong Quản trị gateway trước khi cấp key.',{class:'notice warning'}));
    form.onsubmit=e=>{e.preventDefault();if(pending)return;perform(async()=>{
      const payload=keyPayload(form,row);pending=true;submit.disabled=true;let conflict=false;
      try{const result=await api.request('/api/service/keys'+(row?'/'+encodeURIComponent(row.key_id):''),{method:row?'PATCH':'POST',body:payload});if(!current(t))return;await reloadKeys();if(!current(t))return;dialog.close();notify('Đã lưu khóa máy khách.');if(result.secret)revealKey(result.secret);}
      catch(error){conflict=error.status===409;if(conflict)await reloadKeys();throw error;}
      finally{if(current(t)){pending=false;submit.disabled=conflict;}}
    });};dialog.addEventListener('close',()=>{dialog.remove();if(keyDialog===dialog)keyDialog=null;},{once:true});dialog.showModal();
  }
  async function mutateKey(row,kind,button){
    if(kind==='rotate'&&!confirm('Đổi key? Key cũ sẽ mất hiệu lực.'))return;
    if(kind==='delete'&&!confirm('Thu hồi và xóa key? Lịch sử usage và audit được giữ lại.'))return;
    const t=epoch;button.disabled=true;
    try{
      const path='/api/service/keys/'+encodeURIComponent(row.key_id)+(kind==='rotate'?'/rotate':'');
      const result=await api.request(path,{method:kind==='rotate'?'POST':kind==='delete'?'DELETE':'PATCH',body:kind==='toggle'?{version:row.version,policy:{...row.policy,enabled:!row.policy.enabled}}:{version:row.version}});
      if(!current(t))return;await reloadKeys();if(!current(t))return;notify('Đã cập nhật khóa máy khách.');if(result?.secret)revealKey(result.secret);
    }catch(error){if(current(t))await reloadKeys();throw error;}finally{if(current(t))button.disabled=false;}
  }
  function renderKeys(){keyBox.replaceChildren();const toolbar=panelHead(keyBox,'Khóa máy khách','codex-keys-heading');decorate(action(toolbar,'Thêm khóa',()=>perform(()=>editKey()),'primary'),'plus');
    const cards=node('div','',{class:'codex-key-cards'});for(const k of keys){const c=node('article','',{class:'codex-key-card'}),head=node('div','',{class:'key-top'});head.append(node('strong',k.name||'API key',{class:'key-name'}),node('code',(k.prefix??'')+' ••••••••',{class:'key-secret'}),node('span',k.revoked_at?'Đã thu hồi':k.policy.enabled?'Đã bật':'Đã tắt',{class:'pill '+(!k.revoked_at&&k.policy.enabled?'ok':'')}));
      const buttons=node('div','',{class:'actions'});decorate(action(buttons,'Chỉnh sửa '+(k.name||'key'),()=>perform(()=>editKey(k))),'edit',true).disabled=Boolean(k.revoked_at);
      for(const [kind,label,glyph] of [['toggle',k.policy.enabled?'Tắt key':'Bật key','power'],['rotate','Đổi key','refresh'],['delete','Xóa key','trash']]){const b=decorate(action(buttons,label,()=>perform(()=>mutateKey(k,kind,b))),glyph,true);b.disabled=Boolean(k.revoked_at);}head.append(buttons);c.append(head);
      const b=k.balance??{},meta=node('div','',{class:'key-meta'}),scope=node('div'),limit=node('div'),usage=node('div','',{class:'key-usage'});
      scope.append(node('small','Phạm vi mô hình'),node('strong',k.policy.all_models?'Tất cả model':`${k.policy.model_ids?.length??0} model được cấp`));
      limit.append(node('small','Giới hạn token quy đổi'),node('strong',b.total_micro==null?'Không giới hạn':amount(b.total_micro)));
      for(const [title,v] of [['Đã dùng',amount(b.spent_micro)],['Đang giữ',amount(b.held_micro)],['Còn lại',b.available_micro==null?'Không giới hạn':amount(b.available_micro)]]){const item=node('div');item.append(node('small',title),node('strong',v));usage.append(item);}meta.append(scope,limit,usage);c.append(meta);
      const policy=node('details','',{class:'key-disclosure'}),summary=node('summary');summary.append(icon('settings'),node('strong','Nhóm tài khoản, mô hình và giới hạn token'),node('span','Mở rộng'),icon('chevron'));policy.append(summary);
      policy.append(node('p',`Giao thức: ${(k.policy.protocols??[]).join(', ')} · Model: ${k.policy.all_models?'Tất cả':(k.policy.model_ids??[]).join(', ')||'Chưa cấp'}`,{class:'muted'}),node('p','Nhóm tài khoản tuân theo pool của model. Hệ số input/cache/output và quyền truy cập được quản lý trong Chỉnh sửa khóa.',{class:'muted'}));
      decorate(action(policy,'Chỉnh sửa chính sách',()=>perform(()=>editKey(k))),'settings').disabled=Boolean(k.revoked_at);decorate(action(policy,'Cấp thêm quota cho key',()=>{selectedKey=k.key_id;renderKeys();root.querySelector('#codex-balance')?.scrollIntoView({block:'nearest'});}),'plus');c.append(policy);cards.append(c);
    }if(!keys.length)cards.append(node('p','Chưa có khóa máy khách. Thêm khóa để cấp quyền truy cập API.',{class:'empty'}));keyBox.append(cards);
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
    capBox.replaceChildren();const layout=node('div','',{class:'codex-model-layout'});capBox.append(layout);
    const catalogPanel=panel(layout,'codex-model-catalog','Mô hình khả dụng'),side=node('div','',{class:'codex-model-side'});layout.append(side);
    const priceActions=catalogPanel.querySelector('.panel-head .actions');decorate(action(priceActions,'Hệ số token',()=>document.querySelector('[data-nav="models"]')?.click()),'settings');
    const list=node('div','',{class:'codex-model-list'}),catalog=matrix.model_catalog??[];
    const entries=[...catalog,...models.filter(m=>!catalog.some(c=>c.id===m.model_id)).map(m=>({id:m.model_id,display_name:m.model_id}))];
    if(!entries.some(m=>m.id===selectedModel))selectedModel=entries[0]?.id??null;
    decorate(action(priceActions,'Sao chép model',()=>perform(()=>navigator.clipboard.writeText(selectedModel))),'copy',true).disabled=!selectedModel;
    for(const m of entries){
      const saved=models.find(s=>s.model_id===m.id),row=node('button','',{type:'button',class:'codex-model-row'+(selectedModel===m.id?' active':''),'aria-pressed':String(selectedModel===m.id)});
      row.append(node('code',m.id),node('span',saved?(saved.enabled?'Đã xuất bản':'Đã tắt'):'Chưa xuất bản',{class:'pill'}));row.onclick=()=>{selectedModel=m.id;renderCapabilities();};
      list.append(row);
    }
    catalogPanel.append(list,node('p','Catalog tham khảo. /v1/models chỉ trả về model đã xuất bản và được cấp cho key.',{class:'muted'}));
    const capability=panel(side,'codex-model-capability','Năng lực dịch vụ');
    const chosen=entries.find(m=>m.id===selectedModel),saved=models.find(m=>m.model_id===selectedModel);
    if(chosen){capability.append(node('strong',chosen.display_name??chosen.id),node('p',`Context: ${chosen.context_window?.toLocaleString()??unknown} · ${(chosen.capabilities??[]).join(' / ')}`,{class:'muted'}));if(chosen.reasoning_efforts?.length)capability.append(node('p','Reasoning: '+chosen.reasoning_efforts.join(' / '),{class:'muted'}));if(saved)capability.append(node('p',`Input ${amount(saved.input_micro)} · Cache đọc ${amount(saved.cache_read_micro??saved.input_micro)} · Cache ghi ${amount(saved.cache_write_micro??saved.input_micro)} · Output ${amount(saved.output_micro)}`,{class:'muted'}));}
    const tags=node('div','',{class:'capability-tags'});for(const[n,v]of Object.entries(matrix.codex_oauth??{}))if(typeof v==='boolean')tags.append(node('span',`${n} · ${v?'hỗ trợ':'chưa hỗ trợ'}`,{class:'pill '+(v?'ok':'')}));capability.append(tags,node('p','WebSocket Responses theo lượt; chưa hỗ trợ warmup generate:false hoặc nhiều stream đồng thời.',{class:'muted'}));
    const rules=panel(side,'codex-model-rules','Quy tắc mô hình');
    for(const [title,description,nav] of [['Bí danh mô hình','Ánh xạ tên client về canonical model; không thay đổi cách quyết toán quota.','aliases'],['Model mapping','Gán model upstream và tài khoản phục vụ.','bindings'],['Hiển thị và hệ số token','Bật/tắt model; cấu hình input, cache đọc, cache ghi và output.','models']]){const rule=node('div','',{class:'model-rule'});rule.append(node('strong',title),node('p',description,{class:'muted'}));decorate(action(rule,'Quản lý '+title.toLocaleLowerCase(),()=>document.querySelector(`[data-nav="${nav}"]`)?.click()),'edit');rules.append(rule);}
    async function publishCatalog(images=false){
      const imageAccounts=images?[...selectedAccounts]:[];
      if(images&&!imageAccounts.length){notify('Chọn các tài khoản có quyền tạo ảnh ở Nhóm tài khoản trước.',true);return;}
      if(!confirm(images?'Xác nhận các tài khoản đã chọn có quyền tạo ảnh và xuất bản model ảnh?':'Xuất bản model Codex cho các tài khoản đã nhập? Giữ nguyên mapping, trạng thái và hệ số hiện có.'))return;
      const t=epoch,button=root.querySelector('#codex-publish-models');if(button)button.disabled=true;
      try{const result=await api.request('/api/service/codex-models/publish',{method:'POST',body:{model_ids:catalog.filter(m=>Boolean(m.capabilities.includes('images'))===images).map(m=>m.id),image_credential_ids:imageAccounts}});
        if(!current(t))return;models=await api.request('/api/service/models');if(!current(t))return;renderCapabilities();renderPool();notify(`Đã tạo ${result.models_created} model và ${result.bindings_created} mapping. Hệ số cũ giữ nguyên.`);
      }finally{if(current(t)&&button)button.disabled=false;}
    }
    const publish=node('div','',{class:'actions model-actions'});catalogPanel.append(publish);decorate(action(publish,'Xuất bản catalog Codex',()=>perform(()=>publishCatalog()),'primary'),'plus').id='codex-publish-models';
    decorate(action(publish,'Xuất bản model ảnh cho tài khoản đã chọn',()=>perform(()=>publishCatalog(true))),'image');
  }
  function setupReports(){reportsBox.replaceChildren();
    const range=node('div','',{class:'report-range'});range.append(node('strong','Thống kê sử dụng'));const presets=node('div','',{class:'range-buttons'});range.append(presets);reportsBox.append(range);
    const summary=node('div','',{class:'codex-overview-stats'}),chart=node('section','',{class:'card codex-chart'});reportsBox.append(summary,chart);
    const log=panel(reportsBox,'codex-report-log','Thống kê và nhật ký'),form=node('form','',{class:'codex-report-filters'});log.append(form);
    field(form,'kind','Báo cáo',{choices:[['requests','Từng attempt'],['accounts','Theo tài khoản']]});reportAccountSelect=field(form,'account','Tài khoản',{choices:[['','Tất cả'],...[...reportAccounts]]});field(form,'model','Canonical model',{choices:[['','Tất cả'],...models.map(m=>[m.model_id,m.model_id])]});field(form,'from','Từ (UTC)',{type:'datetime-local'});field(form,'to','Đến (UTC)',{type:'datetime-local'});const submit=node('button','Tải báo cáo đã lưu',{type:'submit'});form.append(submit);const output=node('div','',{class:'report-output'});log.append(output);let page=null,filters={},kind='requests',generation=0;
    for(const [days,label]of [[1,'24 giờ'],[7,'7 ngày'],[30,'30 ngày']]){const button=action(presets,label,()=>{const end=new Date(),start=new Date(end.getTime()-days*86400000);form.elements.namedItem('from').value=start.toISOString().slice(0,16);form.elements.namedItem('to').value=end.toISOString().slice(0,16);for(const b of presets.children)b.classList.toggle('active',b===button);form.requestSubmit();});if(days===1)button.classList.add('active');}
    function drawSummary(result){const totals=result.summary??{};summary.replaceChildren();for(const [label,key] of [['Requests','requests'],['Hoàn tất','completed'],['Tổng token','total_tokens'],['Input token','input_tokens'],['Cache đọc','cached_read'],['Cache ghi','cached_write'],['Output token','output_tokens'],['Reasoning','reasoning'],['Token quy đổi đã trừ','charged_micro']]){const v=totals[key],card=node('div','',{class:'codex-stat'});card.append(node('small',label),node('strong',v==null?unknown:label.includes('quy đổi')?amount(v):BigInt(v).toLocaleString('vi-VN')),node('span','Toàn bộ khoảng thời gian / bộ lọc',{class:'muted'}));summary.append(card);}
      summary.append(node('p',`${totals.pending??0} request chờ đối soát; ${totals.unknown_usage_requests??0} hoàn tất thiếu usage. Không cộng cache/reasoning lần thứ hai.`,{class:'muted'}));
      chart.replaceChildren();panelHead(chart,'Xu hướng attempts · trang hiện tại');const buckets=kind==='requests'?requestTrend(result.items,result.from,result.to):[];
      if(!buckets.some(Boolean)){chart.append(node('div',kind==='accounts'?'Chọn Từng attempt để xem biểu đồ theo thời gian.':'Chưa có dữ liệu attempts trong khoảng này.',{class:'chart-empty'}));return;}
      const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');svg.setAttribute('viewBox','0 0 960 200');svg.setAttribute('role','img');svg.setAttribute('aria-label','Số attempts trong trang báo cáo theo thời gian');
      const make=(tag,attrs)=>{const e=document.createElementNS(svg.namespaceURI,tag);for(const [k,v]of Object.entries(attrs))e.setAttribute(k,String(v));svg.append(e);return e;};
      const max=Math.max(...buckets,1);for(let i=0;i<5;i++){const y=15+i*40;make('path',{d:`M 42 ${y} H 950`,stroke:'#e3e8f0','stroke-dasharray':'3 4',fill:'none'});const label=make('text',{x:2,y:y+4,fill:'#8493a9','font-size':11});label.textContent=String(Math.round(max*(4-i)/4));}
      const points=buckets.map((n,i)=>`${42+i*908/(buckets.length-1)},${175-n/max*160}`);make('path',{d:'M '+points.join(' L ')+` L 950 175 L 42 175 Z`,fill:'#edf2ff'});make('polyline',{points:points.join(' '),stroke:'#4b7aef','stroke-width':2,fill:'none'});chart.append(svg);const caption=node('div','',{class:'chart-caption'});caption.append(node('span',stamp(result.from)),node('span',stamp(result.to)));chart.append(caption);
    }
    async function read(more=false){const g=++generation,t=epoch;const q=new URLSearchParams({...filters,...(more?{from:page.from,to:page.to,after:page.next_after}:{}),limit:'100'});const result=await api.request('/api/service/usage/'+kind+'?'+q);if(!current(t)||g!==generation)return;page=result;drawSummary(result);output.replaceChildren(node('p',`${stamp(result.from)} — ${stamp(result.to)} · ${result.items.length} dòng trên trang hiện tại${result.next_after?' · Còn trang tiếp theo':''}. Không suy diễn tổng từ dữ liệu phân trang.`,{class:'muted'}));
      const name=id=>reportAccounts.get(id)??'Tài khoản đã ẩn / Không rõ';
      if(kind==='requests')table(output,['Tài khoản / model','Attempt / request','Raw input / cache read / cache write / output','Computed / charged / held (weighted tokens)','Chi phí upstream'],result.items.map(r=>[name(r.credential_id)+' · '+r.model_id,r.status+' / '+r.request_state,[r.usage?.input_tokens,r.usage?.cached_read,r.usage?.cached_write,r.usage?.output_tokens].map(v=>v??unknown).join(' / '),[r.computed_micro,r.charged_micro,r.held_micro].map(amount).join(' / '),r.upstream_cost?`${r.upstream_cost.currency} ${r.upstream_cost.amount}`:unknown]));
      else table(output,['Tài khoản','Attempts / completed / unknown usage','Computed / charged / held','Raw input / read / write / output','Chi phí theo tiền tệ / unknown'],result.items.map(r=>[name(r.credential_id),`${r.attempt_count} / ${r.completed_count} / ${r.unknown_usage_count}`,[r.computed_micro,r.charged_micro,r.held_micro].map(amount).join(' / '),[r.input_tokens,r.cached_read,r.cached_write,r.output_tokens].map(v=>v??unknown).join(' / '),r.upstream_costs.map(c=>`${c.currency} ${c.amount}`).join(' · ')+` · Không rõ: ${r.unknown_cost_count}`]));
      if(result.items.some(r=>BigInt(r.held_micro??'0')>0n))output.append(node('p','Pending usage hold cần đối soát; không silent refund.',{class:'notice warning'}));
      output.append(node('p','Failed attempt không charge khách; chỉ serving attempt được quyết toán một lần. Chi phí upstream không quy đổi thành quota; Không rõ không phải 0.',{class:'muted'}));if(result.next_after)action(output,'Trang báo cáo tiếp theo',()=>perform(()=>read(true)));
    }
    form.onsubmit=e=>{e.preventDefault();kind=value(form,'kind');filters=reportAccountFilters(value(form,'account'));if(value(form,'model'))filters.model_id=value(form,'model');for(const n of ['from','to'])if(value(form,n))filters[n]=value(form,n)+':00Z';perform(()=>read());};perform(()=>read());
  }
  function build(){root.replaceChildren();
    const heading=node('div','',{class:'codex-page-heading'});heading.append(node('h2','Accounts'),node('span','?',{class:'codex-help',title:'Quản lý tài khoản OAuth và dịch vụ API Codex'}));root.append(heading);
    const tabsRow=node('div','',{class:'codex-tabs-row'}),context=node('details','',{class:'codex-context'}),summary=node('summary');summary.append(icon('codex'),node('span','Dịch vụ API Codex'),icon('chevron'));context.append(summary);
    const menu=node('div','',{class:'codex-context-menu'});for(const [id,label] of [['accounts','Tài khoản Codex'],['overview','Dịch vụ API Codex']])decorate(action(menu,label,()=>{context.open=false;switchTab(id);}),id==='accounts'?'users':'codex');context.append(menu);
    tabs=node('nav','',{class:'codex-tabs','aria-label':'Codex sections'});
    for(const [id,label,glyph] of [['overview','Tổng quan dịch vụ','codex'],['keys','Khóa máy khách','key'],['accounts','Nhóm tài khoản','users'],['models','Mô hình và năng lực','image'],['usage','Thống kê và nhật ký','activity']]){
      const tab=node('button',label,{type:'button',class:'codex-tab','data-codex-tab':id,'aria-controls':tabPanels[id]});decorate(tab,glyph);tab.onclick=()=>switchTab(id);tabs.append(tab);
    }
    tabsRow.append(context,tabs);root.append(tabsRow);
    const serviceBar=node('section','',{class:'codex-service-bar'}),serviceName=node('div','',{class:'codex-service-name'}),serviceIcon=node('span','',{class:'codex-service-icon'});serviceIcon.append(icon('codex'));
    serviceName.append(serviceIcon,node('strong','Dịch vụ API Codex'),node('span','Hiện tại',{class:'pill blue'}),node('span','Đang đọc trạng thái',{class:'pill service-status'}),node('span','Dịch vụ API',{class:'pill blue'}));serviceBar.append(serviceName);
    const toolbar=node('div','',{class:'actions codex-toolbar'});
    decorate(action(toolbar,'Refresh quota tất cả tài khoản',()=>perform(startQuotaRefresh),'primary'),'refresh').id='codex-refresh-all-toolbar';
    const reload=decorate(action(toolbar,'Làm mới thống kê',()=>perform(async()=>{await readList();if(state.accountId)await actions.read();const t=epoch,updated=await api.request('/api/service/keys');if(current(t)){keys=updated;renderKeys();}await readStatus();})),'refresh');reload.id='codex-reload-saved';reload.setAttribute('aria-label','Tải lại dữ liệu đã lưu');reload.title='Chỉ đọc dữ liệu đã lưu; không gọi provider';
    decorate(action(toolbar,'Chạy thử API',()=>document.querySelector('[data-nav="playground"]')?.click()),'play');
    decorate(action(toolbar,'Proxy đầu ra',()=>{switchTab('overview');root.querySelector('#codex-service-proxy')?.scrollIntoView({block:'nearest'});}),'route');
    serviceBar.append(toolbar);message=node('p','',{id:'codex-message',class:'notice',role:'status','aria-live':'polite'});root.append(serviceBar,message);
    overviewBox=panel(root,'codex-overview','Tổng quan dịch vụ');
    const grid=node('div','',{class:'codex-layout',id:'codex-accounts-panel'});root.append(grid);listBox=panel(grid,'codex-list','Theo tài khoản');poolBox=panel(grid,'codex-pool','Tùy chọn định tuyến');details=node('div','',{class:'codex-details'});grid.append(details);
    keyBox=panel(root,'codex-keys','Khóa máy khách');reportsBox=panel(root,'codex-reports','Thống kê và nhật ký');capBox=panel(root,'codex-capabilities','Mô hình và năng lực');
    const importer=modal(root,'Thêm tài khoản OAuth','codex-import-dialog');importDialog=importer.dialog;importBox=importer.body;
    renderList();renderDetail();renderPool();renderKeys();renderCapabilities();setupReports();switchTab('accounts');perform(readStatus);
  }
  return {
    async open(){active=true;const t=++epoch,g=++listGeneration;clearTimeout(searchTimer);searchTimer=null;listBusy=false;proxyBusy=false;proxyNeedsReload=false;proxyState='loading';serviceStatus=null;serviceProxy=null;root.hidden=false;root.replaceChildren(node('p','Đang đọc danh mục tài khoản…'));await perform(async()=>{const values=[];for(const path of [accountPagePath(pageIndex+1,pageSize,searchText,healthFilter),...['proxies','models','keys','capabilities'].map(p=>'/api/service/'+p)]){if(!current(t))return;values.push(await api.request(path));}if(!current(t))return;acceptAccountPage(values[0],searchText.trim(),healthFilter);[profiles,models,keys,matrix]=values.slice(1);
      // Render the Cockpit-style shell as soon as the catalog is available.
      // Quota snapshots are a storage read for every account and must not
      // block the first paint (226 accounts otherwise looked like a frozen UI).
      if(current(t))build();
      readSavedQuota(accounts,t,g).then(()=>{if(current(t)&&g===listGeneration){renderList();renderDetail();}}).catch(error=>{if(current(t)&&g===listGeneration){notify('Không đọc được một số snapshot quota đã lưu: '+error.message,true);renderList();}});
      perform(readQuotaRefresh);
    });},
    showImport(){if(importDialog?.isConnected)importDialog.showModal();},
    leave(){active=false;epoch++;listGeneration++;statusGeneration++;quotaRefreshGeneration++;clearTimeout(searchTimer);clearTimeout(quotaRefreshTimer);clearTimeout(statsTimer);statsTimer=null;statsBusy=false;usageNote=null;reportAccountSelect=null;searchTimer=null;quotaRefreshTimer=null;listBusy=false;state.select(null);importBusy=false;refreshBusy=false;quotaRefreshBusy=false;proxyBusy=false;for(const dialog of root.querySelectorAll('dialog[open]'))dialog.close();root.hidden=true;root.replaceChildren();details=null;message=null;importBox=null;importDialog=null;keyDialog=null;},
    clear(){this.leave();state.clear();accounts=[];pageSize=48;pageIndex=0;pageCount=1;pageTotal=0;loadedSearch='';loadedHealth='Tất cả';profiles=[];proxyProfiles=[];models=[];keys=[];matrix={};selectedKey=null;pool=null;poolModel='';grantPending=new Set();savedQuota=new Map();savedCredits=new Map();selectedAccounts=new Set();refreshErrors=new Map();reportAccounts=new Map();importResult=null;serviceStatus=null;serviceProxy=null;searchText='';healthFilter='Tất cả';quotaRefresh={settings:null,job:null,concurrency:2};quotaRefreshControls=null;quotaRefreshDraft=false;quotaRefreshPending=null;quotaSnapshotAt=0;}
  };
}
