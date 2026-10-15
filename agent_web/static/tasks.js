let tasks=[];
const activeStatuses=["queued","dispatching","running","cancelling"];
const labels={running:["运行中","当前正在处理"],queued:["排队中","等待系统资源"],succeeded:["今日成功","已完成任务"],failed:["今日失败","需要关注"]};

async function loadTasks(){
  tasks=await api("/api/tasks");renderMetrics();renderTasks();
  const requested=new URLSearchParams(location.search).get("task");
  if(requested){const row=tasks.find(item=>item.id===requested);if(row)openTask(row)}
}
function renderMetrics(){
  const counts={running:0,queued:0,succeeded:0,failed:0};
  tasks.forEach(task=>{const key=["dispatching","cancelling"].includes(task.status)?"running":task.status;if(counts[key]!==undefined)counts[key]++});
  document.getElementById("taskMetrics").innerHTML=Object.entries(labels).map(([key,value])=>`<div class="metric-card"><small>${value[0]}</small><strong>${counts[key]}</strong><span>${value[1]}</span></div>`).join("");
}
function queueCopy(task){
  if(task.status!=="queued")return "";
  const position=task.queue_position?`${task.resource_class==="gpu"?"GPU":"CPU"} 队列第 ${task.queue_position} 位 · `:"";
  return `${position}${task.queue_reason_text||"等待系统调度"}`;
}
function queuePositionCopy(task){return task.queue_position?`预计为 ${task.resource_class==="gpu"?"GPU":"CPU"} 队列第 ${task.queue_position} 位`:"正在计算队列位置"}
function resourceCopy(task){return `CPU × ${Number(task.requested_cpus||0).toLocaleString()} / GPU × ${Number(task.requested_gpus||0).toLocaleString()}`}
function renderTasks(){
  const query=document.getElementById("taskSearch").value.toLowerCase(),status=document.getElementById("taskStatus").value,executor=document.getElementById("taskExecutor").value;
  const rows=tasks.filter(task=>(!query||`${task.name} ${task.id}`.toLowerCase().includes(query))&&(!status||task.status===status)&&(!executor||task.resolved_executor===executor));
  document.getElementById("taskRows").innerHTML=rows.length?rows.map(task=>`<tr><td class="cell-title"><strong>${escapeHtml(task.name)}</strong><small>${escapeHtml(task.id)}</small></td><td class="cell-title"><strong>${resourceCopy(task)}</strong><small>${escapeHtml(task.display_name||task.username||task.conversation_id)}</small></td><td><span class="executor-badge">系统调度</span></td><td><span class="status ${task.status}">${statusLabel(task.status)}</span>${queueCopy(task)?`<small class="queue-copy">${escapeHtml(queueCopy(task))}</small>`:""}</td><td>${formatDate(task.created_at)}</td><td>${task.started_at&&task.finished_at?formatDuration(new Date(task.finished_at)-new Date(task.started_at)):task.status==="running"?"进行中":"—"}</td><td><div class="row-actions"><button data-open="${task.id}">详情</button>${activeStatuses.includes(task.status)?`<button data-cancel="${task.id}">取消</button>`:""}${["failed","cancelled","succeeded"].includes(task.status)?`<button data-retry="${task.id}">重试</button>`:""}</div></td></tr>`).join(""):`<tr><td colspan="7" class="empty-table">当前没有符合条件的任务</td></tr>`;
  document.querySelectorAll("[data-open]").forEach(button=>button.addEventListener("click",()=>openTask(tasks.find(row=>row.id===button.dataset.open))));
  document.querySelectorAll("[data-cancel]").forEach(button=>button.addEventListener("click",()=>cancelTask(button.dataset.cancel)));
  document.querySelectorAll("[data-retry]").forEach(button=>button.addEventListener("click",()=>retryTask(button.dataset.retry)));
}
function openTask(task){
  const drawer=document.getElementById("taskDrawer");drawer.classList.remove("hidden");document.getElementById("taskDrawerTitle").textContent=task.name;
  let plan={};try{plan=JSON.parse(task.plan_json)}catch{}
  const queueSection=task.status==="queued"?`<div class="detail-section"><h3>排队信息</h3><div class="timeline"><div class="timeline-item"><strong>${escapeHtml(task.queue_reason_text||"等待系统调度")}</strong><small>${escapeHtml(queuePositionCopy(task))}</small></div></div></div>`:"";
  drawer.querySelector("#taskDrawerBody").innerHTML=`<div class="detail-grid"><div class="detail-card"><small>任务状态</small><strong><span class="status ${task.status}">${statusLabel(task.status)}</span></strong></div><div class="detail-card"><small>调度方式</small><strong>系统自动调度</strong></div><div class="detail-card"><small>资源申请</small><strong>${resourceCopy(task)}</strong></div><div class="detail-card"><small>实际执行</small><strong>${task.resolved_executor==="ray"?"Ray":"普通执行"}</strong></div></div>${queueSection}<div class="detail-section"><h3>调度说明</h3><p>${escapeHtml(task.recommendation_reason)}</p></div><div class="detail-section"><h3>执行时间线</h3><div class="timeline"><div class="timeline-item"><strong>任务已提交</strong><small>${formatDate(task.created_at)}</small></div>${task.started_at?`<div class="timeline-item"><strong>获得资源并开始执行</strong><small>${formatDate(task.started_at)}</small></div>`:""}${task.finished_at?`<div class="timeline-item"><strong>${statusLabel(task.status)}</strong><small>${formatDate(task.finished_at)}</small></div>`:""}</div></div><div class="detail-section"><h3>Data-Juicer 配置</h3><div class="code-panel"><pre>${escapeHtml(JSON.stringify(plan,null,2))}</pre></div></div><div class="detail-section"><h3>任务日志</h3><div class="log-view">${escapeHtml(task.log_text||task.error_text||"暂无日志")}</div></div>${task.result_relative_path?`<div class="detail-section"><h3>结果</h3><div class="file-item"><span class="file-icon">✓</span><div class="file-copy"><strong>处理结果</strong><small>${escapeHtml(task.result_relative_path)}</small></div><a class="text-button" href="/api/tasks/${task.id}/result/download">下载</a><button class="text-button" data-delete-result="${task.id}">删除结果</button></div></div>`:""}<div class="confirm-actions">${activeStatuses.includes(task.status)?`<button class="secondary-button" data-cancel="${task.id}">取消任务</button>`:""}${["failed","cancelled","succeeded"].includes(task.status)?`<button class="primary-button" data-retry="${task.id}">重试任务</button>`:""}<button class="text-button" onclick="location.href='/?conversation=${task.conversation_id}'">进入来源会话</button></div>`;
  drawer.querySelectorAll("[data-cancel]").forEach(button=>button.addEventListener("click",()=>cancelTask(button.dataset.cancel)));
  drawer.querySelectorAll("[data-retry]").forEach(button=>button.addEventListener("click",()=>retryTask(button.dataset.retry)));
  drawer.querySelector("[data-delete-result]")?.addEventListener("click",()=>deleteResult(task.id));
}
async function cancelTask(id){if(!confirm("确认取消这个任务？"))return;try{await api(`/api/tasks/${id}/cancel`,{method:"POST"});toast("取消请求已提交");await loadTasks()}catch(err){toast(err.message,"error")}}
async function retryTask(id){try{const task=await api(`/api/tasks/${id}/retry`,{method:"POST",body:JSON.stringify({})});toast("已创建重试任务");await loadTasks();openTask(task)}catch(err){toast(err.message,"error")}}
async function deleteResult(id){if(!confirm("结果文件将被永久删除，但任务记录会保留。确认继续？"))return;try{await api(`/api/tasks/${id}/result`,{method:"DELETE"});toast("结果已删除，任务记录已保留");document.getElementById("taskDrawer").classList.add("hidden");await loadTasks()}catch(err){toast(err.message,"error")}}
document.querySelectorAll("[data-close-drawer]").forEach(el=>el.addEventListener("click",()=>document.getElementById("taskDrawer").classList.add("hidden")));
["taskSearch","taskStatus","taskExecutor"].forEach(id=>document.getElementById(id).addEventListener(id==="taskSearch"?"input":"change",renderTasks));
document.getElementById("refreshTasks").addEventListener("click",loadTasks);
loadTasks().catch(err=>toast(err.message,"error"));
setInterval(()=>{if(tasks.some(task=>activeStatuses.includes(task.status)))loadTasks()},3000);
