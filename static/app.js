const token = new URLSearchParams(location.search).get('token') || '';
const $ = (id) => document.getElementById(id);
let state = { bots: [], groups: [], grants: [], key_status: {}, runs: [] };
let selectedBotId = null;
let selectedGroupId = new URLSearchParams(location.search).get('group');
let selectedFiles = new Set();
let drafts = new Map();
let pollTimer = null;
let workflowViewKey = '';

async function api(path, body) {
  const options = { headers: { 'X-Local-Token': token } };
  if (body !== undefined) {
    options.method = 'POST';
    options.headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}

function notice(message, error = false) {
  const box = $('notice');
  box.textContent = message;
  box.classList.toggle('error', error);
  box.hidden = false;
  clearTimeout(notice.timer);
  notice.timer = setTimeout(() => { box.hidden = true; }, 5500);
}

function node(tag, className, text) {
  const item = document.createElement(tag);
  if (className) item.className = className;
  if (text !== undefined) item.textContent = text;
  return item;
}

function page() { return location.pathname === '/chats' ? 'chats' : 'bots'; }

function navigate(destination, groupId = selectedGroupId, push = true) {
  const params = new URLSearchParams({ token });
  if (destination === 'chats' && groupId) params.set('group', groupId);
  if (push) history.pushState({}, '', `/${destination}?${params}`);
  $('bots-page').hidden = destination !== 'bots';
  $('chats-page').hidden = destination !== 'chats';
  $('nav-bots').classList.toggle('active', destination === 'bots');
  $('nav-chats').classList.toggle('active', destination === 'chats');
  if (destination === 'bots') { renderBotList(); renderBotEditor(); }
  else { renderGroupList(); renderChat(true); }
}

function findBot(id) { return state.bots.find((bot) => bot.id === id); }
function findGroup(id) { return state.groups.find((group) => group.id === id); }

function renderBotList() {
  const host = $('bot-list'); host.replaceChildren();
  $('bot-count').textContent = String(state.bots.length);
  if (!state.bots.length) host.append(node('div', 'empty-list', 'No bots yet.'));
  for (const bot of state.bots) {
    const button = node('button', `rail-item${bot.id === selectedBotId ? ' active' : ''}`);
    button.type = 'button';
    const avatar = node('span', 'rail-avatar', bot.name.slice(0, 1).toUpperCase());
    const copy = node('span', 'rail-copy');
    copy.append(node('strong', null, bot.name), node('small', null, bot.role || 'Role not set'));
    button.append(avatar, copy);
    button.addEventListener('click', () => selectBot(bot.id));
    host.append(button);
  }
}

function formDraft() {
  return {
    name: $('bot-name').value,
    role: $('bot-role').value,
    assignment: $('bot-assignment').value,
    endpoint: $('bot-endpoint').value,
    model: $('bot-model').value,
    api_format: $('bot-format').value,
    auth_header: $('bot-auth-header').value,
    auth_prefix: $('bot-auth-prefix').value,
    headers_text: $('bot-headers').value,
    request_template: $('bot-template').value,
    response_path: $('bot-response-path').value,
  };
}

function botDraft(bot) {
  return {
    name: bot.name, role: bot.role, assignment: bot.assignment,
    endpoint: bot.endpoint, model: bot.model, api_format: bot.api_format,
    auth_header: bot.auth_header, auth_prefix: bot.auth_prefix,
    headers_text: JSON.stringify(bot.extra_headers, null, 2),
    request_template: bot.request_template, response_path: bot.response_path,
  };
}

function stashDraft() {
  if (selectedBotId && !$('bot-editor').hidden) drafts.set(selectedBotId, formDraft());
}

function selectBot(id) {
  if (id === selectedBotId) return;
  stashDraft();
  selectedBotId = id;
  renderBotList();
  renderBotEditor();
}

function renderBotEditor() {
  const bot = findBot(selectedBotId);
  $('bot-empty').hidden = !!bot;
  $('bot-editor').hidden = !bot;
  if (!bot) return;
  const draft = drafts.get(bot.id) || botDraft(bot);
  $('editor-title').textContent = draft.name || 'New bot';
  $('bot-name').value = draft.name;
  $('bot-role').value = draft.role;
  $('bot-assignment').value = draft.assignment;
  $('bot-endpoint').value = draft.endpoint;
  $('bot-model').value = draft.model;
  $('bot-format').value = draft.api_format;
  $('bot-auth-header').value = draft.auth_header;
  $('bot-auth-prefix').value = draft.auth_prefix;
  $('bot-headers').value = draft.headers_text;
  $('bot-template').value = draft.request_template;
  $('bot-response-path').value = draft.response_path;
  $('bot-key').value = '';
  $('custom-fields').hidden = draft.api_format !== 'custom';
  const hasKey = !!state.key_status[bot.id];
  $('bot-key-status').textContent = hasKey ? 'Key set for session' : 'No key';
  $('bot-key-status').classList.toggle('ready', hasKey);
  $('save-key').textContent = hasKey ? 'Replace key' : 'Set key';
  renderBotGroups();
}

function switchRow(title, subtitle, checked, onToggle) {
  const row = node('div', 'toggle-row');
  const copy = node('div', 'toggle-copy');
  copy.append(node('strong', null, title));
  if (subtitle) copy.append(node('small', null, subtitle));
  const toggle = node('button', 'switch');
  toggle.type = 'button'; toggle.setAttribute('role', 'switch');
  toggle.setAttribute('aria-label', `${title} ${checked ? 'on' : 'off'}`);
  toggle.setAttribute('aria-checked', String(checked));
  toggle.addEventListener('click', onToggle);
  row.append(copy, toggle);
  return row;
}

async function setMembership(groupId, botId, enabled) {
  try {
    stashDraft();
    state = await api(`/api/groups/${groupId}/members`, { bot_id: botId, enabled });
    renderBotList(); renderBotGroups(); renderGroupList(); renderGroupMembers(); renderRunSettings(); updateGroupSummary();
  } catch (error) { notice(error.message, true); }
}

function renderBotGroups() {
  const host = $('bot-groups'); host.replaceChildren();
  if (!selectedBotId) return;
  if (!state.groups.length) {
    host.append(node('div', 'empty-list', 'No groups yet. Create one on the Group chats page.'));
    return;
  }
  for (const group of state.groups) {
    const joined = group.bot_ids.includes(selectedBotId);
    host.append(switchRow(group.name, `${group.bot_ids.length} bot${group.bot_ids.length === 1 ? '' : 's'}`, joined, () => setMembership(group.id, selectedBotId, !joined)));
  }
}

function renderGroupList() {
  const host = $('group-list'); host.replaceChildren();
  $('group-count').textContent = String(state.groups.length);
  if (!state.groups.length) host.append(node('div', 'empty-list', 'No group chats yet.'));
  for (const group of state.groups) {
    const button = node('button', `rail-item${group.id === selectedGroupId ? ' active' : ''}`);
    button.type = 'button';
    const avatar = node('span', 'rail-avatar', group.name.slice(0, 1).toUpperCase());
    const copy = node('span', 'rail-copy');
    const botCount = group.bot_ids.length;
    const messageCount = group.messages.length;
    copy.append(node('strong', null, group.name), node('small', null, `${botCount} bot${botCount === 1 ? '' : 's'} · ${messageCount} message${messageCount === 1 ? '' : 's'}`));
    button.append(avatar, copy);
    button.addEventListener('click', () => selectGroup(group.id));
    host.append(button);
  }
}

function selectGroup(id) {
  selectedGroupId = id;
  navigate('chats', id);
}

function latestRun(groupId) { return state.runs.find((run) => run.group_id === groupId); }

function renderRunSettings() {
  const group = findGroup(selectedGroupId);
  if (!group) return;
  $('group-mode').value = group.mode || 'conversation';
  $('workflow-settings').hidden = $('group-mode').value !== 'workflow';
  const master = $('group-master'); master.replaceChildren();
  if (!group.bot_ids.length) master.append(new Option('Add bots first', ''));
  for (const id of group.bot_ids) {
    const bot = findBot(id);
    if (bot) master.append(new Option(bot.name, id));
  }
  master.value = group.master_id || group.bot_ids[0] || '';
  $('group-quality').value = group.quality_criteria || '';
}

function runSettingsDraft() {
  return { mode: $('group-mode').value, master_id: $('group-master').value, quality_criteria: $('group-quality').value };
}

function renderWorkflowProgress(group, run) {
  const flow = run && run.mode === 'workflow' ? run.workflow : group.mode === 'workflow' ? group.last_workflow : null;
  const host = $('workflow-progress');
  const signature = JSON.stringify([group.id, flow]);
  if (signature === workflowViewKey) return;
  workflowViewKey = signature;
  host.replaceChildren(); host.hidden = !flow;
  if (!flow) return;
  const phases = { starting: 'Starting workflow', working: 'Worker preparing result', revising: 'Worker revising result', reviewing: 'Master reviewing quality', confirming: 'Worker confirming completion', handoff: 'Both approved · handing off', complete: 'All tasks approved', blocked: 'Waiting for your input', stopped: 'Workflow stopped' };
  host.append(node('strong', null, phases[flow.phase] || 'Task workflow'));
  const results = Array.isArray(flow.approved_results) ? flow.approved_results : [];
  host.append(node('p', 'section-note', `${results.length}/${flow.task_total} tasks approved by both bots${flow.phase !== 'complete' && flow.current_worker ? ` · Current worker: ${flow.current_worker}` : ''}${flow.revision ? ` · Revision ${flow.revision}` : ''}`));
  for (const result of results) {
    const details = document.createElement('details');
    details.append(node('summary', null, `${result.bot_name} · approved result`), node('pre', null, result.result));
    host.append(details);
  }
}

function updateGroupSummary() {
  const group = findGroup(selectedGroupId);
  if (!group) return;
  $('group-summary').textContent = `${group.bot_ids.length} bot${group.bot_ids.length === 1 ? '' : 's'} in this room · ${group.messages.length} message${group.messages.length === 1 ? '' : 's'}`;
  $('member-count').textContent = String(group.bot_ids.length);
  const run = latestRun(group.id);
  const status = $('run-status');
  status.className = 'pill';
  if (run && run.status === 'running') {
    if (run.mode === 'workflow') status.textContent = `Workflow · ${run.workflow?.approved_results?.length || 0}/${Math.max(0, run.total - 1)} approved`;
    else if (run.continuous) status.textContent = `Ongoing · ${run.completed} turns`;
    else if (run.duration_seconds) {
      const seconds = Math.max(0, Math.ceil(run.ends_at - Date.now() / 1000));
      status.textContent = `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')} left · ${run.completed} turns`;
    } else status.textContent = `${run.completed}/${run.total} replying`;
    status.classList.add('running');
  }
  else if (run && run.status === 'failed') { status.textContent = 'Needs attention'; status.classList.add('failed'); }
  else if (run && run.status === 'blocked') { status.textContent = 'Needs input'; status.classList.add('failed'); }
  else if (run && run.status === 'complete') { status.textContent = run.completion_reason === 'time_limit' ? `Time finished · ${run.completed} turns` : 'Complete'; status.classList.add('ready'); }
  else if (run && run.status === 'stopped') status.textContent = `Stopped · ${run.completed} turns`;
  else status.textContent = 'Ready';
  $('run-error').hidden = !(run && ['failed', 'blocked'].includes(run.status));
  $('run-error').textContent = run && ['failed', 'blocked'].includes(run.status) ? run.error : '';
  const missingKeys = group.bot_ids.map(findBot).filter((bot) => bot && bot.auth_header && !state.key_status[bot.id]);
  $('send-message').disabled = !!(run && run.status === 'running') || group.bot_ids.length < 2 || missingKeys.length > 0;
  const workflow = group.mode === 'workflow';
  $('conversation-hint').textContent = group.bot_ids.length < 2 ? 'Add at least two bots to start.' : missingKeys.length ? `Set API keys in Bot setup for: ${missingKeys.map((bot) => bot.name).join(', ')}.` : workflow ? 'Workers follow their assignments in order. The master and current worker must both approve before handoff.' : 'Bots take turns until you stop. Each turn builds on the conversation so far.';
  $('send-message').textContent = workflow ? 'Start workflow' : 'Start conversation ↗';
  $('stop-conversation').textContent = workflow ? 'Stop workflow' : 'Stop conversation';
  $('stop-conversation').hidden = !(run && run.status === 'running');
  $('stop-conversation').disabled = false;
  for (const id of ['group-mode', 'group-master', 'group-quality', 'save-run-settings']) $(id).disabled = !!(run && run.status === 'running');
  renderWorkflowProgress(group, run);
}

function renderGroupMembers() {
  const host = $('group-members'); host.replaceChildren();
  const group = findGroup(selectedGroupId);
  if (!group) return;
  if (!state.bots.length) { host.append(node('div', 'empty-list', 'Create a bot on the Bot setup page.')); return; }
  const joined = group.bot_ids.map(findBot).filter(Boolean);
  const others = state.bots.filter((bot) => !group.bot_ids.includes(bot.id));
  for (const bot of [...joined, ...others]) {
    const enabled = group.bot_ids.includes(bot.id);
    const workflow = group.mode === 'workflow';
    const masterId = group.master_id || group.bot_ids[0];
    const sequence = workflow ? group.bot_ids.filter((id) => id !== masterId) : group.bot_ids;
    const label = workflow && enabled ? `${bot.name} · ${bot.id === masterId ? 'Master' : `Task ${sequence.indexOf(bot.id) + 1}`}` : bot.name;
    const row = switchRow(label, bot.assignment || bot.role || 'No assignment', enabled, () => setMembership(group.id, bot.id, !enabled));
    if (enabled && sequence.includes(bot.id) && sequence.length > 1) {
      const controls = node('span', 'order-actions');
      for (const [symbol, direction] of [['↑', -1], ['↓', 1]]) {
        const button = node('button', 'order-button', symbol);
        button.type = 'button'; button.title = direction < 0 ? 'Move bot earlier' : 'Move bot later';
        button.setAttribute('aria-label', `${button.title}: ${bot.name}`);
        const index = sequence.indexOf(bot.id);
        button.disabled = direction < 0 ? index === 0 : index === sequence.length - 1;
        button.addEventListener('click', async () => {
          const ordered = [...sequence]; [ordered[index], ordered[index + direction]] = [ordered[index + direction], ordered[index]];
          const remaining = [...ordered];
          const order = workflow ? group.bot_ids.map((id) => id === masterId ? id : remaining.shift()) : ordered;
          try { state = await api(`/api/groups/${group.id}/order`, { bot_ids: order }); renderGroupMembers(); renderGroupList(); }
          catch (error) { notice(error.message, true); }
        });
        controls.append(button);
      }
      row.insertBefore(controls, row.lastChild);
    }
    host.append(row);
  }
}

function renderMessages() {
  const group = findGroup(selectedGroupId);
  const host = $('messages');
  const nearBottom = host.scrollHeight - host.scrollTop - host.clientHeight < 90;
  host.replaceChildren();
  if (!group) return;
  if (!group.messages.length) host.append(node('div', 'conversation-empty', 'This group is ready. Send a message to begin.'));
  for (const message of group.messages) {
    const isUser = !message.bot_id;
    const item = node('article', `message${isUser ? ' user' : ''}`);
    item.append(node('div', 'message-avatar', isUser ? 'Y' : message.sender.slice(0, 1).toUpperCase()));
    const content = node('div', 'message-content');
    const head = node('div', 'message-head');
    head.append(node('strong', null, message.sender));
    if (message.created_at) {
      const time = new Date(message.created_at);
      if (!Number.isNaN(time.getTime())) head.append(node('span', null, time.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })));
    }
    content.append(head, node('div', 'message-bubble', message.text));
    if (message.files && message.files.length) content.append(node('div', 'attached-files', `Attached: ${message.files.map((path) => path.split('/').pop()).join(', ')}`));
    item.append(content); host.append(item);
  }
  const run = latestRun(group.id);
  if (run && run.status === 'running') host.append(node('div', 'waiting', run.waiting_reason || (run.current_bot ? `${run.current_bot} is replying…` : 'Starting the conversation…')));
  if (nearBottom || group.messages.length < 3) host.scrollTop = host.scrollHeight;
}

function renderChat(full = false) {
  const group = findGroup(selectedGroupId);
  $('chat-empty').hidden = !!group;
  $('chat-content').hidden = !group;
  if (!group) return;
  if (full) $('group-name').value = group.name;
  if (full) renderRunSettings();
  updateGroupSummary();
  renderMessages();
  renderGroupMembers();
  renderGrants();
}

function renderGrants() {
  selectedFiles = new Set([...selectedFiles].filter((path) => state.grants.includes(path)));
  const host = $('grants'); host.replaceChildren();
  if (!state.grants.length) host.append(node('div', 'empty-list', 'No files allowed yet.'));
  for (const path of state.grants) {
    const row = node('div', 'grant-row');
    const checkbox = node('input'); checkbox.type = 'checkbox'; checkbox.checked = selectedFiles.has(path);
    checkbox.setAttribute('aria-label', `Attach ${path} to this message`);
    checkbox.addEventListener('change', () => { if (checkbox.checked) selectedFiles.add(path); else selectedFiles.delete(path); updateAttachedCount(); });
    const info = node('div', 'grant-name');
    info.append(node('strong', null, path.split('/').pop()), node('small', null, path));
    const remove = node('button', 'remove-button', '×'); remove.type = 'button'; remove.title = 'Remove file permission';
    remove.setAttribute('aria-label', `Remove permission for ${path}`);
    remove.addEventListener('click', async () => {
      try { state = await api('/api/grants/remove', { path }); renderGrants(); notice('File permission removed.'); }
      catch (error) { notice(error.message, true); }
    });
    row.append(checkbox, info, remove); host.append(row);
  }
  updateAttachedCount();
}

function updateAttachedCount() {
  const count = selectedFiles.size;
  $('attached-count').textContent = count ? `${count} Desktop file${count === 1 ? '' : 's'} attached to this message` : 'No Desktop files attached';
}

async function pollRuns() {
  clearTimeout(pollTimer);
  try {
    const previouslyRunning = new Set(state.runs.filter((run) => run.status === 'running').map((run) => run.id));
    state = await api('/api/state');
    renderGroupList(); renderBotGroups(); renderChat(false);
    const failed = state.runs.find((run) => previouslyRunning.has(run.id) && run.status === 'failed');
    if (failed) notice(failed.error, true);
    if (state.runs.some((run) => run.status === 'running')) pollTimer = setTimeout(pollRuns, 1200);
  } catch (error) { notice(error.message, true); }
}

async function initialize() {
  try {
    state = await api('/api/state');
    selectedBotId = state.bots[0]?.id || null;
    if (!findGroup(selectedGroupId)) selectedGroupId = state.groups[0]?.id || null;
    navigate(page(), selectedGroupId, false);
    if (state.runs.some((run) => run.status === 'running')) pollRuns();
  } catch (error) { notice(error.message, true); }
}

$('nav-bots').addEventListener('click', () => { if (page() === 'chats') navigate('bots'); });
$('nav-chats').addEventListener('click', () => { if (page() === 'bots') { stashDraft(); navigate('chats'); } });
window.addEventListener('popstate', () => {
  selectedGroupId = new URLSearchParams(location.search).get('group') || selectedGroupId;
  navigate(page(), selectedGroupId, false);
});

$('new-bot').addEventListener('click', async () => {
  try {
    stashDraft();
    state = await api('/api/bots', {});
    selectedBotId = state.bots.at(-1).id;
    renderBotList(); renderBotEditor();
    $('bot-name').focus();
  } catch (error) { notice(error.message, true); }
});
$('bot-name').addEventListener('input', () => { $('editor-title').textContent = $('bot-name').value || 'New bot'; });
$('bot-format').addEventListener('change', () => { $('custom-fields').hidden = $('bot-format').value !== 'custom'; });
$('save-bot').addEventListener('click', async () => {
  if (!selectedBotId) return;
  try {
    const draft = formDraft();
    let extra_headers;
    try { extra_headers = JSON.parse(draft.headers_text || '{}'); }
    catch { throw new Error('Extra headers must be valid JSON.'); }
    const config = { ...draft, extra_headers };
    delete config.headers_text;
    state = await api(`/api/bots/${selectedBotId}`, config);
    drafts.delete(selectedBotId);
    renderBotList(); renderBotEditor(); renderGroupList(); renderGroupMembers();
    notice('Bot settings saved.');
  } catch (error) { notice(error.message, true); }
});
$('save-key').addEventListener('click', async () => {
  if (!selectedBotId) return;
  try {
    stashDraft();
    const key = $('bot-key').value;
    state = await api(`/api/bots/${selectedBotId}/key`, { key });
    $('bot-key').value = '';
    renderBotEditor();
    notice(key ? 'API key set for this bot and this server session.' : 'API key cleared.');
  } catch (error) { notice(error.message, true); }
});
$('delete-bot').addEventListener('click', async () => {
  const bot = findBot(selectedBotId);
  if (!bot || !confirm(`Remove ${bot.name} from all groups? Existing chat messages will stay.`)) return;
  try {
    state = await api(`/api/bots/${bot.id}/delete`, {});
    drafts.delete(bot.id);
    selectedBotId = state.bots[0]?.id || null;
    renderBotList(); renderBotEditor(); renderGroupList(); renderChat(false);
    notice('Bot removed.');
  } catch (error) { notice(error.message, true); }
});

$('new-group').addEventListener('click', async () => {
  try {
    state = await api('/api/groups', { name: 'New group' });
    selectedGroupId = state.groups.at(-1).id;
    navigate('chats', selectedGroupId);
    $('group-name').focus(); $('group-name').select();
  } catch (error) { notice(error.message, true); }
});
$('save-group-name').addEventListener('click', async () => {
  if (!selectedGroupId) return;
  try {
    state = await api(`/api/groups/${selectedGroupId}/rename`, { name: $('group-name').value });
    renderGroupList(); renderChat(true); renderBotGroups(); notice('Group renamed.');
  } catch (error) { notice(error.message, true); }
});
$('group-name').addEventListener('keydown', (event) => { if (event.key === 'Enter') { event.preventDefault(); $('save-group-name').click(); } });
$('send-message').addEventListener('click', async () => {
  const group = findGroup(selectedGroupId);
  if (!group) return;
  const text = $('chat-input').value;
  $('send-message').disabled = true;
  try {
    const settings = runSettingsDraft();
    state = await api(`/api/groups/${group.id}/settings`, settings);
    await api(`/api/groups/${group.id}/send`, { text, files: [...selectedFiles], mode: settings.mode, continuous: settings.mode !== 'workflow' });
    $('chat-input').value = '';
    selectedFiles.clear();
    state = await api('/api/state');
    renderGrants(); renderGroupList(); renderChat(false);
    pollRuns();
  } catch (error) { $('send-message').disabled = false; $('run-error').textContent = error.message; $('run-error').hidden = false; notice(error.message, true); }
});

async function saveRunSettings() {
  if (!selectedGroupId) return;
  try {
    state = await api(`/api/groups/${selectedGroupId}/settings`, runSettingsDraft());
    renderRunSettings(); renderGroupMembers(); updateGroupSummary();
    notice('Run settings saved.');
  } catch (error) { notice(error.message, true); }
}
$('save-run-settings').addEventListener('click', saveRunSettings);
$('group-mode').addEventListener('change', () => { $('workflow-settings').hidden = $('group-mode').value !== 'workflow'; saveRunSettings(); });

$('stop-conversation').addEventListener('click', async () => {
  if (!selectedGroupId) return;
  $('stop-conversation').disabled = true;
  try {
    state = await api(`/api/groups/${selectedGroupId}/stop`, {});
    renderGroupList(); renderChat(false);
    notice('Conversation stopped.');
  } catch (error) { $('stop-conversation').disabled = false; notice(error.message, true); }
});

$('allow-file').addEventListener('click', async () => {
  try {
    state = await api('/api/grants', { path: $('file-path').value.trim() });
    $('file-path').value = '';
    renderGrants(); notice('Desktop file allowed. Select it to attach to a message.');
  } catch (error) { notice(error.message, true); }
});
$('pick-file').addEventListener('click', async () => {
  const button = $('pick-file'); button.disabled = true;
  try {
    state = await api('/api/pick-file', {});
    renderGrants(); notice('Desktop file allowed. Select it to attach to a message.');
  } catch (error) { notice(error.message, true); }
  finally { button.disabled = false; }
});

initialize();
