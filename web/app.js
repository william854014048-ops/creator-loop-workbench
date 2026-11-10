'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const STEPS = [
    ['素材与角度', '今天，你想讲什么？', '写下发生过的事、灵感和想说的话，再选一个值得展开的方向。'],
    ['怎么讲', '先把讲述思路理清楚', '围绕选定的方向，看看内容怎样组织、哪些地方需要补充。'],
    ['结构与口述', '看着讲述结构，用自己的话讲一遍', '左侧参考结构，右侧重新口述。讲完点下一步，自动保留口述并生成修改建议。'],
    ['标记修改', '让每一处修改都看得见', '检查衔接、重复和拗口的地方，选择你愿意接受的修改。'],
    ['实际成片', '以你真正拍出的内容为准', '拍摄、剪辑完成后，核对实际正文、标题、封面和开头。'],
    ['发布前判断', '让两种方法，各自判断一次', '分别运行、分别审阅。每份判断只依据这次确认的最终作品。'],
    ['发布与数据', '记下同一窗口的真实表现', '发布后手动填入数据与评论，保留平台、账号、发布时间和观察时间。'],
    ['复盘与下一条', '把这次的观察，带到下一条', '对照发布前的两份判断，选择一项相关观察用于后续创作。']
  ];
  // Keep persisted stage IDs compatible with existing works and later steps.
  const VISIBLE_STAGES = [0, 2, 3, 4, 5, 6, 7];
  const KIND_NAMES = {value: '内容价值判断', cheat: '内容作弊盲预测'};
  const KEYS = {last: 'creator-loop:last', draft: 'creator-loop:draft:', form: 'creator-loop:form:'};
  const windowId = (() => {
    let id = sessionStorage.getItem('creator-loop:window-id');
    if (!id) { id = crypto.randomUUID(); sessionStorage.setItem('creator-loop:window-id', id); }
    return id;
  })();
  let work = null, works = [], observations = [];
  let pending = {}, inFlight = {}, saving = null, timer = null, busy = false, conflictWork = null, closing = false;
  let restoredDraftKey = null;
  let activePublication = null, activeMetric = null;
  let settings = null;
  let editingProfileId = null, originalProfile = null;
  let deleteTarget = null;

  function h(tag, attrs = {}, ...children) {
    const element = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
      if (value === undefined || value === null) continue;
      if (key.startsWith('on') && typeof value === 'function') element.addEventListener(key.slice(2).toLowerCase(), value);
      else if (key === 'dataset') Object.assign(element.dataset, value);
      else if (key in element && !key.startsWith('aria')) element[key] = value;
      else element.setAttribute(key, String(value));
    }
    for (const child of children.flat(Infinity)) {
      if (child !== undefined && child !== null && child !== false) element.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return element;
  }
  const button = (text, handler, cls = 'secondary', attrs = {}) => h('button', {type: 'button', className: 'button ' + cls, onClick: handler, ...attrs}, text);
  const tag = (text, cls = '') => h('span', {className: 'tag ' + cls}, text);
  const card = (...nodes) => h('section', {className: 'card'}, nodes);
  const empty = text => h('div', {className: 'empty-state'}, text);
  const asText = value => typeof value === 'string' ? value : value?.text || value?.title || value?.report || '';
  const latest = list => Array.isArray(list) ? list[list.length - 1] : null;
  function dateText(value) {
    if (!value) return '';
    const date = new Date(value);
    return Number.isNaN(date.valueOf()) ? '' : date.toLocaleString('zh-CN', {month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false});
  }
  function localDate(value = new Date()) {
    const date = new Date(value);
    return new Date(date.valueOf() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
  }
  function isoDate(id) {
    const value = $(id).value;
    if (!value || Number.isNaN(new Date(value).valueOf())) throw new Error('请填写完整的日期和时间。');
    return new Date(value).toISOString();
  }
  function numeric(value) {
    const raw = String(value ?? '').trim();
    if (!raw) return null;
    const match = raw.replace(/,/g, '').match(/^(\d+(?:\.\d+)?)\s*([万wW千kK]?)$/);
    if (!match) throw new Error('数据请填写数字，可使用“万”或“千”。');
    const scale = /[万wW]/.test(match[2]) ? 10000 : /[千kK]/.test(match[2]) ? 1000 : 1;
    return Number(match[1]) * scale;
  }
  function notify(message, success = false) {
    $('notice').replaceChildren(document.createTextNode(message));
    $('notice').className = 'notice' + (success ? ' success' : '');
    $('notice').hidden = !message;
  }
  function status(state, text) {
    $('save-status').dataset.state = state;
    $('save-status').textContent = text;
  }
  async function api(path, method = 'GET', body) {
    const options = {method, headers: {'Accept': 'application/json'}};
    if (method !== 'GET') options.headers['X-Creator-Loop'] = 'local';
    if (body !== undefined) { options.headers['Content-Type'] = 'application/json'; options.body = JSON.stringify(body); }
    let response;
    try { response = await fetch(path, options); }
    catch (_) { throw new Error('未能连接本地工作台，输入暂存在当前浏览器。请重新启动程序后重试。'); }
    const data = await response.json().catch(() => ({error: '服务返回了无法读取的内容。'}));
    if (!response.ok) {
      const error = new Error(data.error || '这次操作未完成，请重试。');
      error.status = response.status;
      error.current = data.work || data.current || data.current_work;
      throw error;
    }
    return data?.work?.id ? data.work : data;
  }

  function stash() {
    if (!work) return;
    const patch = {...inFlight, ...pending};
    const key = KEYS.draft + work.id + ':' + windowId;
    try {
      if (Object.keys(patch).length) localStorage.setItem(key, JSON.stringify({revision: work.revision, patch, saved_at: Date.now(), closed: closing}));
      else localStorage.removeItem(key);
    } catch (_) { notify('浏览器暂存空间不足，请保持页面打开，等待本地保存完成。'); }
  }
  function formKey(scope) { return KEYS.form + work.id + ':' + scope + ':' + windowId; }
  function formDraft(scope) {
    try {
      const own = JSON.parse(localStorage.getItem(formKey(scope)));
      if (own) return own.fields || {};
      const prefix = KEYS.form + work.id + ':' + scope + ':';
      const closed = Object.keys(localStorage).filter(key => key.startsWith(prefix)).map(key => {
        try { return {key, value: JSON.parse(localStorage.getItem(key))}; } catch (_) { return null; }
      }).filter(item => item?.value?.closed === true).sort((a, b) => (b.value.saved_at || 0) - (a.value.saved_at || 0))[0];
      if (!closed) return {};
      localStorage.setItem(formKey(scope), JSON.stringify({...closed.value, closed: false}));
      localStorage.removeItem(closed.key);
      return closed.value.fields || {};
    } catch (_) { return {}; }
  }
  function draftField(scope, key, value) {
    try { localStorage.setItem(formKey(scope), JSON.stringify({fields: {...formDraft(scope), [key]: value}, closed: closing, saved_at: Date.now()})); }
    catch (_) { notify('浏览器暂存空间不足，请保持页面打开并保存当前表单。'); }
  }
  function markFormsClosed(value) {
    try {
      for (const key of Object.keys(localStorage).filter(item => item.startsWith(KEYS.form) && item.endsWith(':' + windowId))) {
        const entry = JSON.parse(localStorage.getItem(key));
        if (entry?.fields) localStorage.setItem(key, JSON.stringify({...entry, closed: value}));
      }
    } catch (_) { notify('浏览器暂存空间不足，请保持页面打开并保存当前表单。'); }
  }
  function mark(patch) {
    if (!work) return;
    work = {...work, ...patch};
    pending = {...pending, ...patch};
    if ($('find-angles')) $('find-angles').disabled = !work.material?.trim();
    if ($('save-spoken')) $('save-spoken').disabled = !work.spoken?.trim();
    updateNavigation();
    stash();
    status('pending', '待保存…');
    clearTimeout(timer);
    timer = setTimeout(() => flush().catch(showError), 450);
  }
  function upsertSummary() {
    if (!work) return;
    const summary = {id: work.id, title: work.title, stage: work.stage, revision: work.revision, updated_at: work.updated_at};
    works = [summary, ...works.filter(item => item.id !== work.id)];
    renderList();
  }
  async function flush() {
    clearTimeout(timer);
    if (conflictWork) throw new Error('请先核对两个窗口的版本。当前输入仍保留在页面。');
    if (saving) { await saving; if (Object.keys(pending).length) return flush(); return; }
    if (!work || !Object.keys(pending).length) return;
    saving = (async () => {
      while (Object.keys(pending).length) {
        const patch = pending, id = work.id, revision = work.revision;
        pending = {}; inFlight = patch; status('saving', '正在保存…'); stash();
        try {
          const saved = await api('/api/works/' + id, 'PUT', {revision, patch});
          work = {...saved, ...pending};
          inFlight = {};
          stash(); upsertSummary(); renderTitle();
        } catch (error) {
          pending = {...patch, ...pending}; inFlight = {}; stash();
          if (error.status === 409) openConflict(error.current);
          status(error.status === 409 ? 'conflict' : 'error', error.status === 409 ? '待核对版本' : '保存未完成');
          throw error;
        }
      }
      status('saved', '已保存到本机');
      if (restoredDraftKey) { localStorage.removeItem(restoredDraftKey); restoredDraftKey = null; }
    })().finally(() => { saving = null; });
    return saving;
  }
  function openConflict(serverWork) {
    conflictWork = serverWork || {};
    const fields = ['title', 'material', 'spoken', 'clean_script', 'final', 'applied_observation'];
    $('conflict-local').value = editableText(work, fields);
    $('conflict-server').value = editableText(serverWork || {}, fields);
    if (!$('conflict-dialog').open) $('conflict-dialog').showModal();
  }
  function editableText(record, fields) {
    const labels = {title: '作品名称', material: '素材', spoken: '口述', clean_script: '干净稿',
      'final.platform': '发布平台', 'final.account': '账号名称', 'final.duration_seconds': '实际时长',
      'final.title': '实际标题', 'final.cover': '实际封面文字', 'final.opening': '实际开头', 'final.script': '实际正文',
      'final.confirmed': '成片确认', 'applied_observation.id': '观察来源 ID',
      'applied_observation.text': '观察内容', 'applied_observation.evidence': '观察依据',
      'applied_observation.next_action': '下一条尝试', 'applied_observation.application': '观察应用说明'};
    const paths = fields.flatMap(key => key === 'final' ? ['platform', 'account', 'duration_seconds', 'title', 'cover', 'opening', 'script', 'confirmed'].map(field => 'final.' + field)
      : key === 'applied_observation' ? ['id', 'text', 'evidence', 'next_action', 'application'].map(field => 'applied_observation.' + field) : [key]);
    return paths.map(path => {
      const value = path.split('.').reduce((current, part) => current?.[part], record);
      return (labels[path] || path) + '：\n' + (value == null ? '（空）' : typeof value === 'object' ? JSON.stringify(value, null, 2) : String(value));
    }).join('\n\n');
  }
  function showError(error) {
    notify(error.message || '这次操作未完成。');
    if (conflictWork) $('notice').append(button('查看两个版本', () => openConflict(conflictWork), 'secondary small'));
  }
  function setBusy(value) {
    busy = value;
    document.body.dataset.busy = String(value);
    if (value) {
      document.querySelectorAll('#stage-body button,#stage-body input,#stage-body textarea,#stage-body select,.step-tab,#work-list button,#new-work,#work-title,#delete-work,#previous-step,#next-step').forEach(element => {
        if (element.dataset.busyDisabled === undefined) element.dataset.busyDisabled = String(element.disabled);
        element.disabled = true;
      });
    } else {
      document.querySelectorAll('[data-busy-disabled]').forEach(element => {
        element.disabled = element.dataset.busyDisabled === 'true';
        delete element.dataset.busyDisabled;
      });
      updateNavigation();
      $('stage-processing').hidden = true;
    }
  }
  async function perform(action, parameters = {}, success = '') {
    if (!work || busy) return;
    setBusy(true); notify('');
    const message = {angles: '正在寻找讲述角度…', plan: '正在整理结构和表达，可稍候参考左侧方案…', review: '正在检查并标记修改，完成后可直接选择接受项…'}[action] || '正在处理…';
    status('processing', message);
    $('stage-processing').replaceChildren(h('span', {className: 'busy-pulse', 'aria-hidden': 'true'}, '●'), message);
    $('stage-processing').hidden = false;
    const trigger = document.activeElement;
    const originalText = trigger?.tagName === 'BUTTON' ? trigger.textContent : null;
    if (originalText) { trigger.disabled = true; trigger.textContent = '正在处理…'; }
    try {
      await flush();
      status('processing', message);
      const saved = await api('/api/works/' + work.id + '/actions/' + action, 'POST', {revision: work.revision, ...parameters});
      work = {...saved, ...pending}; stash(); status(Object.keys(pending).length ? 'pending' : 'saved', Object.keys(pending).length ? '待保存…' : '已保存到本机'); upsertSummary();
      if (action === 'publish') { activePublication = latest(work.publications)?.id; localStorage.removeItem(formKey('publication')); }
      if (action === 'metrics') { activeMetric = latest(work.metrics)?.id; localStorage.removeItem(formKey('metrics:' + parameters.publication_id)); }
      if (action === 'merge') localStorage.removeItem(formKey('review:' + parameters.review_id));
      if (action === 'retro') await loadObservations();
      renderWork();
      if (success) notify(success, true);
      return saved;
    } catch (error) {
      if (error.status === 409) { openConflict(error.current); status('conflict', '待核对版本'); }
      else if (!Object.keys(pending).length) status('saved', '输入已保留');
      showError(error);
    }
    finally { setBusy(false); if (originalText && trigger.isConnected) { trigger.disabled = false; trigger.textContent = originalText; } }
  }
  async function loadObservations() {
    const data = await api('/api/observations');
    observations = data.observations || [];
  }
  async function restoreDraft(saved) {
    const prefix = KEYS.draft + saved.id + ':';
    const keys = Object.keys(localStorage).filter(key => key.startsWith(prefix));
    const candidates = keys.map(key => { try { return {key, value: JSON.parse(localStorage.getItem(key))}; } catch (_) { return null; } }).filter(Boolean);
    const differs = item => item.value?.patch && Object.entries(item.value.patch).some(([key, value]) => JSON.stringify(saved[key]) !== JSON.stringify(value));
    const changed = candidates.filter(item => differs(item) && (item.key === prefix + windowId || item.value.closed === true));
    const own = changed.find(item => item.key === prefix + windowId);
    const chosen = own || changed.sort((a, b) => (b.value.saved_at || 0) - (a.value.saved_at || 0))[0];
    const draft = chosen?.value;
    if (!draft?.patch || !Object.keys(draft.patch).length) return;
    work = {...saved, ...draft.patch};
    pending = draft.patch;
    restoredDraftKey = chosen.key === prefix + windowId ? null : chosen.key;
    stash();
    if (draft.revision === saved.revision) { status('pending', '已接回暂存输入'); await flush(); }
    else { openConflict(saved); status('conflict', '已接回本地暂存'); }
  }
  async function selectWork(id) {
    if (busy) return;
    setBusy(true);
    try {
      await flush(); notify('');
      work = await api('/api/works/' + encodeURIComponent(id)); pending = {}; inFlight = {};
      restoredDraftKey = null;
      activeMetric = null; activePublication = null;
      localStorage.setItem(KEYS.last, id);
      status('saved', '已保存到本机');
      await restoreDraft(work);
      renderWork(); $('sidebar').classList.remove('open'); $('sidebar-toggle').setAttribute('aria-expanded', 'false');
    } catch (error) { showError(error); }
    finally { setBusy(false); }
  }
  async function newWork(observation = null) {
    if (busy) return;
    setBusy(true);
    try {
      await flush(); notify('');
      const payload = observation ? {applied_observation: observation} : {};
      work = await api('/api/works', 'POST', payload); pending = {}; inFlight = {};
      restoredDraftKey = null;
      activeMetric = null; activePublication = null;
      localStorage.setItem(KEYS.last, work.id);
      status('saved', '已保存到本机'); upsertSummary(); renderWork();
      $('material')?.focus();
    } catch (error) { showError(error); }
    finally { setBusy(false); }
  }
  function requestDelete(item) {
    if (!item || busy) return;
    deleteTarget = item;
    $('delete-name').textContent = item.title;
    $('delete-notice').textContent = '';
    $('delete-dialog').showModal();
  }
  async function deleteWork() {
    if (!deleteTarget || busy) return;
    const target = deleteTarget;
    setBusy(true);
    $('confirm-delete').disabled = true;
    $('cancel-delete').disabled = true;
    try {
      await flush();
      const revision = target.id === work?.id ? work.revision : target.revision;
      await api('/api/works/' + target.id, 'DELETE', {revision});
      for (const key of Object.keys(localStorage)) {
        if (key.startsWith(KEYS.draft + target.id + ':') || key.startsWith(KEYS.form + target.id + ':')) localStorage.removeItem(key);
      }
      works = (await api('/api/works')).works || [];
      await loadObservations();
      if (target.id === work?.id) {
        clearTimeout(timer);
        pending = {}; inFlight = {}; restoredDraftKey = null;
        activeMetric = null; activePublication = null;
        conflictWork = null;
        localStorage.removeItem(KEYS.last);
        work = works[0] ? await api('/api/works/' + works[0].id) : null;
        if (work) { localStorage.setItem(KEYS.last, work.id); await restoreDraft(work); }
      }
      $('delete-dialog').close(); deleteTarget = null;
      status(work ? 'saved' : 'idle', work ? '已保存到本机' : '本地工作台');
      renderWork(); notify('作品已从列表删除。', true);
    } catch (error) {
      $('delete-notice').textContent = error.message;
      if (conflictWork) { $('delete-dialog').close(); showError(error); return; }
      if (error.status === 409) {
        works = (await api('/api/works')).works || [];
        if (target.id === work?.id) { $('delete-dialog').close(); openConflict(error.current); }
        else { $('delete-dialog').close(); deleteTarget = null; renderList(); showError(error); }
      }
    } finally {
      $('confirm-delete').disabled = false; $('cancel-delete').disabled = false;
      setBusy(false);
    }
  }
  async function goStep(index, start = false) {
    if (!work || busy || index < 0 || index > 7) return;
    if (index === 1) index = 2;
    setBusy(true);
    let navigated = false;
    try {
      await flush(); notify('');
      const patch = {stage: index};
      if (index === 4 && !work.final?.script) {
        patch.final = {...work.final, script: work.clean_script || work.spoken || '',
          title: work.final?.title || asText(work.package?.titles?.[0]) || work.title.replace(/^未命名作品$/, ''),
          cover: work.final?.cover || asText(work.package?.cover),
          opening: work.final?.opening || asText(work.package?.opening),
          platform: work.final?.platform || '抖音', account: work.final?.account || '我的账号',
          duration_seconds: work.final?.duration_seconds || 60};
      }
      mark(patch); await flush(); renderWork();
      navigated = true;
      if (window.innerWidth < 681) $('step-nav').scrollIntoView({block: 'start'});
    } catch (error) { showError(error); }
    finally { setBusy(false); }
    if (!navigated || !start) return;
    if (index === 2 && work.angle && (!work.plan?.report || work.plan_stale)) await perform('plan');
    if (index === 3 && work.spoken?.trim() && !work.review_current) await perform('review');
  }
  async function advance() {
    if (!work || busy) return;
    if (work.stage === 0 && !work.angle) {
      if (!work.material.trim()) { notify('先写下这条作品的素材。'); $('material')?.focus(); return; }
      if (!work.angles?.length) await perform('angles');
      else notify('先选定一个讲述角度，再继续。');
      return;
    }
    if (work.stage === 2 && !work.spoken?.trim()) { notify('先在右侧讲出这一遍口述。'); $('spoken')?.focus(); return; }
    const position = VISIBLE_STAGES.indexOf(work.stage);
    await goStep(VISIBLE_STAGES[position + 1], true);
  }
  function updateNavigation() {
    $('previous-step').disabled = busy || !work || work.stage === 0;
    $('next-step').disabled = busy || !work;
    $('next-step').hidden = work?.stage === 7;
    const next = VISIBLE_STAGES[VISIBLE_STAGES.indexOf(work?.stage) + 1];
    $('next-step').textContent = work?.stage === 0 && !work.angle
      ? (work.angles?.length ? '选定角度后继续 →' : '下一步：找讲述角度 →')
      : '下一步：' + (STEPS[next]?.[0] || '') + ' →';
    $('step-footnote').textContent = work?.stage === 2
      ? (work.review_current ? '下一步查看这一遍已有的修改建议' : '下一步自动保留口述并标记修改')
      : work?.stage === 0 && work.angle ? '下一步自动整理讲述结构' : '原话、版本和判断会分别保存';
  }
  function renderTitle() {
    $('work-title').hidden = !work;
    $('title-source').hidden = !work;
    $('delete-work').hidden = !work;
    if (!work) return;
    if (document.activeElement !== $('work-title')) $('work-title').value = work.title;
    $('title-source').textContent = work.title_source || '';
  }

  function inline(parent, value) {
    const pieces = String(value).split(/(\*\*[^*]+\*\*|`[^`]+`|~~[^~]+~~)/g);
    for (const piece of pieces) {
      if (piece.startsWith('**') && piece.endsWith('**')) parent.append(h('strong', {}, piece.slice(2, -2)));
      else if (piece.startsWith('`') && piece.endsWith('`')) parent.append(h('code', {}, piece.slice(1, -1)));
      else if (piece.startsWith('~~') && piece.endsWith('~~')) parent.append(h('del', {}, piece.slice(2, -2)));
      else parent.append(document.createTextNode(piece));
    }
  }
  function report(value, attrs = {}) {
    const root = h('div', {className: 'report', ...attrs});
    const lines = asText(value).split('\n');
    let list = null, code = null;
    const cells = line => line.trim().replace(/^\|/, '').replace(/\|$/, '').split(/(?<!\\)\|/).map(cell => cell.trim().replace(/\\\|/g, '|'));
    for (let index = 0; index < lines.length; index++) {
      const raw = lines[index];
      const line = raw.trimEnd();
      if (line.startsWith('```')) { if (code) code = null; else { code = h('pre'); root.append(code); } list = null; continue; }
      if (code) { code.append(document.createTextNode(line + '\n')); continue; }
      if (!line.trim() || /^---+$/.test(line.trim())) { list = null; continue; }
      const separators = cells(lines[index + 1] || '');
      if (line.includes('|') && separators.length > 1 && separators.every(cell => /^:?-{3,}:?$/.test(cell))) {
        const headers = cells(line), table = h('table'), head = h('tr'), body = h('tbody');
        headers.forEach(value => { const cell = h('th'); inline(cell, value); head.append(cell); });
        table.append(h('thead', {}, head), body);
        index++;
        while (index + 1 < lines.length && lines[index + 1].trim() && lines[index + 1].includes('|')) {
          const values = cells(lines[++index]), row = h('tr');
          headers.forEach((_, column) => { const cell = h('td'); inline(cell, values[column] || ''); row.append(cell); });
          body.append(row);
        }
        root.append(h('div', {className: 'report-table'}, table)); list = null; continue;
      }
      const heading = line.match(/^#{1,4}\s+(.+)$/);
      if (heading) { const item = h(line.startsWith('###') ? 'h3' : 'h2'); inline(item, heading[1]); root.append(item); list = null; continue; }
      const bullet = line.match(/^\s*(?:[-*]|\d+[.、])\s+(.+)$/);
      if (bullet) { if (!list) { list = h('ul'); root.append(list); } const li = h('li'); inline(li, bullet[1]); list.append(li); continue; }
      list = null;
      const paragraph = h(line.startsWith('>') ? 'blockquote' : 'p'); inline(paragraph, line.replace(/^>\s?/, '')); root.append(paragraph);
    }
    if (!root.childNodes.length) root.append(empty('本次没有返回报告正文，可重新运行。'));
    return root;
  }
  function details(label, content) { return h('details', {className: 'details'}, h('summary', {}, label), content); }
  function copy(text) {
    navigator.clipboard.writeText(text).then(() => notify('已复制。', true)).catch(() => notify('请选择正文后复制。'));
  }
  function textField(id, title, value, handler, options = {}) {
    const input = h(options.multiline ? 'textarea' : 'input', {id, value: value ?? '',
      ...(options.multiline ? {rows: options.rows || 3} : {type: options.type || 'text'}),
      placeholder: options.placeholder || '', onInput: event => handler(event.target.value), ...options.attrs});
    return h('label', {className: 'field'}, title, input, options.note && h('span', {className: 'field-note'}, options.note));
  }
  function selectField(id, title, choices, value, handler) {
    const input = h('select', {id, onChange: event => handler(event.target.value)}, choices.map(choice => {
      const [key, label] = Array.isArray(choice) ? choice : [choice, choice];
      return h('option', {value: key}, label);
    }));
    input.value = value;
    return h('label', {className: 'field'}, title, input);
  }
  function appliedObservation() {
    const observation = work.applied_observation;
    if (!observation) return null;
    return h('div', {className: 'applied', dataset: {testid: 'applied-observation'}},
      h('div', {className: 'source'}, '本条采用的观察 · 来自 ' + (observation.work_title || '历史作品')),
      h('strong', {}, observation.text), h('p', {}, observation.next_action || observation.evidence),
      textField('observation-application', '这条作品准备怎样应用', observation.application || '',
        value => mark({applied_observation: {...work.applied_observation, application: value}}), {placeholder: '写一项这次会实际采用的动作'}));
  }
  function renderList() {
    const query = $('work-search').value.trim().toLowerCase();
    $('work-count').textContent = works.length;
    const items = works.filter(item => item.title.toLowerCase().includes(query));
    $('work-list').replaceChildren(...items.map(item => h('div', {className: 'work-row'}, h('button', {
      type: 'button', className: 'work-link' + (item.id === work?.id ? ' active' : ''),
      onClick: () => selectWork(item.id), 'aria-current': item.id === work?.id ? 'page' : 'false'
    }, h('strong', {}, item.title), h('small', {}, (STEPS[item.stage || 0]?.[0] || '素材与角度') + ' · ' + dateText(item.updated_at))),
    h('button', {type: 'button', className: 'work-delete', 'aria-label': '删除作品 ' + item.title, title: '删除作品', onClick: () => requestDelete(item)}, '删除'))));
    if (busy) setBusy(true);
  }
  function renderWork() {
    if (work?.stage === 1) work.stage = 2;
    $('empty-work').hidden = Boolean(work);
    $('workspace').hidden = !work;
    renderTitle();
    if (!work) { renderList(); return; }
    const stage = Number(work.stage) || 0;
    const position = VISIBLE_STAGES.indexOf(stage);
    $('step-nav').replaceChildren(...VISIBLE_STAGES.map((index, order) => h('button', {type: 'button', dataset: {step: index},
      className: 'step-tab' + (index === stage ? ' current' : index < stage ? ' done' : ''),
      onClick: () => goStep(index), 'aria-current': stage === index ? 'step' : 'false'}, h('span', {}, String(order + 1).padStart(2, '0')), STEPS[index][0])));
    $('stage-kicker').textContent = 'STEP ' + String(position + 1).padStart(2, '0') + ' / 07';
    $('stage-number').textContent = String(position + 1).padStart(2, '0');
    $('stage-title').textContent = STEPS[stage][1];
    $('stage-description').textContent = STEPS[stage][2];
    const renderers = [renderMaterial, renderComposition, renderComposition, renderReview, renderFinal, renderJudgments, renderData, renderRetro];
    $('stage-body').replaceChildren(...renderers[stage]().filter(Boolean));
    updateNavigation();
    renderList();
  }
  function renderMaterial() {
    const editor = h('textarea', {id: 'material', className: 'editor material', rows: 8, value: work.material,
      placeholder: '发生了什么？哪件事让你有感触？你最想让别人听见哪句话？\n\n可以零散，也可以是一段已经写好的内容。',
      onInput: event => { mark({material: event.target.value}); $('material-count').textContent = Array.from(event.target.value).length + ' 字'; }});
    const source = card(h('div', {className: 'card-head'}, h('h2', {}, '我的原始素材'), tag('从真实经历开始')),
      editor, h('div', {className: 'editor-hint'}, h('span', {}, '想法不用一次整理好，写下就会保存'), h('span', {id: 'material-count'}, Array.from(work.material || '').length + ' 字')),
      h('div', {className: 'actions'}, button('找讲述角度', () => perform('angles'), 'primary', {id: 'find-angles', disabled: !work.material?.trim()}), h('span', {className: 'help'}, '最多 4 个方向，你来决定讲哪一个')));
    const nodes = [appliedObservation(), source];
    if (work.questions?.length) nodes.push(card(h('h3', {}, '补充这几件事，会更容易选'), ...work.questions.map(question => h('p', {className: 'muted'}, asText(question)))));
    if (work.angles?.length) {
      nodes.push(h('h2', {className: 'section-label'}, '从中选一个讲述角度'));
      nodes.push(h('div', {className: 'choice-grid'}, work.angles.map((angle, index) => h('article', {className: 'card angle-card' + (angle.id === work.angle?.id ? ' selected' : '')},
        h('div', {className: 'angle-number'}, 'DIRECTION ' + String(index + 1).padStart(2, '0')),
        h('h3', {}, angle.title), h('p', {}, angle.audience && '谁会关心：' + angle.audience), h('p', {}, angle.change),
        angle.example && h('div', {className: 'angle-example'}, angle.example),
        h('div', {className: 'actions'}, button(angle.id === work.angle?.id ? '已选定，重新生成发布文字' : '选这个角度', () => perform('choose-angle', {angle_id: angle.id}), angle.id === work.angle?.id ? 'secondary small' : 'primary small'))))));
    }
    if (work.package?.titles?.length || work.package?.cover) {
      const titles = work.package.titles || [];
      const result = card(h('div', {className: 'card-head'}, h('h2', {}, '这条作品的发布文字'), tag('后续可按成片调整', 'purple')),
        h('div', {className: 'package-grid'}, h('div', {}, h('h3', {}, '标题候选'), h('ol', {className: 'title-list'}, titles.map(title => h('li', {}, asText(title)))),
          h('h3', {}, '开头与接回正文'), h('p', {}, asText(work.package.opening))),
        h('div', {}, h('h3', {}, '封面文字'), h('div', {className: 'package-cover'}, asText(work.package.cover)))),
        work.package.note && h('p', {className: 'help'}, work.package.note));
      result.dataset.testid = 'title-package'; nodes.push(result);
    }
    return nodes;
  }
  function renderPlan() {
    const angle = work.angle;
    const box = card(h('div', {className: 'card-head'}, h('h2', {}, '讲述结构'), tag('dbs-content')),
        h('p', {className: 'card-description'}, '保留你的观点和经历，先理清讲述的重点、顺序与需要补充的内容。'),
        button(work.plan?.report ? '重新整理结构和表达' : '整理结构和表达', () => perform('plan'), 'secondary small', {id: 'run-plan', disabled: !angle}),
        work.plan_stale && h('p', {className: 'inline-notice'}, '素材或角度已更新，请重新整理结构。'),
        work.plan?.report ? h('div', {className: 'plan-content', dataset: {testid: 'content-plan'}}, report(work.plan)) : empty(angle ? '从上一步继续时会自动整理结构，也可点击上方按钮重试。' : '先回到“素材与角度”选定一个方向。'));
    box.classList.add('plan-panel');
    return box;
  }
  function renderComposition() {
    return [appliedObservation(), work.angle && h('div', {className: 'summary'}, h('strong', {}, work.angle.title), h('p', {}, work.angle.audience)),
      h('div', {className: 'composition-grid'}, renderPlan(), renderSpoken())];
  }
  function renderSpoken() {
    const versions = work.spoken_versions || [];
    return card(h('div', {className: 'card-head'}, h('h2', {}, '这一遍，我想这样说'), tag('你的原话', 'purple')),
        h('textarea', {id: 'spoken', className: 'editor', rows: 11, value: work.spoken,
          placeholder: '照着刚才理清的思路，用你平时说话的方式讲一遍。', onInput: event => mark({spoken: event.target.value})}),
        h('div', {className: 'editor-hint'}, h('span', {}, '可用 Windows + H 语音输入。下一步会保留口述并检查修改。')),
        h('div', {className: 'actions'}, button('保存这一遍口述', () => perform('save-spoken', {}, '这一遍口述已保留。'), 'secondary small', {id: 'save-spoken', disabled: !work.spoken?.trim()}),
          button('复制口述', () => copy(work.spoken), 'quiet')),
        versions.length > 0 && details('已保留 ' + versions.length + ' 遍口述', h('div', {}, [...versions].reverse().map((version, index) => h('div', {className: 'version'},
          h('time', {}, '第 ' + (versions.length - index) + ' 遍 · ' + dateText(version.created_at)), h('p', {}, version.text || version.spoken || version.script || ''))))));
  }
  function renderReview() {
    const review = latest(work.reviews);
    const edits = review?.edits || [];
    const current = work.review_current;
    const draft = review ? formDraft('review:' + review.id) : {};
    const selected = new Set(draft.accepted_ids || latest(review?.decisions)?.accepted_ids || []);
    const merges = (work.reviews || []).flatMap(item => (item.decisions || []).map(decision => ({review: item, decision})));
    const box = card(h('div', {className: 'card-head'}, h('h2', {}, '改稿建议'), tag('dbs-script-flow')),
      h('p', {className: 'card-description'}, '每一处都保留原文。勾选你接受的修改，其余部分照原话保留。'),
      button(current ? '重新检查并标记修改' : '检查并标记修改', () => perform('review'), 'secondary small', {id: 'run-review', disabled: !work.spoken?.trim()}));
    if (review) {
      if (!current) box.append(h('p', {className: 'inline-notice'}, '口述已更新。重新检查后，即可选择这一遍的修改。'));
      box.append(h('div', {className: 'details'}, report(review.report)));
      const syncSelection = () => {
        const inputs = [...box.querySelectorAll('#review-edits input')];
        const ids = inputs.filter(input => input.checked).map(input => input.value);
        $('select-all-edits').checked = ids.length === edits.length && edits.length > 0;
        $('select-all-edits').indeterminate = ids.length > 0 && ids.length < edits.length;
        $('selected-edits-count').textContent = '已选 ' + ids.length + ' / ' + edits.length + ' 处';
        draftField('review:' + review.id, 'accepted_ids', ids);
      };
      if (edits.length) box.append(h('div', {className: 'selection-toolbar'},
        h('label', {}, h('input', {id: 'select-all-edits', type: 'checkbox', disabled: !current,
          checked: edits.every(edit => selected.has(edit.id)), indeterminate: edits.some(edit => selected.has(edit.id)) && !edits.every(edit => selected.has(edit.id)),
          onChange: event => { box.querySelectorAll('#review-edits input').forEach(input => { input.checked = event.target.checked; }); syncSelection(); }}), '全选修改'),
        h('span', {id: 'selected-edits-count', className: 'help'}, '已选 ' + edits.filter(edit => selected.has(edit.id)).length + ' / ' + edits.length + ' 处'),
        button('全不选', () => { box.querySelectorAll('#review-edits input').forEach(input => { input.checked = false; }); syncSelection(); }, 'quiet small', {disabled: !current})));
      box.append(h('div', {id: 'review-edits'}, edits.map((edit, index) => h('article', {className: 'edit-card'},
        h('label', {}, h('input', {type: 'checkbox', value: edit.id, checked: selected.has(edit.id), disabled: !current, onChange: syncSelection}), '接受第 ' + (index + 1) + ' 处修改', tag(edit.risk === 'high' ? '优先看这里' : '表达建议')),
        h('div', {className: 'edit-original'}, edit.original), h('div', {className: 'edit-new'}, edit.replacement || '删除这一段'),
        h('p', {className: 'edit-reason'}, edit.reason)))));
      if (!edits.length) box.append(h('p', {className: 'help'}, '这次没有需要改动的地方，可直接保留原话合并。'));
      box.append(h('div', {className: 'actions'}, button('接受所选，合并全文', () => perform('merge', {review_id: review.id,
        accepted_ids: [...document.querySelectorAll('#review-edits input:checked')].map(input => input.value)}, '干净全文已合并，口述原文仍保留。'), 'primary', {disabled: !current})));
    } else box.append(h('div', {style: 'margin-top:18px'}, empty('建议生成后，在这里逐项接受或保留原文。')));
    return [box, card(h('div', {className: 'card-head'}, h('h2', {}, '干净全文'), button('复制全文', () => copy(work.clean_script), 'quiet small')),
      h('textarea', {id: 'clean-script', className: 'editor clean', rows: 10, value: work.clean_script,
        placeholder: '接受修改后，合并的全文会出现在这里。也可以继续手改。', onInput: event => mark({clean_script: event.target.value})}),
      h('p', {className: 'help', style: 'margin-top:10px'}, '读着还有不顺的地方，可以回到“结构与口述”再讲一遍。'),
      merges.length > 0 && details('回看 ' + merges.length + ' 版合并全文', h('div', {}, [...merges].reverse().map(({review: item, decision}, index) =>
        h('div', {className: 'version', dataset: {testid: 'clean-history'}}, h('time', {}, '第 ' + (merges.length - index) + ' 版 · ' + dateText(decision.created_at)),
          h('p', {}, '接受的修改：' + (decision.accepted_ids?.length ? decision.accepted_ids.map(id => {
            const edit = item.edits?.find(entry => entry.id === id);
            return edit ? edit.original + ' → ' + (edit.replacement || '删除') : id;
          }).join('；') : '未接受修改，保留口述原文')),
          h('pre', {className: 'history-script'}, decision.clean_script || ''))))))];
  }
  function renderFinal() {
    const final = work.final || {};
    function edit(key, value) { mark({final: {...work.final, [key]: value, confirmed: false}}); if ($('final-state')) { $('final-state').className = 'tag amber'; $('final-state').textContent = '待核对'; } }
    const state = tag(final.confirmed ? '已确认' : '待核对', final.confirmed ? 'green' : 'amber'); state.id = 'final-state';
    return [card(h('div', {className: 'card-head'}, h('h2', {}, '实际发布的这一版'), state),
      h('div', {className: 'form-grid three'},
        selectField('final-platform', '发布平台', ['抖音', '小红书', 'B站', '视频号', '其他'], final.platform || '抖音', value => edit('platform', value)),
        textField('final-account', '账号名称', final.account, value => edit('account', value), {placeholder: '例如：我的主账号'}),
        textField('final-duration', '实际时长（秒）', final.duration_seconds, value => edit('duration_seconds', numeric(value)), {type: 'number', attrs: {min: 1, max: 7200}})),
      textField('final-title', '实际标题', final.title, value => edit('title', value), {placeholder: '发布界面中采用的标题'}),
      h('div', {className: 'form-grid'}, textField('final-cover', '实际封面文字', final.cover, value => edit('cover', value), {multiline: true, rows: 3}),
        textField('final-opening', '实际开头', final.opening, value => edit('opening', value), {multiline: true, rows: 3})),
      h('label', {className: 'field'}, '实际拍摄内容', h('textarea', {id: 'final-script', className: 'editor', rows: 11, value: final.script,
        placeholder: '对照成片校正，保留实际说出的话。', onInput: event => edit('script', event.target.value)})),
      h('div', {className: 'actions'}, button('确认与实际成片一致', () => perform('confirm-final', {}, '最终版本已确认，可以分别运行发布前判断。'), 'primary'),
        h('span', {className: 'help'}, '标题、封面、开头和正文都核对过后，再确认。')))];
  }
  function probabilityChart(items) {
    if (!Array.isArray(items)) return null;
    return h('div', {className: 'distribution'}, items.map(item => {
      const probability = Number(item.probability ?? item.percent ?? 0);
      return h('div', {className: 'distribution-row'}, h('span', {}, item.bucket), h('div', {className: 'distribution-bar'}, h('span', {style: 'width:' + Math.max(0, Math.min(100, probability)) + '%'})), h('span', {}, probability + '%'));
    }));
  }
  function judgmentCard(kind) {
    const value = kind === 'value', list = work.judgments?.[kind] || [], record = latest(list), result = record?.result || {};
    const stale = record?.stale ?? (record && record.basis_hash !== work.current_basis_hash);
    const locked = record?.status === 'locked';
    const hasPublished = Boolean(work.publications?.length);
    const box = h('section', {className: 'card judgment-card'}, h('div', {className: 'method'}, value ? 'dbs-content-value' : 'cheat-predict'),
      h('div', {className: 'card-head'}, h('h2', {}, KIND_NAMES[kind]), tag('0 基准', 'amber')),
      h('div', {className: 'zero-note'}, '无个人历史样本 · 独立上下文'),
      h('p', {className: 'description'}, value ? '看谁会关心、开头能否留住人，以及这条内容可能带来的关注与商业价值。' : '按通用起步规则，记录维度评分、播放区间与关键假设，与你的实绩逐次对照。'),
      button(value ? '运行内容价值判断' : '运行内容作弊预测', () => perform(value ? 'judge-value' : 'judge-cheat'), 'secondary run-action', {disabled: !work.final?.confirmed || hasPublished}));
    if (!record) { box.append(empty('运行后先审阅，再确认锁定。')); return box; }
    box.append(h('div', {className: 'judgment-status'}, dateText(record.created_at) + ' · 第 ' + list.length + ' 版 · ',
      stale ? tag('对应旧成片', 'amber') : locked ? tag('已锁定', 'green') : tag('待你审阅', 'purple')));
    if (locked) box.dataset.testid = 'judgment-locked';
    if (!value) {
      const dimensions = result.dimensions || result.scores?.dimensions || record.dimensions || {};
      if (Object.keys(dimensions).length) box.append(h('div', {className: 'scores'}, Object.entries(dimensions).map(([key, item]) => h('span', {title: item.reason || ''}, key + ' ' + (item.score ?? item)))));
      const distribution = probabilityChart(result.probabilities);
      if (distribution) box.append(distribution);
    }
    box.append(report(result.report || result));
    if (!locked) box.append(button(value ? '确认并锁定价值判断' : '确认并锁定盲预测', () => perform('lock-judgment', {kind, judgment_id: record.id}, '这份判断已锁定，原结果会保留。'), 'primary lock-action', {disabled: stale || hasPublished}));
    else box.append(h('p', {className: 'help', style: 'margin-top:15px'}, '锁定于 ' + dateText(record.locked_at) + '，复盘会使用这份原始记录。'));
    const snapshot = record.snapshot || {};
    box.append(details('查看这份判断对应的内容', h('div', {className: 'details-content'},
      '标题：' + (snapshot.title || '') + '\n封面：' + (snapshot.cover || '') + '\n开头：' + (snapshot.opening || '') + '\n\n' + (snapshot.script || ''))));
    if (list.length > 1) box.append(details('前面 ' + (list.length - 1) + ' 个版本', h('div', {}, [...list.slice(0, -1)].reverse().map(old => h('div', {className: 'version'},
      h('time', {}, dateText(old.created_at) + ' · ' + (old.status === 'locked' ? '已锁定' : '未锁定')), report(old.result?.report || old.result))))));
    return box;
  }
  function renderJudgments() {
    return [work.final?.confirmed ? h('div', {className: 'confirmed-banner'}, '正在判断已确认的最终版本。两项分别执行，只使用当前标题、封面、开头和正文。') :
      h('div', {className: 'summary'}, '先到“实际成片”核对并确认最终版本，再运行这两项。'),
      work.publications?.length > 0 && h('div', {className: 'summary'}, '这条作品已登记发布，发布前判断已归档。下一步可录入数据并复盘。'),
      h('div', {className: 'judgment-grid'}, judgmentCard('value'), judgmentCard('cheat'))];
  }
  function metricSummary(metric) {
    const counts = metric.data || metric;
    const num = value => value === null || value === undefined ? '未知' : Number(value).toLocaleString('zh-CN');
    return '播放 ' + num(counts.plays) + ' · 赞 ' + num(counts.likes) + ' · 评论 ' + num(counts.comments_count) + ' · 分享 ' + num(counts.shares);
  }
  function selectedPublication() { return work.publications?.find(item => item.id === activePublication) || latest(work.publications); }
  function selectedMetric() { return work.metrics?.find(item => item.id === activeMetric) || latest(work.metrics); }
  function publicationForm() {
    const final = work.final || {};
    const scope = 'publication', draft = formDraft(scope), save = key => value => draftField(scope, key, value);
    const locked = ['value', 'cheat'].every(kind => (work.judgments?.[kind] || []).some(item => item.status === 'locked' && !item.stale && item.basis_hash === work.current_basis_hash));
    return card(h('div', {className: 'card-head'}, h('h2', {}, '登记实际发布时间'), tag('由你手动发布')),
      h('p', {className: 'card-description'}, '平台与账号使用最终版本中确认的信息。登记后，保留对应的两份发布前判断。'),
      h('div', {className: 'form-grid'}, textField('publish-platform', '平台', final.platform, () => {}, {attrs: {readOnly: true}}),
        textField('publish-account', '账号', final.account, () => {}, {attrs: {readOnly: true}})),
      h('div', {className: 'form-grid'}, textField('published-at', '实际发布时间', draft.published_at || localDate(), save('published_at'), {type: 'datetime-local'}),
        selectField('window-hours', '统一观察窗口', [[24, '发布后 24 小时'], [72, '发布后 72 小时'], [168, '发布后 7 天']], draft.window_hours || 72, save('window_hours'))),
      textField('publication-url', '作品链接（可选）', draft.url || '', save('url'), {type: 'url', placeholder: '粘贴发布后的作品链接'}),
      h('div', {className: 'actions'}, button('登记已发布', () => {
        try { perform('publish', {platform: final.platform, account: final.account, url: $('publication-url').value,
          published_at: isoDate('published-at'), window_hours: Number($('window-hours').value)}); } catch (error) { showError(error); }
      }, 'primary', {disabled: !final.confirmed || !locked}), !locked && h('span', {className: 'help'}, '先分别确认并锁定两份发布前判断。')));
  }
  function renderData() {
    const publication = selectedPublication();
    if (!publication) return [publicationForm()];
    const records = (work.metrics || []).filter(item => item.publication_id === publication.id);
    const scope = 'metrics:' + publication.id, draft = formDraft(scope), save = key => value => draftField(scope, key, value);
    const nodes = [h('div', {className: 'summary'}, h('strong', {}, publication.platform + ' · ' + publication.account),
      h('p', {}, '发布：' + dateText(publication.published_at) + '　观察窗口：' + publication.window_hours + ' 小时'))];
    if (work.publications.length > 1) nodes.push(selectField('selected-publication', '本次录入对应哪次发布', work.publications.map(p => [p.id, p.platform + ' · ' + dateText(p.published_at)]), publication.id,
      id => { activePublication = id; renderWork(); }));
    const stats = [['plays', '播放'], ['likes', '点赞'], ['comments_count', '评论数'], ['shares', '分享'], ['favorites', '收藏']];
    nodes.push(card(h('div', {className: 'card-head'}, h('h2', {}, '本次观察数据'), tag('手动录入', 'purple')),
      textField('observed-at', '这些数据的实际观察时间', draft.observed_at || localDate(), save('observed_at'), {type: 'datetime-local'}),
      h('div', {className: 'form-grid five'}, stats.map(([key, title]) => textField('metric-' + key, title, draft[key] ?? '', save(key), {placeholder: '未知可留空', attrs: {inputMode: 'decimal'}}))),
      textField('metric-completion', '完播率（%，可选）', draft.completion_rate || '', save('completion_rate'), {placeholder: '例如 32.5', attrs: {inputMode: 'decimal'}}),
      textField('metric-comments', '观众的代表评论', draft.comments || '', save('comments'), {multiline: true, rows: 6, placeholder: '每行一条，可写“赞数 | 评论”\n\n12 | 我也有这个问题\n5 | 想听听你后面是怎么做的', note: '尽量保留高赞评论和不同意见；评论本身没有赞数时可只写正文。'}),
      textField('comments-reason', '评论暂缺时，记录原因', draft.comments_reason || '', save('comments_reason'), {placeholder: '例如：暂时没有评论，或评论区已关闭'}),
      h('div', {className: 'actions'}, button('保存这次观察数据', () => {
        try {
          const data = {publication_id: publication.id, observed_at: isoDate('observed-at')};
          for (const [key] of stats) data[key] = numeric($('metric-' + key).value);
          data.completion_rate = numeric($('metric-completion').value);
          data.comments = $('metric-comments').value.split('\n').filter(line => line.trim()).map(line => {
            const split = line.match(/^\s*([\d.,万千wWkK]+)\s*[|｜]\s*(.+)$/);
            return split ? {text: split[2].trim(), likes: numeric(split[1])} : {text: line.trim(), likes: null};
          });
          data.comments_unavailable_reason = $('comments-reason').value.trim();
          perform('metrics', data, '这次观察已保存，原有记录仍保留。');
        } catch (error) { showError(error); }
      }, 'primary'), h('span', {className: 'help'}, '留空会记为未知；填 0 会按真实的 0 保存。未提交的表单暂存在当前浏览器。'))));
    if (records.length) nodes.push(card(h('h2', {}, '已保存的观察'), records.map(item => h('div', {className: 'metric-record', dataset: {testid: 'metric-record'}},
      h('div', {}, h('div', {className: 'metric-heading'}, dateText(item.observed_at), '　', tag(item.early ? '提前观察' : item.late ? '超出观察窗口' : '同一观察窗口', item.early || item.late ? 'amber' : 'green')),
        h('div', {className: 'metric-values'}, metricSummary(item))), button('用这次数据复盘', () => { activeMetric = item.id; goStep(7); }, 'secondary small')))));
    return nodes;
  }
  function observationCard(item) {
    return h('article', {className: 'observation-card'}, h('div', {className: 'source'}, '来自 ' + (item.work_title || work.title) + ' · 一项待继续验证的观察'),
      h('h3', {}, item.text), item.evidence && h('p', {}, '这次依据：' + item.evidence), item.next_action && h('p', {}, '下一条试试：' + item.next_action),
      h('div', {className: 'actions'}, button('用于下一条', () => newWork(item), 'secondary small')));
  }
  function renderRetro() {
    const metrics = work.metrics || [], metric = selectedMetric();
    const matchingRetros = (work.retros || []).filter(item => item.metrics_id === metric?.id);
    const retro = latest(matchingRetros), result = retro?.result || retro || {};
    const nodes = [];
    if (metrics.length) {
      nodes.push(card(h('div', {className: 'card-head'}, h('h2', {}, '选一份观察，开始对照'), tag('cheat-retro')),
        selectField('retro-metric', '这次复盘采用的数据', metrics.map(item => [item.id, dateText(item.observed_at) + ' · ' + metricSummary(item)]), metric?.id,
          id => { activeMetric = id; renderWork(); }), h('p', {className: 'help'}, '两份发布前判断分别与同一次观察对照，已锁定的原结果保留。'),
        button('生成对照复盘', () => perform('retro', {metrics_id: $('retro-metric').value}), 'primary')));
    } else nodes.push(empty('先在“发布与数据”登记发布，保存一份数据和评论，再来复盘。'));
    if (retro) {
      const rawComparisons = Array.isArray(result.comparisons) ? result.comparisons : [];
      const validComparisons = rawComparisons.filter(item => item && typeof item === 'object' && ['value', 'cheat'].includes(item.kind) &&
        ['hypothesis', 'verdict', 'evidence'].every(key => typeof item[key] === 'string' && item[key].trim()));
      nodes.push(card(h('div', {className: 'card-head'}, h('h2', {}, '这次对照的结果'), button('复制复盘', () => copy(result.report || ''), 'quiet small')),
        report(result.report || result, {dataset: {testid: 'retro-report'}}),
        (validComparisons.length !== rawComparisons.length || (result.comparisons != null && !Array.isArray(result.comparisons))) &&
          h('p', {className: 'help'}, '这份旧复盘有无法显示的对照项，请重新生成复盘。'),
        validComparisons.map(item => h('div', {className: 'summary'}, h('strong', {}, KIND_NAMES[item.kind] || item.kind),
          h('p', {}, item.hypothesis), h('p', {}, (item.verdict || '') + '：' + (item.evidence || ''))))));
      const current = observations.filter(item => item.work_id === work.id && item.retro_id === retro.id);
      if (current.length) { nodes.push(h('h2', {className: 'section-label'}, '只选一项相关观察，带到下一条')); nodes.push(...current.map(observationCard)); }
      if (matchingRetros.length > 1) nodes.push(details('回看这份数据前面的 ' + (matchingRetros.length - 1) + ' 次复盘', h('div', {}, [...matchingRetros.slice(0, -1)].reverse().map(item => h('div', {className: 'version'}, h('time', {}, dateText(item.created_at)), report(item.result?.report || item.report))))));
    } else if (metric) nodes.push(empty('这份数据还没有复盘，点击“生成对照复盘”开始。'));
    const older = observations.filter(item => item.work_id !== work.id);
    if (older.length) nodes.push(details('从其他作品的 ' + older.length + ' 项观察中选择', h('div', {style: 'padding-top:15px'}, older.map(observationCard))));
    return nodes;
  }

  const defaults = {chat: {base_url: 'https://api.deepseek.com/v1', model: 'deepseek-flash', effort: 'default'}, responses: {base_url: 'https://api.openai.com/v1', model: '', effort: 'default'}};
  function profileList() { return Array.isArray(settings?.profiles) ? settings.profiles : settings ? [settings] : []; }
  function activeModelSummary() {
    const active = profileList().find(profile => (profile.id || profile.profile_id) === settings?.active_profile_id) || settings;
    $('active-model').textContent = active?.ready ? active.name || active.model : '模型待配置';
    $('active-model').title = active?.model || '';
    $('active-model').hidden = !active;
  }
  function profileChoices(selectedId) {
    const choices = profileList().map(profile => h('option', {value: profile.id || profile.profile_id}, profile.name || profile.model || profile.provider));
    if (!selectedId) choices.unshift(h('option', {value: ''}, '新配置（未保存）'));
    $('saved-model-profile').replaceChildren(...choices);
    $('saved-model-profile').value = selectedId || '';
    const active = profileList().find(profile => (profile.id || profile.profile_id) === settings?.active_profile_id) || settings;
    $('model-active-note').textContent = '当前使用：' + (active?.name || active?.model || '尚未配置') + '。保存后使用所选配置。';
  }
  function keyHint() {
    const same = originalProfile && originalProfile.provider === $('model-provider').value &&
      originalProfile.base_url?.replace(/\/+$/, '') === $('model-base-url').value.trim().replace(/\/+$/, '');
    $('key-status').textContent = same && originalProfile.key_status === 'configured' ? '（已加密保存）' : '（尚未填写）';
  }
  function renderSettings(profile) {
    editingProfileId = profile.id || profile.profile_id || null;
    originalProfile = editingProfileId ? profile : null;
    profileChoices(editingProfileId);
    $('model-profile-name').value = profile.name || '';
    $('model-provider').value = profile.provider || 'chat';
    $('model-base-url').value = profile.base_url || defaults[profile.provider || 'chat'].base_url;
    $('model-name').value = profile.model || '';
    $('model-effort').value = profile.effort || 'default';
    $('model-output-limit').value = profile.max_output_tokens || 32768;
    $('model-key').value = '';
    keyHint();
  }
  function modelFields() {
    return {...(editingProfileId ? {profile_id: editingProfileId} : {}), name: $('model-profile-name').value.trim(),
      provider: $('model-provider').value, base_url: $('model-base-url').value.trim(), model: $('model-name').value.trim(), effort: $('model-effort').value,
      max_output_tokens: Number($('model-output-limit').value),
      ...($('model-key').value.trim() ? {key: $('model-key').value.trim()} : {})};
  }
  function settingsBusy(value) {
    for (const control of $('settings-dialog').querySelectorAll('input,select,button')) control.disabled = value;
  }
  $('open-settings').addEventListener('click', async () => {
    try { settings = await api('/api/settings'); activeModelSummary(); renderSettings(settings); $('settings-notice').textContent = ''; $('settings-dialog').showModal(); }
    catch (error) { showError(error); }
  });
  $('saved-model-profile').addEventListener('change', () => {
    const selected = profileList().find(profile => (profile.id || profile.profile_id) === $('saved-model-profile').value);
    if (selected) { renderSettings(selected); $('settings-notice').textContent = ''; }
  });
  $('new-model-profile').addEventListener('click', () => {
    renderSettings({provider: 'chat', ...defaults.chat, max_output_tokens: 32768});
    $('settings-notice').textContent = ''; $('model-profile-name').focus();
  });
  $('model-provider').addEventListener('change', () => {
    const provider = $('model-provider').value;
    $('model-base-url').value = defaults[provider].base_url;
    $('model-name').value = defaults[provider].model;
    $('model-effort').value = 'default'; $('model-key').value = ''; keyHint();
  });
  $('model-base-url').addEventListener('input', keyHint);
  $('close-settings').addEventListener('click', () => { $('model-key').value = ''; $('settings-dialog').close(); });
  $('settings-form').addEventListener('submit', async event => {
    event.preventDefault();
    const fields = modelFields(), creating = !editingProfileId;
    settingsBusy(true);
    try { settings = await api(creating ? '/api/settings/profiles' : '/api/settings', creating ? 'POST' : 'PUT', fields); activeModelSummary(); $('model-key').value = ''; $('settings-dialog').close(); notify('模型配置已保存并使用。', true); }
    catch (error) { $('settings-notice').textContent = error.message; }
    finally { settingsBusy(false); }
  });
  $('test-model').addEventListener('click', async () => {
    const fields = {...modelFields(), ...(!editingProfileId ? {new_profile: true} : {})};
    settingsBusy(true); $('settings-notice').textContent = '正在测试连接…';
    try { await api('/api/settings/test', 'POST', fields); $('settings-notice').textContent = '连接成功。点击“保存并使用”保留设置。'; }
    catch (error) { $('settings-notice').textContent = error.message; }
    finally { settingsBusy(false); }
  });
  $('new-work').addEventListener('click', () => newWork());
  $('work-search').addEventListener('input', renderList);
  $('work-title').addEventListener('input', event => mark({title: event.target.value}));
  $('work-title').addEventListener('blur', () => flush().then(renderTitle).catch(showError));
  $('delete-work').addEventListener('click', () => requestDelete(work));
  $('cancel-delete').addEventListener('click', () => { $('delete-dialog').close(); deleteTarget = null; });
  $('confirm-delete').addEventListener('click', deleteWork);
  $('delete-dialog').addEventListener('cancel', event => { if (busy) event.preventDefault(); else deleteTarget = null; });
  $('previous-step').addEventListener('click', () => goStep(VISIBLE_STAGES[VISIBLE_STAGES.indexOf(work.stage) - 1]));
  $('next-step').addEventListener('click', advance);
  $('version-history').addEventListener('click', async () => {
    try {
      const history = await api('/api/changelog');
      $('history-content').replaceChildren(report(history)); $('history-dialog').showModal();
    } catch (error) { showError(error); }
  });
  $('close-history').addEventListener('click', () => $('history-dialog').close());
  $('sidebar-toggle').addEventListener('click', () => { const opened = $('sidebar').classList.toggle('open'); $('sidebar-toggle').setAttribute('aria-expanded', String(opened)); });
  $('close-conflict').addEventListener('click', () => { $('conflict-dialog').close(); showError(new Error('当前输入已保留。你可以随时重新打开两个版本核对。')); });
  $('load-server').addEventListener('click', () => {
    if (!conflictWork?.id) return;
    work = conflictWork; conflictWork = null; pending = {}; inFlight = {}; stash();
    if (restoredDraftKey) { localStorage.removeItem(restoredDraftKey); restoredDraftKey = null; }
    $('conflict-dialog').close(); status('saved', '已载入磁盘版本'); renderWork(); notify('已载入磁盘版本。', true);
  });
  window.addEventListener('beforeunload', () => { closing = true; stash(); markFormsClosed(true); });
  window.addEventListener('pagehide', () => { closing = true; stash(); markFormsClosed(true); });
  window.addEventListener('pageshow', () => { closing = false; stash(); markFormsClosed(false); });
  document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'hidden') { stash(); flush().catch(() => {}); } });
  (async () => {
    try {
      const data = await api('/api/works'); works = data.works || [];
      const health = await api('/api/health');
      $('version-history').textContent = 'v' + health.version + ' · 更新记录';
      await loadObservations();
      try { settings = await api('/api/settings'); activeModelSummary(); } catch (_) { /* Settings remain available for retry in the dialog. */ }
      const previous = localStorage.getItem(KEYS.last);
      const id = works.some(item => item.id === previous) ? previous : works[0]?.id;
      if (id) await selectWork(id); else renderWork();
    } catch (error) { showError(error); }
    finally { $('new-work').disabled = false; document.body.dataset.ready = 'true'; }
  })();
})();
