import {StepViewer} from './viewer.js';

const $ = id => document.getElementById(id);
const clone = value => structuredClone(value);
const state = {token:'', projects:[], id:null, data:null, plan:null, selected:null,
  viewer:null, sceneKey:null, edit:0, dirty:new Set(), pending:new Set(), busy:false,
  sceneBusy:false, timer:null, template:null, ratio:100, versions:new Map(),tab:'steps',
  selectedGroups:new Set(),inspectGroups:null,languages:null,languageEdits:new Map(),languageBusy:false,conflictedDraft:null};
const step = () => state.plan?.steps.find(item => item.id === state.selected);
const includedSteps = () => (state.plan?.steps || []).filter(item=>item.include_in_manual!==false && item.kind!=='overview');
const isOverview = item => item?.kind==='overview' || item?.include_in_manual===false;
const labelText = (value,limit=33) => {const text=String(value || '').replace(/\s+/g,' ').trim();return text.length>limit?`${text.slice(0,limit)}…`:text;};
const endpoint = (suffix='', id=state.id) => `/api/projects/${id}${suffix}`;
function notice(message, error=false) {
  $('notice').textContent = message; $('notice').hidden = !message;
  $('notice').classList.toggle('error', error);
}
function jobMessage(message, running=true) {
  $('job-status').hidden = !running; $('job-text').textContent = message;
  $('job-progress').removeAttribute('value');
}
async function api(url, body, file) {
  const headers = {'X-App-Token':state.token};
  const options = {headers};
  if (file) {
    options.method='POST'; options.body=file; headers['Content-Type']='application/octet-stream';
    headers['X-File-Name']=encodeURIComponent(file.name);
  } else if (body !== undefined) {
    options.method='POST'; options.body=JSON.stringify(body); headers['Content-Type']='application/json';
  }
  const response = await fetch(url, options);
  const data = await response.json();
  if (!response.ok) {const error=new Error(data.error || `请求失败 (${response.status})`);error.status=response.status;throw error;}
  return data;
}
const delay = ms => new Promise(resolve => setTimeout(resolve,ms));
async function waitJob(jobId, id=state.id) {
  for (;;) {
    const result = await api(`/api/jobs/${jobId}`);
    if (id===state.id) jobMessage(result.message || '后台正在处理…');
    if (result.status==='succeeded') return result.result;
    if (result.status==='failed') throw new Error(result.error || result.message || '后台处理失败');
    await delay(850);
  }
}
async function refreshProjects() {
  state.projects=(await api('/api/projects')).projects;
  $('project-select').replaceChildren(...state.projects.map(project=>new Option(project.title,project.id)));
  if (state.id) $('project-select').value=state.id;
}
function canonicalDraftBase(plan){
  if(!plan)return null;const value=clone(plan);delete value.status;delete value.confirmation;
  for(const item of value.steps || [])delete item.reviewed;
  const ordered=item=>Array.isArray(item)?item.map(ordered):item && typeof item==='object'?Object.fromEntries(Object.keys(item).sort().map(key=>[key,ordered(item[key])])):item;
  return JSON.stringify(ordered(value));
}
function rememberDraft() {
  if (!state.id || !state.plan || !state.dirty.size) return;
  // Conflicting earlier edits have their own recovery key. Normal new edits
  // keep a separate base signature, so neither silently erases the other.
  sessionStorage.setItem(`manual-draft:${state.id}`,JSON.stringify({plan:state.plan,base_signature:canonicalDraftBase(state.data?.plan),base_step_ids:state.data?.plan?.steps.map(item=>item.id),base_template_sha256:state.data?.plan?.pdf_template?.sha256}));
}
function clearDraft(id=state.id) {sessionStorage.removeItem(`manual-draft:${id}`);}
async function fetchState({keepEdits=false}={}) {
  const id=state.id, data=await api(endpoint('',id));
  if (id!==state.id) return;
  state.data=data;
  if (!keepEdits) {state.plan=clone(data.plan);state.dirty.clear();}
  drawWorkspace();
}
async function selectProject(id) {
  rememberDraft();clearTimeout(state.timer);state.pending.clear();
  state.id=id;state.edit++;state.selected=null;state.data=null;state.plan=null;state.dirty.clear();
  state.selectedGroups.clear();state.inspectGroups=null;state.languages=null;state.languageEdits.clear();state.conflictedDraft=null;
  $('draft-conflict').hidden=true;
  $('open-language-pdf').hidden=true;$('open-language-pdf').removeAttribute('href');
  state.viewer?.dispose();state.viewer=null;state.sceneKey=null;state.sceneBusy=false;
  $('viewer').querySelectorAll('canvas').forEach(canvas=>canvas.remove());
  $('scene-message').hidden=false;$('scene-message').textContent='正在准备三维模型…';
  localStorage.setItem('manual-active-project',id);
  await fetchState();
  const recovery=sessionStorage.getItem(`manual-conflict:${id}`);
  if(recovery){try{state.conflictedDraft=JSON.parse(recovery);}catch{state.conflictedDraft={unreadable_draft:recovery};}$('draft-conflict').hidden=false;}
  const saved=sessionStorage.getItem(`manual-draft:${id}`);
  if (saved) {
    try {
      const wrapper=JSON.parse(saved),draft=wrapper.plan || wrapper;
      const currentIds=state.plan?.steps.map(item=>item.id),baseIds=wrapper.base_step_ids || draft.steps.map(item=>item.id);
      const compatible=wrapper.base_signature===canonicalDraftBase(state.plan) && JSON.stringify(baseIds)===JSON.stringify(currentIds) && (wrapper.base_template_sha256 || draft.pdf_template?.sha256)===(state.plan?.pdf_template?.sha256);
      if (compatible && draft.source.sha256===state.plan?.source.sha256 && JSON.stringify(draft)!==JSON.stringify(state.plan)) {
        state.plan=draft;draft.steps.forEach(item=>state.dirty.add(item.id));drawWorkspace();
        notice('已恢复本页尚未生成的修改，旧插图仍可查看。');
      } else if(JSON.stringify(draft)!==JSON.stringify(state.plan)){state.conflictedDraft=state.conflictedDraft?{previous:state.conflictedDraft,latest:draft}:draft;sessionStorage.setItem(`manual-conflict:${id}`,JSON.stringify(state.conflictedDraft));clearDraft();$('draft-conflict').hidden=false;notice('当前使用服务端的新配置。旧草案已保留，可下载后核对差异。');}
    } catch {state.conflictedDraft={previous:state.conflictedDraft,unreadable_draft:saved};sessionStorage.setItem(`manual-conflict:${id}`,JSON.stringify(state.conflictedDraft));clearDraft();$('draft-conflict').hidden=false;notice('旧草案格式无法直接恢复，原数据已保留供下载核对。',true);}
  }
  await refreshProjects();
  if (state.plan) void loadScene(id);
  if(state.tab==='languages')void loadLanguages();
}
function drawWorkspace() {
  const plan=state.plan;
  const pending=state.data?.pending_source_info;
  $('source-recovery').hidden=!pending;
  if(pending){$('source-recovery-text').textContent=pending.status==='failed'?`上次上传的 ${pending.filename} 尚未切换，文件已保留。`:`正在处理 ${pending.filename}。`;$('retry-source').disabled=state.busy || state.sceneBusy || !pending.retry_available;}
  $('workspace').hidden=!plan;$('empty-state').hidden=!!plan;
  if (!plan) return;
  if (!plan.steps.some(item=>item.id===state.selected)) state.selected=plan.steps[0]?.id;
  $('project-title').textContent=plan.product?.title || state.data.project.title;
  const sourceFilename=state.data.source_filename || plan.source?.filename || plan.source?.original_filename;
  $('source-filename').textContent=state.data.source_model_name?`模型：${state.data.source_model_name}${sourceFilename?` · 文件：${sourceFilename}`:''}`:(sourceFilename || '当前 STP 模型');
  $('source-short-hash').textContent=plan.source?.sha256?.slice(0,10) || '';
  const emptyCount=plan.analysis_summary?.non_geometric_occurrences || 0;
  const instructions=includedSteps();
  $('project-summary').textContent=`${instructions.length} 个安装步骤 · ${plan.groups.length} 个安装单元 · ${state.data.part_catalog.length} 个源实例${emptyCount?` · ${emptyCount} 个空占位已记录`:''}`;
  const reference=plan.configuration_origin?.reference || plan.reference_manual || plan.reference || plan.reference_binding || plan.workflow?.reference || plan.manual_document?.reference;
  $('project-status').textContent=plan.status==='confirmed'?'已确认':reference?'参考说明书草案':'自动草案 · 待编排';
  $('project-health').textContent=state.data.stale?'步骤图需要更新':state.dirty.size?'有未生成的修改':'基于当前 STP';
  const guidance=reference?'步骤依据已有参考说明书提出，连接位置、分组和先后仍须核对。':'纯几何不能确定装配顺序。先在“安装单元”核对预装边界，再修改步骤和参与单元。';
  $('draft-guidance').textContent=guidance+(plan.pdf_template?' 当前已绑定固定模板，保留原文与顺序，只调整步骤图。':'');
  $('step-count').textContent=String(instructions.length);
  $('step-structure-note').textContent=plan.pdf_template?'固定模板已绑定。自由添加、删除、重排和原文修改在未绑定模板的草案中可用。':'支持添加、删去、重排；总览不计入说明书步骤。';
  $('add-step').disabled=state.busy || !!plan.pdf_template;
  $('project-status').classList.toggle('confirmed',plan.status==='confirmed');
  $('download-config').href=`/projects/${state.id}/steps.json`;
  $('open-pdf').href=`/projects/${state.id}/manual.pdf`;
  const pdfReady=state.data.pdf_ready && state.dirty.size===0;
  $('open-pdf').setAttribute('aria-disabled',String(!pdfReady));if(!pdfReady)$('open-pdf').removeAttribute('href');
  $('export-pdf').disabled=state.busy || !plan.pdf_template || state.data.stale || state.dirty.size>0;
  $('confirm').disabled=state.busy || state.data.stale || state.dirty.size>0;
  $('generate-all').disabled=state.busy;
  const revision=state.data.revision_report;
  $('revision-details').hidden=!revision;
  if(revision){const info=revision.summary || {};$('revision-summary').textContent=`旧版 ${info.old_objects ?? '?'} 个实例，新版 ${info.new_objects ?? '?'} 个实例。${info.diagrams_requiring_update ?? 0} 个旧步骤的图需更新。对应关系依据几何候选，仍须人工核对；旧图和旧确认不沿用为新版本事实。`;$('download-revision').href=`/projects/${state.id}/revision_report.json`;}
  drawSteps();drawSelected();drawGroups();drawDelivery();
}
function drawSteps() {
  const rows=state.plan.steps.map((item,index)=>{
    const row=document.createElement('div');row.className=`step-row${item.id===state.selected?' active':''}`;
    const number=document.createElement('span');number.className='step-number';number.textContent=String(index+1).padStart(2,'0');
    const select=document.createElement('button');select.className='step-select';select.title=item.title;
    const title=document.createElement('span');title.className='step-label';title.textContent=isOverview(item)?'模型总览':labelText(item.title);select.append(title);
    if(isOverview(item))number.textContent='◈';
    const hint=document.createElement('small');
    const entry=state.data.manifest?.entries.find(entry=>entry.step_id===item.id && entry.asset_id===item.id);
    hint.textContent=isOverview(item)?'不入手册':state.pending.has(item.id)?'等待更新':state.dirty.has(item.id)?'参数已改':entry?.stale?'旧图待更新':!entry?'尚未出图':item.reviewed?'已核对':'待核对';
    select.append(hint);select.onclick=()=>selectStep(item.id);
    const generate=document.createElement('button');generate.className='step-mini-render';generate.textContent='出图';
    generate.setAttribute('aria-label',`单独生成步骤 ${index+1}：${item.title}`);
    generate.onclick=()=>{selectStep(item.id);requestPreview(item.id);};
    row.append(number,select,generate);return row;
  });
  $('step-list').replaceChildren(...rows);
}
function selectStep(id) {
  if (state.selected===id) return;
  state.selected=id;state.inspectGroups=null;state.ratio=100;$('explode-range').value=100;$('explode-value').textContent='100%';
  $('show-all-model').checked=false;drawSteps();drawSelected();updateViewer(false);
}
function currentEntry() {
  if(state.data?.manifest?.source_sha256!==state.plan?.source.sha256)return undefined;
  return state.data.manifest.entries.find(entry=>entry.step_id===state.selected && entry.asset_id===state.selected);
}
function drawSelected() {
  const item=step();if (!item) return;
  const entry=currentEntry();
  // Keep the previous image while parameters change or a background job runs.
  if (entry?.svg_path) {
    const url=`${state.data.diagram_base_url}${encodeURIComponent(entry.svg_path)}?v=${entry.svg_sha256 || ''}`;
    let image=$('diagram').querySelector('img');
    if (!image) {image=new Image();$('diagram').replaceChildren(image);}
    image.alt=item.title;
    if (image.getAttribute('src')!==url) image.src=url;
    $('download-svg').href=url;$('download-svg').setAttribute('aria-disabled','false');
  } else {
    const message=document.createElement('p');message.textContent='三维模型可以先调整，点击“仅生成这一步”生成线稿。';
    $('diagram').replaceChildren(message);$('download-svg').setAttribute('aria-disabled','true');$('download-svg').removeAttribute('href');
  }
  $('diagram-status').textContent=state.pending.has(item.id)?(entry?'保留上一张图 · 正在等待本步更新':'正在等待本步出图'):state.dirty.has(item.id)?(entry?'参数已修改 · 上一版插图':'参数已修改 · 等待出图'):entry?.stale?'上一版插图 · 待更新':entry?'当前配置的插图':'尚未生成插图';
  $('focus-preview').hidden=!entry?.focus?.svg_path;
  if(entry?.focus?.svg_path){const url=`${state.data.diagram_base_url}${encodeURIComponent(entry.focus.svg_path)}?v=${entry.focus.svg_sha256}`;$('focus-image').src=url;$('download-focus').href=url;}
  $('step-title').value=item.title;$('step-instruction').value=item.instruction;
  $('step-cautions').value=(item.consumer_warnings || item.consumer?.caution || []).join('\n');
  const bound=!!state.plan.pdf_template;
  $('step-title').readOnly=bound;$('step-instruction').readOnly=bound;$('step-cautions').readOnly=bound;$('template-copy-note').hidden=!bound;
  if(bound)$('template-copy-note').textContent='当前固定模板保留原文和顺序，仅修改图示。自由步骤编排与原文修改在未绑定模板的草案中可用；本步视角、显隐与分离距离仍可调整。';
  const index=state.plan.steps.findIndex(row=>row.id===item.id),lastAction=includedSteps().at(-1)?.id===item.id;
  $('step-up').disabled=bound || state.busy || index===0 || isOverview(item);
  $('step-down').disabled=bound || state.busy || lastAction || isOverview(item);
  $('delete-step').disabled=bound || state.busy || isOverview(item) || includedSteps().length<=1;
  $('save-step-copy').disabled=bound || state.busy;
  $('apply-step-groups').disabled=state.busy || isOverview(item);
  $('step-reviewed').checked=!!item.reviewed;
  $('step-warnings').replaceChildren(...(item.warnings || []).map(warning=>{const li=document.createElement('li');li.textContent=warning;return li;}));
  $('warning-count').textContent=item.warnings?.length?`(${item.warnings.length})`:'';
  $('step-warning-details').hidden=!item.warnings?.length;
  $('visibility-reason').value=item.visibility_reason || '';
  const moving=item.moving_groups || [];
  $('explode-closer').disabled=!moving.length;$('explode-farther').disabled=!moving.length;$('explode-range').disabled=!moving.length;
  drawPartSettings();drawStepGroups();
}
function drawPartSettings() {
  const item=step(), sceneGroups=new Set([...(item.assembled_groups || []),...(item.moving_groups || [])]);
  const hidden=new Set(item.hidden_part_ids || []),excluded=new Set(state.plan.excluded_part_ids || []);
  const catalog=new Map(state.data.part_catalog.map(part=>[part.id,part]));
  const offsets=(item.moving_groups || []).map(id=>{
    const row=document.createElement('div');row.className='offset-row';
    const label=document.createElement('span');label.textContent=state.plan.groups.find(group=>group.id===id)?.label || id;row.append(label);
    for(let axis=0;axis<3;axis++) {
      const input=document.createElement('input');input.type='number';input.step=5;input.value=item.offsets[id]?.[axis] || 0;
      input.setAttribute('aria-label',`${label.textContent} ${['X','Y','Z'][axis]} 分离毫米`);
      input.onchange=()=>{const value=Number(input.value);if(!Number.isFinite(value))return;
        item.offsets[id] ||= [0,0,0];const delta=value-item.offsets[id][axis];item.offsets[id][axis]=value;
        for(const arrow of item.arrows || []) {
          let owner=arrow.group_id;
          if(!owner && item.moving_groups.length===1)owner=id;
          if(!owner && state.viewer?.sceneData) {
            const parts=new Map(state.viewer.sceneData.parts.map(part=>[part.id,part]));
            const candidates=item.moving_groups.filter(gid=>state.plan.groups.find(group=>group.id===gid)?.part_ids.some(pid=>{
              const bounds=parts.get(pid)?.bbox;return bounds && arrow.to.every((n,k)=>n>=bounds[0][k]-.01 && n<=bounds[1][k]+.01);
            }));if(candidates.length===1)owner=candidates[0];
          }
          if(owner===id)arrow.from[axis]+=delta;
        }
        changed();};row.append(input);
    }return row;
  });$('offset-options').replaceChildren(...offsets);
  const final=state.plan.steps.at(-1)?.id===item.id;
  const lists=state.plan.groups.filter(group=>sceneGroups.has(group.id)).map(group=>{
    const box=document.createElement('div');box.className='part-group';
    const caption=document.createElement('strong');caption.textContent=group.label;box.append(caption);
    for (const id of group.part_ids.filter(id=>!excluded.has(id))) {
      const label=document.createElement('label');label.className='part-check';const check=document.createElement('input');check.type='checkbox';check.checked=hidden.has(id);check.disabled=final;
      const part=catalog.get(id);const name=`${part?.name || '源实例'} [${part?.index ?? id.split('/').at(-1)}]`;
      label.append(check,document.createTextNode(`隐藏 ${name}`));
      check.onchange=()=>{
        if(check.checked && !$('visibility-reason').value.trim()) {notice('先填写隐藏原因，例如“查看内部连接位置”。',true);check.checked=false;return;}
        const next=new Set(item.hidden_part_ids || []);check.checked?next.add(id):next.delete(id);
        if ((item.moving_groups || []).includes(group.id) && !group.part_ids.some(pid=>!excluded.has(pid)&&!next.has(pid))) {notice('分离中的总成需保留至少一个可见实例。',true);check.checked=false;return;}
        item.hidden_part_ids=[...next];item.visibility_reason=$('visibility-reason').value.trim();item.focus_part_ids=(item.focus_part_ids || []).filter(pid=>!next.has(pid));changed();
      };box.append(label);
      const focusLabel=document.createElement('label');focusLabel.className='part-check';const focus=document.createElement('input');focus.type='checkbox';focus.checked=(item.focus_part_ids || []).includes(id);focus.disabled=hidden.has(id);
      focusLabel.append(focus,document.createTextNode(`局部图 ${name}`));
      focus.onchange=()=>{const ids=new Set(item.focus_part_ids || []);focus.checked?ids.add(id):ids.delete(id);item.focus_part_ids=[...ids];changed();};box.append(focusLabel);
    }return box;
  });$('part-options').replaceChildren(...lists);
}
function drawGroups() {
  const catalog=new Map(state.data.part_catalog.map(part=>[part.id,part]));
  const query=$('group-search').value.trim().toLowerCase();
  const roles=state.data.workflow?.roles || [{id:'unclassified',label:'待确认交付状态'},{id:'preassembled',label:'预装单元'},{id:'installation',label:'需要安装'},{id:'auxiliary',label:'辅助参考几何'}];
  $('group-count').textContent=String(state.plan.groups.length);
  const groupRows=state.plan.groups.filter(group=>!query || `${group.label} ${group.part_ids.map(id=>catalog.get(id)?.name || '')}`.toLowerCase().includes(query));
  $('group-options').replaceChildren(...groupRows.map(group=>{
    const card=document.createElement('article');card.className=`group-card${state.selectedGroups.has(group.id)?' selected':''}`;
    const heading=document.createElement('div');heading.className='group-card-heading';
    const select=document.createElement('input');select.type='checkbox';select.value=group.id;select.checked=state.selectedGroups.has(group.id);select.setAttribute('aria-label',`选择安装单元 ${group.label}`);
    select.onchange=()=>{select.checked?state.selectedGroups.add(group.id):state.selectedGroups.delete(group.id);card.classList.toggle('selected',select.checked);drawGroupSelection();};
    const identity=document.createElement('div'),name=document.createElement('input');name.className='group-name-input';name.value=group.label;name.title=group.label;name.setAttribute('aria-label',`安装单元名称 ${group.label}`);name.disabled=state.busy;
    name.onchange=async()=>{if(name.value.trim()!==group.label)await workflowEdit({action:'rename_group',group_id:group.id,label:name.value.trim()},'安装单元名称已保存。');};
    const meta=document.createElement('p');meta.className='group-meta';const summary=state.data.workflow?.groups.find(item=>item.id===group.id);
    meta.textContent=`${group.part_ids.length} 个源实例 · ${summary?.action_count ?? state.plan.steps.filter(item=>item.moving_groups.includes(group.id)).length} 个安装动作`;
    identity.append(name,meta);heading.append(select,identity);card.append(heading);
    const actions=document.createElement('div');actions.className='group-card-actions';
    const role=document.createElement('select');role.setAttribute('aria-label',`${group.label} 交付状态`);role.replaceChildren(...roles.map(item=>new Option(item.label,item.id)));role.value=group.role || 'unclassified';role.disabled=state.busy;
    role.onchange=()=>void workflowEdit({action:'set_group_role',group_id:group.id,role:role.value},'单元交付状态已记录；几何与步骤需要继续核对。');
    const inspect=document.createElement('button');inspect.textContent='三维查看';inspect.onclick=()=>{state.inspectGroups=[group.id];$('show-all-model').checked=false;switchTab('steps');updateViewer(false);notice(`正在单独查看 ${labelText(group.label)}，预览不修改步骤。`);};actions.append(role,inspect);card.append(actions);
    const details=document.createElement('details'),caption=document.createElement('summary');caption.textContent='源部件与拆分';details.append(caption);
    const list=document.createElement('div');list.className='group-source-list';
    for(const id of group.part_ids){const part=catalog.get(id),label=document.createElement('label');label.className='part-check';const check=document.createElement('input');check.type='checkbox';check.value=id;check.setAttribute('data-split-part','');check.disabled=group.part_ids.length<2 || state.busy;label.title=`${part?.name || id} · ${id}`;label.append(check,document.createTextNode(`${labelText(part?.name || '源部件',38)} [${part?.index ?? id.split('/').at(-1)}]`));list.append(label);}
    details.append(list);
    if(group.part_ids.length>1){const splitName=document.createElement('input');splitName.placeholder='拆出的新单元名称';splitName.setAttribute('aria-label',`从 ${group.label} 拆出的新单元名称`);const split=document.createElement('button');split.textContent='将所选源部件拆为新单元';split.disabled=state.busy;split.onclick=()=>{const ids=[...list.querySelectorAll('[data-split-part]:checked')].map(input=>input.value);if(!ids.length || ids.length===group.part_ids.length){notice('选择原单元中的部分源实例，拆分后两侧都需保留几何。',true);return;}if(!splitName.value.trim()){splitName.focus();notice('填写拆出的新单元名称。');return;}void workflowEdit({action:'split_group',group_id:group.id,part_ids:ids,label:splitName.value.trim()},'安装单元已拆分，请核对相关步骤中的参与单元。');};details.append(splitName,split);}
    card.append(details);return card;
  }));
  drawGroupSelection();
  const diagnostic=(state.data.workflow?.warnings || []).map(message=>({label:message}));
  $('assembly-proposals').replaceChildren(...[...diagnostic,...(state.data.assembly_suggestions || [])].map(suggestion=>{
    const p=document.createElement('p');p.className='hint';p.textContent=suggestion.label || suggestion.reason || suggestion.basis || `待核对建议：${suggestion.id}`;return p;
  }));
}
function drawGroupSelection(){
  const known=new Set(state.plan.groups.map(group=>group.id));state.selectedGroups=new Set([...state.selectedGroups].filter(id=>known.has(id)));
  $('selected-group-count').textContent=state.selectedGroups.size?`已选择 ${state.selectedGroups.size} 个安装单元`:'选择两个或更多单元';
  $('merge-selected').disabled=state.busy || state.selectedGroups.size<2;
}
function drawStepGroups(){
  const item=step();if(!item)return;
  $('step-group-options').replaceChildren(...state.plan.groups.map(group=>{
    const row=document.createElement('div');row.className='composition-row';const name=document.createElement('span');name.textContent=labelText(group.label,35);name.title=group.label;row.append(name);
    for(const type of ['assembled','moving']){const label=document.createElement('label'),check=document.createElement('input');check.type='checkbox';check.dataset.groupId=group.id;check.dataset.sceneType=type;check.checked=(item[`${type}_groups`] || []).includes(group.id);check.disabled=state.busy || isOverview(item);check.setAttribute('aria-label',`${group.label} ${type==='moving'?'本步装入':'背景或已安装'}`);check.onchange=()=>{if(check.checked)row.querySelector(`[data-scene-type="${type==='moving'?'assembled':'moving'}"]`).checked=false;};label.append(check);row.append(label);}return row;
  }));
}
function drawDelivery(){
  const steps=includedSteps(),hash=state.plan.source.sha256;
  const entries=(state.data.manifest?.source_sha256===hash?state.data.manifest.entries:[]) || [];
  const current=steps.filter(item=>entries.some(entry=>entry.asset_id===item.id && entry.step_id===item.id && !entry.stale) && !state.dirty.has(item.id)).length;
  const reviewed=steps.filter(item=>item.reviewed).length;
  const checks=[{ok:true,title:'当前模型',hint:$('source-filename').textContent},{ok:current===steps.length,title:`步骤图 ${current} / ${steps.length}`,hint:current===steps.length?'当前源模型的插图已齐全':'有步骤尚未出图或配置已修改，先单独更新。'},{ok:!!state.plan.pdf_template,title:state.plan.pdf_template?'原模板已绑定':'原模板尚未绑定',hint:state.plan.pdf_template?'导出保持原版式，仅替换步骤图。':'上传两页 PDF 并建立图槽对应。'},{ok:reviewed===steps.length,title:`人工核对 ${reviewed} / ${steps.length}`,hint:'核对动作、连接位置和文字后勾选每一步。'}];
  $('delivery-checklist').replaceChildren(...checks.map(check=>{const row=document.createElement('div');row.className=`delivery-check${check.ok?' ok':''}`;const mark=document.createElement('span');mark.className='delivery-check-mark';mark.textContent=check.ok?'✓':'·';const text=document.createElement('div'),title=document.createElement('strong'),hint=document.createElement('p');title.textContent=check.title;hint.className='hint';hint.textContent=check.hint;text.append(title,hint);row.append(mark,text);return row;}));
}
function switchTab(tab){
  const previous=state.tab;
  state.tab=tab;document.querySelectorAll('[data-workspace-tab]').forEach(button=>{const active=button.dataset.workspaceTab===tab;button.setAttribute('aria-selected',String(active));button.tabIndex=active?0:-1;$(`pane-${button.dataset.workspaceTab}`).hidden=!active;});
  // The mesh may finish loading while its pane has no layout width. Let the
  // ResizeObserver restore the real aspect before fitting the displayed scene.
  if(tab==='steps' && previous!==tab)requestAnimationFrame(()=>requestAnimationFrame(()=>{if(state.viewer?.sceneData){state.viewer.controls?.handleResize();state.viewer.fit();}}));
  if(tab==='languages' && !state.languages && state.plan)void loadLanguages();
  if(tab==='delivery')drawDelivery();
}
function updateViewer(preserve=true) {
  if (!state.viewer || !step()) return;
  if(state.inspectGroups){state.viewer.setStep(state.plan,{...step(),assembled_groups:state.inspectGroups,moving_groups:[],offsets:{},hidden_part_ids:[],arrows:[]},{preserveView:preserve});}
  else if ($('show-all-model').checked) {
    state.viewer.setStep({...state.plan,excluded_part_ids:[]},{...step(),camera:state.plan.style?.camera || step().camera,up:state.plan.style?.up || step().up,assembled_groups:state.plan.groups.map(group=>group.id),moving_groups:[],offsets:{},hidden_part_ids:[],arrows:[]},{preserveView:preserve});
  } else state.viewer.setStep(state.plan,step(),{preserveView:preserve});
}
async function loadScene(id) {
  if (state.sceneBusy || id!==state.id) return;state.sceneBusy=true;
  try {
    for (;;) {
      const result=await api(endpoint('/scene',id));
      if (id!==state.id) return;
      if (result.job_id) {await waitJob(result.job_id,id);continue;}
      state.viewer?.dispose();state.viewer=new StepViewer($('viewer'));
      await state.viewer.loadScene(result);if(id!==state.id)return;state.sceneKey=result.source_sha256;
      state.viewer.setAxesVisible($('show-axes').checked);
      $('scene-message').hidden=true;updateViewer(false);notice('三维模型已载入，可拖动选角度，再“采用当前视角”。');break;
    }
  } catch(error) {if(id===state.id){$('scene-message').hidden=false;$('scene-message').textContent=`三维预览未载入：${error.message}。仍可用快捷视角和步骤线稿。`;notice(error.message,true);}}
  finally {if(id===state.id){state.sceneBusy=false;jobMessage('',state.busy);if(state.pending.size)void drainPreviews();}}
}
function changed({render=true, redraw=true}={}) {
  const item=step();item.reviewed=false;state.plan.status='draft';state.edit++;
  state.versions.set(item.id,(state.versions.get(item.id)||0)+1);state.dirty.add(item.id);rememberDraft();
  updateViewer(true);drawSteps();if(redraw)drawSelected();
  $('export-pdf').disabled=true;$('confirm').disabled=true;$('open-pdf').setAttribute('aria-disabled','true');
  if(render && $('auto-preview').checked) {clearTimeout(state.timer);const id=item.id;state.timer=setTimeout(()=>requestPreview(id),650);}
}
function requestPreview(id=state.selected) {
  clearTimeout(state.timer);state.pending.add(id);drawSteps();drawSelected();void drainPreviews();
}
async function drainPreviews() {
  if(state.busy || state.sceneBusy || !state.pending.size) return;
  const id=state.id,sid=state.pending.values().next().value;
  const version=state.versions.get(sid)||0, edit=state.edit, candidate=clone(state.plan);
  state.pending.delete(sid);state.busy=true;jobMessage('正在生成这一步的线稿，上一张图继续保留…');
  if(sid===state.selected)$('diagram-status').textContent='正在生成这一步 · 保留上一张图';
  try {
    const submitted=await api(endpoint('/preview',id),{plan:candidate,step_id:sid});
    await waitJob(submitted.job_id,id);
    if(id!==state.id)return;
    await fetchState({keepEdits:state.edit!==edit});
    if((state.versions.get(sid)||0)===version)state.dirty.delete(sid);
    if(!state.dirty.size)clearDraft();else rememberDraft();
    drawWorkspace();notice('本步插图已更新，其他步骤的图继续保留。');
  } catch(error) {
    if(id===state.id) {
      if(error.status===409) {state.pending.add(sid);await delay(1200);}
      else notice(`本步未更新，上一张图已保留：${error.message}`,true);
    }
  } finally {
    state.busy=false;jobMessage('',state.sceneBusy);if(id===state.id)drawWorkspace();
    if(state.pending.size)void drainPreviews();
  }
}
async function action(name,body={},success='已完成。') {
  if(state.busy || state.sceneBusy){notice('后台正在处理当前项目，完成后即可继续此操作。');return false;}
  clearTimeout(state.timer);state.busy=true;const id=state.id,edit=state.edit;drawWorkspace();
  try {
    const submitted=await api(endpoint(`/${name}`,id),body);const result=await waitJob(submitted.job_id,id);
    const changesPlan=['plan','workflow','render','confirm','bind-template','merge'].includes(name);
    if(id===state.id){await fetchState({keepEdits:!changesPlan || state.edit!==edit});if(changesPlan && state.edit===edit){clearDraft();state.dirty.clear();}updateViewer(true);notice(success);}
    return {ok:true,result};
  } catch(error){notice(error.message,true);return false;}
  finally{state.busy=false;jobMessage('');drawWorkspace();if(state.pending.size)void drainPreviews();}
}
async function workflowEdit(operation,message='编排已保存，请核对修改后的步骤。'){
  if(!state.plan)return false;
  if(state.busy || state.sceneBusy){notice('当前后台任务完成后即可编辑安装单元或步骤。');return false;}
  if(state.dirty.size){state.pending.clear();const saved=await action('plan',{plan:clone(state.plan)},'当前修改已保存。');if(!saved)return false;}
  const source=state.plan.source.sha256;
  const result=await action('workflow',{source_sha256:source,operation},message);
  if(result){state.inspectGroups=null;state.languages=null;state.languageEdits.clear();if(state.tab==='languages')void loadLanguages();}
  return result;
}
async function moveStep(direction){
  const order=state.plan.steps.map(item=>item.id),index=order.indexOf(state.selected),target=index+direction;
  if(target<0 || target>=order.length-1 || isOverview(step()))return;
  [order[index],order[target]]=[order[target],order[index]];
  await workflowEdit({action:'reorder_steps',step_ids:order},'步骤顺序已保存，连接先后仍需核对。');
}
const languageEditKey=(locale,key)=>`${locale}\u0000${key}`;
async function loadLanguages({discard=false}={}){
  const id=state.id;if(!id || state.languageBusy)return;
  state.languageBusy=true;$('language-empty').hidden=false;$('language-empty').textContent='正在读取当前说明书文字…';
  try{
    const data=await api(endpoint('/languages',id));if(id!==state.id)return;
    if(data.source_sha256!==state.plan.source.sha256)throw new Error('文字版本与当前 STP 不符，请重新载入项目。');
    if(discard || (state.languages && state.languages.base_sha256!==data.base_sha256))state.languageEdits.clear();
    state.languages=data;const previous=$('language-select').value;
    const locales=(data.locales || []).filter(item=>item.code!=='zh' && item.code!=='zh-CN');
    if(locales.length)$('language-select').replaceChildren(...locales.map(item=>new Option(item.label || item.code,item.code)));
    $('language-select').value=locales.some(item=>item.code===previous)?previous:(data.selected_locales?.find(code=>code!=='zh') || locales[0]?.code || 'en');
    drawLanguages();
  }catch(error){if(id===state.id){$('language-empty').hidden=false;$('language-empty').textContent=`文字未载入：${error.message}`;notice(error.message,true);}}
  finally{if(id===state.id){state.languageBusy=false;drawLanguageProgress();$('save-languages').disabled=!state.languages || state.busy;}}
}
function translationFor(entry,locale){
  const draft=state.languageEdits.get(languageEditKey(locale,entry.key));return draft || entry.translations?.[locale] || {text:'',status:'proposed',source_sha256:entry.source_sha256};
}
function drawTranslationProvider(){
  const provider=state.languages?.provider_info;
  $('draft-language').textContent=provider?.configured?'起草译文':'套用已知译文';
  $('draft-language').title=provider?.configured?(provider.label || '使用已配置的文字翻译服务'):'只套用完整匹配的已知译文，其余内容保留待填写';
  $('translation-disclosure').textContent=provider?.configured?'已接入文字翻译服务。点击起草仅发送面向用户的说明文字，不发送 STP 或三维模型；已有已核对译文会保留。生成内容仍需人工核对，工程审核文字单独编辑。':'未接入通用文字翻译服务。仅套用完整匹配的已知译文，其余内容保留待填写，不表示所有语言已自动译完。英语、德语、法语、西班牙语均可人工编辑，中文原文保持来源对应。';
}
function drawLanguageProgress(){
  const data=state.languages;if(!data)return;const locale=$('language-select').value;
  drawTranslationProvider();
  const rows=data.entries || [],scope=$('language-scope').value;
  const progressRows=rows.filter(entry=>scope==='all' || (scope==='engineering'?entry.scope==='engineering':entry.scope!=='engineering'));
  const translated=progressRows.filter(entry=>translationFor(entry,locale).text?.trim()).length;
  const reviewed=progressRows.filter(entry=>{const value=translationFor(entry,locale);return value.status==='reviewed' && value.source_sha256===entry.source_sha256 && value.text?.trim();}).length;
  $('language-progress-text').textContent=`${translated} / ${progressRows.length} 已填写`;
  $('language-summary').textContent=`${reviewed} 已核对 · ${progressRows.length-translated} 待填写${state.languageEdits.size?` · ${state.languageEdits.size} 条修改未保存`:''}`;
  $('save-languages').disabled=state.busy || state.languageBusy;
  $('draft-language').disabled=state.busy || state.languageBusy;
  const keys=state.plan.pdf_template?.text_regions?.map(region=>region.key);
  const required=keys?.length?rows.filter(entry=>keys.includes(entry.key)):rows.filter(entry=>entry.scope!=='engineering');
  const complete=required.length>0 && required.every(entry=>{const value=translationFor(entry,locale);return value.text?.trim() && value.source_sha256===entry.source_sha256;});
  const checked=complete && required.every(entry=>translationFor(entry,locale).status==='reviewed');
  const readiness=data.export_readiness?.[locale];
  const exportBlocked=state.busy || state.languageBusy || !!state.languageEdits.size || state.dirty.size>0 || state.data.stale || !keys?.length;
  $('export-language-final').disabled=exportBlocked || !(readiness?readiness.final_ready:checked);
  $('export-language-draft').disabled=exportBlocked || !(readiness?readiness.draft_ready:complete);
  $('export-language-final').title=!keys?.length?'原模板尚未设置可翻译文字区域':!checked?'需要核对原模板中每一条译文':state.data.stale?'当前模型的步骤图尚未生成完整':'';
  $('export-language-draft').title=!keys?.length?'原模板尚未设置可翻译文字区域':!complete?'需要填写原模板中每一条译文':'';
}
function drawLanguages(){
  const data=state.languages;if(!data)return;
  const locale=$('language-select').value,scope=$('language-scope').value;
  const rows=(data.entries || []).filter(entry=>scope==='all' || (scope==='engineering' ? entry.scope==='engineering' : entry.scope!=='engineering'));
  $('language-empty').hidden=!!rows.length;$('language-empty').textContent='此范围没有可编辑的说明文字。';
  $('language-entries').replaceChildren(...rows.map(entry=>{
    const value=translationFor(entry,locale),stale=!!value.text && (value.status==='stale' || value.source_sha256!==entry.source_sha256);
    const row=document.createElement('article');row.className=`translation-row${stale?' stale':''}`;
    const source=document.createElement('div'),kind=document.createElement('span'),text=document.createElement('div');source.className='translation-source';kind.className='translation-kind';
    const kinds={instruction:'操作说明',title:'标题',caution:'注意事项',warning:'注意事项',engineering_warning:'工程审核信息',document:'说明书正文',template_text:'模板文字',assembly_label:'安装单元',review_note:'待核对依据'};
    kind.textContent=`${entry.scope==='engineering'?'工程审核':'说明书正文'} · ${kinds[entry.kind] || entry.kind || entry.key}`;text.textContent=entry.source_text;source.append(kind,text);
    const target=document.createElement('div');target.className='translation-target';const input=document.createElement('textarea');input.value=value.text || '';input.rows=Math.min(7,Math.max(2,Math.ceil(String(entry.source_text).length/55)));input.placeholder='填写译文';input.setAttribute('aria-label',`${locale} 译文：${labelText(entry.source_text,55)}`);
    const hint=document.createElement('p');hint.className='hint';hint.textContent=stale?'原文已变化，旧译文待复核':!value.text?'待填写':value.status==='reviewed'?'已核对':value.provenance==='offline-memory' || value.provenance?.kind==='offline_memory'?'已知表述起草 · 待核对':'草稿 · 待核对';target.append(input,hint);
    const reviewed=document.createElement('label');reviewed.className='translation-reviewed';const check=document.createElement('input');check.type='checkbox';check.checked=value.status==='reviewed' && !stale;check.disabled=!value.text?.trim();check.setAttribute('aria-label',`已核对 ${locale} 译文：${labelText(entry.source_text,40)}`);reviewed.append(check,document.createTextNode('已核对'));
    const update=status=>{state.languageEdits.set(languageEditKey(locale,entry.key),{key:entry.key,locale,text:input.value,status,source_sha256:entry.source_sha256});hint.textContent=input.value.trim()?(status==='reviewed'?'已核对 · 未保存':'已修改 · 未保存'):'待填写';drawLanguageProgress();};
    input.oninput=()=>{check.checked=false;check.disabled=!input.value.trim();update('proposed');};check.onchange=()=>update(check.checked?'reviewed':'proposed');
    row.append(source,target,reviewed);return row;
  }));drawLanguageProgress();
}
async function saveLanguages(){
  if(!state.languages)return false;
  const data=state.languages,locale=$('language-select').value;
  const snapshot=new Map([...state.languageEdits].map(([key,value])=>[key,clone(value)]));
  const translations=[...snapshot.values()];
  const result=await action('languages',{source_sha256:data.source_sha256,base_sha256:data.base_sha256,locales:[...new Set([...(data.selected_locales || []),locale])],translations},'译文已保存，待填写和待复核内容会继续标明。');
  if(result){for(const [key,value] of snapshot)if(JSON.stringify(state.languageEdits.get(key))===JSON.stringify(value))state.languageEdits.delete(key);await loadLanguages();}return result;
}
async function draftLanguage(){
  if(!state.languages)return;
  if(state.languageEdits.size && !await saveLanguages())return;
  const data=state.languages;
  const message=data.provider_info?.configured?'译文草稿已返回，请核对操作表达、规格与数量；已有译文继续保留。':'已套用可完整匹配的已知译文，其他内容保持待填写。请逐条核对。';
  const result=await action('translate',{source_sha256:data.source_sha256,base_sha256:data.base_sha256,locales:[$('language-select').value],overwrite:false},message);
  if(result)await loadLanguages();
}
async function exportLanguage(draft){
  if(!state.languages)return;
  if(state.languageEdits.size && !await saveLanguages())return;
  const id=state.id,locale=$('language-select').value;
  const result=await action('export-language',{source_sha256:state.plan.source.sha256,locale,draft},draft?'翻译草稿已导出，未确认译文仍需人工核对。':'该语言 PDF 已导出。');
  if(result && id===state.id){const link=$('open-language-pdf');link.href=`/projects/${id}/manual-${encodeURIComponent(locale)}.pdf`;link.hidden=false;}
}
function defaultDisplayOffset(groupId){
  const group=state.plan.groups.find(item=>item.id===groupId),ids=new Set(group?.part_ids || []);
  const boxes=(state.viewer?.sceneData?.parts || []).filter(part=>ids.has(part.id)).map(part=>part.bbox).filter(bounds=>Array.isArray(bounds) && bounds.length===2);
  if(!boxes.length)return null;
  const low=[0,1,2].map(axis=>Math.min(...boxes.map(bounds=>bounds[0][axis]))),high=[0,1,2].map(axis=>Math.max(...boxes.map(bounds=>bounds[1][axis])));
  const amount=Math.max(10,Math.hypot(...high.map((n,axis)=>n-low[axis]))*.18);
  const up=state.plan.style?.up || [0,0,1],length=Math.hypot(...up);
  return up.map(n=>n/length*amount);
}
function scaleExplosion(value) {
  const item=step();const factor=value/state.ratio;
  let initialized=false;
  for(const id of item.moving_groups || []){let offset=item.offsets[id] || [0,0,0];if(!offset.some(n=>Math.abs(n)>1e-9)){const display=defaultDisplayOffset(id);if(display){offset=display;initialized=true;}}item.offsets[id]=offset.map(n=>n*factor);}
  for(const arrow of item.arrows || [])arrow.from=arrow.from.map((n,axis)=>arrow.to[axis]+(n-arrow.to[axis])*factor);
  state.ratio=value;$('explode-range').value=value;$('explode-value').textContent=`${Math.round(value)}%`;changed();
  if(initialized)notice('已按当前零件尺寸设置图示分离，可在“分离方向”修改 XYZ。分离位置用于展示。');
}
function downloadJSON(value,name) {
  const url=URL.createObjectURL(new Blob([JSON.stringify(value,null,2)],{type:'application/json'}));
  const a=document.createElement('a');a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
}
function templateSlot() {return state.template?.figure_slots?.find(slot=>slot.step_id===$('template-step').value);}
function openTemplate() {
  if(!state.plan)return;
  state.template=clone(state.plan.pdf_template || {mode:'original_template_figure_replacement',figure_slots:[]});
  $('template-step').replaceChildren(...includedSteps().map((item,index)=>new Option(`${index+1} ${labelText(item.title,60)}`,item.id)));
  $('template-step').value=includedSteps().some(item=>item.id===state.selected)?state.selected:includedSteps()[0]?.id;templateInfo();$('template-dialog').showModal();
}
function templateInfo() {
  const info=state.data.template_info;
  $('template-description').textContent=info?`${info.filename} · 2 页 · ${info.pending?'待绑定':'已绑定'}`:'尚未上传模板';
  $('template-page').replaceChildren(...(info?.pages || []).map((_,index)=>new Option(`第 ${index+1} 页`,index+1)));
  $('template-page').value=String(templateSlot()?.page || (info?2:1));showTemplatePage();showSlot();
}
function showTemplatePage() {
  const info=state.data.template_info,page=$('template-page').value;
  $('template-page-image').hidden=!info;$('template-empty').hidden=!!info;
  if(info && page)$('template-page-image').src=`${info.preview_base_url}${page}.png?v=${info.sha256}`;
  showSlotOverlay();
}
function showSlot() {
  const slot=templateSlot();
  $('template-region-json').value=JSON.stringify(slot?{page:slot.page,rect:slot.rect,safe_white_masks_pt:slot.safe_white_masks_pt}:{page:Number($('template-page').value)||2,rect:null,safe_white_masks_pt:[]},null,2);
  if(slot && $('template-page').value!==String(slot.page)) {$('template-page').value=String(slot.page);showTemplatePage();}
  showSlotOverlay();
}
function showSlotOverlay() {
  const slot=templateSlot(),info=state.data.template_info,page=Number($('template-page').value),image=$('template-page-image');
  const selection=$('template-selection');selection.hidden=!slot || slot.page!==page || !info;
  if(selection.hidden)return;
  const [x0,y0,x1,y1]=slot.rect, dimensions=info.pages[page-1];
  Object.assign(selection.style,{left:`${x0/dimensions.width*image.clientWidth}px`,top:`${y0/dimensions.height*image.clientHeight}px`,width:`${(x1-x0)/dimensions.width*image.clientWidth}px`,height:`${(y1-y0)/dimensions.height*image.clientHeight}px`});
}
function applySlot(value) {
  const id=$('template-step').value,info=state.data.template_info;
  if(!info || !Number.isInteger(value.page) || !info.pages[value.page-1] || !Array.isArray(value.rect) || value.rect.length!==4 || value.rect.some(n=>!Number.isFinite(n)) || value.rect[2]<=value.rect[0] || value.rect[3]<=value.rect[1]) throw new Error('区域必须有有效页码和 [左,上,右,下] 坐标。');
  const slot={step_id:id,...value};state.template.figure_slots=state.template.figure_slots.filter(item=>item.step_id!==id);state.template.figure_slots.push(slot);showSlot();
}

$('project-select').onchange=()=>void selectProject($('project-select').value).catch(error=>notice(error.message,true));
$('download-conflict-draft').onclick=()=>downloadJSON(state.conflictedDraft,'manual-conflicting-draft.json');
$('dismiss-conflict-draft').onclick=()=>{sessionStorage.removeItem(`manual-conflict:${state.id}`);state.conflictedDraft=null;$('draft-conflict').hidden=true;rememberDraft();notice('旧草案恢复提示已关闭，当前配置继续保留。');};
document.querySelectorAll('[data-workspace-tab]').forEach(button=>{
  button.onclick=()=>switchTab(button.dataset.workspaceTab);
  button.onkeydown=event=>{if(!['ArrowLeft','ArrowRight'].includes(event.key))return;event.preventDefault();const buttons=[...document.querySelectorAll('[data-workspace-tab]')],index=buttons.indexOf(button),next=buttons[(index+(event.key==='ArrowRight'?1:buttons.length-1))%buttons.length];next.focus();switchTab(next.dataset.workspaceTab);};
});
for(const id of ['new-project','empty-create'])$(id).onclick=()=>$('new-project-dialog').showModal();
document.querySelectorAll('[data-close-dialog]').forEach(button=>button.onclick=()=>$(button.dataset.closeDialog).close());
$('new-project-form').onsubmit=async event=>{
  event.preventDefault();const file=$('new-project-source').files[0];if(!file)return;
  if(state.busy || state.sceneBusy){notice('当前后台任务完成后即可创建项目。');return;}
  state.busy=true;
  $('create-project-submit').disabled=true;
  try {
    const created=await api('/api/projects',{title:$('new-project-title').value.trim()});
    const id=created.project.id;const uploaded=await api(endpoint('/source',id),undefined,file);
    $('new-project-dialog').close();rememberDraft();state.id=id;state.plan=null;state.data=null;$('workspace').hidden=true;$('empty-state').hidden=true;jobMessage('正在上传、识别模型并起草步骤…');
    await waitJob(uploaded.job_id,id);state.busy=false;await selectProject(id);notice('草案已生成。先核对总成与顺序，再生成插图。');
  } catch(error){notice(error.message,true);await refreshProjects();}
  finally{state.busy=false;$('create-project-submit').disabled=false;jobMessage('',state.sceneBusy);if(!state.plan)$('empty-state').hidden=false;}
};
$('revision-source').onchange=async()=>{
  const file=$('revision-source').files[0];if(!file || !state.id)return;
  if(state.busy || state.sceneBusy){notice('当前项目后台任务完成后即可上传新版本。');$('revision-source').value='';return;}
  const id=state.id;state.busy=true;
  try {
    const result=await api(endpoint('/source',id),undefined,file);jobMessage('正在分析新 STP，旧版本和原图会保留在历史中…');
    await waitJob(result.job_id,id);clearDraft(id);state.busy=false;
    if(id===state.id)await selectProject(id);
    notice('已切换到新 STP 草案。旧图与确认已失效，请核对新版本的步骤和模板对应。');
  } catch(error){notice(`新版本未切换，原项目保留：${error.message}`,true);if(id===state.id)await fetchState({keepEdits:true});}
  finally{state.busy=false;$('revision-source').value='';jobMessage('',state.sceneBusy);drawWorkspace();}
};
$('retry-source').onclick=async()=>{
  if(state.busy || state.sceneBusy)return;
  const id=state.id;state.busy=true;drawWorkspace();
  try {
    const result=await api(endpoint('/retry-source',id),{});jobMessage('正在重新处理已上传的 STP，完整导入缓存会复用…');
    await waitJob(result.job_id,id);clearDraft(id);state.busy=false;
    if(id===state.id)await selectProject(id);
    notice('已切换到新 STP 草案。空结构占位已记录，有实际几何的对象保留。');
  } catch(error){notice(`重试未切换，原项目保留：${error.message}`,true);if(id===state.id)await fetchState({keepEdits:true});}
  finally{state.busy=false;jobMessage('',state.sceneBusy);drawWorkspace();}
};
$('show-all-model').onchange=()=>{state.inspectGroups=null;updateViewer(false);};
$('show-group-model').onclick=()=>{state.inspectGroups=null;$('show-all-model').checked=true;switchTab('steps');updateViewer(false);};
$('group-search').oninput=drawGroups;
function navigateModel(method,...args) {
  try {
    if(!state.viewer?.sceneData)throw new Error('三维模型还在准备中。');
    state.viewer[method](...args);
  } catch(error){notice(error.message,true);}
}
document.querySelectorAll('[data-view-axis]').forEach(button=>button.onclick=()=>navigateModel('setAxisView',button.dataset.viewAxis,Number(button.dataset.viewSign)));
$('fit-model').onclick=()=>navigateModel('fit');
$('reset-view').onclick=()=>navigateModel('resetView');
$('roll-left').onclick=()=>navigateModel('roll',Math.PI/12);
$('roll-right').onclick=()=>navigateModel('roll',-Math.PI/12);
$('show-axes').onchange=()=>state.viewer?.setAxesVisible($('show-axes').checked);
$('apply-view').onclick=()=>{try{if(!state.viewer)throw new Error('三维模型还在准备中。');state.inspectGroups=null;$('show-all-model').checked=false;Object.assign(step(),state.viewer.getView());changed();notice('当前视角已采用到这一步。');}catch(error){notice(error.message,true);}};
$('view-preset').onchange=()=>{
  const views={front:[-1,.45,1],back:[1,.45,1],side:[-1,.45,-1],top:[.001,1,.001],bottom:[.001,-1,.001]};
  const value=$('view-preset').value;if(!views[value])return;
  if(!state.viewer){notice('三维模型正在准备，载入后即可使用快捷视角。');$('view-preset').value='';return;}
  const current=step(),preview={...current,camera:views[value],up:value==='top'||value==='bottom'?[0,0,1]:[0,1,0]};
  if($('show-all-model').checked)Object.assign(preview,{assembled_groups:state.plan.groups.map(group=>group.id),moving_groups:[],offsets:{},hidden_part_ids:[],arrows:[]});
  if(state.inspectGroups)Object.assign(preview,{assembled_groups:state.inspectGroups,moving_groups:[],offsets:{},hidden_part_ids:[],arrows:[]});
  state.viewer.setStep(state.plan,preview,{preserveView:false});$('view-preset').value='';notice('快捷视角仅用于预览，满意后点击“采用当前视角”。');
};
$('explode-closer').onclick=()=>scaleExplosion(Math.max(20,state.ratio*.8));
$('explode-farther').onclick=()=>scaleExplosion(Math.min(300,state.ratio*1.25));
$('explode-range').oninput=()=>scaleExplosion(Number($('explode-range').value));
$('visibility-reason').onchange=()=>{step().visibility_reason=$('visibility-reason').value;changed({render:false,redraw:false});};
$('generate-step').onclick=()=>requestPreview();
for(const [id,field] of [['step-title','title'],['step-instruction','instruction']])$(id).onchange=()=>{step()[field]=$(id).value;changed({render:false,redraw:false});};
$('step-cautions').onchange=()=>{step().consumer_warnings=$('step-cautions').value.split('\n').map(text=>text.trim()).filter(Boolean);changed({render:false,redraw:false});};
$('save-step-copy').onclick=()=>void action('plan',{plan:clone(state.plan)},'步骤文字已保存。原文变化后，对应译文需要复核。');
$('step-up').onclick=()=>void moveStep(-1);$('step-down').onclick=()=>void moveStep(1);
$('delete-step').onclick=()=>{const current=step();if(!current || isOverview(current))return;$('remove-step-name').textContent=current.title;$('remove-step-target').replaceChildren(new Option('不转移（预装或已在其他动作中安装）',''),...includedSteps().filter(item=>item.id!==current.id).map(item=>new Option(labelText(item.title,60),item.id)));$('remove-step-dialog').showModal();};
$('remove-step-submit').onclick=async()=>{const operation={action:'remove_step',step_id:state.selected};if($('remove-step-target').value)operation.reassign_to=$('remove-step-target').value;const result=await workflowEdit(operation,'步骤已删除，相关安装单元的动作已保留。');if(result)$('remove-step-dialog').close();};
$('add-step').onclick=()=>{if(state.plan.pdf_template)return;$('new-step-title').value='';$('new-step-position').replaceChildren(new Option('放在最后一个安装动作后',''),...includedSteps().map(item=>new Option(`放在“${labelText(item.title,35)}”后`,item.id)));$('new-step-position').value=isOverview(step())?'':state.selected;$('add-step-dialog').showModal();};
$('add-step-form').onsubmit=async event=>{event.preventDefault();const previous=new Set(state.plan.steps.map(item=>item.id)),item=step();const sourceGroups=item?.assembled_groups?.length?item.assembled_groups:state.plan.groups.slice(0,1).map(group=>group.id);const operation={action:'add_step',title:$('new-step-title').value.trim(),instruction:'核对本步安装单元、连接方式与操作先后。',moving_groups:[],assembled_groups:sourceGroups};if($('new-step-position').value)operation.after_step_id=$('new-step-position').value;$('create-step-submit').disabled=true;try{const result=await workflowEdit(operation,'步骤已添加。选择参与单元，填写实际操作说明后出图。');if(result){const added=state.plan.steps.find(row=>!previous.has(row.id));if(added)selectStep(added.id);$('add-step-dialog').close();$('scene-composition').open=true;}}finally{$('create-step-submit').disabled=false;}};
$('apply-step-groups').onclick=async()=>{const options=$('step-group-options'),assembled=[...options.querySelectorAll('[data-scene-type="assembled"]:checked')].map(input=>input.dataset.groupId),moving=[...options.querySelectorAll('[data-scene-type="moving"]:checked')].map(input=>input.dataset.groupId);if(!assembled.length && !moving.length){notice('至少选择一个背景或本步装入单元。',true);return;}const offsets={};for(const id of moving)offsets[id]=clone(step().offsets?.[id] || state.plan.steps.find(row=>row.offsets?.[id])?.offsets[id] || [0,0,0]);await workflowEdit({action:'update_step',step_id:state.selected,assembled_groups:assembled,moving_groups:moving,offsets},'本步参与单元已保存，可继续调整视角和分离距离。');updateViewer(false);};
$('step-reviewed').onchange=async()=>{
  const reviewed=$('step-reviewed').checked;step().reviewed=reviewed;
  if(state.dirty.size){rememberDraft();notice('先生成修改后的插图，再核对这一步。');step().reviewed=false;$('step-reviewed').checked=false;return;}
  await action('plan',{plan:clone(state.plan)},reviewed?'本步核对已保存。':'本步已改为待核对。');
};
$('generate-all').onclick=()=>void action('render',{plan:clone(state.plan)},'全部步骤图已生成。现在可逐步调整三维视角。');
$('export-pdf').onclick=()=>void action('export',{},'已将当前步骤图放回你的原模板，PDF 可以查看。');
$('confirm').onclick=()=>void action('confirm',{plan:clone(state.plan)},'这套说明书已确认。');
$('advanced-open').onclick=()=>{$('advanced-json').value=JSON.stringify(state.plan,null,2);$('advanced-dialog').showModal();};
$('apply-advanced').onclick=async()=>{try{const plan=JSON.parse($('advanced-json').value);await action('plan',{plan},'配置已保存，修改过的步骤可单独出图。');$('advanced-dialog').close();}catch(error){notice(error.message,true);}};
$('merge-selected').onclick=()=>{if(!$('merge-name').value.trim()){notice('填写合并后的安装单元名称。');$('merge-name').focus();return;}void workflowEdit({action:'merge_groups',group_ids:[...state.selectedGroups],label:$('merge-name').value.trim()},'安装单元已合并，相关步骤的参与单元已更新。');};
$('language-select').onchange=()=>{$('open-language-pdf').hidden=true;$('open-language-pdf').removeAttribute('href');drawLanguages();};$('language-scope').onchange=drawLanguages;
$('language-reload').onclick=()=>void loadLanguages({discard:true});$('save-languages').onclick=()=>void saveLanguages();$('draft-language').onclick=()=>void draftLanguage();
$('delivery-download-config').onclick=()=>downloadJSON(state.plan,'steps.json');
$('delivery-download-languages').onclick=async()=>{if(!state.languages)await loadLanguages();if(state.languages)downloadJSON(state.languages,'manual-languages.json');};
$('export-language-draft').onclick=()=>void exportLanguage(true);$('export-language-final').onclick=()=>void exportLanguage(false);
$('template-settings').onclick=openTemplate;
$('template-step').onchange=showSlot;$('template-page').onchange=showTemplatePage;
$('template-page-image').onload=showSlotOverlay;
$('template-file').onchange=async()=>{try{const file=$('template-file').files[0];if(!file)return;await api(endpoint('/template'),undefined,file);await fetchState({keepEdits:true});state.template={mode:'original_template_figure_replacement',figure_slots:[]};templateInfo();notice('模板已上传，请逐步设置替换图的区域。');}catch(error){notice(error.message,true);}};
$('template-config-file').onchange=async()=>{try{const file=$('template-config-file').files[0];if(!file)return;const config=JSON.parse(await file.text());if(!Array.isArray(config.figure_slots))throw new Error('配置缺少 figure_slots。');state.template=config;templateInfo();notice('已载入图槽，保存前会校验模板、步骤和文字。');}catch(error){notice(error.message,true);}};
$('apply-region').onclick=()=>{try{applySlot(JSON.parse($('template-region-json').value));}catch(error){notice(error.message,true);}};
let dragStart=null;
$('template-canvas').onpointerdown=event=>{
  if(!state.data.template_info || $('template-page-image').hidden)return;
  const bounds=$('template-page-image').getBoundingClientRect();
  dragStart={x:Math.max(0,Math.min(bounds.width,event.clientX-bounds.left)),y:Math.max(0,Math.min(bounds.height,event.clientY-bounds.top)),bounds};
  $('template-canvas').setPointerCapture(event.pointerId);
};
$('template-canvas').onpointermove=event=>{
  if(!dragStart)return;const {x,y,bounds}=dragStart, x1=Math.max(0,Math.min(bounds.width,event.clientX-bounds.left)),y1=Math.max(0,Math.min(bounds.height,event.clientY-bounds.top));
  $('template-selection').hidden=false;Object.assign($('template-selection').style,{left:`${Math.min(x,x1)}px`,top:`${Math.min(y,y1)}px`,width:`${Math.abs(x-x1)}px`,height:`${Math.abs(y-y1)}px`});
};
$('template-canvas').onpointerup=event=>{
  if(!dragStart)return;const {x,y,bounds}=dragStart;dragStart=null;
  const x1=Math.max(0,Math.min(bounds.width,event.clientX-bounds.left)),y1=Math.max(0,Math.min(bounds.height,event.clientY-bounds.top));
  if(Math.abs(x1-x)<4 || Math.abs(y1-y)<4)return;
  const page=Number($('template-page').value),size=state.data.template_info.pages[page-1];
  const rect=[Math.min(x,x1)/bounds.width*size.width,Math.min(y,y1)/bounds.height*size.height,Math.max(x,x1)/bounds.width*size.width,Math.max(y,y1)/bounds.height*size.height].map(n=>Math.round(n*10)/10);
  try{applySlot({page,rect,safe_white_masks_pt:[rect]});}catch(error){notice(error.message,true);}
};
$('bind-template').onclick=async()=>{
  const config=clone(state.template);
  if(!includedSteps().every(item=>config.figure_slots.some(slot=>slot.step_id===item.id))){notice('每个安装步骤都需要一个对应的步骤图区域。',true);return;}
  const ids=new Set(includedSteps().map(item=>item.id));config.figure_slots=config.figure_slots.filter(slot=>ids.has(slot.step_id));
  config.step_bindings=includedSteps().map(item=>({step_id:item.id,title:item.title,instruction:item.instruction,consumer:clone(item.consumer||{})}));
  if(state.plan.manual_document)config.document_binding=clone(state.plan.manual_document);
  await action('bind-template',{config},'原模板对应关系已保存。导出只替换步骤插图。');
};
$('download-template-config').onclick=()=>downloadJSON(state.template,'template-regions.json');
window.addEventListener('beforeunload',()=>rememberDraft());
async function start() {
  try {
    const bootstrap=await api('/api/bootstrap');state.token=bootstrap.token;state.projects=bootstrap.projects;
    await refreshProjects();
    const preferred=localStorage.getItem('manual-active-project') || bootstrap.active_project_id;
    const id=state.projects.find(project=>project.id===preferred)?.id || state.projects[0]?.id;
    if(id)await selectProject(id);else{$('empty-state').hidden=false;$('project-select').replaceChildren(new Option('尚无项目',''));}
  } catch(error){notice(`应用载入失败：${error.message}`,true);}
}
void start();
