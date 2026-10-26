const $=s=>document.querySelector(s);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const money=v=>(v<0?'-':'')+'₹'+Math.abs(v).toLocaleString('en-IN',{minimumFractionDigits:2,maximumFractionDigits:2});
const S={user:null,csrf:'',perms:new Set(),sys:{},tab:'overview',kind:'ledger_without_payment',es:'pending',fs:'pending',ep:0,fp:0,uploaded:{},maps:{},boot:false};
const can=p=>S.perms.has(p);
async function api(p,o={}){const m=o.method||'GET',h={...(o.headers||{})};if(m!=='GET')h['X-CSRF-Token']=S.csrf||'';
 const r=await fetch('/api'+p,{...o,headers:h}),j=await r.json().catch(()=>({}));
 if(r.status===401&&p!=='/auth/login'&&S.user){S.user=null;showLogin('Your session ended. Please sign in again.');throw new Error('signed out')}
 if(!r.ok)throw Object.assign(new Error(typeof j.detail==='string'?j.detail:JSON.stringify(j.detail||j)),{detail:j.detail});return j}
const post=(p,b)=>api(p,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(b||{})});
function toast(msg,bad){let t=$('#toast');if(!t){t=document.createElement('div');t.id='toast';t.style.cssText='position:fixed;bottom:18px;left:50%;transform:translateX(-50%);padding:10px 16px;border-radius:10px;background:var(--surf);border:1px solid var(--line);box-shadow:0 6px 24px rgba(0,0,0,.3);z-index:20;max-width:90vw';document.body.appendChild(t)}
 t.textContent=msg;t.style.borderColor=bad?'var(--bad)':'var(--ok)';t.style.display='block';clearTimeout(t._h);t._h=setTimeout(()=>t.style.display='none',4500)}
const KL={ledger_without_payment:'Orders with no payment',gateway_without_order:'Payments not in books',batch_without_bank_credit:'Payouts not in bank',credit_without_batch:'Unexplained bank credits',settlement_amount_difference:'Payout amount differs'};
const KNAME={bank:'Bank statement',gateway:'Gateway settlements',ledger:'Books / sales ledger'};
// ---------- auth
function showLogin(msg){$('#u').value='';$('#p').value='';$('#llm').classList.add('hide');$('#app').classList.add('hide');$('#who').classList.add('hide');$('#nav').innerHTML='';$('#loginbox').classList.remove('hide');$('#loginmsg').textContent=msg||'';
 const b=$('#boot');b.classList.toggle('hide',!S.boot);if(S.boot)b.innerHTML='No users exist yet. Create the first administrator on the server:<br><code>python -m tessera.recon.admin create-user NAME --role admin</code>'}
async function signin(){$('#loginmsg').textContent='';try{const r=await api('/auth/login',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({username:$('#u').value,password:$('#p').value})});$('#p').value='';setUser(r);await init()}catch(e){$('#loginmsg').textContent=e.message}}
$('#signin').onclick=signin;$('#p').onkeydown=e=>{if(e.key==='Enter')signin()};
function setUser(r){$('#llm').classList.remove('hide');S.user=r.user;S.csrf=r.csrf;S.perms=new Set(r.permissions);$('#loginbox').classList.add('hide');$('#app').classList.remove('hide');
 $('#who').classList.remove('hide');$('#who').innerHTML=`${esc(S.user.display_name||S.user.username)} <span class="role">${esc(S.user.role)}</span> <button class="b s" id="out">Sign out</button>`;
 $('#out').onclick=async()=>{try{await post('/auth/logout')}catch{}S.user=null;S.csrf='';showLogin()}}
// ---------- nav
function tabs(){const t=[];if(can('import'))t.push(['import','Import']);t.push(['overview','Overview'],['exc','Exceptions'],['flags','Flags'],['approvals','Approvals']);if(can('audit'))t.push(['audit','Audit']);if(can('manage_users'))t.push(['admin','Users']);return t}
function nav(c){$('#nav').innerHTML=tabs().map(([i,l])=>`<button data-t="${i}" class="${i===S.tab?'on':''}">${l}${c&&c[i]?`<span class="ct">${c[i]}</span>`:''}</button>`).join('')}
$('#nav').onclick=e=>e.target.closest('[data-t]')&&show(e.target.closest('[data-t]').dataset.t);
async function counts(){try{const s=await api('/recon/summary');nav({exc:s.exceptions.reduce((a,x)=>a+x.pending,0),flags:s.flags.pending,approvals:s.proposals_open});return s}catch{nav();return null}}
function show(t){S.tab=t;document.querySelectorAll('main section').forEach(s=>s.classList.toggle('on',s.id===t));({overview:loadOverview,exc:loadExc,flags:loadFlags,approvals:loadApprovals,audit:loadAudit,admin:loadAdmin,import:loadImport}[t])();counts()}
const seg=(el,opts,cur,cb)=>{$(el).innerHTML=opts.map(([v,l])=>`<button data-v="${v}" class="${v===cur?'on':''}">${esc(l)}</button>`).join('');$(el).onclick=e=>{const v=e.target.dataset.v;if(v)cb(v)}};
function pager(el,total,p,n,cb){const pages=Math.max(1,Math.ceil(total/n));$(el).innerHTML=`<button class="b s" ${p<=0?'disabled':''} id="${el.slice(1)}p">‹ Prev</button><span class="mut small">${total?`${p*n+1}–${Math.min(total,(p+1)*n)} of ${total.toLocaleString()}`:'0'}</span><button class="b s" ${p>=pages-1?'disabled':''} id="${el.slice(1)}n">Next ›</button>`;$(el+'p').onclick=()=>cb(p-1);$(el+'n').onclick=()=>cb(p+1)}
// ---------- decisions are proposals
function proposalRow(p){let h=`<span class="tag wait">awaiting approval</span><span class="small">${esc(p.action)} proposed by <b>${esc(p.by)}</b> · ${esc(p.at)}${p.note?' · <i>'+esc(p.note)+'</i>':''}</span>`;
 if(p.by_id===S.user.id)h+=`<button class="b s" data-a="withdraw" data-id="${p.id}">Withdraw</button>`;
 else if(can('approve'))h+=`<input type="text" class="note" placeholder="note (optional)" size="16"><button class="b ok" data-a="approve" data-id="${p.id}">Approve</button><button class="b no" data-a="decline" data-id="${p.id}">Decline</button>`;
 return h}
function actions(scope,it){if(it.proposal)return proposalRow(it.proposal);if(!can('propose'))return '<span class="mut small">read-only</span>';
 if(it.status!=='pending')return `<button class="b s" data-a="undo">Propose undo</button>`;
 const ok=scope==='flag'?'Confirm (dispute)':'Confirm discrepancy',no=scope==='flag'?'Dismiss (legitimate)':'Reject (explained)';
 return `<input type="text" placeholder="note (optional)" size="22" class="note"><button class="b ok" data-a="confirm">${ok}</button><button class="b no" data-a="reject">${no}</button>${scope==='exc'&&S.kind==='ledger_without_payment'?'<button class="b s" data-a="find">Find matching payment</button>':''}${scope==='flag'?'<button class="b s" data-a="reword">Reword with AI</button>':''}`}
async function proposalAction(a,el){const id=el.dataset.id,note=el.closest('.item,.prop,tr')?.querySelector('.note')?.value||'';
 if(a==='approve'){await post(`/recon/proposals/${id}/approve`,{note});toast('Approved.')}else if(a==='decline'){await post(`/recon/proposals/${id}/decline`,{note});toast('Declined.')}else{await post(`/recon/proposals/${id}/withdraw`);toast('Withdrawn.')}}
const PROPOSED='Proposed. It takes effect once a different approver approves it.';
// ---------- import
function loadImport(){$('#demorow').classList.toggle('hide',!(S.sys.demo&&can('manage_users')));
 $('#drops').innerHTML=Object.keys(KNAME).map(k=>`<div><div class="drop ${S.uploaded[k]?'has':''}"><b>${KNAME[k]}</b><div class="small">${S.uploaded[k]?'✓ '+esc(S.uploaded[k]):'drop a .csv here or choose'}</div><input type="file" accept=".csv,text/csv" data-k="${k}" style="margin-top:6px;max-width:100%"></div></div>`).join('')}
$('#drops').addEventListener('change',e=>{if(e.target.type==='file'&&e.target.files[0])up(e.target.dataset.k,e.target.files[0])});
async function up(k,f){try{const r=await fetch('/api/recon/upload/'+k+'?name='+encodeURIComponent(f.name),{method:'PUT',headers:{'X-CSRF-Token':S.csrf},body:await f.arrayBuffer()});const j=await r.json();if(!r.ok)throw new Error(j.detail);S.uploaded[k]=f.name+' · '+j.columns.length+' columns';loadImport();maybeMap()}catch(e){toast(k+': '+e.message,1)}}
async function demo(messy){try{await post('/recon/demo?messy='+messy);Object.keys(KNAME).forEach(k=>S.uploaded[k]='demo file');loadImport();maybeMap()}catch(e){toast(e.message,1)}}
$('#demo1').onclick=()=>demo(false);$('#demo2').onclick=()=>demo(true);
async function maybeMap(){if(!Object.keys(KNAME).every(k=>S.uploaded[k]))return;$('#mapcard').classList.remove('hide');S.maps={};
 try{for(const k of Object.keys(KNAME)){S.maps[k]=await api('/recon/mapping/'+k);renderMaps()}}catch(e){$('#maps').innerHTML='<div class="err">'+esc(e.message)+'</div>'}}
function renderMaps(){$('#maps').innerHTML=Object.keys(KNAME).map(k=>{const m=S.maps[k],opt='<option value="">— not mapped —</option>';if(!m)return `<div style="margin-bottom:16px" class="mut"><b>${KNAME[k]}</b> · reading…</div>`;
 const rows=Object.keys(m.fields).map(f=>{const req=m.required.includes(f),cur=m.mapping[f]||'',ai=(m.llm_fields||[]).includes(f);
  return `<tr><td><b>${esc(f)}</b>${req?'':' <span class="mut small">(optional)</span>'}<div class="mut small">${esc(m.fields[f])}</div></td><td><select data-k="${k}" data-f="${f}">${opt}${m.headers.map(h=>`<option ${h===cur?'selected':''}>${esc(h)}</option>`).join('')}</select> ${ai?'<span class="tag ai">AI-proposed</span>':cur?'<span class="tag">auto</span>':''}</td><td class="mut small">${cur?esc((m.samples.map(s=>s[cur]).filter(x=>x!==''&&x!=null).slice(0,2)).join(' · ')):''}</td></tr>`}).join('');
 return `<div style="margin-bottom:16px"><b>${KNAME[k]}</b> <span class="mut small" id="pr-${k}"></span><table><tr><th>Field</th><th>Column in your file</th><th>Sample</th></tr>${rows}</table></div>`}).join('');validateAll()}
let vt;$('#maps').addEventListener('change',e=>{const s=e.target;if(s.tagName!=='SELECT')return;const m=S.maps[s.dataset.k];if(s.value)m.mapping[s.dataset.f]=s.value;else delete m.mapping[s.dataset.f];clearTimeout(vt);vt=setTimeout(validateAll,150)});
async function validateAll(){let ok=true;for(const k of Object.keys(KNAME)){const m=S.maps[k];if(!m){ok=false;continue}try{const r=await post('/recon/validate/'+k,{mapping:m.mapping});m.problems=r.problems}catch{m.problems=['could not validate']}
 const el=$('#pr-'+k);if(el)el.innerHTML=m.problems.length?'<span class="err">'+m.problems.map(esc).join(' · ')+'</span>':'<span class="okc">✓ checks pass</span>';if(m.problems.length)ok=false}$('#doimport').disabled=!ok}
$('#doimport').onclick=async()=>{$('#doimport').disabled=true;$('#impmsg').textContent='importing and matching…';try{const r=await post('/recon/import',{mappings:Object.fromEntries(Object.keys(KNAME).map(k=>[k,S.maps[k].mapping]))});$('#impmsg').innerHTML='<span class="okc">Imported '+Object.entries(r.rows).map(([k,v])=>v.toLocaleString()+' '+k).join(', ')+'.</span>';S.ep=S.fp=0;setTimeout(()=>show('overview'),700)}catch(e){$('#impmsg').innerHTML='<span class="err">'+esc(e.message)+'</span>';$('#doimport').disabled=false}};
// ---------- overview
async function loadOverview(){const s=await counts();if(!s||!s.counts){$('#ov').innerHTML='<div class="callout">Nothing imported yet.'+(can('import')?' Start on the Import tab.':' Ask a reviewer or admin to import the files.')+'</div>';return}
 const ex=s.exceptions.reduce((a,x)=>a+x.pending,0);
 $('#ov').innerHTML=`<div class="grid"><div class="card kpi"><div class="v">${(s.l1_match_rate*100).toFixed(1)}%</div><div class="l">orders matched to a payment automatically</div></div><div class="card kpi"><div class="v">${s.counts.orders.toLocaleString()}</div><div class="l">orders · ${s.counts.payments.toLocaleString()} payments · ${s.counts.bank_rows.toLocaleString()} bank rows</div></div><div class="card kpi"><div class="v">${ex.toLocaleString()}</div><div class="l">exceptions waiting for review</div></div><div class="card kpi"><div class="v">${s.flags.pending}</div><div class="l">flagged transactions waiting (${s.flags.n} total)</div></div><div class="card kpi"><div class="v">${s.proposals_open}</div><div class="l">proposals awaiting approval</div></div></div>
 <div class="grid"><div class="card"><h3>Exception queues</h3><table><tr><th>Queue</th><th class="n">Open</th><th class="n">Reviewed</th></tr>${s.exceptions.map(x=>`<tr><td><a href="#" data-go="${x.kind}">${esc(KL[x.kind]||x.kind)}</a></td><td class="n">${x.pending}</td><td class="n">${x.reviewed}</td></tr>`).join('')||'<tr><td colspan=3 class="mut">none</td></tr>'}</table></div>
 <div class="card"><h3>How matches were made</h3><table><tr><th>Layer</th><th>Rule</th><th class="n">Matches</th></tr>${s.matches.map(x=>`<tr><td>${x.layer==='L1'?'order ↔ payment':'payout ↔ bank'}</td><td>${esc(x.pass)}${x.status!=='auto'?' <span class="tag warn">'+esc(x.status.replace(/_/g,' '))+'</span>':''}</td><td class="n">${x.n.toLocaleString()}</td></tr>`).join('')}</table></div></div>`}
$('#ov').addEventListener('click',e=>{const k=e.target.dataset.go;if(k){e.preventDefault();S.kind=k;S.ep=0;show('exc')}});
// ---------- exceptions
async function loadExc(){const s=await counts();$('#excnote').innerHTML=(S.sys.require_checker?'Every decision here is a <b>proposal</b>: it takes effect only after a different approver approves it. ':'')+'<b>Confirm</b> = a real discrepancy to chase. <b>Reject</b> = explained or a false alarm.';
 const kinds=(s?s.exceptions:[]);if(!kinds.length){$('#exclist').innerHTML='<div class="callout">Nothing to review yet.</div>';$('#exckinds').innerHTML='';$('#excstate').innerHTML='';$('#excpager').innerHTML='';return}
 if(!kinds.find(x=>x.kind===S.kind))S.kind=kinds[0].kind;
 seg('#exckinds',kinds.map(x=>[x.kind,(KL[x.kind]||x.kind)+' ('+x.pending+')']),S.kind,v=>{S.kind=v;S.ep=0;loadExc()});
 seg('#excstate',[['pending','Open'],['confirmed','Confirmed'],['rejected','Rejected'],...(S.kind==='ledger_without_payment'?[['linked','Linked']]:[])],S.es,v=>{S.es=v;S.ep=0;loadExc()});
 const r=await api(`/recon/exceptions/${S.kind}?state=${S.es}&limit=25&offset=${S.ep*25}`);
 $('#exclist').innerHTML=r.items.map(it=>`<div class="item ${it.status==='confirmed'||it.status==='linked'?'decided':it.status==='rejected'?'rej':''}" data-key="${esc(it.item_key)}"><div class="top"><b>${esc(it.item_key.length>24?it.item_key.slice(0,10)+'…':it.item_key)}</b><span class="amt">${money(it.amount)}</span><span class="mut">${esc(it.on_date||'')}</span>${it.status!=='pending'?`<span class="tag ${it.status==='rejected'?'bad':'ok'}">${esc(it.status)}</span>`:''}</div><div class="why">${esc(it.detail)}${it.note?' · <i>'+esc(it.note)+'</i>':''}</div><div class="row">${actions('exc',it)}</div><div class="cands"></div></div>`).join('')||'<div class="mut">Nothing here.</div>';
 pager('#excpager',r.total,S.ep,25,p=>{S.ep=p;loadExc()})}
$('#exclist').addEventListener('click',async e=>{const a=e.target.dataset.a;if(!a)return;const el=e.target.closest('.item'),key=el.dataset.key;
 try{if(a==='find'){const r=await api('/recon/candidates/'+encodeURIComponent(key));el.querySelector('.cands').innerHTML=r.candidates.length?`<table style="margin-top:8px"><tr><th>Payment</th><th class="n">Amount</th><th>Captured</th><th class="n">Δ</th><th></th></tr>${r.candidates.map(c=>`<tr><td>${esc(c.payment_id)}</td><td class="n">${money(c.amount)}</td><td>${esc(c.captured_on)}</td><td class="n">${c.diff_paise?money(c.diff_paise/100):'exact'}</td><td><button class="b" data-a="link" data-p="${esc(c.payment_id)}">Propose link</button></td></tr>`).join('')}</table>`:'<div class="mut small" style="margin-top:6px">No unmatched payment with a similar amount near that date.</div>';return}
  const note=el.querySelector('.note')?.value||'';
  if(['approve','decline','withdraw'].includes(a))await proposalAction(a,e.target);
  else if(a==='link'){await post('/recon/link',{order_id:key,payment_id:e.target.dataset.p,note});toast(PROPOSED)}
  else if(a==='undo'){await post('/recon/undo',{kind:S.kind,key});toast(PROPOSED)}
  else{await post('/recon/review',{kind:S.kind,key,action:a,note});toast(PROPOSED)}
  loadExc();counts()}catch(err){toast(err.message,1)}});
// ---------- flags
async function loadFlags(){await counts();seg('#flagstate',[['pending','Open'],['confirmed','Confirmed'],['rejected','Dismissed']],S.fs,v=>{S.fs=v;S.fp=0;loadFlags()});
 const r=await api(`/recon/flags?state=${S.fs}&limit=20&offset=${S.fp*20}`);
 const ex=await Promise.all(r.items.map(it=>api('/recon/explain/'+it.row_id).catch(()=>null)));
 $('#flaglist').innerHTML=r.items.map((it,i)=>`<div class="item ${it.status==='confirmed'?'decided':it.status==='rejected'?'rej':''}" data-key="${esc(it.item_key)}" data-row="${it.row_id}"><div class="top"><b>${esc(it.payee)}</b><span class="amt">${money(it.amount)}</span><span class="mut">${esc(it.posted_on)}</span><span class="bar" title="score ${it.score}"><i style="width:${Math.round(it.score*100)}%"></i></span>${it.rules.map(x=>`<span class="tag warn">${esc(x.replace(/_/g,' '))}</span>`).join('')}${it.status!=='pending'?`<span class="tag ${it.status==='rejected'?'bad':'ok'}">${esc(it.status==='rejected'?'dismissed':it.status)}</span>`:''}</div><div class="why expl">${ex[i]?esc(ex[i].text):''}${it.note?' · <i>'+esc(it.note)+'</i>':''}</div><div class="row">${actions('flag',it)}</div></div>`).join('')||'<div class="mut">Nothing here.</div>';
 pager('#flagpager',r.total,S.fp,20,p=>{S.fp=p;loadFlags()})}
$('#flaglist').addEventListener('click',async e=>{const a=e.target.dataset.a;if(!a)return;const el=e.target.closest('.item'),key=el.dataset.key;
 try{if(a==='reword'){e.target.disabled=true;e.target.textContent='asking local model…';const r=await api('/recon/explain/'+el.dataset.row+'?llm=true');el.querySelector('.expl').innerHTML=esc(r.text)+(r.source==='llm'?' <span class="tag ai">AI-reworded</span>':' <span class="tag">kept original: AI wording failed checks</span>');e.target.remove();return}
  const note=el.querySelector('.note')?.value||'';
  if(['approve','decline','withdraw'].includes(a))await proposalAction(a,e.target);
  else if(a==='undo'){await post('/recon/undo',{kind:'flag',key});toast(PROPOSED)}
  else{await post('/recon/review',{kind:'flag',key,action:a,note});toast(PROPOSED)}
  loadFlags();counts()}catch(err){toast(err.message,1)}});
// ---------- approvals
async function loadApprovals(){const r=await api('/recon/proposals');await counts();
 $('#applist').innerHTML=r.length?r.map(p=>{const c=p.context||{},mine=p.proposed_by===S.user.id;
  return `<div class="item"><div class="top"><b>${esc(p.action)}</b><span class="tag">${esc(KL[p.kind]||p.kind)}</span><span class="mono">${esc(p.item_key.length>24?p.item_key.slice(0,10)+'…':p.item_key)}</span>${c.amount!=null?`<span class="amt">${money(c.amount)}</span>`:''}<span class="mut">${esc(c.on_date||'')}</span></div>
  <div class="why">${esc(c.detail||c.payee||'')}${p.action==='link'&&p.payload?' · to payment <b>'+esc(p.payload.payment_id)+'</b>':''} · proposed by <b>${esc(p.proposed_by_name)}</b> ${esc(p.proposed_at)}${p.note?' · <i>'+esc(p.note)+'</i>':''}</div>
  <div class="row prop">${mine?`<span class="mut small">You proposed this; a different approver must decide.</span><button class="b s" data-a="withdraw" data-id="${p.id}">Withdraw</button>`:can('approve')?`<input type="text" class="note" placeholder="note (optional)" size="20"><button class="b ok" data-a="approve" data-id="${p.id}">Approve</button><button class="b no" data-a="decline" data-id="${p.id}">Decline</button>`:'<span class="mut small">Waiting for an approver.</span>'}</div></div>`}).join(''):'<div class="callout">Nothing is waiting for approval.</div>'}
$('#applist').addEventListener('click',async e=>{const a=e.target.dataset.a;if(!a)return;try{await proposalAction(a,e.target);loadApprovals()}catch(err){toast(err.message,1)}});
// ---------- audit + users
async function loadAudit(){const a=await api('/admin/audit?limit=200');$('#act').innerHTML=`<table><tr><th>Time (UTC)</th><th>Who</th><th>Action</th><th>Detail</th></tr>${a.map(x=>`<tr><td class="mut small">${esc(x.ts)}</td><td>${esc(x.actor)}</td><td>${esc(x.action)}</td><td class="small">${esc(JSON.stringify(x.detail)).slice(0,160)}</td></tr>`).join('')}</table>`}
$('#verify').onclick=async()=>{const r=await api('/admin/audit/verify');$('#verifymsg').innerHTML=r.ok?`<span class="okc">✓ chain intact (${r.rows} entries)</span>`:`<span class="err">✗ chain broken at entry ${r.first_bad_id}</span>`};
async function loadAdmin(){const u=await api('/admin/users');$('#userlist').innerHTML=`<table><tr><th>User</th><th>Role</th><th>Last sign-in</th><th>Reset password</th><th></th></tr>${u.map(x=>`<tr><td><b>${esc(x.username)}</b>${x.disabled?' <span class="tag bad">disabled</span>':''}</td><td><select data-role="${x.id}">${['viewer','reviewer','approver','admin'].map(r=>`<option ${r===x.role?'selected':''}>${r}</option>`).join('')}</select></td><td class="mut small">${esc(x.last_login||'never')}</td><td><input type="password" data-pw="${x.id}" placeholder="new password" size="14"><button class="b s" data-reset="${x.id}">Set</button></td><td><button class="b s" data-dis="${x.id}" data-to="${x.disabled?'enable':'disable'}">${x.disabled?'Enable':'Disable'}</button></td></tr>`).join('')}</table>`}
$('#userlist').addEventListener('change',async e=>{const id=e.target.dataset.role;if(!id)return;try{await post(`/admin/users/${id}/role`,{role:e.target.value});toast('Role changed; that user was signed out.')}catch(err){toast(err.message,1);loadAdmin()}});
$('#userlist').addEventListener('click',async e=>{try{if(e.target.dataset.dis){await post(`/admin/users/${e.target.dataset.dis}/${e.target.dataset.to}`);loadAdmin()}
 if(e.target.dataset.reset){const pw=document.querySelector(`[data-pw="${e.target.dataset.reset}"]`).value;await post(`/admin/users/${e.target.dataset.reset}/password`,{password:pw});toast('Password set; that user was signed out.')}}catch(err){toast(err.message,1)}});
$('#adduser').onclick=async()=>{try{await post('/admin/users',{username:$('#nu').value,password:$('#np').value,role:$('#nr').value});$('#nu').value=$('#np').value='';$('#addmsg').textContent='Created.';loadAdmin()}catch(e){$('#addmsg').innerHTML='<span class="err">'+esc(e.message)+'</span>'}};
// ---------- start
async function init(){try{S.sys=await api('/recon/system')}catch{S.sys={}}
 $('#dot').classList.toggle('ok',!!S.sys.llm&&S.sys.llm.ok);$('#llmt').textContent=S.sys.llm&&S.sys.llm.ok?'local model ready':'local model off: rules and templates only';
 Object.entries(S.sys.uploaded||{}).forEach(([k,v])=>{if(v)S.uploaded[k]='previous upload'});
 show(S.sys.loaded?'overview':(can('import')?'import':'overview'))}
(async()=>{try{S.boot=(await fetch('/api/health').then(r=>r.json())).bootstrap_needed}catch{}
 try{setUser(await api('/auth/me'));await init()}catch{showLogin()}})();
