import test from 'node:test';
import assert from 'node:assert/strict';
import { summarizeRows, shouldPoll, displayReason, canStart, readInputFile } from '../../autobuild_json/static/app.js';

test('counts statuses separately and ignores unknown fields', () => {
  assert.deepEqual(summarizeRows([{status:'success'}, {status:'phone_verify'}, {status:'error'}, {status:'running'}, {status:'__proto__'}]),
    {queued:0,running:1,success:1,error:1,phone_verify:1,cancelled:0});
});

test('poll only while active and visible', () => {
  for (const status of ['queued','running','stopping']) {
    assert.equal(shouldPoll({status}, false), true);
    assert.equal(shouldPoll({status}, true), false);
  }
  for (const status of ['completed','error','cancelled']) assert.equal(shouldPoll({status}, false), false);
  assert.equal(shouldPoll(null, false), false);
});

test('Start requires validation, readiness and no active batch', () => {
  const ready = {authenticated:true, validCount:1, authReady:true, storageReady:true, busy:false, activeJob:null};
  assert.equal(canStart(ready), true);
  for (const field of ['authenticated','authReady','storageReady']) assert.equal(canStart({...ready,[field]:false}), false);
  assert.equal(canStart({...ready,validCount:0}), false);
  assert.equal(canStart({...ready,busy:true}), false);
  assert.equal(canStart({...ready,activeJob:'job'}), false);
});

test('phone label remains exact; unknown provider error is not echoed', () => {
  assert.equal(displayReason({status:'phone_verify'}), 'Phone number verify');
  assert.match(displayReason({code:'INVALID_CREDENTIALS'}), /mật khẩu/);
  assert.equal(displayReason({code:'UNKNOWN',reason:'secret-value'}).includes('secret-value'), false);
  assert.equal(displayReason({status:'success'}), '—');
});

test('diagnostic error details explain network and HTTP failures without secrets', () => {
  assert.equal(displayReason({code:'REQUEST_TIMEOUT', details:{method:'GET',host:'chatgpt.com',endpoint:'/',curl_code:28}}),
    'Timeout · GET chatgpt.com/ · curl 28');
  assert.equal(displayReason({code:'HTTP_ERROR', details:{method:'POST',host:'auth.openai.com',endpoint:'/api/accounts/password/verify',http_status:500}}),
    'HTTP 500 · POST auth.openai.com/api/accounts/password/verify');
  assert.equal(displayReason({code:'AUTH_BLOCKED', details:{method:'GET',host:'auth.openai.com',endpoint:'/log-in',http_status:403}}),
    'HTTP 403 · GET auth.openai.com/log-in');
  assert.equal(displayReason({code:'OAUTH_PARSE_ERROR', details:{selection_kind:'workspace',candidate_count:0}}),
    'Không đọc được lựa chọn workspace (0 lựa chọn)');
  assert.equal(displayReason({code:'HTTP_ERROR', details:{endpoint:'/x',message:'password-secret'}}).includes('password-secret'), false);
});

test('email route reports observed redirect rather than blaming the TOTP secret', () => {
  const reason=displayReason({code:'EMAIL_OTP_REQUIRED',details:{http_status:302,
    method:'GET',host:'auth.openai.com',endpoint:'/api/accounts/authorize',
    evidence:'redirect',redirect_path:'/email-verification'}});
  assert.match(reason,/HTTP 302/);
  assert.match(reason,/redirect → \/email-verification/);
  assert.match(reason,/TOTP chưa được kiểm tra/);
  assert.equal(reason.includes('Sai'),false);
});

test('real multiple workspace error includes candidate count', () => {
  assert.match(displayReason({code:'WORKSPACE_SELECTION_REQUIRED',details:{selection_kind:'workspace',candidate_count:2}}),/2 lựa chọn/);
});

test('UTF-8 file read preserves passwords, rejects invalid bytes and oversized files', async () => {
  const content = '\ufeffu@example.com| p:word |JBSWY3DPEHPK3PXP\r\n';
  assert.equal(await readInputFile(new Blob([content])), content.replace('\ufeff',''));
  await assert.rejects(readInputFile(new Blob([new Uint8Array([0xff, 0xfe])])), /UTF-8/);
  await assert.rejects(readInputFile({size:5*1024*1024+1, arrayBuffer(){throw new Error('must not read');}}), /5 MiB/);
});
