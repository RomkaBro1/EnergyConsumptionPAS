'use strict';

const $ = id => document.getElementById(id);
const state = {view: 'overview', hours: 24, days: 2, model: 'boosting', overviewHours: 24, overviewModel: 'boosting', delta: 0, ready: false, busy: false, charts: new Map()};
const labels = {boosting: 'Градиентный бустинг', seasonal: 'Недельный профиль'};
const views = {
  overview: ['МОНИТОРИНГ · ГЕРМАНИЯ', 'Мониторинг электропотребления', 'Нагрузка энергосистемы Германии, погода и календарь.', 'Обзор системы'],
  forecast: ['ПЛАНИРОВАНИЕ · 24–168 ЧАСОВ', 'Прогноз потребления', 'Почасовая нагрузка и погодные сценарии.', 'Прогноз потребления'],
  models: ['ВАЛИДАЦИЯ · МЕТРИКИ И ОГРАНИЧЕНИЯ', 'Сравнение моделей', 'Результаты проверки на отложенных данных.', 'Качество моделей'],
  operations: ['ОПЕРАЦИОННЫЙ ДАШБОРД', 'Источники и загрузки', 'Качество данных и журнал обработки.', 'Источники и загрузки']
};
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const num = (v, digits = 1) => v == null || !Number.isFinite(Number(v)) ? '—' : Number(v).toLocaleString('ru-RU', {minimumFractionDigits: digits, maximumFractionDigits: digits});
const date = (v, opts = {}) => v ? new Intl.DateTimeFormat('ru-RU', {timeZone: 'Europe/Berlin', day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit', ...opts}).format(new Date(v)) : '—';
const fullDate = v => date(v, {year:'numeric', timeZoneName:'shortOffset'});
const query = () => new URLSearchParams({hours:state.hours, model:state.model, temperature_delta:state.delta});

async function api(url, options = {}) {
  const response = await fetch(url, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `Ошибка сервиса: ${response.status}`);
  return data;
}
function toast(message) { $('toast').textContent = message; $('toast').classList.remove('hidden'); clearTimeout(state.toast); state.toast = setTimeout(() => $('toast').classList.add('hidden'), 5000); }
function error(message) { $('error-banner').textContent = message; $('error-banner').classList.toggle('hidden', !message); }

class Chart {
  constructor(id, rows, series, options = {}) {
    this.canvas = $(id); this.ctx = this.canvas.getContext('2d'); this.rows = rows; this.series = series; this.options = options; this.hover = null;
    this.tooltip = this.canvas.parentElement.querySelector('.chart-tooltip');
    this.canvas.onmousemove = event => this.move(event);
    this.canvas.onmouseleave = () => { this.hover = null; this.tooltip.classList.add('hidden'); this.draw(); };
    state.charts.set(id, this); this.draw();
  }
  draw() {
    const rect = this.canvas.getBoundingClientRect(); if (!rect.width) return;
    const dpr = window.devicePixelRatio || 1; const w = rect.width, h = rect.height;
    this.canvas.width = Math.round(w*dpr); this.canvas.height = Math.round(h*dpr);
    const ctx = this.ctx; ctx.scale(dpr,dpr); ctx.clearRect(0,0,w,h);
    const theme=getComputedStyle(this.canvas);
    const gridColor=theme.getPropertyValue('--chart-grid').trim();
    const textColor=theme.getPropertyValue('--muted').trim();
    const surfaceColor=theme.getPropertyValue('--panel').trim();
    const pad = {l:43,r:16,t:15,b:33}; const pw = w-pad.l-pad.r, ph = h-pad.t-pad.b;
    const rows = this.rows; if (!rows.length) return;
    const times = rows.map(r => +new Date(r.timestamp_utc));
    const values = rows.flatMap(r => [...this.series.map(s => r[s.key]), ...(this.options.band ? [r.lower, r.upper] : [])]).filter(v => v != null && Number.isFinite(v));
    if (!values.length) return;
    let low = Math.min(...values), high = Math.max(...values); let span = high-low || 10;
    low = Math.max(this.options.negative ? -100 : 0, low-span*.18); high += span*.18;
    const step = (high-low)/4; low = Math.floor(low/step)*step; high = Math.ceil(high/step)*step;
    const xmin = Math.min(...times), xmax = Math.max(...times) || xmin+1;
    const x = t => pad.l + (t-xmin)/(xmax-xmin || 1)*pw; const y = v => pad.t + (high-v)/(high-low)*ph;
    this.geom = {x,y,pad,pw,ph,w,h,times};
    ctx.font = '10px Segoe UI, sans-serif'; ctx.textAlign = 'right'; ctx.textBaseline = 'middle';
    for (let i=0;i<=4;i++) { const v=low+(high-low)*i/4; const yy=y(v); ctx.strokeStyle=gridColor; ctx.lineWidth=1; ctx.beginPath(); ctx.moveTo(pad.l,yy); ctx.lineTo(w-pad.r,yy); ctx.stroke(); ctx.fillStyle=textColor; ctx.fillText(num(v,0),pad.l-11,yy); }
    const ticks = w < 600 ? 4 : 7;
    ctx.textAlign='center'; ctx.fillStyle=textColor;
    for(let i=0;i<ticks;i++){ const t=times[Math.round((times.length-1)*i/(ticks-1))]; const format=(xmax-xmin)>5*86400000?{day:'2-digit',month:'2-digit'}:{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'}; const text=new Intl.DateTimeFormat('ru-RU',{timeZone:'Europe/Berlin',...format}).format(t).replace(', ',' · '); ctx.fillText(text,Math.max(45,Math.min(w-42,x(t))),h-13); }
    if(this.options.band){
      const band=rows.filter(r=>r.lower!=null && r.upper!=null);
      if(band.length){ctx.beginPath();band.forEach((r,i)=>{const xx=x(+new Date(r.timestamp_utc));i?ctx.lineTo(xx,y(r.upper)):ctx.moveTo(xx,y(r.upper));}); [...band].reverse().forEach(r=>ctx.lineTo(x(+new Date(r.timestamp_utc)),y(r.lower))); ctx.closePath();ctx.fillStyle='#aaa0ed26';ctx.fill();}
    }
    if(this.options.boundary){const xx=x(+new Date(this.options.boundary));if(xx>pad.l&&xx<w-pad.r){ctx.strokeStyle='#cbd4dd';ctx.setLineDash([4,4]);ctx.beginPath();ctx.moveTo(xx,pad.t);ctx.lineTo(xx,pad.t+ph);ctx.stroke();ctx.setLineDash([]);ctx.font='9px Segoe UI';ctx.textAlign='left';ctx.fillStyle='#9a94bf';ctx.fillText('ПРОГНОЗ →',xx+8,pad.t+6);}}
    this.series.forEach(s=>{ctx.beginPath();ctx.strokeStyle=s.color;ctx.lineWidth=s.width||2;ctx.setLineDash(s.dash||[]);let previous=null;rows.forEach(r=>{const v=r[s.key],t=+new Date(r.timestamp_utc);if(v==null||!Number.isFinite(v)){previous=null;return;}if(previous==null||t-previous>3700000)ctx.moveTo(x(t),y(v));else ctx.lineTo(x(t),y(v));previous=t;});ctx.stroke();ctx.setLineDash([]);});
    if(this.hover!=null){const r=rows[this.hover];const xx=x(times[this.hover]);ctx.beginPath();ctx.moveTo(xx,pad.t);ctx.lineTo(xx,pad.t+ph);ctx.strokeStyle=textColor;ctx.lineWidth=1;ctx.setLineDash([3,3]);ctx.stroke();ctx.setLineDash([]);this.series.forEach(s=>{if(r[s.key]!=null){ctx.beginPath();ctx.arc(xx,y(r[s.key]),3.5,0,2*Math.PI);ctx.fillStyle=s.color;ctx.fill();ctx.strokeStyle=surfaceColor;ctx.lineWidth=1.5;ctx.stroke();}});}
  }
  move(event){if(!this.geom)return;const {times,x,w}=this.geom;const px=event.offsetX;let best=0;for(let i=1;i<times.length;i++)if(Math.abs(x(times[i])-px)<Math.abs(x(times[best])-px))best=i;this.hover=best;const r=this.rows[best];this.tooltip.innerHTML=`<div>${esc(fullDate(r.timestamp_utc))}</div>`+this.series.filter(s=>r[s.key]!=null).map(s=>`<div>${esc(s.label)}: <b>${num(r[s.key],2)} ${esc(this.options.unit||'ГВт')}</b></div>`).join('');this.tooltip.classList.remove('hidden');this.tooltip.style.left=`${Math.min(w-this.tooltip.offsetWidth-5,Math.max(0,px+13))}px`;this.tooltip.style.top='9px';this.draw();}
}

function showView(name){state.view=name;document.querySelectorAll('.nav-item').forEach(b=>b.classList.toggle('active',b.dataset.view===name));document.querySelectorAll('.view').forEach(v=>v.classList.toggle('hidden',!state.ready||v.id!==`view-${name}`));const [eyebrow,title,subtitle,breadcrumb]=views[name];$('eyebrow').textContent=eyebrow;$('page-title').textContent=title;$('page-subtitle').textContent=subtitle;$('breadcrumb').textContent=breadcrumb;location.hash=name;requestAnimationFrame(()=>state.charts.forEach(c=>c.draw()));if(state.ready&&name==='operations')loadOperations().catch(e=>error(e.message));if(state.ready&&name==='forecast')loadForecast().catch(e=>error(e.message));}

function forecastRows(data){return data.map(r=>({...r,prediction:r.prediction_mw/1000,baseline:r.baseline_mw/1000,lower:r.lower_mw/1000,upper:r.upper_mw/1000}));}

async function loadOverview(){
  const token=Symbol();state.overviewToken=token;
  const hours=state.overviewHours, model=state.overviewModel;
  $('overview-panel').setAttribute('aria-busy','true');
  $('overview-status').textContent='Обновляем график…';
  try {
    const forecastRequest=api('/api/forecast?'+new URLSearchParams({hours,model}));
    const [history, forecast, week] = await Promise.all([api(`/api/history?days=${state.days}`),forecastRequest,hours===168?forecastRequest:api('/api/forecast?hours=168')]);
    // A slow response for an older selection must not replace the user's latest choice.
    if(state.overviewToken!==token)return;
    state.overviewForecast=forecast;
    $('overview-forecast-label').textContent=`${forecast.meta.model_label} · ${hours} ч`;
    $('kpi-peak-label').textContent=`Ожидаемый пик · ${hours} ч`;
    $('kpi-energy-label').textContent=`Энергия · следующие ${hours} ч`;
    $('temperature-period').textContent=`на следующие ${hours} ч`;
    $('insights-period').textContent=`Поддержка планирования на следующие ${hours} ч`;
    $('overview-status').textContent=`${forecast.meta.model_label} · ${date(forecast.data[0].timestamp_utc)} — ${date(forecast.data.at(-1).timestamp_utc)}`;
    $('actual-date').textContent=fullDate(history.summary.last_actual);
    $('kpi-load').innerHTML=`${num(history.summary.latest_mw/1000)}<small>ГВт</small>`;
    $('kpi-load-date').textContent=date(history.summary.last_actual);
    $('kpi-peak').innerHTML=`${num(forecast.summary.peak_mw/1000)}<small>ГВт</small>`;
    $('kpi-peak-date').textContent=date(forecast.summary.peak_at);
    $('kpi-energy').innerHTML=`${num(forecast.summary.energy_mwh/1000,0)}<small>ГВт·ч</small>`;
    $('kpi-error').innerHTML=`${num(forecast.meta.metrics.mape_pct,2)}<small>%</small>`;
    $('temperature').textContent=`${num(forecast.summary.temperature_mean)} °C`;
    const rows=[...history.data.map(r=>({timestamp_utc:r.timestamp_utc,actual:r.load_mean_mw==null?null:r.load_mean_mw/1000})),...forecastRows(forecast.data)].sort((a,b)=>+new Date(a.timestamp_utc)-+new Date(b.timestamp_utc));
    new Chart('overview-chart',rows,[{key:'actual',color:'#66c5b6',label:'Факт'},{key:'prediction',color:'#b0a4ee',label:'Прогноз',dash:[5,4]}],{band:true,boundary:forecast.data[0].timestamp_utc});
    new Chart('weather-chart',forecast.data,[{key:'temperature_c',color:'#d7a052',label:'Температура',width:2}],{unit:'°C',negative:true});
    const warnings=forecast.meta.warnings.filter(w=>!w.startsWith('90%'));
    const weatherNote=model==='boosting'?'Температура, ветер и солнечная энергия используются вместе с календарём и историей нагрузки.':'Недельный профиль использует историю нагрузки. Погода показана для справки и не влияет на эту модель.';
    $('insights').innerHTML=`<div class="insight"><div class="insight-mark">↗</div><div><h3>Пик спроса — ${esc(date(forecast.summary.peak_at))}</h3><p>Ожидается ${num(forecast.summary.peak_mw/1000)} ГВт. Учитывайте верхнюю границу интервала при оценке потребности в мощности.</p></div></div><div class="insight violet"><div class="insight-mark">◈</div><div><h3>Погодный прогноз доступен для ${num(forecast.summary.weather_coverage_pct,0)}% часов</h3><p>${weatherNote}</p></div></div><div class="insight amber"><div class="insight-mark">${warnings.length?'!':'✓'}</div><div><h3>${warnings.length?'Условия использования прогноза':'Интервал неопределённости'}</h3><p>${esc(warnings[0]||'Диапазон отражает прошлые ошибки модели. Это не гарантированные границы будущей нагрузки.')}</p></div></div>`;
    const days = new Map(); week.data.forEach(r=>{const key=r.datetime_local.slice(0,10);if(!days.has(key))days.set(key,r);});
    $('week-calendar').innerHTML=[...days.values()].slice(0,7).map(r=>{const dt=new Date(r.timestamp_utc),weekday=new Intl.DateTimeFormat('en-US',{timeZone:'Europe/Berlin',weekday:'short'}).format(dt),weekend=['Sat','Sun'].includes(weekday),holiday=!!r.holiday_name,css=holiday?'holiday':weekend?'weekend':'';return `<div class="day-card ${css}" title="${esc(r.holiday_name||'Доля земель с рабочим днём: '+num(r.workday_fraction*100,0)+'%')}"><div class="day-name">${esc(new Intl.DateTimeFormat('ru-RU',{timeZone:'Europe/Berlin',weekday:'short'}).format(dt).toUpperCase())}</div><div class="day-date">${esc(new Intl.DateTimeFormat('ru-RU',{timeZone:'Europe/Berlin',day:'2-digit',month:'short'}).format(dt))}</div><div class="day-type">${holiday?'Праздник':weekend?'Выходной':'Рабочий день'}</div><div class="day-bar"><i style="width:${Math.round(r.workday_fraction*100)}%"></i></div></div>`;}).join('');
  } catch(e) {
    if(state.overviewToken!==token)return;
    $('overview-status').textContent='Не удалось обновить график. Показаны предыдущие данные; повторите выбор параметров.';
    throw e;
  } finally {
    if(state.overviewToken===token)$('overview-panel').setAttribute('aria-busy','false');
  }
}

async function loadForecast(){
  const token=Symbol();state.forecastToken=token;
  const data=await api('/api/forecast?'+query());if(state.forecastToken!==token)return;
  $('forecast-issued').textContent=`${data.meta.model_label} · рассчитан ${date(data.meta.issued_at)}`;
  $('forecast-range').textContent=`${date(data.data[0].timestamp_utc)} — ${date(data.data.at(-1).timestamp_utc)} · время Германии`;
  $('forecast-export').href='/api/export/forecast?'+query();
  $('scenario-peak').textContent=num(data.summary.peak_mw/1000)+' ГВт';
  $('scenario-energy').textContent=num(data.summary.energy_mwh/1000,0)+' ГВт·ч';
  new Chart('forecast-chart',forecastRows(data.data),[{key:'baseline',color:'#b8c1cc',label:'Недельный профиль',width:1.5,dash:[4,4]},{key:'prediction',color:'#b0a4ee',label:'Прогноз'}],{band:true});
  $('peak-table').innerHTML=[...data.data].sort((a,b)=>b.prediction_mw-a.prediction_mw).slice(0,5).map(r=>`<tr><td>${esc(date(r.timestamp_utc))}</td><td><strong>${num(r.prediction_mw/1000,2)}</strong></td><td>${num(r.lower_mw/1000,2)}–${num(r.upper_mw/1000,2)}</td><td>${num(r.temperature_c)} °C</td><td>${num(r.workday_fraction*100,0)}%</td></tr>`).join('');
  $('forecast-notes').innerHTML=data.meta.warnings.map(w=>`<div class="note">${esc(w)}</div>`).join('');
}

async function loadModels(){
  const report=await api('/api/models');
  $('model-cards').innerHTML=Object.entries(report.models).map(([name,m])=>`<article class="panel model-card ${name===report.recommended_model?'recommended':''}"><div class="model-card-top"><h2>${labels[name]}</h2><span class="badge ${name===report.recommended_model?'ok':'cache'}">${name===report.recommended_model?'Лучшая MAE на тесте':'Модель сравнения'}</span></div><p>${name==='boosting'?'Погода + календарь + историческая нагрузка':'Нагрузка в тот же час предыдущей недели'}</p><div class="model-numbers"><div class="model-number"><strong>${num(m.mae_mw,0)}</strong><small>MAE · МВт</small></div><div class="model-number"><strong>${num(m.rmse_mw,0)}</strong><small>RMSE · МВт</small></div><div class="model-number"><strong>${num(m.mape_pct,2)}%</strong><small>MAPE</small></div></div><div class="coverage">Фактическое покрытие 90% интервала: <b>${num(m.coverage_90_pct)}%</b> · ${m.n} часов</div></article>`).join('');
  $('horizon-table').innerHTML=report.horizons.map(r=>`<tr><td>${labels[r.model]}</td><td>${r.from}–${r.to} ч</td><td>${num(r.mae_mw,0)}</td><td>${num(r.rmse_mw,0)}</td><td><strong>${num(r.mape_pct,2)}%</strong></td><td>${r.n}</td></tr>`).join('');
  $('validation-protocol').innerHTML=`<dt>Обучение проверяемой модели</dt><dd>${esc(fullDate(report.train_start))} — ${esc(fullDate(report.train_end))}</dd><dt>Независимый тест</dt><dd>${esc(fullDate(report.test_start))} — ${esc(fullDate(report.test_end))}</dd><dt>Калибровка интервала</dt><dd>${report.calibration_hours} часов перед независимым тестом</dd><dt>Погода в проверочном периоде</dt><dd>${esc(report.weather_note)} Доля часов с архивным прогнозом: ${num(report.weather_forecast_fraction*100,0)}%.</dd><dt>Итоговая модель</dt><dd>${num(report.training_rows,0)} наблюдений; история до ${esc(fullDate(report.production_train_end))}</dd><dt>Версия</dt><dd>${esc(report.version)} · ${esc(report.model_sha256.slice(0,16))}</dd>`;
}
function badge(status){const words={ok:'Успешно',error:'Ошибка',partial:'Частично',running:'Выполняется',warning:'Внимание',cache:'Кэш',not_modified:'Без изменений'};return `<span class="badge ${esc(status)}">${esc(words[status]||status)}</span>`;}

async function loadOperations(){
  const data=await api('/api/operations'); const sets=Object.fromEntries(data.datasets.map(d=>[d.dataset,d]));
  $('source-cards').innerHTML=[['energy','Energy-Charts','Электрическая нагрузка · REST / JSON','ϟ'],['weather','Deutscher Wetterdienst','Наблюдения ZIP / CSV · прогноз KMZ / XML','☀'],['calendar','Производственный календарь','Feiertage API + локальный календарь','▦']].map(([key,name,desc,icon])=>{const item=sets[key];return `<article class="source-card"><header><span class="soft-icon">${icon}</span><span class="source-tag">${item?'Данные загружены':key==='calendar'?'Локальный расчёт':'Нет данных'}</span></header><h2>${name}</h2><p>${desc}</p><strong>${item?num(item.rows,0):'Автономно'}</strong><small>${item?'Записей · обновлено '+esc(date(item.updated_at)):'Национальные праздники и 16 земель'}</small></article>`;}).join('');
  const warnings=data.quality.filter(c=>c.status!=='ok').length;
  $('quality-badge').textContent=warnings?`${warnings} предупрежд.`:'Проверки пройдены';
  $('quality-grid').innerHTML=data.quality.map(c=>`<div class="quality-item ${esc(c.status)}"><span class="status-mark">${c.status==='ok'?'✓':'!'}</span><div><h3>${esc(c.name)}</h3><strong>${num(c.value,1)}</strong><p>${esc(c.detail)}</p></div></div>`).join('');
  $('runs-table').innerHTML=data.runs.map(r=>`<tr><td>${esc(date(r.started_at))}</td><td>${r.kind==='refresh'?'Загрузка и прогноз':'Обработка и обучение'}</td><td>${badge(r.status)}</td><td>${esc(date(r.finished_at))}</td><td title="${esc(r.message)}">${esc((r.message||'Конвейер выполнен').slice(0,180))}</td></tr>`).join('');
  $('schedule-label').textContent=data.refresh_minutes>0?`Автообновление: каждые ${data.refresh_minutes/60} ч`:'Автообновление выключено';
  $('versions-count').textContent=`История исправлений: ${num(data.history_count,0)}`;
  $('journal-table').innerHTML=data.journal.map(r=>`<tr><td>${esc(date(r.at))}</td><td>${esc(r.source)}</td><td>${esc(r.stage)}</td><td>${badge(r.status)}</td><td title="${esc(r.url)}">${esc(r.message||r.url)}</td></tr>`).join('');
}

async function loadAll(){error('');await Promise.all([loadOverview(),loadModels()]);if(state.view==='forecast')await loadForecast();if(state.view==='operations')await loadOperations();requestAnimationFrame(()=>state.charts.forEach(c=>c.draw()));}
async function poll(){
  try{
    const data=await api('/api/status');
    $('job-banner').classList.toggle('hidden',!data.running);$('job-message').textContent=data.message;
    $('refresh').disabled=data.running;$('retrain').disabled=data.running;
    if(data.error)error(data.error);
    if(data.ready&&!state.ready){state.ready=true;$('loading').classList.add('hidden');showView(state.view);await loadAll();}
    else if(state.busy&&!data.running&&data.ready){await loadAll();toast(data.message);}
    if(!data.ready&&!data.running&&data.error)$('loading').querySelector('p').textContent='Проверьте сообщение об ошибке и повторите загрузку кнопкой «Обновить данные».';
    state.busy=data.running;
  }catch(e){error('Не удалось связаться с локальным сервисом: '+e.message);}
}
async function startJob(kind){try{await api(`/api/jobs/${kind}`,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});state.busy=true;toast(kind==='refresh'?'Загрузка источников запущена':'Переобучение запущено');await poll();}catch(e){error(e.message);}}

document.querySelectorAll('.nav-item').forEach(b=>b.addEventListener('click',()=>showView(b.dataset.view)));
$('refresh').addEventListener('click',()=>startJob('refresh'));
$('retrain').addEventListener('click',()=>startJob('train'));
$('history-controls').addEventListener('click',async event=>{const b=event.target.closest('[data-days]');if(!b)return;state.days=Number(b.dataset.days);$('history-controls').querySelectorAll('button').forEach(x=>x.classList.toggle('selected',x===b));try{await loadOverview();}catch(e){error(e.message);}});
for(const id of ['overview-model','overview-horizon']){
  $(id).addEventListener('change',async()=>{
    state.overviewModel=$('overview-model').value;
    state.overviewHours=Number($('overview-horizon').value);
    error('');
    try{await loadOverview();}catch(e){error(e.message);}
  });
}
$('horizon-controls').addEventListener('click',async event=>{const b=event.target.closest('[data-hours]');if(!b)return;state.hours=Number(b.dataset.hours);$('horizon-controls').querySelectorAll('button').forEach(x=>x.classList.toggle('selected',x===b));try{await loadForecast();}catch(e){error(e.message);}});
$('temperature-delta').addEventListener('input',event=>{$('delta-value').textContent=(Number(event.target.value)>0?'+':'')+event.target.value+' °C';});
$('apply-scenario').addEventListener('click',async()=>{state.model=$('model-select').value;state.delta=Number($('temperature-delta').value);$('apply-scenario').disabled=true;try{await loadForecast();toast('Сценарий рассчитан');}catch(e){error(e.message);}finally{$('apply-scenario').disabled=false;}});
$('reset-scenario').addEventListener('click',()=>{$('model-select').value='boosting';$('temperature-delta').value='0';$('delta-value').textContent='0 °C';state.model='boosting';state.delta=0;loadForecast().catch(e=>error(e.message));});
$('lineage-button').addEventListener('click',async()=>{try{const data=await api('/api/lineage');const originals=data.origin.upstream||[data.origin];$('lineage-content').innerHTML=`<div class="lineage-steps">${data.steps.map((s,i)=>`<div class="lineage-step"><b>0${i+1}</b><p>${esc(s)}</p></div>`).join('')}</div><p>Выбранный час: <b>${esc(fullDate(data.origin.timestamp_utc))}</b></p><p>SHA-256 записи / импортированного файла: <code>${esc(data.origin.raw_sha256)}</code></p>`+originals.slice(0,4).map(r=>`<p>Источник ${esc(date(r.timestamp_utc))}<br><code>${esc(r.raw_sha256)}</code><br><code>${esc(r.source_url)}</code><br><a href="/api/raw/${encodeURIComponent(r.raw_sha256)}">Скачать исходный ответ ↗</a></p>`).join('');}catch(e){error(e.message);}});
let resizeTimer;window.addEventListener('resize',()=>{clearTimeout(resizeTimer);resizeTimer=setTimeout(()=>state.charts.forEach(c=>c.draw()),100);});
window.addEventListener('hashchange',()=>{const v=location.hash.slice(1);if(views[v]&&v!==state.view)showView(v);});
if(views[location.hash.slice(1)])state.view=location.hash.slice(1);
showView(state.view);poll();setInterval(poll,4000);
