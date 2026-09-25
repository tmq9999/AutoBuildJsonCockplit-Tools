import {canConsumeReset,resetLocked} from './codex-state.js';
import {decimalToMicro} from './api.js';

export function createCodexActions(api,state,changed,uuid=()=>crypto.randomUUID()){
  const path=id=>`/api/service/oauth-accounts/${encodeURIComponent(id)}`;
  const poolPath=model=>`/api/service/account-pools/${model.split('/').map(encodeURIComponent).join('/')}`;
  async function run(fn){const ticket=state.ticket();if(!ticket.id||state.busy)throw new Error('Thao tác đang chạy');state.busy=true;changed();
    try{return await fn(ticket);}finally{if(state.release(ticket))changed();}
  }
  return {
    async read(){return run(async t=>{const [usage,credits]=await Promise.all([api.request(path(t.id)+'/quota'),api.request(path(t.id)+'/reset-credits')]);state.accept(t,{usage,credits});});},
    async refresh(){return run(async t=>{const result=await api.request(path(t.id)+'/quota/refresh',{method:'POST',body:{}});state.accept(t,result);});},
    confirm(){if(state.busy||!canConsumeReset({available_count:state.credits?.snapshot?.available_count,fresh:state.credits?.stale===false,operation_state:state.operation?.state}))throw new Error('Reset bị khóa hoặc credit chưa mới');
      // Allocate a new confirmation only after prior operation reached a known terminal state.
      state.confirmed={request_id:uuid(),credits_version:state.credits.snapshot.version,acknowledge:true};return state.confirmed;
    },
    async consume(){if(!state.confirmed||resetLocked(state.operation))throw new Error('Không gửi lại reset chưa rõ kết quả');
      return run(async t=>{const body={...state.confirmed};state.operation={operation_id:body.request_id,state:'unknown',version:null};changed();
        try{const operation=await api.request(path(t.id)+'/reset-credits/consume',{method:'POST',body});state.accept(t,{operation});}
        catch(error){
          // A timeout/cancellation may have dispatched; only a definite HTTP
          // rejection plus an authoritative reload can release the local lock.
          if(error.name!=='AbortError'&&error.status>=400&&error.status<500&&error.status!==408&&state.current(t)){
            const credits=await api.request(path(t.id)+'/reset-credits');
            if(state.accept(t,{credits})&&credits.active_reset===null){state.operation=null;state.confirmed=null;}
          }
          throw error;
        }
        // No automatic retry, nor clearing uncertainty using an optimistic toast.
      });
    },
    async resolve(outcome,reason){if(!['unknown','succeeded_refresh_failed'].includes(state.operation?.state)||state.operation.version==null)throw new Error('Cần tải trạng thái server trước khi đối soát');
      return run(async t=>{const op=state.operation;const result=await api.request(`/api/service/reset-requests/${op.operation_id}/resolve`,{method:'POST',body:{version:op.version,outcome,reason}});
        if(state.accept(t,{operation:result})){state.credits={...state.credits,stale:true};const credits=await api.request(path(t.id)+'/reset-credits');state.accept(t,{credits});}
      });
    },
    async saveAccount(row,enabled,profile){return run(async t=>{if(resetLocked(state.operation))throw new Error('Reset đang khóa tài khoản');return api.request(path(t.id),{method:'PATCH',body:{version:row.version,enabled,proxy_profile_id:profile||null}});});},
    async refreshToken(){if(resetLocked(state.operation))throw new Error('Reset đang khóa tài khoản');return run(t=>api.request(`/api/service/oauth/${t.id}/refresh`,{method:'POST',body:{}}));},
    async savePool(model,body){return api.request(poolPath(model),{method:'PUT',body});},
    async grant(keyId,{version,amount,reason}){return api.request(`/api/service/keys/${encodeURIComponent(keyId)}/quota-adjust`,{method:'POST',body:{request_id:uuid(),version,amount_micro:decimalToMicro(amount),reason}});}
  };
}
