/* Shared-source search UI. No source requests are made by the browser itself. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const all = selector => [...document.querySelectorAll(selector)];
  const number = value => new Intl.NumberFormat('ru-RU').format(value);
  const csrf = document.querySelector('[name=csrfmiddlewaretoken]').value;
  const state = {id:null, feed:false, platform:'all', sort:'newest', page:1, pages:1, counts:null, timer:null, serial:0, busy:false, data:null};
  const catalogue = new Map(), pendingCatalogue = new Map();
  const filterConfig = JSON.parse($('filter-config').textContent);
  const filterFields = filterConfig.map(field=>field.name);
  const selectedValues = field => all(`[data-filter="${field}"]:checked`).map(n=>n.value);
  function applyOptions(filters){
    for(const field of filterConfig){
      const values=(filters[field.name]||[]).map(value=>field.aliases[value]||value);
      all(`[data-filter="${field.name}"]`).forEach(n=>n.checked=values.includes(n.value));
      if(field.name==='color')$('color-custom').value=values.filter(value=>!field.options.some(([key])=>key===value)).join(', ');
    }
    updateAdvancedCount();
  }
  function updateAdvancedCount(){
    let count=filterFields.filter(field=>selectedValues(field).length || field==='color'&&$('color-custom').value).length;
    for(const name of ['mileage','owners'])if($(name+'-min').value||$(name+'-max').value)count++;
    if($('variant').value)count++;
    if(all('[name=source]:checked').length!==3)count++;
    $('advanced-count').hidden=!count; $('advanced-count').textContent=count;
  }
  function clearAdvanced(){
    all('[data-filter]').forEach(n=>n.checked=false);
    all('[name=source]').forEach(n=>n.checked=true);
    for(const id of ['mileage-min','mileage-max','owners-min','owners-max','color-custom','variant'])$(id).value='';
    updateAdvancedCount();
  }
  $('open-advanced').addEventListener('click',()=>$('advanced-filters').showModal());
  $('close-advanced').addEventListener('click',()=>$('advanced-filters').close());
  $('clear-advanced').addEventListener('click',clearAdvanced);
  $('advanced-filters').addEventListener('input',updateAdvancedCount);
  $('advanced-filters').addEventListener('click',event=>{if(event.target===$('advanced-filters')){const r=event.target.getBoundingClientRect();if(event.clientX<r.left||event.clientX>r.right||event.clientY<r.top||event.clientY>r.bottom)event.target.close();}});
  let galleryCar = null, galleryIndex = 0, generationSerial = 0;
  const element = (tag, className, text) => {const node=document.createElement(tag); if(className)node.className=className; if(text!=null)node.textContent=text; return node;};

  async function api(url, data, signal) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 35000);
    const abort = () => controller.abort();
    signal?.addEventListener('abort', abort, {once:true});
    try {
      const response = await fetch(url, {method:data === undefined ? 'GET':'POST', credentials:'same-origin', signal:controller.signal,
        headers:data === undefined ? {}:{'Content-Type':'application/json','X-CSRFToken':csrf}, body:data === undefined ? undefined:JSON.stringify(data)});
      let value;
      try {value=await response.json();} catch {throw new Error('Сервис временно недоступен. Повторите запрос.');}
      if(!response.ok) {const error=new Error(value.error || 'Не удалось выполнить запрос.'); error.fields=value.errors; error.status=response.status; throw error;}
      return value;
    } finally {clearTimeout(timeout); signal?.removeEventListener('abort', abort);}
  }
  function showError(error) {
    const banner=$('error-banner'); banner.hidden=false;
    banner.textContent = error.name === 'AbortError' ? 'Запрос занял слишком много времени. Попробуйте ещё раз.' : error.message;
    all('.invalid').forEach(n=>n.classList.remove('invalid'));
    if(error.fields) {
      const labels={make_id:'make-label',model_id:'model-label',generation_ids:'generation-details',city:'city-label',radius_km:'radius',mileage_max:'mileage-max'};
      const messages=[];
      for(const [key,values] of Object.entries(error.fields)) {
        (labels[key] ? $(labels[key]):$(key.replaceAll('_','-')))?.classList.add('invalid');
        messages.push(...values);
      }
      if(messages.length)banner.textContent=messages.join(' ');
    }
    $('advanced-error').textContent=banner.textContent;
    $('advanced-error').hidden=!$('advanced-filters').open;
  }
  function clearError(){ $('error-banner').hidden=true; $('advanced-error').hidden=true; all('.invalid').forEach(n=>n.classList.remove('invalid')); }
  function setBusy(value){
    state.busy=value;
    for(const id of ['quick-submit','filter-submit','advanced-submit']){$(id).disabled=value;$(id).setAttribute('aria-busy',value);}
    $('results').setAttribute('aria-busy',value);
  }
  function toast(message){const node=element('div','toast',message); $('toasts').append(node); while($('toasts').children.length>3)$('toasts').firstChild.remove(); setTimeout(()=>node.remove(),6500);}
  async function options(parent=null,q='') {
    const key=`${parent||''}:${q}`; const saved=catalogue.get(key);
    if(saved && Date.now()-saved.at<300000)return saved.rows;
    if(pendingCatalogue.has(key))return pendingCatalogue.get(key);
    const params=new URLSearchParams({q}); if(parent)params.set('parent',parent);
    const task=api('/catalog/?'+params).then(value=>{catalogue.set(key,{rows:value.options,at:Date.now()}); return value.options;}).finally(()=>pendingCatalogue.delete(key));
    pendingCatalogue.set(key,task); return task;
  }
  function setRadius(value){$('radius').value=value||0; $('radius-value').textContent=Number(value)?`+${number(value)} км`:'Только город'; all('[data-radius]').forEach(n=>n.classList.toggle('selected',Number(n.dataset.radius)===Number(value||0)));}
  $('radius').addEventListener('input',e=>setRadius(e.target.value));
  all('[data-radius]').forEach(n=>n.addEventListener('click',()=>setRadius(n.dataset.radius)));
  function clearGenerations(){generationSerial++; $('generation-details').open=false; $('generations').replaceChildren(element('p','field-note','Сначала выберите модель.')); $('generation-count').textContent='Любые'; $('generation-hint').value='';}
  async function selectMake(option){$('make-id').value=option.id; $('make-label').value=option.name; $('model-id').value=''; $('model-label').value=''; $('model-label').disabled=false; $('model-label').placeholder='Например: X5, камри…'; $('variant').value=''; clearGenerations();}
  async function selectModel(option){$('model-id').value=option.id; $('model-label').value=option.name; $('variant').value=''; $('generation-hint').value=''; loadGenerations(option.id);}
  function selectCity(option){$('city-id').value=option.id; $('city-label').value=option.name; $('city-region').textContent=option.region||'Можно добавить радиус ниже';}
  function updateGenerationCount(){const count=all('#generations input:checked').length; $('generation-count').textContent=count ? `Выбрано: ${count}`:($('generation-hint').value||'Любые');}
  async function loadGenerations(modelId, keys=[]) {
    const serial=++generationSerial;
    $('generations').replaceChildren(element('p','field-note','Загружаем поколения…'));
    try {
      const rows=await options(modelId);
      if(serial!==generationSerial || String(modelId)!==$('model-id').value)return;
      $('generations').replaceChildren();
      if(!rows.length)$('generations').append(element('p','field-note','В каталоге пока нет поколений. Можно искать по годам.'));
      for(const option of rows){const label=element('label','generation-option'); const input=document.createElement('input'); input.type='checkbox'; input.value=option.id; input.dataset.key=option.key; input.checked=keys.includes(option.key); input.addEventListener('change',()=>{$('generation-hint').value=''; updateGenerationCount();}); label.append(input,element('span','',option.name)); $('generations').append(label);}
      updateGenerationCount();
    } catch(error){if(serial===generationSerial)$('generations').replaceChildren(element('p','field-note',error.message+' Можно искать по годам.'));}
  }

  function autocomplete(kind, getRows, onSelect, onEdit) {
    const input=$(kind+'-label'), panel=$(kind+'-options'); let timer, serial=0, selected=-1, buttons=[], rows=[];
    const close=()=>{panel.hidden=true; input.setAttribute('aria-expanded','false'); input.removeAttribute('aria-activedescendant');};
    async function load(){const request=++serial; panel.hidden=false; input.setAttribute('aria-expanded','true'); panel.replaceChildren(element('div','suggestion-note','Загружаем варианты…'));
      try{rows=await getRows(input.value); if(request!==serial)return; panel.replaceChildren(); selected=-1; buttons=[];
        if(!rows.length)panel.append(element('div','suggestion-note','Ничего не найдено. Уточните название.'));
        rows.forEach((row,index)=>{const button=element('button','suggestion',row.name); button.type='button'; button.id=`${kind}-option-${index}`; button.setAttribute('role','option'); if(row.region)button.append(element('small','',row.region)); button.addEventListener('click',async()=>{close(); await onSelect(row);}); panel.append(button); buttons.push(button);});
      }catch(error){if(request===serial)panel.replaceChildren(element('div','suggestion-note',error.message));}}
    input.addEventListener('focus',load);
    input.addEventListener('input',()=>{serial++; onEdit(); clearTimeout(timer); timer=setTimeout(load,200);});
    input.addEventListener('keydown',async e=>{
      if(e.key==='Escape'){serial++; close(); return;}
      if(e.key==='ArrowDown'||e.key==='ArrowUp'){e.preventDefault(); if(panel.hidden){await load();} if(!buttons.length)return; selected=(selected+(e.key==='ArrowDown'?1:-1)+buttons.length)%buttons.length; buttons.forEach((b,i)=>{b.classList.toggle('selected',i===selected); b.setAttribute('aria-selected',i===selected);}); input.setAttribute('aria-activedescendant',buttons[selected].id); buttons[selected].scrollIntoView({block:'nearest'});}
      if(e.key==='Enter' && !panel.hidden){e.preventDefault(); if(rows.length && (selected>=0 || rows.length===1)){close(); await onSelect(rows[selected>=0?selected:0]);}}
    });
    document.addEventListener('click',e=>{if(!input.parentElement.contains(e.target)){serial++; close();}});
    return close;
  }
  autocomplete('make',q=>options(null,q),selectMake,()=>{$('make-id').value=''; $('model-id').value=''; $('model-label').value=''; $('model-label').disabled=true; $('variant').value=''; clearGenerations();});
  autocomplete('model',q=>options($('make-id').value,q),selectModel,()=>{$('model-id').value=''; $('variant').value=''; clearGenerations();});
  autocomplete('city',q=>api('/api/cities/?q='+encodeURIComponent(q.replace(/\+\s*\d+\s*(км)?\s*$/i,''))).then(v=>v.options),selectCity,()=>{$('city-id').value=''; $('city-region').textContent='Можно написать Самара +200 км'; if(!$('city-label').value)setRadius(0);});

  function resetForm(){ $('filter-form').reset(); $('make-id').value=''; $('model-id').value=''; $('city-id').value=''; $('variant').value=''; $('model-label').disabled=true; $('model-label').placeholder='Сначала выберите марку'; $('city-region').textContent='Можно написать Самара +200 км'; clearGenerations(); setRadius(0); clearAdvanced(); $('query-choices').hidden=true; clearError();}
  $('reset').addEventListener('click',()=>{resetForm(); $('query').value=''; $('make-label').focus();});
  $('toggle-filters').addEventListener('click',()=>{$('filter-panel').classList.toggle('visible'); if($('filter-panel').classList.contains('visible'))$('filter-panel').scrollIntoView({behavior:'smooth',block:'start'});});

  function formData(){
    const sources=all('[name=source]:checked').map(n=>n.value);
    if(!sources.length)throw new Error('Выберите хотя бы одну площадку.');
    const data={make_id:$('make-id').value, model_id:$('model-id').value, generation_ids:all('#generations input:checked').map(n=>Number(n.value)),
      city:$('city-label').value,city_id:$('city-id').value,radius_km:Number($('radius').value),platform:sources.length===3?[]:sources,variant:$('variant').value,generation_hint:$('generation-hint').value};
    for(const key of ['price_min','price_max','year_min','year_max','mileage_min','mileage_max','owners_min','owners_max','color_custom'])data[key]=$(key.replaceAll('_','-')).value;
    for(const key of filterFields)data[key]=selectedValues(key);
    return data;
  }
  async function resolveTypedSelection(kind,parent) {
    if($(kind+'-id').value || !$(kind+'-label').value.trim())return;
    const query=$(kind+'-label').value.trim(),rows=await options(parent,query);
    const exact=rows.find(n=>n.name.toLocaleLowerCase('ru')===query.toLocaleLowerCase('ru'));
    const option=exact || (rows.length===1?rows[0]:null);
    if(!option)throw new Error(`Выберите ${kind==='make'?'марку':'модель'} из подсказок.`);
    if(kind==='make')await selectMake(option); else await selectModel(option);
  }
  async function runSearch(){clearError(); setBusy(true); clearTimeout(state.timer); state.serial++;
    try{
      await resolveTypedSelection('make',null); await resolveTypedSelection('model',$('make-id').value);
      const data=await api('/api/searches/',formData());
      state.id=data.search_id; state.feed=false; state.page=1; state.platform='all'; state.sort='newest'; state.counts=null; $('sort').value='newest';
      all('[data-platform]').forEach(n=>{n.classList.toggle('active',n.dataset.platform==='all'); n.setAttribute('aria-pressed',n.dataset.platform==='all');});
      if(data.filters.location?.id)selectCity(data.filters.location);
      setRadius(data.filters.radius_km);
      applyRange('price',data.filters.price); applyRange('year',data.filters.year);
      applyRange('mileage',data.filters.mileage); applyRange('owners',data.filters.owners);
      applyOptions(data.filters); updateAdvancedCount();
      render(data); $('filter-panel').classList.remove('visible'); $('advanced-filters').close(); schedule();
    }catch(error){showError(error); schedule();}finally{setBusy(false);}
  }
  $('filter-form').addEventListener('submit',e=>{e.preventDefault(); if(!state.busy)runSearch();});

  function applyRange(field,value){const parts=(value||'').split('-'); if(parts.length===1)parts.unshift(''); for(const [index,suffix] of ['min','max'].entries())$(field+'-'+suffix).value=parts[index]?(field==='price'||field==='mileage'?number(Number(parts[index])):parts[index]):'';}
  async function populateResolved(value, model=null, city=null) {
    resetForm();
    await selectMake({id:model?.make_id||value.make_id,name:model?.make_name||value.make_name});
    if(model)await selectModel(model);
    const extras=value.extra;
    applyRange('year',value.year); applyRange('price',extras.price);
    applyRange('mileage',extras.mileage); applyRange('owners',extras.owners);
    $('variant').value=value.variant||''; $('generation-hint').value=extras.generation_hint||'';
    applyOptions(extras);
    const location=city||extras.location; if(location?.id)selectCity(location);
    setRadius(extras.radius_km||0);
    const sources=extras.platform?.length?extras.platform:['avito','autoru','drom']; all('[name=source]').forEach(n=>n.checked=sources.includes(n.value)); updateAdvancedCount();
    if(model && extras.generation_hint){$('generation-count').textContent=extras.generation_hint;}
  }
  async function runQuery(){clearError(); setBusy(true); $('query-choices').hidden=true;
    try{
      const resolved=await api('/api/query/',{text:$('query').value});
      let model=resolved.models.length===1&&!resolved.needs_confirmation?resolved.models[0]:null;
      let city=null;
      async function next(){
        const panel=$('query-choices'); panel.replaceChildren(); panel.hidden=false;
        if((resolved.models.length && !model)){
          panel.append(element('p','','Уточните модель автомобиля:'));
          for(const option of resolved.models){const b=element('button','',option.name); b.type='button'; b.addEventListener('click',()=>{model=option; next().catch(showError);}); panel.append(b);} return;
        }
        if(resolved.extra.city_choices?.length&&!city){panel.append(element('p','','Уточните город и регион:')); for(const option of resolved.extra.city_choices){const b=element('button','',`${option.name} · ${option.region}`); b.type='button'; b.addEventListener('click',()=>{city=option; next().catch(showError);}); panel.append(b);} return;}
        panel.hidden=true; setBusy(true);
        try{await populateResolved(resolved,model,city); await runSearch();}finally{setBusy(false);}
      }
      await next();
    }catch(error){showError(error);}finally{setBusy(false);}
  }
  $('quick-form').addEventListener('submit',e=>{e.preventDefault(); if(!state.busy)runQuery();});
  all('[data-query]').forEach(n=>n.addEventListener('click',()=>{$('query').value=n.dataset.query; if(!state.busy)runQuery();}));
  function showPhoto(img, placeholder, url){
    img.hidden=false; placeholder.hidden=true;
    img.onerror=()=>{img.hidden=true; placeholder.hidden=false;};
    if(url)img.src=url; else {img.removeAttribute('src'); img.hidden=true; placeholder.hidden=false;}
  }
  function render(data) {
    state.data=data; state.page=data.page; state.pages=data.pages;
    const lines=data.summary.split('\n');
    $('results-title').textContent=lines[0];
    $('result-summary').textContent=lines.slice(1).map(line=>line.startsWith('Год:')?line.replace(/(\d) (?=\d)/g,'$1'):line).join(' · ') || `${number(data.total_all)} объявлений в общей ленте`;
    $('count-all').textContent=number(data.total_all);
    for(const source of ['avito','autoru','drom']){
      $('count-'+source).textContent=number(data.counts[source]);
      const added=data.counts[source]-(state.counts?.[source]??data.counts[source]);
      if(added>0)toast(`➕ ${data.sources.find(s=>s.id===source).name}: ${number(added)} объявлений добавлено в ленту`);
    }
    state.counts=data.counts;
    const statuses=$('source-status'); statuses.replaceChildren();
    for(const source of data.sources){const warning=Boolean(source.message); const item=element('span','source-item'+(warning?' warning':source.status==='В очереди'?' waiting':'')); item.title=source.message||source.status; item.append(element('span','tiny-dot'),document.createTextNode(`${source.name}: ${source.status}`)); statuses.append(item);}
    $('live-strip').hidden=false;
    const downloading=data.sources.some(source=>source.collecting&&!source.retry_seconds);
    const paused=data.sources.filter(source=>source.collecting&&source.retry_seconds);
    $('live-text').textContent=data.mode==='feed'?'Показываем сохранённые объявления. Для подбора задайте условия поиска.':downloading?'Сбор продолжается — новые объявления подгружаются автоматически.':paused.length?`${paused.map(source=>source.name).join(', ')}: пауза, повтор через ${Math.ceil(Math.min(...paused.map(source=>source.retry_seconds))/60)} мин.`:`${number(data.total_all)} объявлений по вашим условиям`;
    $('live-strip').querySelector('.tiny-dot').classList.toggle('pulse',downloading);
    const grid=$('results');
    // Avoid recreating photographs on each poll: update only when visible rows changed.
    const signature=JSON.stringify(data.items.map(ad=>[ad.id,ad.price,ad.photos,ad.title,ad.city,ad.mileage,ad.year,ad.engine,ad.gearbox]));
    if(grid.dataset.signature!==signature){
      grid.dataset.signature=signature; const fragment=document.createDocumentFragment();
      for(const ad of data.items){const card=$('car-template').content.firstElementChild.cloneNode(true);
        const photo=card.querySelector('.car-photo'),title=card.querySelector('.car-title'); photo.href=ad.detail_url; title.href=ad.detail_url; title.textContent=ad.title; title.title=ad.title;
        const img=card.querySelector('img'); img.alt=ad.title; showPhoto(img,card.querySelector('.no-photo'),ad.photos[0]);
        card.querySelector('.platform-badge').textContent=ad.platform_name;
        card.querySelector('.car-price').textContent=number(ad.price)+' ₽';
        const specs=[]; if(ad.year)specs.push(ad.year+' г.'); if(ad.mileage!=null)specs.push(number(ad.mileage)+' км'); if(ad.engine)specs.push(ad.engine); if(ad.gearbox)specs.push(ad.gearbox);
        card.querySelector('.car-specs').textContent=specs.join(' · ');
        card.querySelector('.car-location span').textContent=ad.city||'Город не указан';
        const gallery=card.querySelector('.gallery-button'); gallery.textContent=ad.photos.length?`${ad.photos.length} фото`:'Нет фото'; gallery.disabled=!ad.photos.length; gallery.addEventListener('click',()=>openGallery(ad));
        const link=card.querySelector('.source-link'); if(ad.url)link.href=ad.url; else link.hidden=true;
        fragment.append(card);
      }
      grid.replaceChildren(fragment);
    }
    $('empty-state').hidden=Boolean(data.items.length);
    if(!data.items.length){$('empty-title').textContent=data.mode==='feed'?'В базе пока нет объявлений':data.collecting?'Ищем подходящие автомобили…':'Объявлений пока нет'; $('empty-text').textContent=data.mode==='feed'?'Напишите автомобиль или выберите марку, чтобы найти объявления на площадках.':data.collecting?'Первые результаты появятся автоматически. Пока можно уточнить фильтры.':'Попробуйте увеличить радиус, изменить годы или убрать часть ограничений. Проверьте статусы площадок выше.';}
    $('pagination').hidden=data.pages<=1; $('prev-page').disabled=data.page<=1; $('next-page').disabled=data.page>=data.pages;
    $('page-info').textContent=`${data.page} / ${data.pages} · ${number(data.total)} объявлений`;
  }
  function schedule(){clearTimeout(state.timer); if(state.id||state.feed)state.timer=setTimeout(poll,state.data?.sources.some(source=>source.collecting&&!source.retry_seconds)?5000:20000);}
  async function poll(){if((!state.id&&!state.feed) || state.busy || document.hidden){schedule(); return;}
    const serial=++state.serial, id=state.id;
    try{const params=new URLSearchParams({platform:state.platform,sort:state.sort,page:state.page}); const data=await api(id?`/api/searches/${id}/?${params}`:`/api/feed/?${params}`);
      if(serial===state.serial && id===state.id)render(data);
    }catch(error){if(serial===state.serial){if(error.status===404){state.id=null; showError(new Error('Поиск недоступен. Запустите его снова.'));}else{$('live-text').textContent='Связь с сервисом прервалась. Повторим загрузку автоматически.';}}}finally{schedule();}
  }
  document.addEventListener('visibilitychange',()=>{if(!document.hidden && (state.id||state.feed))poll(); else clearTimeout(state.timer);});
  window.addEventListener('pagehide',()=>{clearTimeout(state.timer); state.serial++;});
  window.addEventListener('pageshow',e=>{if(e.persisted&&(state.id||state.feed))poll();});
  all('[data-platform]').forEach(n=>n.addEventListener('click',()=>{state.platform=n.dataset.platform; state.page=1; all('[data-platform]').forEach(b=>{b.classList.toggle('active',b===n); b.setAttribute('aria-pressed',b===n);}); if(state.id||state.feed)poll();}));
  $('sort').addEventListener('change',()=>{state.sort=$('sort').value; state.page=1; if(state.id||state.feed)poll();});
  all('[data-view]').forEach(n=>n.addEventListener('click',()=>{$('results').classList.toggle('list-view',n.dataset.view==='list'); all('[data-view]').forEach(b=>{b.classList.toggle('active',b===n); b.setAttribute('aria-pressed',b===n);});}));
  $('prev-page').addEventListener('click',()=>{state.page=Math.max(1,state.page-1); poll(); $('results-title').scrollIntoView({behavior:'smooth'});});
  $('next-page').addEventListener('click',()=>{state.page=Math.min(state.pages,state.page+1); poll(); $('results-title').scrollIntoView({behavior:'smooth'});});
  $('refresh').addEventListener('click',async()=>{if(!state.id&&!state.feed)return; $('refresh').disabled=true; try{if(state.id)await api(`/api/searches/${state.id}/refresh/`,{}); await poll();}catch(error){showError(error);}finally{$('refresh').disabled=false;}});

  function galleryPhoto(){if(!galleryCar)return; $('gallery-position').textContent=`Фото ${galleryIndex+1} / ${galleryCar.photos.length}`; $('gallery-image').alt=galleryCar.title; showPhoto($('gallery-image'),$('gallery-unavailable'),galleryCar.photos[galleryIndex]); $('gallery-prev').disabled=$('gallery-next').disabled=galleryCar.photos.length<2;}
  function openGallery(ad){galleryCar=ad; galleryIndex=0; $('gallery-title').textContent=ad.title; $('gallery-price').textContent=number(ad.price)+' ₽ · '+ad.platform_name; $('gallery-source').hidden=!ad.url; if(ad.url)$('gallery-source').href=ad.url; galleryPhoto(); $('gallery').showModal();}
  function moveGallery(delta){if(!galleryCar)return; galleryIndex=(galleryIndex+delta+galleryCar.photos.length)%galleryCar.photos.length; galleryPhoto();}
  $('gallery-close').addEventListener('click',()=>$('gallery').close()); $('gallery-prev').addEventListener('click',()=>moveGallery(-1)); $('gallery-next').addEventListener('click',()=>moveGallery(1));
  $('gallery').addEventListener('click',e=>{if(e.target===$('gallery')){const r=$('gallery').getBoundingClientRect(); if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)$('gallery').close();}});
  $('gallery').addEventListener('keydown',e=>{if(e.key==='ArrowLeft')moveGallery(-1); if(e.key==='ArrowRight')moveGallery(1);});

  async function restoreForm(filters){
    const vehicle=filters.vehicle||{},make=vehicle.make,model=vehicle.model;
    if(make){let row=(await options()).find(n=>n.key===make.key); if(!row)row=(await options(null,make.name)).find(n=>n.key===make.key); if(row)await selectMake(row);}
    if(model&&$('make-id').value){let row=(await options($('make-id').value)).find(n=>n.key===model.key); if(!row)row=(await options($('make-id').value,model.name)).find(n=>n.key===model.key); if(row){await selectModel(row); await loadGenerations(row.id,filters.generation_keys||[]);}}
    applyRange('price',filters.price); applyRange('year',filters.year);
    if(filters.location?.id)selectCity(filters.location); else $('city-label').value=filters.city?.[0]||'';
    setRadius(filters.radius_km); $('variant').value=filters.variant||''; $('generation-hint').value=filters.generation_hint||'';
    applyRange('mileage',filters.mileage); applyRange('owners',filters.owners);
    applyOptions(filters);
    const sources=filters.platform?.length?filters.platform:['avito','autoru','drom']; all('[name=source]').forEach(n=>n.checked=sources.includes(n.value)); updateAdvancedCount();
  }
  const initial=JSON.parse($('initial-search').textContent);
  if(initial){state.id=initial; api(`/api/searches/${initial}/`).then(async data=>{render(data); schedule(); try{await restoreForm(data.filters);}catch{toast('Выдача восстановлена. Каталог фильтров можно загрузить заново.');}}).catch(showError);}
  else{state.feed=true; poll();}
})();
