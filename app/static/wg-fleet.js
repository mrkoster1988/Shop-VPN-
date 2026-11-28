'use strict';
window.pushokWgFleet = (() => {
  const states = {active:'Принимает подключения',draining:'Выводится из работы',maintenance:'Обслуживание',disabled:'Выключен'};
  const phases = {pending:'В очереди',queued:'В очереди',running:'Перенос выполняется',migrating:'Перенос выполняется',
    preparing:'Готовятся новые подключения',revoking:'Отключаются прежние подключения',waiting:'Ожидает подтверждения',waiting_clients:'Ожидает обновления клиентов',partial:'Часть подключений ожидает переноса',
    completed:'Перенос завершён',complete:'Перенос завершён',failed:'Нужен повтор',error:'Нужен повтор',cancelled:'Остановлен'};
  const messages = {
    wg_fleet_disabled:'Управление серверами пока выключено в Shop.',
    wg_fleet_management_disabled:'Управление серверами из Mini App пока выключено в Shop.',
    wg_fleet_invalid_input:'Проверьте параметры узла. Адрес панели указывается без пароля и токена в ссылке.',
    wg_fleet_changed:'Параметры уже изменились. Обновите список и повторите действие.',
    wg_fleet_version_conflict:'Параметры уже изменились. Обновите список и повторите действие.',
    wg_fleet_node_missing:'Узел больше не найден. Обновите список.',
    wg_fleet_operation_missing:'Операция переноса не найдена. Обновите список.',
    wg_fleet_node_unavailable:'Не удалось получить состояние сервера. Повторите проверку.',
    wg_fleet_not_ready:'Узел ещё не готов к выдаче подключений. Проверьте панель, DNS и настройки срока.',
    wg_fleet_no_capacity:'На подходящих узлах пока нет свободных мест.',
    wg_fleet_no_reserve:'Нет подходящего резервного узла. Добавьте и проверьте ещё один сервер.',
    wg_fleet_pending_operation:'Для узла уже выполняется изменение. Следите за его состоянием ниже.',
    wg_fleet_integration:'Shop не подтвердил доступ к управлению серверами. Проверьте подключение Mini App в настройках Shop.',
    wg_fleet_change_unconfirmed:'Shop не подтвердил результат. Обновите состояние; повтор того же запроса использует тот же номер операции.',
    wg_fleet_dns_unavailable:'Сервис DNS не подтвердил изменение. Узел ещё не готов к новым подключениям.',
    wg_fleet_dns_not_configured:'Автоматическое управление DNS пока не настроено в Shop.',
    wg_fleet_in_use:'На узле остаются подключения. Сначала выполните перенос пользователей.',
    wg_fleet_request_conflict:'Этот запрос уже использован с другими параметрами. Обновите список.',
    wg_fleet_validation_failed:'Проверка узла не завершена. Проверьте подключение к WGDashboard.',
    wg_fleet_traffic_unverified:'Работа туннеля пока не подтверждена. Проверьте реальное подключение.',
    wg_fleet_expiry_required:'Включите управление сроком пиров в настройках WGDashboard.',
    wg_fleet_remote_revoke_unconfirmed:'Прежний узел не подтвердил отключение ключей. Операция остаётся незавершённой.',
    wg_fleet_peer_preparation_failed:'Не удалось подготовить подключение на резервном узле. Проверьте его состояние.',
    wg_fleet_archived:'Этот узел архивирован. Для новой машины добавьте новый сервер.',
    wg_fleet_dns_not_ready:'DNS подключения ещё не подтверждён. Проверьте публичный IP и настройку DNS.',
    wg_fleet_policy_disabled:'Сначала включите распределение подключений в этом разделе.',
    wg_fleet_unknown_error:'Сервер не подтвердил действие. Проверьте его состояние и повторите запрос.',
    wg_fleet_role_required:'Изменять серверы может только разработчик.',
    wg_fleet_tls_required:'Для управления серверами требуется защищённое подключение Shop.'
  };
  function el(tag,text,cls){const node=document.createElement(tag);if(text!==undefined)node.textContent=text;if(cls)node.className=cls;return node;}
  async function fleetApi(path,{method='GET',body}={}){
    try {
      if(path==='/vpn/fleet')return await api('wg-fleet');
      const action=path.slice('/vpn/fleet/'.length);
      return await api('wg-fleet/commands',{method:'POST',data:{action,...body}});
    } catch(error){throw new Error(messages[aliases[error.message]||error.message]||'Shop не подтвердил действие. Обновите состояние и повторите запрос.');}
  }
  function notice(message){const node=$('wg-fleet-status');if(node)node.textContent=message;}

  let activeHost=null,activeRoute='',pollTimer=null;
  function reset(){clearTimeout(pollTimer);pollTimer=null;if(activeHost){activeHost.querySelectorAll('input[type=password]').forEach(node=>{node.value='';});activeHost.replaceChildren();}activeHost=null;}
  const staff = () => canGroup('servers_read');
  const numeric = value => typeof value === 'number' && Number.isFinite(value);
  const display = value => numeric(value) ? value.toLocaleString('ru-RU',{maximumFractionDigits:1}) : '—';
  const when = value => numeric(value) && value > 0 ? new Date(value*1000).toLocaleString('ru-RU',{
    timeZone:'Europe/Moscow',day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'})+' мск' : 'нет данных';
  const aliases={
    "NoHealthyCapacity": "wg_fleet_no_capacity",
    "RemoteRevokeUnconfirmed": "wg_fleet_remote_revoke_unconfirmed",
    "PeerPreparationFailed": "wg_fleet_peer_preparation_failed",
    "AssignmentChanged": "wg_fleet_changed",
    "Сервер не найден": "wg_fleet_node_missing",
    "Операция не найдена": "wg_fleet_operation_missing",
    "Сервер уже изменён. Обновите список.": "wg_fleet_changed",
    "Версия изменилась. Обновите список.": "wg_fleet_changed",
    "Настройки сервера изменились во время проверки.": "wg_fleet_changed",
    "Управление серверами из Mini App не настроено.": "wg_fleet_management_disabled",
    "Управление узлами доступно разработчику.": "wg_fleet_role_required",
    "Сервер архивирован. Для новой машины добавьте новый узел.": "wg_fleet_archived",
    "Сначала подтвердите DNS WireGuard.": "wg_fleet_dns_not_ready",
    "Нужны успешная проверка API и свежая проверка трафика WireGuard.": "wg_fleet_not_ready",
    "Сначала завершите перенос и отзыв прежних подключений.": "wg_fleet_pending_operation",
    "Для узла нужен WGDashboard с заданием окончания доступа и часовым поясом.": "wg_fleet_expiry_required",
    "У узла есть подключения. Для новой машины добавьте отдельный сервер.": "wg_fleet_in_use",
    "Изменение IP занятого узла выполняется через добавление и перенос.": "wg_fleet_in_use",
    "У узла есть подключения. Используйте перенос.": "wg_fleet_in_use",
    "Сначала включите управление пулом WireGuard.": "wg_fleet_policy_disabled",
    "Этот request_id уже использован для другого действия.": "wg_fleet_request_conflict",
    "Этот request_id уже использован.": "wg_fleet_request_conflict",
    "Проверьте поля узла и профиль подключения.": "wg_fleet_invalid_input",
    "Регистрация проверки доступна разработчику.": "wg_fleet_role_required",
    "Передача ключа проверки требует HTTPS.": "wg_fleet_tls_required"
  };
  const reason = code => messages[aliases[code]||code] || messages.wg_fleet_unknown_error;
  function field(label,input){const node=el('label',label);node.append(input);return node;}
  function input(value,type='text'){const node=el('input');node.type=type;node.value=value??'';return node;}
  function integer(value,min,max){const node=input(value,'number');Object.assign(node,{min:String(min),max:String(max),step:'1',required:true});return node;}
  function fact(label,value){const node=el('div');node.append(el('dt',label),el('dd',value));return node;}
  function link(label,id){const node=el('a',label,'ui-button');node.href='#'+id;node.dataset.adminTarget=id;return node;}
  function action(label,callback){const node=el('button',label,'ui-button');node.type='button';node.onclick=callback;return node;}
  function feedback(){const node=el('p',undefined,'wg-fleet-feedback');node.setAttribute('role','status');node.setAttribute('aria-live','polite');return node;}

  // Keep the id of an uncertain request until that same intent is confirmed.
  function mutation(){
    let intent=null;
    return async (kind,payload) => {
      const fingerprint=JSON.stringify([kind,payload]);
      if(!intent||intent.fingerprint!==fingerprint)intent={fingerprint,request_id:crypto.randomUUID()};
      const result=await fleetApi('/vpn/fleet/'+kind,{method:'POST',body:{request_id:intent.request_id,payload}});
      intent=null;
      return result;
    };
  }

  function connectionForm(row,done,cancel,alive){
    const form=el('form',undefined,'wg-fleet-form'),grid=el('div',undefined,'wg-fleet-form-grid');
    const name=input(row?.name||''),place=input(row?.location||''),capacity=integer(row?.capacity??500,0,1000000);
    const endpoint=input(row?.endpoint_ip||'');endpoint.maxLength=45;endpoint.placeholder='Публичный IPv4 или IPv6';
    name.required=true;name.maxLength=80;place.maxLength=80;
    grid.append(field('Название сервера',name),field('Расположение',place),field('Лимит подключений (0 — без лимита)',capacity),field('IP подключения WireGuard',endpoint));
    const settings=row?.settings,canEditSettings=!row||!!settings;
    const url=input(settings?.url||'','url'),tokenInput=input('','password'),configuration=input(settings?.configuration||'wg0'),timezone=input(settings?.panel_timezone||'Europe/Moscow');
    if(canEditSettings){
      url.required=true;url.maxLength=500;url.placeholder='https://wg1-panel.pushok.shop';
      tokenInput.required=!row;tokenInput.autocomplete='new-password';tokenInput.minLength=32;tokenInput.maxLength=2048;
      tokenInput.placeholder=row?'Оставьте пустым, чтобы сохранить ключ':'';
      configuration.required=timezone.required=true;configuration.maxLength=15;configuration.pattern='[a-zA-Z0-9_=+.\\-]+';timezone.maxLength=80;
      grid.append(field('Адрес панели WGDashboard',url),field(row?'Новый API-ключ панели':'API-ключ панели',tokenInput),
        field('Интерфейс WireGuard',configuration),field('Часовой пояс панели',timezone));
    }
    const note=el('p',row?'Сохранённый API-ключ не показывается. Пустые поля ключа и IP сохраняют прежние значения.':'Имя подключения wg1.pushok.shop, wg2.pushok.shop и далее назначается автоматически. Укажите IP туннеля отдельно от адреса панели. Новый узел добавляется выключенным.','wg-fleet-note');
    const status=feedback(),bar=el('div',undefined,'wg-fleet-actions'),submit=el('button',row?'Сохранить параметры':'Добавить сервер','ui-button');submit.type='submit';
    const dismiss=action('Отмена',()=>{tokenInput.value='';cancel();});bar.append(submit,dismiss);form.append(grid,note,bar,status);
    const send=mutation();let busy=false;
    form.onsubmit=async event=>{
      event.preventDefault();if(busy||!alive()||!form.reportValidity())return;
      const payload={name:name.value.trim(),location:place.value.trim(),capacity:Number(capacity.value)};
      if(endpoint.value.trim())payload.endpoint_ip=endpoint.value.trim();
      if(row){payload.id=row.id;payload.version=row.version;}else payload.protocol='wg';
      const connectionChanged=!row||url.value.trim()!==(settings?.url||'')||configuration.value.trim()!==(settings?.configuration||'wg0')||timezone.value.trim()!==(settings?.panel_timezone||'Europe/Moscow')||!!tokenInput.value.trim();
      if(canEditSettings&&connectionChanged){
        payload.settings={api:'wgdashboard',url:url.value.trim(),configuration:configuration.value.trim(),schedule_expiry:true,panel_timezone:timezone.value.trim()};
        if(tokenInput.value.trim())payload.settings.token=tokenInput.value.trim();
      }
      busy=true;submit.disabled=dismiss.disabled=true;status.classList.remove('wg-fleet-error');status.textContent='Сохраняем параметры…';
      try{
        const result=await send(row?'update':'create',payload);
        if(!alive())return;
        tokenInput.value='';await done(result,row?'Параметры сервера сохранены.':'Сервер добавлен выключенным. Проверьте его перед включением.');
      }catch(error){if(alive()){status.textContent=error.message;status.classList.add('wg-fleet-error');}}
      finally{busy=false;submit.disabled=dismiss.disabled=false;}
    };
    return form;
  }

  function nodeCard(initial,canManage,done,alive){
    let row=initial;
    const tile=el('article',undefined,'wg-fleet-card');tile.dataset.nodeId=row.id;
    const head=el('div',undefined,'wg-fleet-card-heading'),identity=el('div'),name=el('h3'),domain=el('span',undefined,'wg-fleet-domain'),state=el('span',undefined,'wg-fleet-pill');
    identity.append(name,domain);head.append(identity,state);
    const health=el('p',undefined,'wg-fleet-health'),detail=el('p',undefined,'wg-fleet-health-detail');
    const facts=el('dl',undefined,'wg-fleet-facts'),capacityBox=el('div',undefined,'wg-fleet-capacity'),capacityLabel=el('span'),meter=el('meter');meter.min=0;meter.max=1;meter.setAttribute('aria-label','Заполнение сервера');capacityBox.append(capacityLabel,meter);
    const problem=el('p',undefined,'wg-fleet-error'),editor=el('div',undefined,'wg-fleet-editor'),status=feedback();
    tile.append(head,health,detail,facts,capacityBox,problem);
    function paint(next){
      row=next;name.textContent=row.name||'Сервер';domain.textContent=[row.location,row.domain||'Домен ещё не назначен'].filter(Boolean).join(' · ');
      state.textContent=row.archived?'Архив':states[row.state]||'Состояние уточняется';state.dataset.state=row.archived?'archived':row.state||'unknown';
      tile.dataset.archived=String(!!row.archived);
      if(row.archived){editor.querySelectorAll('input[type=password]').forEach(node=>{node.value='';});editor.replaceChildren();}
      health.dataset.health=row.health||'unknown';
      health.textContent=((({ok:'Панель отвечает',error:'Проверка панели не прошла',unknown:'Панель ещё не проверена'})[row.health])||'Состояние панели неизвестно')+' · '+when(row.api_checked_at);
      const dns=({pending:'DNS ожидает настройки',ready:'DNS настроен',error:'Ошибка DNS',unconfigured:'DNS не настроен'})[row.dns_state]||'DNS не проверен';
      const traffic=row.traffic_verified?'Трафик подтверждён':({unhealthy:'Проверка трафика не прошла',stale:'Проверка трафика устарела',configuration_changed:'Нужна новая проверка трафика'})[row.probe_status]||'Трафик не проверен';
      detail.textContent=traffic+' · '+when(row.checked_at)+' · '+dns;
      facts.replaceChildren(fact('Основные',display(row.primary_count)),fact('Резервные',display(row.standby_count)),
        fact('Занято',display(row.occupied)),fact('Нагрузка',numeric(row.load_score)?Math.round(row.load_score*100)+'%':'—'));
      capacityLabel.textContent=row.capacity===0?'Занято: '+display(row.occupied)+' · лимит не задан':'Места: '+display(row.occupied)+' из '+display(row.capacity);
      meter.hidden=!numeric(row.capacity)||row.capacity<=0||!numeric(row.occupied);
      if(!meter.hidden)meter.value=Math.min(1,Math.max(0,row.occupied/row.capacity));
      problem.hidden=!row.error_code;problem.textContent=row.error_code?reason(row.error_code):'';
    }
    if(canManage){
      const tools=el('div',undefined,'wg-fleet-actions'),send=mutation();
      const check=action('Проверить панель',async()=>{
        if(!alive()||check.disabled)return;check.disabled=true;status.textContent='Проверяем WGDashboard…';status.classList.remove('wg-fleet-error');
        try{const result=await send('check',{id:row.id});if(alive()){paint(result.node);status.textContent='Проверка панели завершена. Работа интернета оценивается отдельно.';}}
        catch(error){if(alive()){status.textContent=error.message;status.classList.add('wg-fleet-error');}}
        finally{check.disabled=false;}
      });
      const edit=action('Изменить',()=>{editor.replaceChildren(connectionForm({...row},done,()=>editor.replaceChildren(),alive));});
      const mode=action('Режим',()=>{
        const base={...row},form=el('form',undefined,'wg-fleet-form'),select=el('select'),notice=feedback();
        for(const [key,label] of Object.entries(states)){const item=el('option',label);item.value=key;select.append(item);}select.value=base.state;
        const submit=el('button','Применить режим','ui-button');submit.type='submit';const sendState=mutation();
        form.append(field('Режим сервера',select),el('p','Активный узел получает новые подключения после проверки. Для переноса существующих выберите «Перенести пользователей».','wg-fleet-note'),submit,notice);
        form.onsubmit=async event=>{event.preventDefault();if(submit.disabled||!alive())return;submit.disabled=true;notice.textContent='Применяем режим…';
          try{const result=await sendState('state',{id:base.id,version:base.version,state:select.value});if(alive())await done(result,'Режим сервера обновлён.');}
          catch(error){if(alive())notice.textContent=error.message;}finally{submit.disabled=false;}};
        editor.replaceChildren(form);
      });
      const drain=action('Перенести пользователей',()=>{
        const base={...row},form=el('form',undefined,'wg-fleet-form'),hours=integer(24,1,720),lost=input('', 'checkbox'),notice=feedback();
        const lostLabel=field('Исходный сервер недоступен',lost);lostLabel.className='wg-fleet-checkbox';
        const submit=el('button','Начать перенос','ui-button');submit.type='submit';const sendDrain=mutation();
        form.append(el('p','Shop подготовит подключения на других узлах. Сроки подписок сохраняются. Завершение переноса отслеживается ниже.','wg-fleet-note'),field('Время на переход, часов',hours),lostLabel,submit,notice);
        form.onsubmit=async event=>{event.preventDefault();if(submit.disabled||!alive()||!form.reportValidity())return;submit.disabled=true;notice.textContent='Ставим перенос в очередь…';
          try{const result=await sendDrain('drain',{id:base.id,version:base.version,deadline_hours:Number(hours.value),lost:lost.checked});if(alive())await done(result,phases[result.operation.state]||'Перенос принят. Следите за состоянием операции.');}
          catch(error){if(alive())notice.textContent=error.message;}finally{submit.disabled=false;}};
        editor.replaceChildren(form);
      });
      const enroll=action('Подключить проверку трафика',async()=>{
        if(!alive()||enroll.disabled)return;const before={...row};enroll.disabled=true;status.textContent='Создаём доступ для узла проверки…';
        try{
          const credentials=await api('wg-fleet/probes/enroll',{method:'POST',data:{id:before.id,version:before.version}});
          if(!alive())return;
          if(typeof credentials.token!=='string'||!credentials.token||typeof credentials.probe_id!=='string')throw new Error('wg_fleet_change_unconfirmed');
          if(numeric(credentials.server_version))row={...row,version:credentials.server_version};
          const box=el('div',undefined,'wg-fleet-form'),secret=input(credentials.token,'password');secret.readOnly=true;secret.autocomplete='off';
          const message=feedback(),copy=action('Скопировать реквизиты проверки',async()=>{
            try{await navigator.clipboard.writeText(JSON.stringify({server_id:credentials.server_id,server_version:credentials.server_version,probe_id:credentials.probe_id,generation:credentials.generation,token:secret.value},null,2));message.textContent='Реквизиты скопированы.';}
            catch{message.textContent='Не удалось скопировать. Выделите и скопируйте секрет вручную.';secret.focus();secret.select();}
          });
          box.append(el('h3','Доступ для независимой проверки'),el('p','Секрет выдаётся один раз. Сохраните реквизиты на узле проверки и настройте агент. Повторная выдача заменяет предыдущий доступ.','wg-fleet-note'),
            el('p','Сервер: '+credentials.server_id+' · проверка: '+credentials.probe_id+' · поколение: '+credentials.generation,'wg-fleet-note'),field('Секрет проверки',secret),copy,
            action('Закрыть реквизиты',()=>{secret.value='';editor.replaceChildren();}),message);
          editor.replaceChildren(box);status.textContent='Доступ создан. Подтверждение трафика появится после реальной проверки.';
        }catch(error){if(alive())status.textContent=messages[aliases[error.message]||error.message]||messages.wg_fleet_change_unconfirmed;}
        finally{enroll.disabled=false;}
      });
      tools.append(check,edit,mode,drain,enroll);tile.append(tools,status,editor);
    }
    paint(initial);return {tile,paint};
  }

  function renderOperations(host,operations,nodes,canManage,done,alive){
    host.replaceChildren(el('h2','Переносы подключений'));
    if(!operations.length){host.append(el('p','Переносов пока нет.','wg-fleet-note'));return;}
    for(const row of operations){
      const source=nodes.find(node=>node.id===row.source_server_id),item=el('article',undefined,'wg-fleet-operation');item.dataset.operationId=row.id;
      const state=phases[row.state]||'Состояние уточняется',title=el('h3',(source?.name||'Сервер')+' · '+state);
      const meta=el('div',undefined,'wg-fleet-operation-meta');meta.append(el('span','Перенесено: '+display(row.completed)+' из '+display(row.total)),el('span','Ошибок: '+display(row.failed)));
      const progress=el('progress');progress.setAttribute('aria-label','Перенос подключений');
      if(numeric(row.total)&&row.total>0&&numeric(row.completed)){progress.max=row.total;progress.value=Math.min(row.total,row.completed);}
      item.append(title,meta,progress);
      if(numeric(row.pending_revocations))item.append(el('p','На прежнем узле ждут отключения: '+display(row.pending_revocations),'wg-fleet-note'));
      if(row.deadline&&!row.lost)item.append(el('p','Переходный период до '+when(row.deadline),'wg-fleet-note'));
      if(row.error){const error=el('p',reason(row.error),'wg-fleet-error');item.append(error);}
      if(canManage&&(['failed','error','partial'].includes(row.state)||(row.failed>0)||row.error)){
        const status=feedback(),send=mutation(),retry=action('Повторить перенос',async()=>{
          if(retry.disabled||!alive())return;retry.disabled=true;status.textContent='Отправляем повторный запрос…';
          try{const result=await send('retry',{id:row.id});if(alive())await done(result,phases[result.operation.state]||'Повтор принят в обработку.');}
          catch(error){if(alive())status.textContent=error.message;}finally{retry.disabled=false;}
        });item.append(retry,status);
      }
      host.append(item);
    }
  }

  function policyForm(record,canManage,done,alive){
    const box=el('section',undefined,'wg-fleet-policy');box.append(el('h2','Распределение подключений'));
    if(!canManage){box.append(el('p',record.enabled?'Включено · один резервный узел для каждого аккаунта':'Автоматическое распределение выключено','wg-fleet-note'));return box;}
    const form=el('form',undefined,'wg-fleet-form'),on=input('', 'checkbox');on.checked=record.enabled;
    const label=field('Распределять подключения и готовить резерв',on);label.className='wg-fleet-checkbox';
    const note=el('p','При включении одна подписка аккаунта действует на всех его устройствах; технические профили не требуют отдельных подписок. Резерв: один другой узел на аккаунт. При нехватке подходящих серверов состояние останется ожидающим.','wg-fleet-note'),submit=el('button','Сохранить распределение','ui-button'),status=feedback();submit.type='submit';
    form.append(label,note,submit,status);box.append(form);const send=mutation();
    form.onsubmit=async event=>{event.preventDefault();if(submit.disabled||!alive())return;submit.disabled=true;status.textContent='Сохраняем…';
      try{const result=await send('policy',{version:record.version,enabled:on.checked,standby_count:1});if(alive())await done(result,result.policy.enabled?'Распределение включено.':'Распределение выключено.');}
      catch(error){if(alive())status.textContent=error.message;}finally{submit.disabled=false;}};
    return box;
  }

  async function show(target){
    reset();
    target.replaceChildren();
    if(!staff()){target.append(el('p','Для раздела требуется доступ к просмотру серверов.','muted'));return;}
    const session=authEpoch,route=currentManagementSection,host=el('section',undefined,'wg-fleet');target.append(host);
    activeHost=host;activeRoute=route;
    const alive=()=>activeHost===host&&host.isConnected&&session===authEpoch&&route===currentManagementSection&&staff();
    const head=el('header'),titlebar=el('div',undefined,'wg-fleet-head'),refresh=action('Обновить',()=>show(target));
    titlebar.append(el('h2','Серверы WireGuard'),refresh);const nav=el('nav',undefined,'wg-fleet-toolbar');nav.setAttribute('aria-label','Управление VPN');
    nav.append(link('Подписки','users-section'),link('Все серверы','servers-section'));head.append(titlebar,nav);host.append(head);
    const content=el('div','Загрузка серверов…');host.append(content);
    try{
      const data=await fleetApi('/vpn/fleet');if(!alive())return;
      const canManage=data.can_manage!==false&&canGroup('developer');content.replaceChildren();content.className='wg-fleet';
      const summary=el('dl',undefined,'wg-fleet-summary'),nodes=el('div',undefined,'wg-fleet-nodes'),operations=el('section',undefined,'wg-fleet-operations'),live=feedback();
      const done=async(result,message)=>{if(!alive())return;await show(target);if(session===authEpoch&&route===currentManagementSection)notice(message,true);};
      content.append(summary);
      if(!canManage)content.append(el('p','Просмотр серверов. Изменять параметры и запускать перенос может разработчик.','wg-fleet-note'));
      content.append(policyForm(data.policy,canManage,done,alive));
      if(canManage){
        const add=el('details',undefined,'wg-fleet-add');add.append(el('summary','Добавить сервер'));
        const slot=el('div');add.append(slot);slot.append(connectionForm(null,done,()=>{add.open=false;},alive));content.append(add);
      }
      content.append(nodes,operations,live);
      const cards=new Map();
      function updateSnapshot(next){
        summary.replaceChildren(fact('Всего узлов',String(next.nodes.length)),fact('Панель отвечает',String(next.nodes.filter(node=>node.health==='ok').length)),fact('Активных',String(next.nodes.filter(node=>node.state==='active').length)));
        for(const row of next.nodes){
          if(cards.has(row.id))cards.get(row.id).paint(row);
          else{const card=nodeCard(row,canManage,done,alive);cards.set(row.id,card);nodes.append(card.tile);}
        }
        for(const [id,card] of cards){if(!next.nodes.some(row=>row.id===id)){card.tile.remove();cards.delete(id);}}
        nodes.querySelector('.wg-fleet-empty')?.remove();
        if(!next.nodes.length)nodes.append(el('p','Серверов WireGuard пока нет.','wg-fleet-note wg-fleet-empty'));
        renderOperations(operations,next.operations,next.nodes,canManage,done,alive);
      }
      updateSnapshot(data);
      let polling=false;
      const poll=async()=>{
        if(!alive())return;
        if(document.hidden||polling){pollTimer=setTimeout(poll,12000);return;}
        polling=true;
        try{const next=await fleetApi('/vpn/fleet');if(alive()){if(next.can_manage!==data.can_manage){await show(target);return;}updateSnapshot(next);live.textContent='';live.classList.remove('wg-fleet-warning');}}
        catch(error){if(alive()){live.textContent='Автообновление приостановлено: '+error.message;live.classList.add('wg-fleet-warning');}}
        finally{polling=false;if(alive())pollTimer=setTimeout(poll,12000);}
      };
      pollTimer=setTimeout(poll,12000);
    }catch(error){if(alive()){content.replaceChildren(el('p',error.message,'wg-fleet-error'));}}
  }
  return {show,reset};
})();

if(currentManagementSection==='wg-fleet-section'&&canGroup('servers_read'))void window.pushokWgFleet.show($('wg-fleet-content'));
