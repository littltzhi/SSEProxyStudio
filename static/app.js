const $ = (selector) => document.querySelector(selector);
const token = $('meta[name="api-token"]').content;
let state = null;
let activeId = null;
let activeDocument = null;
let editableDocument = null;
let activeTab = "events";
let selectionGeneration = 0;
let noticeTimer;
let connected = false;
let historySignature = "";

async function api(path, { method = "GET", body } = {}) {
  const response = await fetch(path, {
    method, headers: { "X-SSE-Key": token, "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
  return result;
}

function notice(message, error = false) {
  clearTimeout(noticeTimer);
  $("#notice").textContent = message;
  $("#notice").hidden = !message;
  if (!error) noticeTimer = setTimeout(() => { $("#notice").hidden = true; }, 5000);
}

async function action(work) {
  try { await work(); } catch (error) { notice(error.message, true); }
}

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function size(bytes) {
  return bytes > 1024 * 1024 ? `${(bytes / 1024 / 1024).toFixed(1)} MB` : `${(bytes / 1024).toFixed(1)} KB`;
}

async function refresh() {
  try {
    const previousError = state?.last_error;
    state = await api("/api/state");
    connected = true;
    $("#connection").textContent = `代理 ${state.proxy}`;
    $("#connection-dot").style.background = "#34a98b";
    $("#proxy-address").textContent = state.proxy;
    $("#data-path").textContent = state.data_dir;
    document.querySelectorAll("[data-mode]").forEach(button => {
      button.classList.toggle("active", button.dataset.mode === state.settings.mode);
    });
    $("#mode-description").textContent = {
      off: "流量经过代理，暂不录制", record: "真实 SSE 正在实时记录", replay: "命中规则的请求使用本地响应",
    }[state.settings.mode];
    $("#speed").value = state.settings.speed;
    $("#include-query").checked = state.settings.include_query;
    for (const [selector, key] of [["#host-filter", "host_filter"], ["#path-filter", "path_filter"]]) {
      if (document.activeElement !== $(selector)) $(selector).value = state.settings[key];
    }
    const selected = state.recordings.find(item => item.id === state.settings.selected);
    $("#selected-rule").textContent = selected ? `${selected.method} ${selected.url}${state.settings.include_query ? "" : "（忽略 query）"}` : "尚未选用录制";
    $("#hits").textContent = `${state.replay_hits} 次命中`;
    renderHistory();
    if (state.last_error && state.last_error !== previousError) notice(state.last_error, true);
  } catch (error) {
    connected = false;
    $("#connection").textContent = "代理未连接";
    $("#connection-dot").style.background = "#d34d6b";
  }
}

function renderHistory() {
  $("#record-count").textContent = state.recordings.length;
  const search = $("#search").value.toLowerCase();
  const signature = JSON.stringify([state.recordings, state.settings.selected, activeId, search]);
  if (signature === historySignature) return;
  historySignature = signature;
  const records = state.recordings.filter(item => `${item.name} ${item.url}`.toLowerCase().includes(search));
  const nodes = records.map(item => {
    const row = element("button", `record-row${item.id === activeId ? " active" : ""}`);
    const title = element("div", "row-title");
    title.append(element("span", "method", item.method), element("span", "row-name", item.name));
    if (item.id === state.settings.selected) title.append(element("span", "chosen", "已选用"));
    const meta = element("div", "row-meta");
    meta.append(
      element("span", "", new Date(item.created_at).toLocaleTimeString()),
      element("span", "", `${item.duration.toFixed(2)}s`),
      element("span", "", size(item.byte_count)),
      element("span", `outcome ${item.outcome}`, item.outcome),
    );
    row.append(title, element("div", "row-url", item.url), meta);
    row.onclick = () => action(() => openRecording(item.id));
    return row;
  });
  if (!nodes.length) nodes.push(element("div", "no-records", search ? "没有匹配的记录" : "还没有录制\n连接代理，开始第一条 SSE"));
  $("#recordings").replaceChildren(...nodes);
}

async function configure(updates) {
  await api("/api/settings", { method: "POST", body: updates });
  await refresh();
}

async function openRecording(identifier) {
  if (!confirmDiscard()) return;
  const generation = ++selectionGeneration;
  const document = await api(`/api/recordings/${identifier}`);
  let editable = null;
  try { editable = await api(`/api/recordings/${identifier}?editable=1`); } catch {}
  if (generation !== selectionGeneration) return;
  activeId = identifier;
  activeDocument = document;
  editableDocument = editable;
  renderDetail();
  renderHistory();
}

let editorBaseline = "";

function setEditor(document) {
  editorBaseline = JSON.stringify(document, null, 2);
  $("#editor").value = editorBaseline;
}

function confirmDiscard() {
  return $("#editor").value === editorBaseline || confirm("JSON 有未保存修改，放弃这些修改？");
}

function renderDetail() {
  const document = activeDocument;
  $("#empty").hidden = true;
  $("#detail-content").hidden = false;
  $("#detail-name").textContent = document.name;
  $("#detail-url").textContent = `${document.request.method} ${document.request.url}`;
  const frames = editableDocument?.frames || [];
  const metrics = [
    ["HTTP", document.response.status],
    ["总时长", `${document.duration.toFixed(2)} s`],
    ["事件", editableDocument ? frames.length : "原始字节"],
    ["结束状态", document.outcome],
  ];
  $("#metrics").replaceChildren(...metrics.map(([label, value]) => {
    const node = element("div", "metric");
    node.append(element("span", "", label), element("strong", "", value));
    return node;
  }));
  const eventNodes = frames.map(frame => {
    const row = element("div", "event");
    row.append(element("span", "event-time", `+${frame.offset.toFixed(3)}s`));
    const body = element("div");
    const name = frame.event || (frame.data === undefined ? "heartbeat / control" : "message");
    body.append(element("h3", "", `${name}${frame.id !== undefined ? ` · ${frame.id}` : ""}`));
    let text = frame.data === undefined ? JSON.stringify(frame, null, 2) : frame.data;
    try { text = JSON.stringify(JSON.parse(text), null, 2); } catch {}
    body.append(element("pre", "", text));
    row.append(body);
    return row;
  });
  if (!eventNodes.length) eventNodes.push(element("div", "no-records",
    document.outcome === "recording" ? "正在录制，点击左侧记录可刷新详情。" : "没有可展示的完整事件。原始字节可在 JSON 编辑器查看。"));
  $("#events-panel").replaceChildren(...eventNodes);
  setEditor(editableDocument || document);
  $("#editor-caption").textContent = editableDocument
    ? "事件 JSON：可编辑 data、offset 或匹配 URL。保存时创建副本。"
    : "原始字节 JSON：base64 保留分块和不完整帧，保存时创建副本。";
  $("#request-details").textContent = JSON.stringify({
    request: document.request, response: document.response,
    response_offset: document.response_offset, error: document.error,
  }, null, 2);
  $("#use-button").disabled = !activeId || document.outcome === "recording";
  $("#delete-button").disabled = !activeId || document.outcome === "recording";
  $("#export-button").disabled = !activeId || document.outcome === "recording";
  $("#save-button").disabled = document.outcome === "recording";
  switchTab(activeTab);
}

function switchTab(tab) {
  activeTab = tab;
  for (const name of ["events", "json", "request"]) $(`#${name}-panel`).hidden = name !== tab;
  document.querySelectorAll("[data-tab]").forEach(button => button.classList.toggle("active", button.dataset.tab === tab));
}

function newDocument() {
  if (!confirmDiscard()) return;
  selectionGeneration++;
  activeId = null;
  activeDocument = {
    format: "sse-proxy/v1", name: "本地 SSE 示例",
    request: { method: "POST", url: "http://127.0.0.1:9101/stream", headers: {}, body: "{}" },
    response: { status: 200, headers: { "content-type": "text/event-stream; charset=utf-8", "cache-control": "no-cache" } },
    response_offset: .1, duration: 1.6, outcome: "completed",
    frames: [
      { offset: .2, event: "message", id: "1", data: { text: "Hello " } },
      { offset: .8, event: "message", id: "2", data: { text: "from local JSON 👋" } },
      { offset: 1.6, event: "done", data: "[DONE]" },
    ],
  };
  editableDocument = activeDocument;
  activeTab = "json";
  renderDetail();
  if (state) renderHistory();
}

function download(data, filename, type = "application/json") {
  const url = URL.createObjectURL(new Blob([data], { type }));
  const anchor = element("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

document.querySelectorAll("[data-mode]").forEach(button => {
  button.onclick = () => action(() => configure({ mode: button.dataset.mode }));
});
document.querySelectorAll("[data-tab]").forEach(button => button.onclick = () => switchTab(button.dataset.tab));
$("#speed").onchange = () => action(() => configure({ speed: Number($("#speed").value) }));
$("#include-query").onchange = () => action(() => configure({ include_query: $("#include-query").checked }));
$("#scope-save").onclick = () => action(() => configure({
  host_filter: $("#host-filter").value.trim(), path_filter: $("#path-filter").value.trim(),
}));
$("#search").oninput = () => { if (state) renderHistory(); };
$("#new-button").onclick = newDocument;
$("#empty-new").onclick = newDocument;
$("#import-button").onclick = () => $("#import-file").click();
$("#import-file").onchange = () => action(async () => {
  const file = $("#import-file").files[0];
  $("#import-file").value = "";
  if (!file) return;
  if (file.size > 32 * 1024 * 1024) throw new Error("JSON 文件最大为 32 MB。");
  if (!confirmDiscard()) return;
  const result = await api("/api/import", { method: "POST", body: JSON.parse(await file.text()) });
  editorBaseline = $("#editor").value;
  await refresh();
  await openRecording(result.id);
  notice("已导入并选用。切换到「回放」后生效。");
});
$("#use-button").onclick = () => action(async () => {
  await configure({ selected: activeId });
  notice("已选为回放源。切换到「回放」后生效。");
});
$("#save-button").onclick = () => action(async () => {
  const result = await api("/api/import", { method: "POST", body: JSON.parse($("#editor").value) });
  editorBaseline = $("#editor").value;
  await refresh();
  await openRecording(result.id);
  notice("副本已保存并选用。");
});
$("#raw-button").onclick = () => {
  if (confirmDiscard()) setEditor(activeDocument);
};
$("#export-button").onclick = () => action(async () => {
  const document = await api(`/api/recordings/${activeId}`);
  download(JSON.stringify(document, null, 2), `sse-${activeId}.json`);
});
$("#delete-button").onclick = () => action(async () => {
  if (!confirm("删除这条录制？此操作无法撤销。")) return;
  await api(`/api/recordings/${activeId}`, { method: "DELETE" });
  selectionGeneration++;
  activeId = null;
  activeDocument = null;
  setEditor({});
  $("#detail-content").hidden = true;
  $("#empty").hidden = false;
  await refresh();
});
$("#setup-button").onclick = () => $("#setup").showModal();
$("#setup-close").onclick = () => $("#setup").close();
$("#certificate-button").onclick = () => action(async () => {
  const response = await fetch("/api/certificate", { headers: { "X-SSE-Key": token } });
  if (!response.ok) throw new Error("CA 证书还未生成，请确认代理已启动。");
  download(await response.text(), "sse-proxy-ca.pem", "application/x-pem-file");
});
window.addEventListener("beforeunload", event => {
  if ($("#editor").value !== editorBaseline) { event.preventDefault(); event.returnValue = ""; }
});

async function poll() {
  await refresh();
  setTimeout(poll, connected ? 1500 : 3000);
}
poll();
