'use strict';
const $ = id => document.getElementById(id);
const labels = {sent:'Опубликовано',ready:'Готово к доставке',review:'Нужна проверка',held:'Отложено',failed:'Ошибка',uncertain:'Исход отправки неизвестен',sending:'Отправляется',needs_reverify:'Повторная проверка',expired:'Устарело',evidence_ready:'Ждёт редакции',discovered:'Найдено',drafting:'Готовится'};
let data;
function el(tag, text, cls) {const e=document.createElement(tag);if(text!==undefined)e.textContent=String(text);if(cls)e.className=cls;return e;}
function date(value){if(!value)return 'Неизвестно'; const d=new Date(value);return Number.isNaN(d.valueOf())?'Неизвестно':d.toLocaleString('ru-RU',{timeZone:'Europe/Moscow'})+' МСК';}
function empty(parent,text){parent.append(el('div',text,'empty'));}
function link(parent,label,url){try{const u=new URL(url);if(u.protocol!=='https:'||u.username||u.password)return;const a=el('a',label);a.href=u.href;a.target='_blank';a.rel='noopener noreferrer';parent.append(a);}catch{ /* unknown URL stays plain text */ }}
function render(){
 if(!data)return;
 const city=data.cities.find(c=>c.id===$('city').value);if(!city)return;
 $('city-name').textContent=city.name;
 const available=city.runtime_status==='available';
 const stale=available&&(!city.observed_at||Date.now()-Date.parse(city.observed_at)>6*3600000);
 $('notice').textContent=!available?'Доступен реестр источников. Данные рабочей очереди не поступили: публикации, решения и вызовы моделей неизвестны.':stale?'Снимок очереди устарел. Проверьте последний прогон и загрузку данных.':'Снимок канонической очереди. Готовность материала и подтверждённая отправка показаны отдельно.';
 $('notice').className='notice'+(!available||stale?' warning':'');
 if(data.last_attempt&&data.last_attempt.status!=='success'){
  $('notice').textContent+=' Последняя попытка Actions для Белграда не завершилась успешно. Время попытки не означает обновление снимка очереди.';
  $('notice').className='notice warning';
 }
 $('stamp').textContent='Сборка витрины: '+date(data.generated_at)+' · Очередь: '+date(city.observed_at);
 $('metrics').replaceChildren();
 for(const [name,value] of [['Событий в очереди',city.total_events],['Опубликовано',city.counts?.sent],['Готово к доставке',city.counts?.ready],['Источников в реестре',city.sources.length]]){const m=el('div',undefined,'metric');m.append(el('b',value??(available&&city.counts?0:'—')),el('span',name));$('metrics').append(m);}
 const events=$('event-list');events.replaceChildren();const query=$('search').value.toLowerCase();const state=$('state').value;
 const items=(city.items||[]).filter(i=>(!state||i.state===state)&&(!query||[i.title,i.reason_code,i.source_id].join(' ').toLowerCase().includes(query)));
 $('event-count').textContent=city.items===null?'Данные недоступны':`${items.length} показано / ${city.total_events} всего`;
 if(!items.length)empty(events,city.items===null?'Нужен снимок рабочей БД. Пустой экран не означает отсутствие найденных новостей.':'Материалов по выбранному фильтру нет.');
 for(const item of items){const card=el('article',undefined,'event');const head=el('div',undefined,'event-head');head.append(el('strong',item.title||'Материал #'+item.raw_item_ids.join(', ')+' · заголовок скрыт'),el('span',labels[item.state]||item.state,'pill'));card.append(head,el('p','Источник: '+item.source_id+' · Дата: '+date(item.published_at)),el('p','Причина: '+item.reason_code));const details=el('details');details.append(el('summary','Этапы и доказательства'),el('p','Discovery-модель: '+(item.discovered_by||'Неизвестно')+' · Редактор: '+(item.editor_model||'Неизвестно')),el('p','Доставка: '+item.delivery_status+' · Telegram ID: '+(item.telegram_message_id??'Нет квитанции')));
 for(const role of ['facts','utility']){const r=item.reviews.find(v=>v.role===role);details.append(el('p',role+': '+(r?`${r.verdict} · ${r.score}/100 · источник открыт: ${r.live_source_checked===true?'да':'нет'}`:'Нет сохранённого ревью')));}details.append(el('p','Версия правил: '+(item.editorial_rule_version||'Неизвестно')+' · Изменений источника: '+item.source_revision_count));card.append(details);events.append(card);}
 $('source-list').replaceChildren();for(const s of city.sources.filter(s=>!$('platform').value||s.platform===$('platform').value)){const row=el('tr');const name=el('td');link(name,s.name,s.url);name.append(el('small',s.platform+' · '+s.id));row.append(name,el('td',s.configured_status+' / '+(s.runtime_enabled===null?'БД неизвестна':s.runtime_enabled?'Включён':'Отключён')),el('td',s.auto_publish?'По общим проверкам':s.high_risk?'High-risk review':'Автопубликация запрещена'),el('td',(s.reason||'Причина не записана')+' · '+(s.strategy||'Адаптер не подключён')));$('source-list').append(row);}
 $('ai-list').replaceChildren();if(!city.ai_usage?.length)empty($('ai-list'),city.ai_usage===null?'События моделей недоступны. Нельзя определить, что нашёл Gemini или GPT.':'В журнале AIUsageEvent нет событий. Это не доказывает отсутствие вызовов: старые прогоны могли не записывать их.');else for(const a of city.ai_usage)$('ai-list').append(el('div',`${date(a.occurred_at)} · ${a.provider} / ${a.model} · ${a.task} · ${a.status}`,'run'));
 $('cycle-info').replaceChildren();if(city.last_cycle){for(const a of city.last_cycle.ai_calls||[])$('cycle-info').append(el('div',`${date(a.occurred_at)} · ${a.provider||'Неизвестно'} / ${a.model||'Неизвестно'} · ${a.task} · ${a.status}`,'run'));$('cycle-info').append(el('p',`Последний цикл: ${city.last_cycle.status??'неизвестно'} · ${date(city.last_cycle.observed_at)}`));const j=city.last_cycle.jev; $('cycle-info').append(el('pre',j?JSON.stringify(j,null,2):'Jev: нет наблюдения для этого прогона.'));}
 const attempt=data.last_attempt;if(attempt){const row=el('p',`Actions (Белград): ${attempt.status} · ${attempt.mode} · ${date(attempt.observed_at)} · ${attempt.projection==='verified'?'Проверенная проекция':'Предыдущий снимок или данные недоступны'} · `);link(row,'Прогон #'+attempt.run_id,`https://github.com/Stolyarovmn/relocant-media/actions/runs/${attempt.run_id}/attempts/${attempt.run_attempt}`);$('cycle-info').append(row);}
 $('run-list').replaceChildren();if(!city.runs?.length)empty($('run-list'),city.runs===null?'История прогонов недоступна.':'В scheduler_run прогонов для этого города нет.');else for(const r of city.runs)$('run-list').append(el('div',`#${r.id} · ${date(r.started_at)} → ${date(r.finished_at)} · ${r.status}${r.error_type?' · '+r.error_type:''}`,'run'));
}
function accept(value){if(value.schema_version!==1||!Array.isArray(value.cities)||value.mode!=='public_read_only')throw Error('Несовместимый снимок');data=value;render();}
async function load(){try{const response=await fetch('data.json',{cache:'no-store'});if(!response.ok)throw Error('HTTP '+response.status);accept(await response.json());}catch(e){$('notice').textContent='Не удалось обновить данные. '+e.message;$('notice').className='notice warning';}}
for(const id of ['city','state','platform'])$(id).addEventListener('change',render);$('search').addEventListener('input',render);$('refresh').addEventListener('click',load);$('file').addEventListener('change',async e=>{try{accept(JSON.parse(await e.target.files[0].text()));}catch(err){$('notice').textContent='Ошибка снимка: '+err.message;}});load();setInterval(load,60000);
