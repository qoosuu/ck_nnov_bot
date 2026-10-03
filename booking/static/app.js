'use strict';
const $ = id => document.getElementById(id);
const tg = window.Telegram?.WebApp;
const state = {me: null, date: null, bookings: [], bans: [], formBans: [], formLoading: false, formSequence: 0, editing: null, busy: false, tab: 'bookings', sequence: 0};
const sessionId = crypto.randomUUID();
let toastTimer, pendingMutation = null;
function node(tag, cls, text) { const n = document.createElement(tag); if(cls) n.className = cls; if(text !== undefined) n.textContent = text; return n; }
function toast(message) { $('toast').textContent = message; $('toast').hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => $('toast').hidden = true, 5000); }
function failAccess(message) { $('booking-dialog').close(); $('app').hidden = true; $('gate').hidden = false; $('gate-message').textContent = message; $('retry').hidden = false; state.me = null; }
async function api(path, method='GET', data, key) {
  const headers = {'Authorization': 'tma ' + (tg?.initData || '')};
  if(data !== undefined) headers['Content-Type'] = 'application/json';
  if(key) headers['Idempotency-Key'] = key;
  let response;
  try { response = await fetch('/api/' + path, {method, headers, body:data === undefined ? undefined : JSON.stringify(data), signal:AbortSignal.timeout(20000)}); }
  catch { throw new Error('Нет ответа от сервера. Проверьте соединение и повторите действие.'); }
  let result;
  try { result = await response.json(); } catch { throw new Error('Сервер временно недоступен. Попробуйте позже.'); }
  if(!response.ok) {
    if(response.status === 401 || response.status === 403) failAccess(result.error);
    throw new Error(result.error || 'Не удалось выполнить действие.');
  }
  return result;
}
function shiftDate(s) { return new Date(s + 'T12:00:00Z'); }
function isoDate(d) { return d.toISOString().slice(0,10); }
function addDays(s,n) { const d=shiftDate(s); d.setUTCDate(d.getUTCDate()+n); return isoDate(d); }
function isLate(s) { return [5,6].includes(shiftDate(s).getUTCDay()); }
function dateTitle(s) { return shiftDate(s).toLocaleDateString('ru-RU',{day:'numeric',month:'long',weekday:'long',timeZone:'UTC'}); }
function hours(s) { return '18:00–' + (isLate(s) ? '03:00' : '00:00'); }
function formHours() {
  const value = $('booking-form').elements.shift_date.value;
  if(value) $('form-hours').textContent = 'Часы смены: ' + hours(value) + '. ' + (isLate(value) ? 'Время после полуночи относится к следующему календарному дню.' : 'Окончание в 00:00 — полночь после этой смены.');
}
function renderDates() {
  $('day-title').textContent = dateTitle(state.date);
  $('date-jump').value = state.date;
  $('hours').textContent = 'Часы бара · ' + hours(state.date);
  $('night-hint').textContent = isLate(state.date) ? 'Брони после полуночи и до 03:00 входят в эту смену.' : '17 столов · Выберите стол, чтобы добавить бронь.';
  $('dates').replaceChildren();
  for(let i=-3;i<=17;i++) {
    const value=addDays(state.date,i), d=shiftDate(value);
    const b=node('button','date-button' + (i===0?' selected':''));
    b.type='button'; b.setAttribute('aria-label', dateTitle(value)); b.setAttribute('aria-pressed',String(i===0));
    b.append(node('span','',d.toLocaleDateString('ru-RU',{weekday:'short',timeZone:'UTC'})), node('strong','',String(d.getUTCDate())),node('span','',d.toLocaleDateString('ru-RU',{month:'short',timeZone:'UTC'})));
    b.onclick=()=>selectDate(value); $('dates').append(b);
  }
  requestAnimationFrame(()=> { const selected = $('dates').querySelector('.selected'); $('dates').scrollLeft = Math.max(0,selected.offsetLeft - $('dates').offsetLeft - 120); });
}
function minute(value) { const [h,m]=value.split(':').map(Number); return h*60+m+(h<18?1440:0); }
function banText(b) { return `Смена ${b.shift_date}: с ${b.start_time} до ${b.end_time} брони не принимаем. ${b.reason}`; }
function fullyClosed() {
  let end=1080;
  for(const b of state.bans) { if(minute(b.start_time)>end) return false; end=Math.max(end,minute(b.end_time)); }
  return end>=minute(isLate(state.date)?'03:00':'00:00');
}
function checkFormBan() {
  const f=$('booking-form'), start=f.elements.start_time.value, end=f.elements.end_time.value;
  const hits=start?state.formBans.filter(b=>minute(start)<minute(b.end_time) && minute(end||(isLate(f.elements.shift_date.value)?'03:00':'00:00'))>minute(b.start_time)):[];
  const message=state.editing?.restriction || (state.formLoading?'Проверяем запреты…':hits.map(banText).join('\n'));
  $('form-ban').textContent=message+(hits.length && !end?' Укажите окончание до запрета или выберите другое время.':'');
  $('form-ban').hidden=!message;
  $('save-booking').disabled=state.busy || !!message;
  for(const field of f.querySelectorAll('input,select,textarea')) field.disabled=!!state.editing?.restriction;
}
async function loadFormBans() {
  const seq=++state.formSequence, value=$('booking-form').elements.shift_date.value;
  formHours(); state.formLoading=true; checkFormBan();
  try {
    const d=await api('bookings?date='+encodeURIComponent(value));
    if(seq!==state.formSequence)return;
    state.formBans=d.bans;
    if(state.editing && state.editing.shift_date===value) state.editing.restriction=d.bookings.find(b=>b.id===state.editing.id)?.restriction || '';
    state.formLoading=false; checkFormBan();
  } catch(e) { if(seq===state.formSequence) { $('form-ban').textContent=e.message+' Закройте карточку и повторите попытку.'; $('form-ban').hidden=false; } }
}
function renderTables() {
  $('ban-notice').hidden=!state.bans.length;
  $('ban-notice').replaceChildren(...state.bans.map(b=>node('p','',banText(b))));
  const closed=fullyClosed();
  $('tables').replaceChildren();
  const total=state.bookings.reduce((n,b)=>n+b.guests,0);
  $('summary').textContent = `Броней: ${state.bookings.length} · Гостей: ${total}`;
  for(let t=1;t<=17;t++) {
    const items=state.bookings.filter(b=>b.table_no===t), card=node('article','table-card'), head=node('div','table-head');
    head.append(node('h2','',`Стол №${t}`),node('span','badge'+(items.length?'':' empty'),items.length?`Броней: ${items.length}`:'Без броней'));
    card.append(head);
    for(const b of items) {
      const button=node('button','booking'), top=node('div','booking-top');
      button.type='button'; button.setAttribute('aria-label',`Бронь: ${b.guest_name}, стол ${t}, ${b.start_time}`);
      top.append(node('span','booking-time',b.start_time+(b.end_time?'–'+b.end_time:'')),node('span','booking-guests',`${b.guests} чел.`));
      button.append(top,node('span','booking-name',b.guest_name));
      if(b.restriction) button.append(node('span','booking-comment','Изменение закрыто: '+b.restriction));
      if(b.comment) button.append(node('span','booking-comment',b.comment));
      button.onclick=()=>openBooking(b); card.append(button);
    }
    if(!items.length) card.append(node('p','empty-text',closed?'Бронирование на смену закрыто.':'Пока тихо. Можно бронировать.'));
    const add=node('button','add-booking'); add.type='button'; add.append(node('span','plus','+'),node('span','',items.length?'Добавить ещё одну бронь':'Добавить бронь'));
    add.disabled=closed;
    add.querySelector('.plus').setAttribute('aria-hidden','true'); add.onclick=()=>openBooking(null,t); card.append(add); $('tables').append(card);
  }
}
async function loadBookings(quiet=false) {
  const seq=++state.sequence, selected=state.date;
  try {
    const result=await api('bookings?date='+encodeURIComponent(selected));
    if(seq!==state.sequence || selected!==state.date) return;
    state.bookings=result.bookings; state.bans=result.bans; renderTables();
  } catch(e) { if(!quiet) toast(e.message); }
}
async function selectDate(value) { if(!value) return; state.date=value; state.bookings=[]; state.bans=[]; $('ban-notice').hidden=true; renderDates(); $('tables').replaceChildren(); $('summary').textContent='Загружаем брони…'; await loadBookings(); }
function openBooking(b, table=1) {
  state.editing=b; pendingMutation=null;
  const f=$('booking-form'); f.reset(); $('form-error').hidden=true;
  const values=b || {shift_date:state.date,table_no:table,guest_name:'',guest_link:'',guests:2,start_time:'18:00',end_time:'',comment:''};
  for(const key of ['shift_date','table_no','guest_name','guest_link','guests','start_time','end_time','comment']) f.elements[key].value=values[key] ?? '';
  $('dialog-kicker').textContent=b?`Стол №${b.table_no} · Бронь #${b.id}`:'Новая запись';
  $('dialog-title').textContent=b?'Детали брони':'Новая бронь';
  $('save-booking').textContent=b?'Сохранить изменения':'Создать бронь';
  $('delete-booking').hidden=!b;
  $('guest-link').hidden=!b?.guest_link;
  if(b?.guest_link) $('guest-link').href=b.guest_link;
  $('booking-meta').textContent=b?`Создал: ${b.created_by}. Последнее изменение: ${new Date(b.updated_at).toLocaleString('ru-RU',{timeZone:'Europe/Moscow'})}, сотрудник ${b.updated_by}.`:'';
  formHours(); $('booking-dialog').showModal(); loadFormBans();
}
function setBusy(value) { state.busy=value; for(const button of $('booking-form').querySelectorAll('button')) button.disabled=value; checkFormBan(); }
async function mutation(path, method, data) {
  const fingerprint=JSON.stringify([path,method,data]);
  if(!pendingMutation || pendingMutation.fingerprint!==fingerprint) pendingMutation={fingerprint,key:crypto.randomUUID()};
  const result=await api(path,method,data,pendingMutation.key); pendingMutation=null; return result;
}
$('booking-form').onsubmit=async event=> {
  event.preventDefault(); if(state.busy || $('save-booking').disabled) return;
  const f=$('booking-form'), data=Object.fromEntries(new FormData(f)); data.table_no=Number(data.table_no); data.guests=Number(data.guests); data.end_time=data.end_time||null;
  if(state.editing) data.version=state.editing.version;
  setBusy(true); $('form-error').hidden=true;
  try {
    await mutation('bookings'+(state.editing?'/'+state.editing.id:''),state.editing?'PUT':'POST',data);
    $('booking-dialog').close(); toast(state.editing?'Изменения сохранены':'Бронь добавлена'); await selectDate(data.shift_date);
  } catch(e) { $('form-error').textContent=e.message; $('form-error').hidden=false; }
  finally { setBusy(false); }
};
$('delete-booking').onclick=async()=> {
  if(state.busy || !state.editing || !confirm('Удалить эту бронь? Если сводка уже отправлена, в рабочий чат придёт уведомление об отмене.')) return;
  setBusy(true); $('form-error').hidden=true;
  try { await mutation('bookings/'+state.editing.id,'DELETE',{version:state.editing.version}); $('booking-dialog').close(); toast('Бронь удалена'); await loadBookings(); }
  catch(e) { $('form-error').textContent=e.message; $('form-error').hidden=false; } finally { setBusy(false); }
};
$('close-dialog').onclick=()=>{if(!state.busy) $('booking-dialog').close();};
$('booking-dialog').addEventListener('cancel',event=>{if(state.busy)event.preventDefault();});
$('booking-form').elements.shift_date.onchange=loadFormBans;
for(const key of ['start_time','end_time']) $('booking-form').elements[key].oninput=checkFormBan;
for(let i=1;i<=17;i++){const o=node('option','',`Стол №${i}`);o.value=String(i);$('booking-form').elements.table_no.append(o);}
$('date-jump').onchange=e=>selectDate(e.target.value);
$('today').onclick=async()=>{try{const me=await api('me'); await selectDate(me.shift_date);}catch(e){toast(e.message);}};
$('refresh').onclick=()=>loadBookings();
function switchTab(which) { state.tab=which; $('bookings-view').hidden=which!=='bookings'; $('admin-view').hidden=which!=='admin'; $('bookings-tab').classList.toggle('active',which==='bookings'); $('admin-tab').classList.toggle('active',which==='admin'); if(which==='admin') loadAdmin(); else loadBookings(); }
$('bookings-tab').onclick=()=>switchTab('bookings'); $('admin-tab').onclick=()=>switchTab('admin');
async function loadAdmin() {
  try {
    if(!$('ban-date').value) { $('ban-date').value=state.date; $('ban-end').value=isLate(state.date)?'03:00':'00:00'; }
    await loadBans();
    const d=await api('admin/overview'); $('metrics').replaceChildren();
    for(const metric of d.metrics) { const card=node('div','metric'); card.append(node('span','',metric.days===1?'Сегодня':`За ${metric.days} дней`),node('strong','',String(metric.opens)),node('small','',`Сотрудников: ${metric.people}`)); $('metrics').append(card); }
    $('chat-name').textContent=d.chat_id?`Рабочий чат: ${d.chat_title} (${d.chat_id})`:'Рабочий чат не зарегистрирован.';
    $('digest-time').value=d.digest_time; if(!$('digest-date').value)$('digest-date').value=state.date;
    $('delivery').textContent=`Сообщений в очереди: ${d.pending}.`+(d.delivery_errors.length?' Есть ошибки доставки — бот повторит попытку. Проверьте, что он остался в чате и может отправлять сообщения.':'');
    $('people').replaceChildren();
    for(const p of d.people) {
      const row=node('div','person'), info=node('div'); info.append(node('span','name',p.name),node('small','',`${p.username?'@'+p.username+' · ':''}${p.id} · ${p.id===439275668?'Владелец':p.blocked?'Доступ отключён':'Доступ по участию в чате'}`)); row.append(info);
      if(p.id!==439275668) {const b=node('button',p.blocked?'secondary':'danger-outline',p.blocked?'Разрешить':'Отключить'); b.onclick=async()=> {b.disabled=true;try{await api('admin/access','PUT',{user_id:p.id,blocked:!p.blocked}); await loadAdmin();}catch(e){toast(e.message);b.disabled=false;}};row.append(b);}
      $('people').append(row);
    }
    if(!d.people.length)$('people').append(node('p','hint','Сотрудники ещё не открывали приложение.'));
    const actions={ban_create:'Добавил запрет на бронирование',ban_delete:'Снял запрет на бронирование',create:'Создал бронь',update:'Изменил бронь',delete:'Удалил бронь',register:'Зарегистрировал чат',manual_digest:'Запросил сводку',digest_time:'Изменил время сводки',block:'Отключил доступ',unblock:'Вернул доступ'};
    $('audit').replaceChildren(); for(const a of d.audit){const row=node('div','audit-row',`${actions[a.action]||a.action}${a.booking_id?' #'+a.booking_id:''}`);row.append(node('small','',`${new Date(a.created_at).toLocaleString('ru-RU',{timeZone:'Europe/Moscow'})} · ${a.actor}`));$('audit').append(row);}
  }catch(e){toast(e.message);}
}
let banSequence=0;
async function loadBans() {
  const seq=++banSequence, date=$('ban-date').value;
  const d=await api('bookings?date='+encodeURIComponent(date));
  if(seq!==banSequence)return;
  $('ban-list').replaceChildren();
  for(const ban of d.bans) {
    const row=node('div','person'), remove=node('button','danger-outline','Снять запрет');
    row.append(node('p','',banText(ban)),remove);
    remove.type='button'; remove.onclick=async()=> {
      if(!confirm('Снять запрет? '+banText(ban)))return;
      remove.disabled=true;
      try { await api('admin/bans/'+ban.id,'DELETE'); await loadBans(); toast('Запрет снят'); }
      catch(e) { toast(e.message); remove.disabled=false; }
    };
    $('ban-list').append(row);
  }
  if(!d.bans.length)$('ban-list').append(node('p','hint','В эту смену запретов нет.'));
}
$('ban-date').onchange=()=> { $('ban-end').value=isLate($('ban-date').value)?'03:00':'00:00'; loadBans().catch(e=>toast(e.message)); };
$('ban-full').onclick=()=> { $('ban-start').value='18:00'; $('ban-end').value=isLate($('ban-date').value)?'03:00':'00:00'; };
$('ban-form').onsubmit=async event=> {
  event.preventDefault(); const button=event.submitter; button.disabled=true;
  try { await api('admin/bans','POST',Object.fromEntries(new FormData($('ban-form')))); await loadBans(); toast('Запрет сохранён'); }
  catch(e) {toast(e.message);} finally {button.disabled=false;}
};
$('settings-form').onsubmit=async event=>{event.preventDefault();const b=event.submitter;b.disabled=true;try{await api('admin/settings','PUT',{digest_time:$('digest-time').value});toast('Время сводки сохранено');}catch(e){toast(e.message);}finally{b.disabled=false;}};
$('send-digest').onclick=async()=>{const date=$('digest-date').value;if(!date || !confirm('Отправить сводку за '+dateTitle(date)+' в рабочий чат?'))return;const b=$('send-digest');b.disabled=true;try{const d=await api('admin/digest','POST',{shift_date:date});toast(d.queued?'Сводка поставлена в очередь отправки':'На эту смену нет броней — сводка не отправлена');await loadAdmin();}catch(e){toast(e.message);}finally{b.disabled=false;}};
$('access-form').onsubmit=async event=>{event.preventDefault();const b=event.submitter;b.disabled=true;try{await api('admin/access','PUT',{user_id:Number($('access-id').value),blocked:b.value==='block'});$('access-id').value='';toast('Доступ обновлён');await loadAdmin();}catch(e){toast(e.message);}finally{b.disabled=false;}};
async function init(){
  $('retry').hidden=true;
  if(!tg?.initData){failAccess('Откройте «Бронирования» через меню бота в Telegram. Доступ есть только у сотрудников рабочего чата.');return;}
  try {
    tg.ready();tg.expand();
    state.me=await api('me'); state.date=state.me.shift_date;
    $('gate').hidden=true;$('app').hidden=false;$('admin-tab').hidden=!state.me.is_owner;$('who').textContent=state.me.user.first_name||'Сотрудник';
    $('registration-warning').hidden=state.me.registered;
    renderDates();await loadBookings();
    api('visits','POST',{session_id:sessionId}).catch(()=>{});
  }catch(e){failAccess(e.message);}
}
$('retry').onclick=init;
setInterval(()=>{if(state.me && !document.hidden && !$('booking-dialog').open && state.tab==='bookings')loadBookings(true);},15000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden && state.me && !$('booking-dialog').open)loadBookings(true);});
init();
