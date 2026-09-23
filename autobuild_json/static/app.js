const STATUS = {queued:'Chờ',running:'Đang chạy',success:'Thành công',error:'Lỗi',phone_verify:'Phone number verify',cancelled:'Đã hủy'};
const BATCH = {queued:'Đang chờ',running:'Đang chạy',stopping:'Đang dừng…',completed:'Đã xử lý xong',cancelled:'Đã dừng',error:'Batch gặp lỗi'};
const STAGES = {queued:'Chờ',initialize:'Khởi tạo phiên HTTP',email:'Email',password:'Mật khẩu',totp:'TOTP 2FA',codex_oauth:'OAuth Codex',token_exchange:'Đổi token',complete:'Hoàn tất',authentication:'Xác thực',batch:'Batch',processing:'Xử lý'};
const REASONS = {
  INVALID_CREDENTIALS:'Email hoặc mật khẩu không hợp lệ.', ACCOUNT_DEACTIVATED:'Tài khoản đã bị vô hiệu hóa.',
  MFA_ERROR:'Xác minh TOTP không thành công.', EMAIL_OTP_REQUIRED:'Cần xác minh mã qua email.', PHONE_VERIFY:'Phone number verify',
  PROXY_ERROR:'Không kết nối được proxy.', NETWORK_ERROR:'Lỗi kết nối mạng.', TIMEOUT:'Quá thời gian xử lý tài khoản.',
  RATE_LIMITED:'Nhà cung cấp giới hạn tần suất. Hãy thử lại sau.', AUTH_BLOCKED:'Nhà cung cấp chặn luồng đăng nhập.',
  ACTION_REQUIRED:'Cần thao tác xác minh bổ sung.', INVALID_STATE:'Phiên OAuth không hợp lệ, hết hạn hoặc đã dùng.',
  OAUTH_DENIED:'OAuth bị từ chối.', WORKSPACE_SELECTION_REQUIRED:'Cần chọn workspace thủ công.',
  TOKEN_EXCHANGE_ERROR:'Không đổi được mã OAuth.', TOKEN_EXCHANGE_UNCERTAIN:'Chưa rõ kết quả đổi token. Hãy tạo phiên OAuth mới.',
  INVALID_TOKEN_RESPONSE:'Token hoặc danh tính trả về không hợp lệ.', ACCOUNT_MISMATCH:'Email đăng nhập không khớp email đã yêu cầu.',
  CONFIGURATION_ERROR:'Cấu hình hoặc dependency CheckLive chưa sẵn sàng.', INVALID_INPUT:'Dữ liệu không hợp lệ hoặc vượt giới hạn.',
  INVALID_REQUEST:'Các trường nhập không hợp lệ.', CANCELLED:'Đã hủy theo yêu cầu.', INTERRUPTED:'Gián đoạn do ứng dụng khởi động lại.',
  STORAGE_ERROR:'Không ghi được kết quả. Hãy kiểm tra dung lượng và quyền thư mục data.', NOT_FOUND:'Không tìm thấy kết quả.',
  BATCH_CONFLICT:'Đã có một batch đang chạy. Bấm Làm mới để xem.', UNAUTHORIZED:'Phiên chưa đăng nhập hoặc đã hết hạn.',
  CSRF_REJECTED:'Phiên bảo mật đã thay đổi. Hãy đăng nhập lại.', LOGIN_RATE_LIMITED:'Thử đăng nhập quá nhiều lần. Chờ một phút.',
  INPUT_TOO_LARGE:'Dữ liệu vượt giới hạn cho phép.', UNEXPECTED_ERROR:'Có lỗi xử lý. Không có bí mật nào được ghi vào thông báo.',
};

export function summarizeRows(rows) {
  const counts = {queued:0,running:0,success:0,error:0,phone_verify:0,cancelled:0};
  for (const row of rows) if (Object.hasOwn(counts,row.status)) counts[row.status]++;
  return counts;
}
export function shouldPoll(job, hidden) { return Boolean(job && !hidden && ['queued','running','stopping'].includes(job.status)); }
export function canStart(state) { return state.authenticated && state.validCount>0 && state.authReady && state.storageReady && !state.busy && !state.activeJob; }
export function displayReason(row) {
  if (row.status==='phone_verify') return 'Phone number verify';
  if (!row.code && !['error','cancelled'].includes(row.status)) return '—';
  return REASONS[row.code] || REASONS.UNEXPECTED_ERROR;
}
export async function readInputFile(file) {
  if (file.size>5*1024*1024) throw new Error('File vượt quá 5 MiB.');
  try { return new TextDecoder('utf-8',{fatal:true}).decode(await file.arrayBuffer()); }
  catch { throw new Error('File phải là UTF-8 hợp lệ.'); }
}

export function mountDashboard(doc) {
  const win = doc.defaultView;
  const $ = id => doc.getElementById(id);
  const state = {authenticated:false,validCount:0,authReady:false,storageReady:false,busy:false,activeJob:null,
    job:null,csrf:'',generation:0,inputRevision:0,page:0,manual:null,manualBusy:false};
  const controllers = new Set();
  let pollTimer, pollController, refreshFlight;

  function notice(text, kind='error') { $('message').textContent=text; $('message').className='notice '+kind; $('message').hidden=!text; }
  function controls() {
    $('start-btn').disabled=!canStart(state);
    $('validate-btn').disabled=state.busy || !state.authenticated || !($('accounts-text').value.trim());
    $('stop-btn').disabled=!state.activeJob || state.busy || state.job?.status==='stopping';
    for (const id of ['accounts-text','accounts-file','proxies-text','mode','timeout']) $(id).disabled=state.busy || Boolean(state.activeJob);
    $('workers').disabled=state.busy || Boolean(state.activeJob) || $('mode').value==='sequential';
    for (const button of doc.querySelectorAll('[data-export]')) button.disabled=!state.job || !state.authenticated;
    $('generate-link-btn').disabled=state.manualBusy || !state.authenticated;
    $('complete-btn').disabled=state.manualBusy || !state.manual || !($('callback-url').value.trim());
    $('copy-link-btn').disabled=!state.manual || state.manualBusy;
    $('job-history').disabled=state.busy;
  }
  function clearManual() {
    state.manual=null;
    for (const id of ['oauth-link','callback-url']) $(id).value='';
    $('open-link').hidden=true; $('open-link').removeAttribute('href');
  }
  function resetSession() {
    state.generation++; state.authenticated=false; state.csrf=''; state.validCount=0; state.job=null; state.activeJob=null;
    state.busy=false; state.manualBusy=false; clearTimeout(pollTimer);
    for (const c of controllers) c.abort(); controllers.clear();
    for (const id of ['accounts-text','proxies-text','admin-token','manual-proxy','manual-email','accounts-file']) $(id).value='';
    clearManual(); $('dashboard').hidden=true; $('login-panel').hidden=false; $('logout-btn').hidden=true;
    $('connection-status').textContent='Chưa đăng nhập'; $('validation-summary').textContent='Chưa kiểm tra dữ liệu.';
    $('validation-details').hidden=true; $('filtered-list').replaceChildren(); $('duplicate-warning').textContent='';
    $('job-history').replaceChildren(new Option('Chưa có batch',''));
    renderJob(); controls();
  }
  async function request(path, {method='GET',body,blob=false,controller=new AbortController(),login=false}={}) {
    const generation=state.generation;
    controllers.add(controller);
    try {
      const response=await win.fetch(path, {method,credentials:'same-origin',cache:'no-store',signal:controller.signal,
        headers:{...(body!==undefined?{'Content-Type':'application/json'}:{}),...(method!=='GET' && state.csrf?{'X-CSRF-Token':state.csrf}:{})},
        ...(body!==undefined?{body:JSON.stringify(body)}:{})});
      if (generation!==state.generation) throw new DOMException('Stale session','AbortError');
      if (!response.ok) {
        const error=await response.json().catch(()=>({}));
        if (response.status===401 && !login) resetSession();
        const failure=new Error(REASONS[error.error] || REASONS.UNEXPECTED_ERROR);
        failure.code=error.error; failure.status=response.status; throw failure;
      }
      const value=response.status===204?null:blob?await response.blob():await response.json();
      if (generation!==state.generation) throw new DOMException('Stale session','AbortError');
      return value;
    } finally { controllers.delete(controller); }
  }
  function report(error) { if (error.name!=='AbortError') notice(error.message || REASONS.UNEXPECTED_ERROR); }
  function renderJob() {
    const job=state.job, rows=job?.rows || [], counts=summarizeRows(rows);
    for (const el of doc.querySelectorAll('[data-count]')) el.textContent=String(counts[el.dataset.count]);
    $('job-status').textContent=job?(BATCH[job.status] || 'Không xác định'):'Chưa có batch';
    $('job-status').dataset.status=job?.status || 'idle';
    const done=counts.success+counts.error+counts.phone_verify+counts.cancelled;
    $('progress').max=Math.max(1,rows.length); $('progress').value=done; $('progress-text').textContent=`${done} / ${rows.length}`;
    const filter=$('status-filter').value;
    const selected=filter==='all'?rows:rows.filter(row=>row.status===filter);
    state.page=Math.max(0,Math.min(state.page,Math.ceil(selected.length/100)-1));
    const visible=selected.slice(state.page*100,state.page*100+100), fragment=doc.createDocumentFragment();
    for (const row of visible) {
      const tr=doc.createElement('tr'); tr.dataset.row=row.account_job_id;
      for (const [index,text] of [row.email,STAGES[row.stage] || 'Xử lý',STATUS[row.status] || 'Không xác định',displayReason(row)].entries()) {
        const td=doc.createElement('td');
        if (index===2) { const pill=doc.createElement('span'); pill.className='status-pill '+(Object.hasOwn(STATUS,row.status)?row.status:''); pill.textContent=text; td.append(pill); }
        else td.textContent=text;
        tr.append(td);
      }
      fragment.append(tr);
    }
    if (!visible.length) { const tr=doc.createElement('tr'), td=doc.createElement('td'); td.colSpan=4; td.className='empty'; td.textContent=job?'Không có tài khoản trong bộ lọc này.':'Chưa có dữ liệu thực thi. Nhập tài khoản để bắt đầu.'; tr.append(td); fragment.append(tr); }
    $('results-body').replaceChildren(fragment);
    $('row-summary').textContent=selected.length?`${state.page*100+1}–${state.page*100+visible.length} / ${selected.length} tài khoản`:'0 tài khoản';
    $('prev-page').disabled=state.page===0; $('next-page').disabled=(state.page+1)*100>=selected.length;
    if (job?.batch_error) notice(displayReason(job.batch_error));
    controls();
  }
  function schedulePoll() {
    clearTimeout(pollTimer);
    if (state.authenticated && state.activeJob && !doc.hidden) pollTimer=win.setTimeout(poll,1000);
  }
  async function poll() {
    if (!state.authenticated || !state.activeJob || doc.hidden || pollController) return;
    const id=state.activeJob, selected=state.job?.id;
    pollController=new AbortController();
    try {
      const job=await request(`/api/jobs/${id}`,{controller:pollController});
      if (state.job?.id===selected && selected===id) { state.job=job; renderJob(); }
      if (!shouldPoll(job,false)) { state.activeJob=null; await refresh(false); }
    } catch(error) { report(error); }
    finally { pollController=null; controls(); schedulePoll(); }
  }
  async function refresh(selectLatest=false) {
    if (refreshFlight) return refreshFlight;
    refreshFlight=(async()=>{
      const [health,jobs]=await Promise.all([request('/api/health'),request('/api/jobs')]);
      state.authReady=health.auth_ready; state.storageReady=health.storage_ready;
      $('connection-status').textContent=health.storage_ready?(health.auth_ready?'Sẵn sàng · '+win.location.host:'Auth chưa sẵn sàng'):'Chỉ đọc · lỗi lưu trữ';
      $('readiness').hidden=health.auth_ready && health.storage_ready;
      $('readiness').textContent=!health.storage_ready?REASONS.STORAGE_ERROR+' Chỉ tải được kết quả đã lưu; khởi động lại sau khi sửa lỗi.':!health.auth_ready?'CheckLive chưa sẵn sàng. Chạy scripts/setup_checklive.py, kiểm tra CHECKLIVE_PATH rồi khởi động lại. OAuth thủ công vẫn dùng được.':'';
      state.activeJob=jobs.find(job=>shouldPoll(job,false))?.id || null;
      const id=selectLatest?(state.activeJob || jobs[0]?.id):(state.job?.id || state.activeJob || jobs[0]?.id);
      $('job-history').replaceChildren(new Option('Chọn batch đã lưu',''));
      for (const job of jobs) $('job-history').append(new Option(`${new Date(job.created_at).toLocaleString('vi-VN')} · ${BATCH[job.status] || job.status}`,job.id));
      if (id) { const job=await request(`/api/jobs/${id}`); state.job=job; $('job-history').value=id; }
      renderJob(); schedulePoll();
    })();
    try { await refreshFlight; } finally { refreshFlight=null; }
  }
  async function signedIn(csrf) {
    state.generation++;
    for (const controller of controllers) controller.abort();
    controllers.clear();
    state.csrf=csrf; state.authenticated=true;
    $('login-panel').hidden=true; $('dashboard').hidden=false; $('logout-btn').hidden=false; $('admin-token').value='';
    await refresh(true); controls();
  }
  function download(blob,name) {
    const url=win.URL.createObjectURL(blob), link=doc.createElement('a');
    link.href=url; link.download=name; link.hidden=true; doc.body.append(link); link.click(); link.remove();
    win.setTimeout(()=>win.URL.revokeObjectURL(url),1000);
  }
  function invalidateInput() { state.inputRevision++; state.validCount=0; $('validation-summary').textContent='Dữ liệu đã thay đổi. Hãy kiểm tra trước khi chạy.'; $('validation-details').hidden=true; controls(); }
  $('login-form').addEventListener('submit',async event=>{
    event.preventDefault(); $('login-btn').disabled=true; notice('');
    try { const result=await request('/api/session',{method:'POST',body:{token:$('admin-token').value},login:true}); await signedIn(result.csrf_token); }
    catch(error) { report(error); } finally { $('admin-token').value=''; $('login-btn').disabled=false; }
  });
  $('logout-btn').addEventListener('click',async()=>{
    try { await request('/api/session',{method:'DELETE'}); resetSession(); notice('Đã đăng xuất. Batch đang chạy vẫn tiếp tục ở backend.','success'); }
    catch(error) { report(error); }
  });
  $('accounts-text').addEventListener('input',invalidateInput);
  $('accounts-file').addEventListener('change',async()=>{
    const file=$('accounts-file').files[0], generation=state.generation;
    if (!file) return;
    try { const text=await readInputFile(file); if (generation===state.generation && state.authenticated) { $('accounts-text').value=text; invalidateInput(); } }
    catch(error) { report(error); } finally { $('accounts-file').value=''; }
  });
  $('validate-btn').addEventListener('click',async()=>{
    const revision=state.inputRevision; state.busy=true; controls(); notice('');
    try {
      const result=await request('/api/accounts/validate',{method:'POST',body:{accounts_text:$('accounts-text').value}});
      if (revision!==state.inputRevision) return;
      state.validCount=result.valid_count;
      $('validation-summary').textContent=`${result.valid_count} hợp lệ · ${result.filtered_count} bị lọc`;
      $('filtered-list').replaceChildren();
      for (const item of result.filtered) { const li=doc.createElement('li'); li.textContent=`Dòng ${item.line_number}: ${item.reason}`; $('filtered-list').append(li); }
      $('duplicate-warning').textContent=result.duplicate_lines.length?`Email trùng ở dòng ${result.duplicate_lines.join(', ')}. Không chạy cùng email đồng thời.`:'';
      $('validation-details').hidden=!result.filtered_count && !result.duplicate_lines.length;
    } catch(error) { report(error); } finally { state.busy=false; controls(); }
  });
  $('mode').addEventListener('change',controls);
  $('batch-form').addEventListener('submit',async event=>{
    event.preventDefault(); if (!canStart(state) || !$('batch-form').reportValidity()) return;
    state.busy=true; controls(); notice('');
    try {
      const result=await request('/api/jobs',{method:'POST',body:{accounts_text:$('accounts-text').value,proxies_text:$('proxies-text').value,
        mode:$('mode').value,workers:Number($('workers').value),timeout:Number($('timeout').value)}});
      $('accounts-text').value=''; $('proxies-text').value=''; state.validCount=0; state.activeJob=result.id; state.page=0;
      $('validation-summary').textContent='Đã nhận batch; dữ liệu bí mật đã được xóa khỏi ô nhập.';
      state.job=await request(`/api/jobs/${result.id}`); await refresh(false);
    } catch(error) { report(error); if (error.code==='BATCH_CONFLICT') await refresh(true).catch(report); }
    finally { state.busy=false; controls(); schedulePoll(); }
  });
  $('stop-btn').addEventListener('click',async()=>{
    if (!state.activeJob) return;
    state.busy=true; controls();
    try { await request(`/api/jobs/${state.activeJob}/stop`,{method:'POST',body:{}}); await refresh(false); }
    catch(error) { report(error); } finally { state.busy=false; controls(); }
  });
  $('refresh-btn').addEventListener('click',()=>refresh(false).catch(report));
  $('job-history').addEventListener('change',async()=>{
    const id=$('job-history').value; if (!id) return;
    try { const job=await request(`/api/jobs/${id}`); if ($('job-history').value===id) { state.job=job; state.page=0; renderJob(); } }
    catch(error) { report(error); }
  });
  $('status-filter').addEventListener('change',()=>{state.page=0;renderJob();});
  $('prev-page').addEventListener('click',()=>{state.page--;renderJob();});
  $('next-page').addEventListener('click',()=>{state.page++;renderJob();});
  for (const button of doc.querySelectorAll('[data-export]')) button.addEventListener('click',async()=>{
    if (!state.job) return;
    button.disabled=true;
    try { download(await request(`/api/jobs/${state.job.id}/exports/${button.dataset.export}`,{blob:true}),button.dataset.export+'.json'); }
    catch(error) { report(error); } finally { controls(); }
  });
  $('callback-url').addEventListener('input',controls);
  $('link-form').addEventListener('submit',async event=>{
    event.preventDefault(); state.manualBusy=true; clearManual(); controls(); notice('');
    try {
      const item=await request('/api/oauth/links',{method:'POST',body:{email:$('manual-email').value,proxy:$('manual-proxy').value}});
      state.manual=item; $('oauth-link').value=item.desktop_url;
      $('open-link').href=item.desktop_url; $('open-link').hidden=false;
      $('manual-status').textContent='Link có thời hạn 10 phút, chỉ dùng một lần. Mở link hoặc sao chép vào trình duyệt.';
    } catch(error) { report(error); } finally { $('manual-proxy').value=''; state.manualBusy=false; controls(); }
  });
  $('copy-link-btn').addEventListener('click',async()=>{
    try { await win.navigator.clipboard.writeText($('oauth-link').value); notice('Đã sao chép link.','success'); }
    catch { $('oauth-link').select(); notice('Không truy cập được clipboard. Hãy sao chép link đã chọn.','warning'); }
  });
  $('callback-form').addEventListener('submit',async event=>{
    event.preventDefault(); if (!state.manual || state.manualBusy) return;
    state.manualBusy=true; controls(); notice('');
    try {
      const blob=await request('/api/oauth/complete',{method:'POST',body:{session_id:state.manual.session_id,callback_url:$('callback-url').value},blob:true});
      download(blob,'success.json'); notice('OAuth thủ công thành công. File JSON đã được tải xuống.','success');
    } catch(error) { report(error); }
    finally { clearManual(); state.manualBusy=false; controls(); $('manual-status').textContent='Phiên đã kết thúc. Tạo link mới nếu cần thử lại.'; }
  });
  doc.addEventListener('visibilitychange',()=>{
    clearTimeout(pollTimer);
    if (doc.hidden) pollController?.abort();
    else if (state.authenticated) refresh(false).catch(report);
  });
  win.addEventListener('pagehide',()=>{ clearTimeout(pollTimer); for (const c of controllers) c.abort(); });
  controls();
  request('/api/session').then(result=>signedIn(result.csrf_token)).catch(error=>{ if (error.status!==401) report(error); });
}
