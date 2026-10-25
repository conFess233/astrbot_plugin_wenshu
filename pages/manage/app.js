const $ = (selector) => document.querySelector(selector);
const bridge = window.AstrBotPluginPage;
const state = { saved: null, draft: null, revision: '', selected: '', resources: {}, records: [], busy: 0, body: '' };
const clone = (value) => structuredClone(value);
const enums = {
  mode: [['generate','检索并生成文案'],['direct','直接发送固定文案（不判定）'],['detect_only','仅判定后发送固定文案']],
  search_mode: [['required','必须搜索'],['auto','模型按需'],['off','关闭搜索']],
  failure_policy: [['stop','提示失败并结束'],['grounded','已有可靠资料降级'],['passthrough','交回普通聊天（可能调用主模型）']],
  persona_mode: [['current','继承当前人格'],['specified','使用指定人格'],['none','不加载人格']],
  group_scope: [['wake','跟随宿主唤醒规则'],['all','监听所有群消息']],
};
const labels = { enabled:'启用文枢', detect_provider:'意图检测模型', generate_provider:'搜索与文案生成模型',
  search_mode:'联网搜索模式', failure_policy:'失败策略', persona_mode:'写作人格', persona_id:'指定人格',
  kb_ids:'知识库', group_scope:'群聊检测范围', max_concurrency:'全局并发上限', history_messages:'只读历史最大条数',
  history_chars:'只读历史最大字符数', max_tool_calls:'每任务工具调用上限', task_timeout:'任务总时限（秒）', record_limit:'摘要保留条数' };
const numeric = { max_concurrency:[1,16], history_messages:[0,50], history_chars:[0,30000], max_tool_calls:[1,30], task_timeout:[15,600], record_limit:[0,1000] };
const overrides = ['detect_provider','generate_provider','persona_mode','persona_id','kb_ids','search_mode','failure_policy','group_scope'];
const statusLabels = { success:'成功', degraded:'降级完成', failed:'失败', passthrough:'交回普通聊天', no_match:'不匹配', needs_topic:'缺少主题', send_unknown:'发送结果未知' };

function el(tag, content, className) {
  const node = document.createElement(tag);
  if (content !== undefined) node.textContent = content;
  if (className) node.className = className;
  return node;
}
function notify(message = '', error = false) {
  $('#status').textContent = message;
  $('#status').classList.toggle('error', error);
}
function dirty() { return state.draft && JSON.stringify(state.draft) !== JSON.stringify(state.saved); }
function changed() { $('#save-bar').hidden = !dirty(); }
function confirmAction(title, body, action = '确认') {
  const dialog = $('#confirm-dialog');
  const previous = document.activeElement;
  $('#confirm-title').textContent = title;
  $('#confirm-body').textContent = body;
  $('#confirm-ok').textContent = action;
  dialog.returnValue = 'cancel';
  dialog.showModal();
  return new Promise((resolve) => dialog.addEventListener('close', () => {
    previous?.focus(); resolve(dialog.returnValue === 'ok');
  }, { once:true }));
}
async function task(work) {
  state.busy++;
  const previousDisabled = new Map([...document.querySelectorAll('button,input,select,textarea')].map((node) => [node,node.disabled]));
  for (const node of previousDisabled.keys()) node.disabled = true;
  try { return await work(); }
  catch (error) { notify(error.message || '操作失败，请重试。', true); }
  finally {
    state.busy--;
    for (const [node,disabled] of previousDisabled) node.disabled = disabled;
    $('#copy').disabled = !state.body;
    changed();
  }
}
function listOptions(key) {
  if (enums[key]) return enums[key];
  if (key.endsWith('_provider')) return [['','请选择 provider'], ...(state.resources.providers || []).map((p) => [p.id, `${p.id}${p.model ? ' · ' + p.model : ''}`])];
  if (key === 'persona_id') return [['','请选择人格'], ...(state.resources.personas || []).map((p) => [p.id,p.name])];
  if (key === 'kb_ids') return (state.resources.knowledge_bases || []).map((k) => [k.id,k.name]);
  return null;
}
function toggleButton(checked, label, update) {
  const node = el('button', undefined, 'toggle-button');
  node.type = 'button'; node.setAttribute('role','switch');
  node.setAttribute('aria-label',label);
  const track = el('span', undefined, 'toggle-track'); track.setAttribute('aria-hidden','true');
  track.append(el('span', undefined, 'toggle-thumb'));
  node.append(track,el('span',undefined,'toggle-state'));
  setToggle(node,checked);
  node.addEventListener('click', () => {
    const next = node.getAttribute('aria-checked') !== 'true';
    setToggle(node,next); update(next); changed();
  });
  return node;
}
function setToggle(node, checked) {
  node.setAttribute('aria-checked',String(checked));
  node.querySelector('.toggle-state').textContent = checked ? '已启用' : '未启用';
}
function presetToggle(p) {
  const node = toggleButton(p.enabled,`启用预设「${p.name}」`,(value) => {
    p.enabled = value;
    for (const toggle of document.querySelectorAll('[data-preset-toggle]')) {
      if (toggle.dataset.presetToggle === p.id) setToggle(toggle,value);
    }
  });
  node.dataset.presetToggle = p.id;
  return node;
}
function presetEnabledField(p) {
  const wrapper = el('div',undefined,'field-label');
  const toggle = presetToggle(p); toggle.dataset.field = 'enabled';
  wrapper.append(el('span','启用这份预设'),toggle);
  return wrapper;
}
function field(key, value, update, labelText = labels[key], help = '') {
  if (typeof value === 'boolean') {
    const wrapper = el('div',undefined,'field-label');
    const input = toggleButton(value,labelText,update); input.dataset.field = key;
    wrapper.append(el('span',labelText),input);
    if (help) wrapper.append(el('small',help));
    return wrapper;
  }
  const label = el('label', labelText);
  let input;
  const options = typeof value === 'boolean' ? null : listOptions(key);
  if (options) {
    input = el('select');
    if (key === 'kb_ids') input.multiple = true;
    const existing = new Set(options.map(([id]) => id));
    const values = Array.isArray(value) ? value : [value];
    for (const missing of values.filter((v) => v && !existing.has(v))) options.push([missing, `${missing}（当前不可用）`]);
    for (const [id,name] of options) {
      const option = el('option', name); option.value = id;
      option.selected = values.includes(id); input.append(option);
    }
    input.addEventListener('change', () => { update(input.multiple ? [...input.selectedOptions].map((o) => o.value) : input.value); changed(); });
  } else {
    input = el('input'); input.type = typeof value === 'boolean' ? 'checkbox' : numeric[key] ? 'number' : 'text';
    if (numeric[key]) [input.min,input.max] = numeric[key];
    if (input.type === 'checkbox') input.checked = value; else input.value = value;
    input.addEventListener('input', () => { update(input.type === 'checkbox' ? input.checked : input.type === 'number' ? Number(input.value) : input.value); changed(); });
  }
  input.dataset.field = key;
  label.append(input);
  if (help) label.append(el('small', help));
  return label;
}
function area(title, value, update, { rows=5, help='', mono=false } = {}) {
  const label = el('label', title);
  const input = el('textarea'); input.value = value; input.rows = rows;
  if (rows >= 9) input.classList.add('large');
  if (mono) input.style.fontFamily = 'Consolas,monospace';
  input.addEventListener('input', () => { update(input.value); changed(); });
  label.append(input); if (help) label.append(el('small', help)); return label;
}
function button(label, action, style = 'secondary') {
  const node = el('button', label, style); node.type = 'button'; node.addEventListener('click', action); return node;
}
function current() { return state.draft.presets.find((p) => p.id === state.selected); }
function renderList() {
  const list = $('#preset-list'); list.replaceChildren();
  const query = $('#filter').value.trim().toLowerCase();
  const rows = state.draft.presets.filter((p) => `${p.name} ${p.description}`.toLowerCase().includes(query));
  if (!rows.length) list.append(el('div', query ? '没有符合条件的预设。' : '书架还是空的，新建或载入示例吧。', 'empty'));
  for (const p of rows) {
    const node = el('article', undefined, `preset-item${p.id === state.selected ? ' active' : ''}`);
    const select = el('button',undefined,'preset-select'); select.type = 'button';
    select.append(el('strong',p.name),el('p',p.description || '尚未填写说明'),
      el('small',`优先级 ${p.priority} · ${p.keywords.length + p.patterns.length} 条规则`),
      el('span',displayValue('mode',p.mode || 'generate'),'badge'));
    select.setAttribute('aria-pressed',String(p.id === state.selected));
    select.addEventListener('click', () => { state.selected = p.id; renderList(); renderEditor(); });
    const toggle = presetToggle(p); toggle.classList.add('compact');
    node.append(select,toggle);
    list.append(node);
  }
}
function renderGlobal() {
  const box = $('#global-fields'); box.replaceChildren();
  for (const key of Object.keys(labels)) {
    const help = key === 'failure_policy' ? '交回普通聊天可能调用主模型；可靠资料降级仍要求资料足够。'
      : key === 'kb_ids' ? '可多选；空列表关闭知识库检索。资料导入仍在宿主管理。'
      : key === 'search_mode' ? '搜索服务商和凭证随当前聊天的有效 profile 读取，不在文枢重复填写。'
      : key === 'record_limit' ? '0 表示不保留记录；仅保存摘要，不保存完整正文。' : '';
    box.append(field(key, state.draft[key], (v) => { state.draft[key] = v; }, labels[key], help));
  }
}
function renderEditor() {
  const box = $('#editor'); box.replaceChildren();
  const p = current();
  if (!p) { box.append(el('div', '选择一份预设，或从新建开始。', 'empty')); return; }
  const header = el('div', undefined, 'editor-heading');
  header.append(el('h2', '编辑预设'));
  const actions = el('div', undefined, 'actions');
  actions.append(button('复制', () => { const duplicate = clone(p); duplicate.id = crypto.randomUUID(); duplicate.name += ' · 副本'; duplicate.enabled = false; state.draft.presets.push(duplicate); state.selected = duplicate.id; renderList(); renderEditor(); changed(); }),
    button('上移', () => movePreset(-1)), button('下移', () => movePreset(1)),
    button('删除', async () => { if (await confirmAction('删除预设', `将从草稿删除「${p.name}」，保存后生效。`, '删除')) { state.draft.presets = state.draft.presets.filter((x) => x !== p); state.selected = ''; renderList(); renderEditor(); changed(); } }, 'danger'));
  header.append(actions); box.append(header);
  const basic = el('div', undefined, 'form-grid');
  basic.append(field('name', p.name, (v) => { p.name = v; renderList(); $('#editor [data-field="enabled"]').setAttribute('aria-label',`启用预设「${p.name}」`); }, '预设名称'), field('id', p.id, (v) => { p.id = v; state.selected = v; renderList(); $('#editor [data-field="enabled"]').dataset.presetToggle = v; }, '稳定 ID'),
    presetEnabledField(p));
  numeric.priority = [-100000,100000];
  basic.append(field('priority', p.priority, (v) => { p.priority = v; renderList(); }, '优先级（越大越优先）'));
  basic.append(field('mode',p.mode || 'generate',(value) => { p.mode = value; renderList(); renderEditor(); },'执行模式'));
  box.append(basic, area('说明', p.description, (v) => { p.description = v; renderList(); }, {rows:2}));
  const trigger = el('div', undefined, 'editor-section'); trigger.append(el('h3','01 / 触发与意图'));
  const lines = (v) => v.split('\n').filter((line) => line.trim());
  trigger.append(area('关键词 · 每行一个', p.keywords.join('\n'), (v) => { p.keywords = lines(v); }, {rows:2,help:p.mode === 'direct' ? '任一规则命中后直接发送，不调用任何模型。' : '关键词或正则任一命中即进入候选，再由检测模型判断意图。'}),
    area('正则表达式 · 每行一条', p.patterns.join('\n'), (v) => { p.patterns = lines(v); }, {rows:3,mono:true,help:'直接输入实际正则，不自动去转义；单条匹配超时将提示失败。'}));
  if (p.mode !== 'direct') trigger.append(area('意图判定说明', p.intent, (v) => { p.intent = v; }, {rows:4,help:'描述何时执行、何时不执行。仅判定模式不要求补充主题，也不调用生成模型。'}));
  box.append(trigger);
  if ((p.mode || 'generate') === 'generate') {
    const writing = el('div', undefined, 'editor-section'); writing.append(el('h3','02 / 写作要求与完整范文'));
    writing.append(area('生成要求', p.prompt, (v) => { p.prompt = v; }, {rows:9}), area('完整范文', p.example, (v) => { p.example = v; }, {rows:12,help:'完整保存原文；范文决定风格，事实须另行核实。'}));
    const format = el('div', undefined, 'form-grid'); numeric.paragraphs = [0,30];
    format.append(field('opening', p.opening, (v) => { p.opening = v; }, '固定开头（留空不检查）'), field('paragraphs', p.paragraphs, (v) => { p.paragraphs = v; }, '正文段落数（0 不检查）'));
    writing.append(format); box.append(writing);
  } else {
    const writing = el('div',undefined,'editor-section');
    writing.append(el('h3','02 / 固定文案'),area('固定文案（原样发送）',p.reply || '',(value) => { p.reply = value; },
      {rows:12,help:'不搜索、不检索知识库、不调用生成模型，不替换变量。切换模式保留原有提示词和范文。'}));
    box.append(writing);
  }
  const overrideBox = el('div', undefined, 'editor-section'); overrideBox.append(el('h3','03 / 模型与行为覆盖'), el('p','未勾选时继承全局设置；空知识库覆盖表示关闭检索。'));
  const availableOverrides = p.mode === 'direct' ? ['failure_policy','group_scope'] : p.mode === 'detect_only' ? ['detect_provider','failure_policy','group_scope'] : overrides;
  for (const key of availableOverrides) {
    const row = el('div', undefined, 'override-row'); const enabled = Object.hasOwn(p.overrides,key);
    row.append(field('override_'+key, enabled, (checked) => { if (checked) p.overrides[key] = clone(state.draft[key]); else delete p.overrides[key]; renderEditor(); }, `单独设置${labels[key]}`));
    if (enabled) row.append(field(key, p.overrides[key], (v) => { p.overrides[key] = v; }));
    else row.append(el('div', '继承：' + displayValue(key,state.draft[key]), 'effective'));
    overrideBox.append(row);
  }
  box.append(overrideBox);
}
function displayValue(key,value) {
  if (Array.isArray(value)) return value.length ? value.join('、') : '不检索知识库';
  return listOptions(key)?.find(([id]) => id === value)?.[1] || value || '尚未设置';
}
function movePreset(delta) {
  const index = state.draft.presets.indexOf(current()), target = index + delta;
  if (target < 0 || target >= state.draft.presets.length) return;
  [state.draft.presets[index],state.draft.presets[target]] = [state.draft.presets[target],state.draft.presets[index]];
  renderList(); renderEditor(); changed();
}
function renderRecords() {
  const list = $('#record-list'); list.replaceChildren();
  const query = $('#record-filter').value.toLowerCase(), wanted = $('#record-status').value;
  const rows = state.records.filter((r) => (!wanted || r.status === wanted) && `${r.preset_name} ${r.preset_id}`.toLowerCase().includes(query));
  if (!rows.length) list.append(el('div','没有符合条件的运行摘要。','card empty'));
  for (const row of rows) {
    const node = el('article',undefined,'card record'); const detail = el('div',undefined,'record-detail');
    detail.append(el('strong',row.preset_name || '意图检测'),el('div', `${new Date(row.time).toLocaleString()} · ${row.test ? '页面试运行' : '聊天请求'} · ${row.duration_ms} ms`, 'record-meta'),
      el('div', `检测 ${row.detect_provider || '—'} / 生成 ${row.generate_provider || '—'} · 工具 ${row.tool_calls} 次 · 搜索成功 ${row.search_successes} 次`, 'record-meta'));
    if (row.usage) detail.append(el('div', `Tokens：${JSON.stringify(row.usage)}${row.usage_complete ? '' : '（部分调用未返回用量）'}`, 'record-meta'));
    else detail.append(el('div','用量不可用','record-meta'));
    if (row.reason) detail.append(el('p',row.reason));
    node.append(detail,el('span',statusLabels[row.status] || row.status,'badge')); list.append(node);
  }
}
async function load() {
  const [settings,resources] = await Promise.all([bridge.apiGet('settings'),bridge.apiGet('resources')]);
  state.saved = clone(settings.config); state.draft = clone(settings.config); state.revision = settings.revision; state.resources = resources;
  if (!state.draft.presets.some((p) => p.id === state.selected)) state.selected = state.draft.presets[0]?.id || '';
  renderList(); renderGlobal(); renderEditor(); changed(); notify('已连接 · 仅在命中预设时独立调用模型');
}
async function runTest(mode) {
  if (mode !== 'rules' && state.draft.presets.some((p) => p.enabled && p.mode !== 'direct') && !await confirmAction('确认模型调用费用', '本次按当前草稿的模式执行：直接发送不调用模型；仅判定只调用检测模型；检索生成可能调用检测、生成和检索服务。结果仅在页面预览，不发送聊天。', '开始测试')) return;
  await task(async () => {
    $('#test-progress').textContent = mode === 'rules' ? '正在检查规则…' : '正在执行，请稍候；完整任务可能需要数分钟。';
    try {
      let history; try { history = JSON.parse($('#test-history').value); } catch { throw new Error('历史 JSON 无法解析'); }
      let result = await bridge.apiPost('test', { config:clone(state.draft), message:$('#test-message').value, mode, confirm_cost:mode !== 'rules',
        is_group:$('#test-group').checked, awake:$('#test-awake').checked, sender:$('#test-sender').value, umo:$('#test-umo').value, history });
      if (result.job_id) {
        const id = result.job_id;
        while (true) {
          const job = await bridge.apiGet('test-result', {id});
          if (job.status === 'done') { result = job.result; break; }
          if (job.status === 'failed') throw new Error(job.error || '试运行失败');
          await new Promise((resolve) => setTimeout(resolve,750));
        }
      }
      state.body = result.body || ''; $('#test-output').textContent = state.body || JSON.stringify(result.decision || result.candidates,null,2);
      $('#test-meta').textContent = `候选：${result.candidates.map((p) => p.name).join('、') || '无'} · ${result.status ? statusLabels[result.status] || result.status : '规则检查完成'}${result.detect_provider ? ' · 检测：' + result.detect_provider : ''}${result.generate_provider ? ' · 生成：' + result.generate_provider : ''} · 手工历史 / 当前草稿`;
      $('#test-sources').replaceChildren();
      for (const source of result.sources || []) { const link = el('a',source.title || source.url); const url = new URL(source.url); if (!['http:','https:'].includes(url.protocol)) continue; link.href = url.href; link.target = '_blank'; link.rel = 'noopener noreferrer'; $('#test-sources').append(link); }
      notify('试运行完成，未保存配置、未发送聊天。');
    } finally { $('#test-progress').textContent = ''; }
  });
}

$('#add').addEventListener('click', () => {
  const p = {__template_key:'preset',id:crypto.randomUUID(),name:'新预设',description:'',enabled:false,priority:0,mode:'generate',reply:'',keywords:[],patterns:[],intent:'',prompt:'',example:'',opening:'',paragraphs:0,overrides:{}};
  state.draft.presets.push(p); state.selected = p.id; renderList(); renderEditor(); changed();
});
$('#examples').addEventListener('click', async () => {
  const examples = await task(() => bridge.apiGet('examples'));
  if (!examples || !await confirmAction(`载入 ${examples.presets.length} 份示例`, examples.notes.join('\n') + '\n\n示例作为未启用草稿加入，原有预设不会被覆盖。', '加入草稿')) return;
  for (const example of examples.presets) { const p = clone(example); p.id = crypto.randomUUID(); state.draft.presets.push(p); }
  state.selected = state.draft.presets.at(-1).id; renderList(); renderEditor(); changed();
});
$('#filter').addEventListener('input', renderList);
$('#record-filter').addEventListener('input',renderRecords); $('#record-status').addEventListener('change',renderRecords);
$('#save').addEventListener('click', () => task(async () => {
  const result = await bridge.apiPost('settings',{config:clone(state.draft),revision:state.revision});
  state.saved = clone(result.config); state.draft = clone(result.config); state.revision = result.revision;
  renderList(); renderGlobal(); renderEditor(); notify('配置已保存，后续任务使用新配置。');
}));
$('#discard').addEventListener('click', async () => { if (await confirmAction('放弃修改','恢复到上次加载或保存的配置，未保存内容将被丢弃。','放弃修改')) { state.draft = clone(state.saved); renderList(); renderGlobal(); renderEditor(); changed(); } });
$('#refresh').addEventListener('click', async () => { if (dirty() && !await confirmAction('重新加载','当前草稿尚未保存，继续将丢弃草稿并读取宿主最新配置。','重新加载')) return; await task(load); });
$('#load-records').addEventListener('click', () => task(async () => { state.records = (await bridge.apiGet('records')).items; renderRecords(); }));
for (const tab of document.querySelectorAll('[data-tab]')) tab.addEventListener('click', async () => {
  document.querySelectorAll('.panel').forEach((p) => { p.hidden = p.id !== tab.dataset.tab; });
  document.querySelectorAll('.tab').forEach((b) => { b.classList.toggle('active',b === tab); b.setAttribute('aria-selected',String(b === tab)); });
  if (tab.dataset.tab === 'global') renderGlobal();
  if (tab.dataset.tab === 'presets') renderEditor();
  if (tab.dataset.tab === 'records') await task(async () => { state.records = (await bridge.apiGet('records')).items; renderRecords(); });
});
for (const node of document.querySelectorAll('[data-test]')) node.addEventListener('click', () => runTest(node.dataset.test));
$('#copy').addEventListener('click', () => task(async () => { await navigator.clipboard.writeText(state.body); notify('正文已复制。'); }));
window.addEventListener('beforeunload',(event) => { if (dirty()) { event.preventDefault(); event.returnValue = ''; } });
if (!bridge) { notify('请从 AstrBot 插件详情中的 Plugin Pages 打开文枢管理页面。',true); document.querySelectorAll('button').forEach((b) => { b.disabled = true; }); }
else { await task(async () => { await bridge.ready(); await load(); }); }

$('#test-umo').addEventListener('change', () => task(async () => {
  if (!$('#test-umo').value.trim()) { $('#test-profile').textContent = '搜索配置随填写的实际会话读取。'; return; }
  const result = await bridge.apiGet('resources', {umo:$('#test-umo').value.trim()});
  const search = result.search;
  $('#test-profile').textContent = `有效配置：${search.profile_id} · 搜索服务：${search.provider} · ${search.enabled ? '已启用（权限与凭证由运行时检查）' : '未启用'}`;
}));
