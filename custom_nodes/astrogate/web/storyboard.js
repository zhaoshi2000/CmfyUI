import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const base = "/astrogate/storyboard";
const states = {prepared: "等待参考图 / 排队中", queued: "排队中", running: "正在生成", analysing: "正在分析剧本", succeeded: "已完成", failed: "失败", blocked: "本批次未执行", unknown: "结果未知 · 可明确重绘", cancelling: "正在停止", cancelled: "已取消"};
const active = new Set(["prepared", "queued", "running", "analysing", "cancelling"]);
const sceneOnly = "无人环境素材：只绘制建筑、空间、陈设与光照，不出现任何人物、路人、剪影或人物倒影。";
const sceneAngles = {scene_left: "左侧机位", scene_right: "右侧机位", scene_reverse: "反打机位", scene_high: "高位俯视"};
let activityPanel;
const value = (node, name) => node.widgets?.find(w => w.name === name)?.value;
const nodes = () => app.graph._nodes;
function setValue(node, name, input) {
    const widget = node.widgets?.find(w => w.name === name);
    if (widget) widget.value = input;
}
async function request(path, body) {
    const response = await api.fetchApi(base + path, body === undefined ? {} : {
        method: "POST", headers: body instanceof FormData ? {} : {"Content-Type": "application/json"},
        body: body instanceof FormData ? body : JSON.stringify(body),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `请求失败 (${response.status})`);
    return data;
}
function element(tag, text, className) {
    const el = document.createElement(tag);
    if (text !== undefined) el.textContent = text;
    if (className) el.className = className;
    return el;
}
function progressBar(node) {
    const root = element("div", undefined, "ag-progress");
    const bar = element("progress");
    bar.max = 1; bar.value = 0;
    const label = element("div", "", "ag-hint");
    root.append(bar, label);
    root.hidden = true;
    node.agProgress = {root, bar, label};
    return root;
}
function showProgress(node, text, done = null, total = 1) {
    if (!node.agProgress) return;
    const {root, bar, label} = node.agProgress;
    root.hidden = false;
    label.textContent = text;
    bar.max = total || 1;
    if (done === null) bar.removeAttribute("value"); else bar.value = done;
}
function batchProgress(node, jobs) {
    const batch = (node.properties.astrogate_generation_jobs || []).map(id => jobs.get(id)).filter(Boolean);
    if (!batch.length) return;
    const complete = batch.filter(j => j.status === "succeeded").length;
    const running = batch.filter(j => j.status === "running").length;
    const waiting = batch.filter(j => ["prepared", "queued"].includes(j.status)).length;
    const failed = batch.length - complete - running - waiting;
    showProgress(node, `已完成 ${complete}/${batch.length} 张 · 生成中 ${running} · 等待 ${waiting}${failed ? ` · 未完成/异常 ${failed}` : ""}`, complete, batch.length);
}
function message(node, text, busy = false, error = false) {
    const changed = node.properties.astrogate_status !== text || node.agBusy !== busy || node.agError !== error;
    node.properties.astrogate_status = text;
    node.agBusy = busy;
    node.agError = error;
    if (node.agStatus) {
        node.agStatus.textContent = text;
        node.agStatus.classList.toggle("busy", busy);
        node.agStatus.classList.toggle("error", error);
        if (node.type === "AstroGateStoryboardFrame") node.agStatus.hidden = !error;
    }
    if (node.type === "AstroGateStoryboard") {
        const preview = node.widgets?.find(w => w.name === "$$node-text-preview");
        if (preview) preview.hidden = true;
    }
    if (changed) node.setDirtyCanvas(true, true);
}

const stageNames = {assets: "提取人物、场景与道具", shots: "编写分镜与提示词"};
function logText(text) {
    return String(text || "").replace(/sk-[A-Za-z0-9_-]{12,}/g, "[密钥已隐藏]").replace(/Bearer\s+\S+/gi, "Bearer [已隐藏]");
}
function activityStatus(job) {
    if (job.status === "cancelling") return "正在停止，等待当前请求结束";
    if (job.status === "analysing") return stageNames[job.stage] || "分析剧本";
    if (job.status === "unknown") return "远端结果未知";
    return states[job.status] || job.status;
}
function activityLines(job) {
    const events = job.events?.length ? [...job.events] : [{time: job.created_at, message: "任务已建立"},
        ...(job.stage ? [{time: job.stage_started_at, stage: job.stage}] : [])];
    if (events.findLast(e => e.status)?.status !== job.status) events.push({time: job.updated_at || job.created_at, status: job.status, error: job.error});
    return events.map(event => {
        const timestamp = event.time ? new Date(event.time * 1000).toLocaleTimeString("zh-CN", {hour12: false}) : "--:--:--";
        const text = [event.message, event.stage && stageNames[event.stage], event.status && activityStatus({...job, status: event.status, stage: event.stage}), event.error].filter(Boolean).join(" · ");
        return `[${timestamp}] ${logText(text)}`;
    }).join("\n");
}
function createActivityPanel() {
    const root = element("details", undefined, "ag-activity");
    root.open = localStorage.getItem("astrogate.activity.open") === "true";
    const summary = element("summary");
    const indicator = element("span", undefined, "ag-activity-indicator");
    const title = element("span", "分镜日志 · 就绪");
    summary.append(indicator, title);
    const body = element("div", undefined, "ag-activity-body");
    const picker = element("select");
    picker.setAttribute("aria-label", "选择要查看的 AstroGate 任务");
    const info = element("div", "当前画布暂无分镜任务", "ag-activity-info");
    const logs = element("pre", "选择 TXT 文件并开始分析后，这里显示实时日志。", "ag-activity-log");
    logs.setAttribute("role", "log");
    logs.setAttribute("aria-live", "polite");
    const controls = element("div", undefined, "ag-activity-actions");
    const locate = element("button", "定位节点");
    const stop = element("button", "停止此任务");
    const copy = element("button", "复制日志");
    const collapse = element("button", "收起");
    const note = element("div", "每 3 秒更新 · 外部接口没有总进度百分比", "ag-activity-note");
    controls.append(locate, stop, copy, collapse);
    body.append(picker, info, logs, controls, note);
    root.append(summary, body);
    document.body.append(root);
    let entries = [], selected = "", manual = false, connectionError = "", actionError = "", optionsKey = "";
    const current = () => entries.find(entry => entry.job.id === selected);
    function render() {
        const busy = entries.filter(entry => active.has(entry.job.status));
        const entry = current();
        const headline = busy[0] || entry;
        const state = headline ? activityStatus(headline.job) : "就绪";
        const generating = entries.filter(entry => entry.job.kind === "image" && entry.job.status === "running").length;
        const waiting = entries.filter(entry => entry.job.kind === "image" && ["prepared", "queued"].includes(entry.job.status)).length;
        title.textContent = `分镜日志 · ${connectionError ? "状态连接中断" : generating ? `${generating} 张生成中${waiting ? ` · ${waiting} 张等待` : ""}` : state}`;
        summary.title = title.textContent;
        root.classList.toggle("busy", busy.length > 0 && !connectionError);
        root.classList.toggle("error", Boolean(connectionError || entry?.job.error));
        picker.disabled = !entries.length;
        locate.disabled = !entry;
        stop.disabled = !entry || !active.has(entry.job.status) || entry.job.status === "cancelling" || Boolean(connectionError);
        copy.disabled = !entry;
        collapse.disabled = false;
        stop.textContent = entry?.job.kind === "image" ? "停止此批次" : "停止此任务";
        stop.title = entry?.job.kind === "image" ? "停止同一队列任务内尚未完成的图片；已发出的远端请求可能继续，返回的结果仍会保存。" : "停止所选分镜分析；已完成草稿会保留。";
        if (!entry) {
            info.textContent = connectionError || "当前画布暂无分镜任务";
            logs.textContent = "选择 TXT 文件并开始分析后，这里显示实时日志。";
            return;
        }
        const {job, node} = entry;
        const start = active.has(job.status) ? job.stage_started_at || job.created_at : job.created_at;
        const elapsed = Math.max(0, Math.floor((active.has(job.status) ? Date.now() / 1000 : job.updated_at || job.created_at) - start));
        const transport = {connecting: "请求已发起，等待接口响应", waiting: "接口已响应，等待正文", receiving: "正在接收正文", received: "正文已接收"}[job.request_state];
        const details = [logText(node.title), `${activityStatus(job)} · ${elapsed} 秒`,
            job.model && `${job.model}${job.model === "gpt-5.6-terra" ? " / xhigh" : ""}`,
            active.has(job.status) && transport,
            job.received_chars > 0 && `已接收 ${job.received_chars.toLocaleString()} 字`,
            active.has(job.status) && job.last_activity_at && `接口最近响应：${Math.max(0, Math.floor(Date.now() / 1000 - job.last_activity_at))} 秒前`,
            connectionError || actionError].filter(Boolean);
        info.textContent = details.join("\n");
        const text = activityLines(job);
        if (logs.textContent !== text) {
            const atBottom = logs.scrollHeight - logs.scrollTop - logs.clientHeight < 30;
            logs.textContent = text;
            if (atBottom) logs.scrollTop = logs.scrollHeight;
        }
    }
    picker.addEventListener("change", () => { selected = picker.value; manual = true; actionError = ""; render(); });
    root.addEventListener("toggle", () => localStorage.setItem("astrogate.activity.open", String(root.open)));
    for (const event of ["pointerdown", "dblclick", "wheel"]) root.addEventListener(event, e => e.stopPropagation());
    for (const [control, action] of [
        [locate, () => { const entry = current(); if (entry) focusNode(entry.node); }],
        [stop, async () => { const entry = current(); if (entry) entry.job = await request(`/jobs/${entry.job.id}/cancel`, {}); }],
        [copy, () => navigator.clipboard.writeText(info.textContent + "\n\n" + logs.textContent)],
        [collapse, () => { root.open = false; }],
    ]) {
        control.type = "button";
        control.addEventListener("click", async () => {
            control.disabled = true;
            try { actionError = ""; await action(); }
            catch (error) { actionError = logText(error.message); }
            finally { render(); }
        });
    }
    return {update(next, error = "") {
        connectionError = logText(error);
        if (!error || next.length) entries = next.sort((a, b) => Number(active.has(b.job.status)) - Number(active.has(a.job.status)) || (b.job.updated_at || b.job.created_at) - (a.job.updated_at || a.job.created_at));
        if (!manual || !current()) { selected = entries[0]?.job.id || ""; manual = false; }
        const key = entries.map(e => `${e.job.id}:${e.node.title}:${e.job.status}`).join("|");
        if (optionsKey !== key) {
            picker.replaceChildren(...entries.map(({node, job}) => { const option = element("option", logText(`${node.title} · ${states[job.status] || job.status}`)); option.value = job.id; return option; }));
            optionsKey = key;
        }
        picker.value = selected;
        render();
    }};
}
function button(node, title, action, className = "") {
    const el = element("button", title, className);
    el.type = "button";
    el.addEventListener("click", async () => {
        el.disabled = true;
        try { await action(); } catch (error) { message(node, error.message, false, true); }
        finally { el.disabled = false; }
    });
    return el;
}
function panelWidget(node, root, height) {
    // This DOM widget belongs to the real LGraphNode and follows its position/zoom.
    const widget = node.addDOMWidget("astrogate_card", "astrogate_card", root, {
        serialize: false, hideOnZoom: false, getMinHeight: height, getHeight: height,
    });
    widget.options.serialize = false;
    root.addEventListener("pointerdown", e => {
        if (e.button === 1) {
            e.preventDefault();
            e.stopPropagation();
            app.canvas.canvas.dispatchEvent(new PointerEvent(e.type, e));
        } else if (e.target.closest("button,input,textarea,select,summary,a")) e.stopPropagation();
    });
    root.addEventListener("auxclick", e => { if (e.button === 1) e.preventDefault(); });
    for (const event of ["keydown", "wheel"]) root.addEventListener(event, e => {
        if (e.target.closest("button,input,textarea,select,summary,a")) e.stopPropagation();
    });
    node.agStatus = element("div", "就绪", "ag-native-status");
    return widget;
}
function focusNode(node) {
    app.canvas.ds.scale = 0.85;
    app.canvas.centerOnNode(node);
    app.canvas.selectNode(node);
    app.graph.setDirtyCanvas(true, true);
}
function addNode(type, pos) {
    const node = LiteGraph.createNode(type);
    if (!node) throw new Error(`节点 ${type} 尚未注册，请重启服务并刷新页面。`);
    node.pos = [...pos];
    app.graph.add(node);
    return node;
}
function connect(source, output, target, name, type) {
    let slot = target.findInputSlot(name);
    if (slot < 0) {
        target.addInput(name, type, {shape: 7});
        slot = target.findInputSlot(name);
    }
    source.connect(output, target, slot);
}
function sourceNode(node, name) {
    const input = node.inputs?.find(i => i.name === name);
    const link = input?.link != null ? app.graph.links[input.link] : null;
    return link ? app.graph.getNodeById(link.origin_id) : null;
}
function imageSources(node) {
    return (node.inputs || []).filter(i => i.name.startsWith("reference_images.") && i.link != null)
        .sort((a, b) => Number(a.name.split("_").at(-1)) - Number(b.name.split("_").at(-1)))
        .map(i => ({slot: i.name, node: sourceNode(node, i.name)}));
}
function labelReferences(node) {
    for (const input of node.inputs || []) {
        if (!input.name.startsWith("reference_images.")) continue;
        const source = sourceNode(node, input.name);
        input.label = `参考 ${input.name.split("_").at(-1)}${source ? " · " + source.title : ""}`;
    }
    node.agRefreshReferences?.();
    node.setDirtyCanvas(true, true);
}
function restorePreview(node) {
    const images = node.properties.astrogate_images;
    if (images?.length) {
        app.nodeOutputs[String(node.id)] = {images};
        if (node.agImage) {
            const url = api.apiURL("/view?" + new URLSearchParams(images[0]));
            if (node.agImage.getAttribute("src") !== url) node.agImage.src = url;
            node.agImage.hidden = false;
            node.agEmpty.hidden = true;
        }
    }
}
function hideNativePreviews(node) {
    let changed = false;
    for (const widget of node.widgets || []) {
        if (["$$canvas-image-preview", "$$node-text-preview"].includes(widget.name) && !widget.hidden) {
            widget.hidden = true;
            changed = true;
        }
    }
    return changed;
}
function installFold(nodeType) {
    function apply(node) {
        for (const w of node.widgets || []) if (["prompt", "consistency"].includes(w.name)) w.hidden = Boolean(node.properties.astrogate_prompt_folded);
        node.setDirtyCanvas(true, true);
    }
    const created = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function () {
        const result = created?.apply(this, arguments);
        this.addWidget("button", "提示词展开/收起", "", () => {
            this.properties.astrogate_prompt_folded = !this.properties.astrogate_prompt_folded;
            apply(this);
        }, {serialize: false});
        apply(this);
        return result;
    };
    const configured = nodeType.prototype.onConfigure;
    nodeType.prototype.onConfigure = function () {
        const result = configured?.apply(this, arguments);
        apply(this);
        queueMicrotask(() => restorePreview(this));
        return result;
    };
    const executed = nodeType.prototype.onExecuted;
    nodeType.prototype.onExecuted = function (output) {
        const result = executed?.apply(this, arguments);
        if (output?.images?.length) this.properties.astrogate_images = output.images;
        return result;
    };
}
function mountGraph(board, recipe, relayout = false) {
    const mapping = new Map([["$board", board]]);
    const created = new Set();
    const upgrade = new Set();
    for (const item of recipe.nodes) {
        let node = nodes().find(n => n.properties?.astrogate_key === item.id && n.properties?.astrogate_project === recipe.project_id);
        if (!node) {
            node = addNode(item.type, [board.pos[0] + item.pos[0], board.pos[1] + item.pos[1]]);
            Object.assign(node.properties, item.properties);
            node.title = item.title;
            node.color = item.color;
            for (const [key, input] of Object.entries(item.widgets)) setValue(node, key, input);
            node.properties.astrogate_base_settings = [value(node, "aspect_ratio"), value(node, "resolution"), value(node, "quality")];
            created.add(node);
        } else if (relayout) {
            node.pos = [board.pos[0] + item.pos[0], board.pos[1] + item.pos[1]];
            if (item.properties.astrogate_layout_version && node.properties.astrogate_layout_version !== item.properties.astrogate_layout_version) {
                const editable = value(node, "prompt") === node.properties.astrogate_base_prompt;
                if (item.properties.astrogate_kind === "character" && !node.properties.astrogate_view) {
                    if (editable) { setValue(node, "prompt", item.widgets.prompt); node.properties.astrogate_base_prompt = item.widgets.prompt; }
                    setValue(node, "job_id", item.widgets.job_id);
                }
                for (const key of ["astrogate_view", "astrogate_layout_version", "astrogate_reference_purpose", "astrogate_prompt_folded"]) node.properties[key] = item.properties[key];
                node.title = item.title;
                if (editable && !value(node, "job_id")) { setValue(node, "prompt", item.widgets.prompt); node.properties.astrogate_base_prompt = item.widgets.prompt; }
                if (item.properties.astrogate_kind === "character") setValue(node, "aspect_ratio", "2:3");
                upgrade.add(node);
            }
        }
        node.properties.astrogate_chapter = recipe.chapter_id;
        node.agRefresh?.();
        restorePreview(node);
        mapping.set(item.id, node);
    }
    for (const link of recipe.links) {
        const target = mapping.get(link.to);
        // Restoring or arranging a chapter preserves manually edited wires and prompts.
        if (created.has(target) || upgrade.has(target)) connect(mapping.get(link.from), link.slot, target, link.input, link.type);
    }
    for (const node of created) {
        labelReferences(node);
        node.properties.astrogate_signature = nodeSignature(node);
        node.setSize?.([430, node.computeSize?.()[1] || 400]);
    }
    app.graph.setDirtyCanvas(true, true);
    return mapping;
}
async function saveBoard(board) {
    const info = board.properties.astrogate_board;
    if (info) await request(`/projects/${info.project_id}/edit`, {action: "workflow", chapter_id: info.chapter_id, workflow: app.graph.serialize()});
}
async function addSceneAngle(source, view) {
    const key = source.properties.astrogate_key + "::" + view;
    const existing = nodes().find(n => n.properties.astrogate_project === source.properties.astrogate_project && n.properties.astrogate_key === key);
    if (existing) { focusNode(existing); return existing; }
    const index = Object.keys(sceneAngles).indexOf(view);
    const pos = [source.pos[0] + source.size[0] + 70, source.pos[1] + index * 440];
    let overlap;
    while ((overlap = nodes().find(n => n.pos && n.size && pos[0] < n.pos[0] + n.size[0] + 30 && pos[0] + 460 > n.pos[0] && pos[1] < n.pos[1] + n.size[1] + 30 && pos[1] + 430 > n.pos[1]))) pos[1] = overlap.pos[1] + overlap.size[1] + 60;
    const node = addNode("AstroGateStoryboardFrame", pos);
    node.properties = {...source.properties, astrogate_key: key, astrogate_view: view, astrogate_images: [],
        astrogate_prompt_folded: true, astrogate_references_folded: true,
        astrogate_reference_purpose: `${source.title} · ${sceneAngles[view]}：同一无人场景的空间和光照参考`};
    delete node.properties.astrogate_signature;
    delete node.properties.astrogate_status;
    node.title = `${source.title} · ${sceneAngles[view]}`;
    const prompt = `${value(source, "prompt")}\n根据参考图改为${sceneAngles[view]}，仅改变摄影机位置和朝向，保留建筑结构、家具位置与光照关系；独立单幅，不拼贴。\n${sceneOnly}`;
    for (const name of ["element_id", "aspect_ratio", "resolution", "quality"]) setValue(node, name, value(source, name));
    setValue(node, "prompt", prompt);
    setValue(node, "job_id", "");
    node.properties.astrogate_base_prompt = prompt;
    connect(source, 0, node, "reference_images.reference_1", "IMAGE");
    const board = sourceNode(source, "board");
    if (board) connect(board, 0, node, "board", "ASTROGATE_BOARD");
    node.agRefresh?.();
    labelReferences(node);
    if (board) await saveBoard(board);
    focusNode(node);
    return node;
}
async function buildBoard(board, info, relayout = false) {
    if (board.agBuilding) return board.agBuilding;
    board.agBuilding = (async () => {
        board.properties.astrogate_board = info;
        await applyFrameAspect(board);
        const recipe = await request(`/projects/${info.project_id}/native/canvas`, info);
        const mapping = mountGraph(board, recipe, relayout || board.properties.astrogate_layout_version !== 2);
        board.properties.astrogate_layout_version = 2;
        const frames = [...mapping.values()].filter(n => n.properties.astrogate_kind === "shot");
        const auto = frames.length && board.properties.astrogate_autogenerate !== false && board.properties.astrogate_auto_started !== info.analysis_id;
        if (auto) board.properties.astrogate_auto_started = info.analysis_id;
        message(board, `已建立 ${frames.length} 个分镜与人物双视图。${auto ? "正在自动生成图片…" : "可生成本章或选中图片。"}`);
        await saveBoard(board);
        if (frames.length) focusNode([...mapping.values()].find(n => n.properties.astrogate_view === "front") || frames[0]);
        if (auto) await generate(frames, false, board);
    })();
    try { return await board.agBuilding; }
    finally { delete board.agBuilding; }
}
async function applyFrameAspect(board) {
    let ratio = String(value(board, "frame_aspect_ratio") || "16:9").trim().replace("：", ":");
    const parts = ratio.split(":").map(Number);
    if (parts.length !== 2 || parts.some(v => !Number.isFinite(v) || v <= 0)) throw new Error("画幅请输入宽:高，例如 16:9 或 2.39:1。");
    ratio = parts.join(":");
    setValue(board, "frame_aspect_ratio", ratio);
    const project = value(sourceNode(board, "script") || {}, "project_id") || board.properties.astrogate_board?.project_id;
    if (project) await request(`/projects/${project}/edit`, {action: "frame_aspect_ratio", aspect_ratio: ratio});
    for (const node of nodes().filter(n => n.type === "AstroGateStoryboardFrame" && sourceNode(n, "board") === board && n.properties.astrogate_kind !== "character")) {
        setValue(node, "aspect_ratio", ratio);
        node.agRefresh?.();
    }
}
async function queueNodes(targets, jobs = [], graph = null) {
    graph ||= await app.graphToPrompt();
    const promptId = crypto.randomUUID();
    for (const job of jobs) await request(`/jobs/${job.id}/queue`, {prompt_id: promptId});
    let response;
    try {
        response = await api.fetchApi("/prompt", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({
            prompt: graph.output, prompt_id: promptId, client_id: api.clientId,
            partial_execution_targets: targets.map(n => String(n.id)), extra_data: {extra_pnginfo: {workflow: graph.workflow}},
        })});
    } catch {
        throw new Error("队列提交结果未知，请查看 ComfyUI 队列，不要重复提交。");
    }
    if (!response.ok) {
        for (const job of jobs) if (job.status !== "succeeded") await request(`/jobs/${job.id}/cancel`, {});
        const result = await response.json();
        throw new Error(result.error?.message || "工作流未通过检查，请查看 ComfyUI 的错误提示。");
    }
    return promptId;
}
function nodeSignature(node) {
    return JSON.stringify([value(node, "prompt"), value(node, "aspect_ratio"), value(node, "resolution"), value(node, "quality"),
        imageSources(node).map(r => [r.slot, r.node.id, value(r.node, "job_id"), r.node.type === "AstroGateStoryboardFrame" ? null : r.node.widgets?.filter(w => w.options?.serialize !== false).map(w => w.value)]), sourceNode(node, "story_context")?.properties.astrogate_owner]);
}
function changed(node, seen = new Set()) {
    if (seen.has(node.id)) throw new Error("图片参考存在循环，请断开其中一条 IMAGE 连线。");
    seen.add(node.id);
    const p = node.properties;
    const result = value(node, "prompt") !== p.astrogate_base_prompt ||
        (p.astrogate_base_settings && JSON.stringify(p.astrogate_base_settings) !== JSON.stringify([value(node, "aspect_ratio"), value(node, "resolution"), value(node, "quality")])) ||
        (p.astrogate_signature && p.astrogate_signature !== nodeSignature(node)) ||
        imageSources(node).some(r => r.node.type === "AstroGateStoryboardFrame" && changed(r.node, seen));
    seen.delete(node.id);
    return Boolean(result);
}
let submitting = false;
async function generate(targets, force = false, reporter = targets[0]) {
    targets = targets.filter(n => n.type === "AstroGateStoryboardFrame");
    if (!targets.length) throw new Error("请先选中分镜图片卡片。");
    if (submitting) throw new Error("正在准备本次请求，请稍候。");
    submitting = true;
    const prepared = [], visited = new Set(), visiting = new Set();
    let submitted = false;
    async function visit(node, requested) {
        if (visited.has(node.id)) return;
        if (visiting.has(node.id)) throw new Error("图片参考存在循环，请断开其中一条 IMAGE 连线。");
        visiting.add(node.id);
        const project = node.properties.astrogate_project;
        if (!project) throw new Error("请从 TXT → AI 分镜脚本建立图片卡片；自由生图使用 AstroGate AI 生成图像节点。");
        const previous = value(node, "job_id") ? await request(`/jobs/${value(node, "job_id")}`) : null;
        const unchanged = !changed(node);
        if (previous?.status === "succeeded" && unchanged && !(requested && force)) {
            visited.add(node.id);
            visiting.delete(node.id);
            return;
        }
        if (previous && active.has(previous.status)) throw new Error(`${node.title} 已有任务，请等待或停止后再生成。`);
        for (const ref of imageSources(node)) if (ref.node.type === "AstroGateStoryboardFrame") await visit(ref.node, false);
        const refs = imageSources(node).map(ref => ({slot: ref.slot,
            purpose: ref.node.properties.astrogate_reference_purpose || `${ref.node.title}：参考画面与细节`,
            ...(ref.node.type === "AstroGateStoryboardFrame" ? {job_id: value(ref.node, "job_id")} : {source_signature: JSON.stringify(ref.node.widgets?.filter(w => w.options?.serialize !== false).map(w => w.value))}),
        }));
        message(node, `准备生成 · ${refs.length} 路图片参考`, true);
        const job = await request(`/projects/${project}/native/prepare`, {owner_id: node.properties.astrogate_owner,
            version_id: node.properties.astrogate_version, view: node.properties.astrogate_view,
            owner_type: node.properties.astrogate_owner_type || "shot", prompt: value(node, "prompt"),
            settings: {aspect_ratio: value(node, "aspect_ratio"), resolution: value(node, "resolution"), quality: value(node, "quality")},
            references: refs, story_parent_id: sourceNode(node, "story_context")?.properties.astrogate_owner || null, force: requested && force,
        });
        setValue(node, "job_id", job.id);
        node.properties.astrogate_base_prompt = value(node, "prompt");
        node.properties.astrogate_base_settings = [value(node, "aspect_ratio"), value(node, "resolution"), value(node, "quality")];
        node.properties.astrogate_signature = nodeSignature(node);
        prepared.push(job);
        visited.add(node.id);
        visiting.delete(node.id);
        message(node, states[job.status], active.has(job.status));
    }
    try {
        for (const board of new Set(targets.map(n => sourceNode(n, "board")).filter(Boolean))) await applyFrameAspect(board);
        for (const node of targets) await visit(node, true);
        const count = prepared.filter(j => j.status !== "succeeded").length;
        message(reporter, `本次 ${count} 张新图 · ${Math.max(0, visited.size - count)} 张复用 · 按 IMAGE 连线执行`, true);
        const graph = await app.graphToPrompt();
        submitted = true;
        await queueNodes(targets, prepared, graph);
        const batch = [...visited].map(id => value(app.graph.getNodeById(id), "job_id")).filter(Boolean);
        for (const board of new Set([reporter, ...targets.map(n => sourceNode(n, "board"))].filter(n => n?.type === "AstroGateStoryboard"))) board.properties.astrogate_generation_jobs = batch;
        for (const board of nodes().filter(n => n.type === "AstroGateStoryboard" && n.properties.astrogate_board)) await saveBoard(board);
    } catch (error) {
        if (!submitted) for (const job of prepared) if (job.status !== "succeeded") await request(`/jobs/${job.id}/cancel`, {});
        throw error;
    } finally { submitting = false; }
}
function pickTXT(node) {
    const input = element("input");
    input.type = "file";
    input.accept = ".txt,text/plain";
    input.addEventListener("change", async () => {
        if (!input.files[0]) return;
        message(node, "正在导入 TXT…", true);
        try {
            const form = new FormData();
            form.append("file", input.files[0]);
            const project = await request("/projects", form);
            setValue(node, "project_id", project.id);
            node.properties.astrogate_filename = input.files[0].name;
            node.properties.astrogate_chapters = project.chapters.map(c => c.title);
            node.title = "TXT · " + project.title;
            node.agRefresh?.();
            message(node, `已导入 ${project.chapters.length} 章。连接“剧本”输出到 AI 分镜脚本，然后点击分析。`);
        } catch (error) { message(node, error.message, false, true); }
    });
    input.click();
}
function starter() {
    const canvas = app.canvas;
    const pos = [-canvas.ds.offset[0] + 160 / canvas.ds.scale, -canvas.ds.offset[1] + 140 / canvas.ds.scale];
    const file = addNode("AstroGateTXT", pos);
    const board = addNode("AstroGateStoryboard", [pos[0] + 490, pos[1]]);
    connect(file, 0, board, "script", "ASTROGATE_SCRIPT");
    focusNode(file);
    pickTXT(file);
    return file;
}
function installSource(node) {
    node.widgets.find(w => w.name === "project_id").hidden = true;
    const root = element("div", undefined, "ag-native ag-source");
    panelWidget(node, root, () => 150);
    const info = element("div", "未选择 TXT 文件", "ag-file-info");
    root.append(info, button(node, "选择 TXT 文件", () => pickTXT(node), "primary"), node.agStatus);
    node.agRefresh = () => { info.textContent = [node.properties.astrogate_filename || "未选择 TXT 文件", ...(node.properties.astrogate_chapters || []).map((v, i) => `${i + 1}. ${v}`)].join("\n"); };
    node.size = [390, 240];
}
const scriptSources = new Map();
async function analysisContext(node) {
    const projectId = value(sourceNode(node, "script") || {}, "project_id");
    if (!projectId) throw new Error("请先选择 TXT 文件，并把“剧本”输出连到此节点。");
    if (!scriptSources.has(projectId)) {
        const {project} = await request(`/projects/${projectId}`);
        scriptSources.set(projectId, {hash: project.source.hash, chapters: project.chapters.map(c => c.id)});
    }
    const source = scriptSources.get(projectId);
    const chapterId = source.chapters[Number(value(node, "chapter")) - 1];
    if (!chapterId) throw new Error(`本剧本共 ${source.chapters.length} 章，请修改章节序号。`);
    const fields = [source.hash, chapterId, value(node, "text_model"), value(node, "style"), value(node, "density"), value(node, "analysis_revision")];
    // Match the persisted Python JSON digest, including Chinese text and spaces.
    const serialized = "[" + fields.map(v => JSON.stringify(v).replace(/[\u007f-\uffff]/g, c => "\\u" + c.charCodeAt(0).toString(16).padStart(4, "0"))).join(", ") + "]";
    const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(serialized));
    const key = Array.from(new Uint8Array(digest), b => b.toString(16).padStart(2, "0")).join("");
    return {projectId, chapterId, key};
}
function showAnalysisState(node, job) {
    const busy = job && active.has(job.status);
    const failed = job && ["failed", "unknown", "cancelled", "blocked"].includes(job.status);
    if (node.agAnalysisButton) node.agAnalysisButton.disabled = Boolean(busy);
    if (node.agRetryButton) {
        node.agRetryButton.hidden = !failed;
        node.agRetryButton.disabled = Boolean(busy);
        node.agRetryButton.textContent = job?.status === "unknown" ? "结果未知，重新提交" : "重试未完成分析";
    }
    if (!job) return;
    if (failed) message(node, `${job.error || states[job.status]}\n点击“${node.agRetryButton?.textContent || "重试未完成分析"}”手动继续；已完成的步骤会保留。`, false, true);
    else if (busy) {
        const stage = job.stage === "shots" ? "正在编写分镜与提示词" : job.stage === "assets" ? "正在提取人物、场景与道具" : "正在分析";
        const elapsed = job.stage_started_at ? ` · 已等待 ${Math.max(0, Math.floor(Date.now() / 1000 - job.stage_started_at))} 秒` : "";
        message(node, `${job.status === "cancelling" ? "正在停止" : stage} · ${job.completed_chunks || 0}/${job.total_chunks || "?"} 个正文块完成${elapsed}`, true);
        showProgress(node, `${stage}${elapsed}`);
    }
    else if (job.status === "succeeded") message(node, "分镜分析已完成，人物、场景与镜头节点已建立。");
}
async function startAnalysis(node, retry = false) {
    const context = await analysisContext(node);
    const {jobs} = await request(`/projects/${context.projectId}?summary=1`);
    const job = jobs.find(j => j.kind === "text" && j.analysis_key === context.key);
    if (job && active.has(job.status)) { showAnalysisState(node, job); return; }
    if (job && ["failed", "unknown", "cancelled", "blocked"].includes(job.status)) {
        if (!retry) { showAnalysisState(node, job); return; }
        if (["failed", "unknown"].includes(job.status)) {
            await request(`/jobs/${job.id}/resume`, {});
            node.properties.astrogate_resume_job = job.id;
            app.graph.change();
            showAnalysisState(node, {...job, status: "analysing", error: null});
            return;
        }
        setValue(node, "analysis_revision", Number(value(node, "analysis_revision")) + 1);
    }
    message(node, "正在分析剧本、提取素材、编写逐镜提示词…", true);
    node.properties.astrogate_prompt_id = await queueNodes([node]);
    node.properties.astrogate_prompt_key = (await analysisContext(node)).key;
}
function installBoard(node) {
    const root = element("div", undefined, "ag-native");
    panelWidget(node, root, () => 265);
    const ratio = node.widgets.find(w => w.name === "frame_aspect_ratio");
    if (ratio) {
        const previous = ratio.callback;
        ratio.callback = function () {
            previous?.apply(this, arguments);
            applyFrameAspect(node).then(() => saveBoard(node)).catch(error => message(node, error.message, false, true));
        };
    }
    const autoLabel = element("label", undefined, "ag-auto-generate");
    const auto = element("input");
    auto.type = "checkbox";
    auto.checked = node.properties.astrogate_autogenerate !== false;
    auto.addEventListener("change", () => { node.properties.astrogate_autogenerate = auto.checked; app.graph.change(); });
    autoLabel.append(auto, element("span", "建好节点后自动生成图片"));
    node.agRefresh = () => { auto.checked = node.properties.astrogate_autogenerate !== false; };
    const controls = element("div", undefined, "ag-native-actions");
    node.agAnalysisButton = button(node, "分析并自动建立节点", () => startAnalysis(node), "primary");
    node.agRetryButton = button(node, "重试未完成分析", () => startAnalysis(node, true));
    node.agRetryButton.hidden = true;
    controls.append(node.agAnalysisButton, node.agRetryButton, button(node, "恢复 / 整理节点排版", async () => {
        const info = node.properties.astrogate_board;
        if (!info) throw new Error("请先分析剧本。");
        await buildBoard(node, info, true);
    }), button(node, "生成本章图片", async () => {
        const info = node.properties.astrogate_board;
        if (!info) throw new Error("请先分析剧本。");
        await generate(nodes().filter(n => n.properties.astrogate_project === info.project_id && n.properties.astrogate_chapter === info.chapter_id && n.properties.astrogate_kind === "shot"), false, node);
    }), button(node, "停止分析", async () => {
        const context = await analysisContext(node);
        const promptId = node.properties.astrogate_prompt_key === context.key ? node.properties.astrogate_prompt_id : null;
        const result = await request(`/projects/${context.projectId}?summary=1`);
        for (const job of result.jobs.filter(j => j.kind === "text" && j.analysis_key === context.key && ["analysing", "cancelling"].includes(j.status))) await request(`/jobs/${job.id}/cancel`, {});
        if (promptId) {
            await api.fetchApi("/queue", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({delete: [promptId]})});
            await api.fetchApi("/interrupt", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({prompt_id: promptId})});
        }
        message(node, "已请求停止；已发出的文本请求可能仍会返回，完成的分析会保留。");
    }), button(node, "导出项目与图片", async () => {
        const info = node.properties.astrogate_board;
        if (!info) throw new Error("请先分析剧本。");
        await saveBoard(node);
        const a = element("a");
        a.href = api.apiURL(`${base}/projects/${info.project_id}/export`);
        a.download = "astrogate-storyboard.zip";
        a.click();
    }));
    root.append(autoLabel, controls, progressBar(node), element("p", "人物正视 → 左视 → 实际出场分镜。人物设计 2:3，其余使用上方统一画幅；图片 high / 4K。", "ag-hint"), node.agStatus);
    node.size = [430, 650];
}
function installFrame(node) {
    node.properties.astrogate_prompt_folded ??= true;
    node.properties.astrogate_references_folded ??= true;
    for (const w of node.widgets) if (["prompt", "job_id", "element_id", "aspect_ratio", "resolution", "quality"].includes(w.name)) w.hidden = true;
    node.hideOutputImages = true;
    const root = element("div", undefined, "ag-native ag-image-card");
    const content = element("div");
    root.append(content);
    const cardHeight = () => (content.offsetHeight || (node.properties.astrogate_prompt_folded ? 335 : 500)) + 20;
    const computeSize = node.computeSize;
    node.computeSize = function () {
        hideNativePreviews(this);
        const size = computeSize.apply(this, arguments);
        if (this.properties.astrogate_references_folded) size[1] = cardHeight() + 60;
        return size;
    };
    const resizeCard = () => {
        const height = node.computeSize()[1];
        if (node.size[1] !== height) node.setSize([node.size[0], height]);
        node.setDirtyCanvas(true, true);
    };
    const observer = new ResizeObserver(resizeCard);
    observer.observe(content);
    const removed = node.onRemoved;
    node.onRemoved = function () {
        observer.disconnect();
        return removed?.apply(this, arguments);
    };
    const drawBackground = node.onDrawBackground;
    node.onDrawBackground = function () {
        const result = drawBackground?.apply(this, arguments);
        if (hideNativePreviews(this)) resizeCard();
        return result;
    };
    const referenceToggle = element("button", "", "ag-reference-toggle");
    referenceToggle.type = "button";
    referenceToggle.title = "展开后可查看、增删参考连线；收起保留所有连接";
    node.agRefreshReferences = () => {
        const folded = node.properties.astrogate_references_folded;
        const count = (node.inputs || []).filter(input => input.name.startsWith("reference_images.") && input.link != null).length;
        referenceToggle.textContent = `${folded ? "▸" : "▾"} 参考图 ${count} · ${folded ? "展开连接" : "收起连接"}`;
        for (const input of node.inputs || []) {
            if (folded) input.pos = [10, 14]; else delete input.pos;
        }
        if (folded) node.widgets_start_y = 44; else delete node.widgets_start_y;
        resizeCard();
    };
    const drawSlots = node.drawSlots;
    node.drawSlots = function (ctx, options) {
        if (!this.properties.astrogate_references_folded) return drawSlots?.call(this, ctx, options);
        ctx.save();
        ctx.globalAlpha = options.editorAlpha ?? 1;
        ctx.font = this.innerFontStyle;
        ctx.textAlign = "right";
        for (const [index, output] of (this.outputs || []).entries()) {
            const pos = this.getOutputPos(index);
            const x = pos[0] - this.pos[0], y = pos[1] - this.pos[1];
            ctx.fillStyle = output.type === "IMAGE" ? "#68ccff" : "#a39ab6";
            ctx.beginPath(); ctx.arc(x, y, 4, 0, Math.PI * 2); ctx.fill();
            ctx.fillStyle = "#cbd2d9";
            ctx.fillText(output.label || output.localized_name || output.name, x - 8, y + 4);
        }
        ctx.fillStyle = "#68ccff";
        ctx.beginPath(); ctx.arc(10, 14, 4, 0, Math.PI * 2); ctx.fill();
        ctx.restore();
    };
    const getSlotInPosition = node.getSlotInPosition;
    node.getSlotInPosition = function () {
        const slot = getSlotInPosition?.apply(this, arguments);
        return this.properties.astrogate_references_folded && slot?.input ? null : slot;
    };
    referenceToggle.addEventListener("click", () => {
        node.properties.astrogate_references_folded = !node.properties.astrogate_references_folded;
        node.agRefreshReferences(); app.graph.change();
    });
    const stage = element("div", undefined, "ag-image-stage");
    const image = element("img");
    image.alt = "当前图片结果";
    image.hidden = true;
    const empty = element("div", "等待生成图像", "ag-image-empty");
    stage.append(image, empty);
    node.agImage = image;
    node.agEmpty = empty;
    const detail = element("details");
    detail.open = !node.properties.astrogate_prompt_folded;
    const summary = element("summary", "提示词与生成参数");
    const prompt = element("textarea");
    prompt.placeholder = "描述这一张图的主体、动作、景别和光线…";
    prompt.value = value(node, "prompt") || "";
    prompt.addEventListener("input", () => { setValue(node, "prompt", prompt.value); app.graph.change(); });
    const parameters = element("div", undefined, "ag-parameters");
    const selects = [];
    for (const [name, label] of [["aspect_ratio", "画幅"], ["resolution", "分辨率"], ["quality", "质量"]]) {
        const w = node.widgets.find(w => w.name === name);
        const select = element(name === "aspect_ratio" ? "input" : "select");
        if (name === "aspect_ratio") select.placeholder = "宽:高，例如 2:3";
        else for (const option of w.options.values) { const item = element("option", option); item.value = option; select.append(item); }
        select.value = w.value;
        select.addEventListener("change", () => { w.value = select.value; app.graph.change(); });
        selects.push([select, w]);
        const field = element("label", label);
        field.append(select);
        parameters.append(field);
    }
    detail.append(summary, prompt, parameters);
    detail.addEventListener("toggle", () => {
        node.properties.astrogate_prompt_folded = !detail.open;
        resizeCard();
    });
    panelWidget(node, root, cardHeight);
    node.agStatus.hidden = true;
    const controls = element("div", undefined, "ag-native-actions");
    controls.append(button(node, "生成图像", () => generate([node]), "primary"), button(node, "重绘", () => generate([node], true)),
        button(node, "停止", async () => {
            const id = value(node, "job_id");
            if (id) await request(`/jobs/${id}/cancel`, {});
        }));
    const approve = button(node, "设为定稿素材", async () => {
        await request(`/projects/${node.properties.astrogate_project}/edit`, {action: "approve", job_id: value(node, "job_id")});
        message(node, "素材已定稿，其他章节可以复用。");
    });
    controls.append(approve);
    const addAngle = element("button", "添加角度");
    addAngle.type = "button";
    addAngle.addEventListener("click", event => new LiteGraph.ContextMenu(Object.entries(sceneAngles).map(([view, label]) => ({
        content: label, callback: () => addSceneAngle(node, view).catch(error => message(node, error.message, false, true)),
    })), {event}));
    controls.append(addAngle);
    content.append(referenceToggle, stage, controls, progressBar(node), detail, node.agStatus);
    node.agRefresh = () => {
        if (!node.properties.astrogate_compact_card) {
            node.properties.astrogate_prompt_folded = true;
            node.properties.astrogate_compact_card = true;
        }
        const scene = ["scene", "scene_state"].includes(node.properties.astrogate_kind);
        if (scene && !String(value(node, "prompt")).includes(sceneOnly)) setValue(node, "prompt", `${value(node, "prompt") || ""}\n${sceneOnly}`);
        prompt.value = value(node, "prompt") || "";
        for (const [select, w] of selects) select.value = w.value;
        detail.open = !node.properties.astrogate_prompt_folded;
        const ratio = selects.find(([, w]) => w.name === "aspect_ratio")[0];
        ratio.disabled = node.properties.astrogate_kind !== "character" && Boolean(sourceNode(node, "board"));
        ratio.title = ratio.disabled ? "在所连的 AI 分镜脚本节点统一设置画幅" : "人物设计图独立画幅";
        approve.hidden = node.properties.astrogate_owner_type !== "asset" || node.properties.astrogate_view?.startsWith("scene_");
        addAngle.hidden = !scene || node.properties.astrogate_view?.startsWith("scene_");
        node.agRefreshReferences();
        restorePreview(node);
    };
    node.size = [430, 400];
    node.agRefresh();
}
async function poll() {
    const activityEntries = [];
    let activityError = "";
    const frames = nodes().filter(n => n.type === "AstroGateStoryboardFrame" && value(n, "job_id"));
    const boards = nodes().filter(n => n.type === "AstroGateStoryboard");
    const projects = new Set([...frames.map(n => n.properties.astrogate_project), ...boards.map(n => value(sourceNode(n, "script") || {}, "project_id"))].filter(Boolean));
    for (const project of projects) {
        try {
            const result = await request(`/projects/${project}?summary=1`);
            const jobs = new Map(result.jobs.map(j => [j.id, j]));
            for (const node of frames.filter(n => n.properties.astrogate_project === project)) {
                const job = jobs.get(value(node, "job_id"));
                if (!job) continue;
                activityEntries.push({node, job});
                message(node, (states[job.status] || job.status) + (job.error ? ` · ${job.error}` : ""), active.has(job.status), ["failed", "unknown", "blocked"].includes(job.status));
                showProgress(node, states[job.status] || job.status, job.status === "succeeded" ? 1 : active.has(job.status) ? null : 0);
                if (job.saved_result) { node.properties.astrogate_images = [job.saved_result]; restorePreview(node); }
            }
            for (const node of boards.filter(n => value(sourceNode(n, "script") || {}, "project_id") === project)) {
                const context = await analysisContext(node);
                const job = result.jobs.find(j => j.kind === "text" && j.analysis_key === context.key);
                if (job) activityEntries.push({node, job});
                if (node.agAnalysisKey !== context.key) {
                    node.agAnalysisKey = context.key;
                    if (!job) message(node, "就绪，点击“分析并自动建立节点”开始。");
                }
                showAnalysisState(node, job);
                batchProgress(node, jobs);
                if (job?.status === "succeeded" && (node.properties.astrogate_board?.analysis_id !== job.id || node.properties.astrogate_layout_version !== 2)) {
                    delete node.properties.astrogate_resume_job;
                    await buildBoard(node, {project_id: project, chapter_id: job.chapter_id, analysis_id: job.id});
                }
            }
        } catch (error) {
            activityError = `状态读取失败：${error.message}`;
            for (const node of [...frames.filter(n => n.properties.astrogate_project === project), ...boards.filter(n => value(sourceNode(n, "script") || {}, "project_id") === project)]) message(node, `状态读取失败：${error.message}`, false, true);
        }
    }
    activityPanel?.update(activityEntries, activityError);
}
app.registerExtension({
    name: "AstroGate.Storyboard",
    beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name === "AstroGateImage") { installFold(nodeType); return; }
        if (nodeData.name === "AstroGateStoryboardFrame" && !nodeData.input?.optional?.element_id) {
            installFold(nodeType);
            return;
        }
        if (!["AstroGateTXT", "AstroGateStoryboard", "AstroGateStoryboardFrame"].includes(nodeData.name)) return;
        const created = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            const result = created?.apply(this, arguments);
            ({AstroGateTXT: installSource, AstroGateStoryboard: installBoard, AstroGateStoryboardFrame: installFrame})[nodeData.name](this);
            return result;
        };
        const configured = nodeType.prototype.onConfigure;
        nodeType.prototype.onConfigure = function () {
            const result = configured?.apply(this, arguments);
            queueMicrotask(() => {
                this.agRefresh?.();
                if (nodeData.name === "AstroGateStoryboardFrame") labelReferences(this);
                restorePreview(this);
                if (this.properties.astrogate_status) message(this, this.properties.astrogate_status);
            });
            return result;
        };
        const executed = nodeType.prototype.onExecuted;
        nodeType.prototype.onExecuted = function (output) {
            const result = executed?.apply(this, arguments);
            if (output?.images?.length) { this.properties.astrogate_images = output.images; restorePreview(this); message(this, "图像已保存"); }
            if (output?.astrogate_board?.[0]) buildBoard(this, output.astrogate_board[0]).catch(error => message(this, error.message, false, true));
            return result;
        };
        if (nodeData.name === "AstroGateStoryboardFrame") {
            const connected = nodeType.prototype.onConnectionsChange;
            nodeType.prototype.onConnectionsChange = function () {
                const result = connected?.apply(this, arguments);
                queueMicrotask(() => labelReferences(this));
                return result;
            };
        }
    },
    getCanvasMenuItems() {
        return [{content: "导入 TXT → AI 自动分镜", callback: starter},
            {content: "生成选中的分镜图片", callback: () => {
                const selected = Object.values(app.canvas.selected_nodes || {});
                generate(selected).catch(error => { if (selected[0]) message(selected[0], error.message, false, true); });
            }}];
    },
    setup() {
        const style = element("link");
        style.rel = "stylesheet";
        style.href = new URL("./storyboard.css", import.meta.url).href;
        document.head.append(style);
        activityPanel = createActivityPanel();
        const bar = element("div", undefined, "ag-native-toolbar");
        const importButton = element("button", "导入 TXT → AI 分镜");
        importButton.type = "button";
        importButton.addEventListener("click", () => {
            try { starter(); } catch (error) { importButton.textContent = error.message; }
        });
        const selectedButton = element("button", "生成选中图片");
        selectedButton.type = "button";
        selectedButton.addEventListener("click", () => {
            const selected = Object.values(app.canvas.selected_nodes || {}).filter(n => n.type === "AstroGateStoryboardFrame");
            if (!selected.length) { selectedButton.textContent = "请先选中图片卡片"; setTimeout(() => selectedButton.textContent = "生成选中图片", 2500); return; }
            generate(selected).catch(error => message(selected[0], error.message, false, true));
        });
        bar.append(importButton, selectedButton);
        document.body.append(bar);
        api.addEventListener("execution_error", event => {
            const node = app.graph.getNodeById(event.detail.node_id);
            if (node?.type?.startsWith("AstroGate")) message(node, event.detail.exception_message || "执行失败，请查看任务信息", false, true);
        });
        api.addEventListener("progress_text", event => {
            const node = app.graph.getNodeById(event.detail.nodeId);
            if (node?.type === "AstroGateStoryboard") queueMicrotask(() => {
                const preview = node.widgets?.find(w => w.name === "$$node-text-preview");
                if (preview) preview.hidden = true;
            });
        });
        async function tick() { try { await poll(); } finally { setTimeout(tick, 3000); } }
        setTimeout(tick, 3000);
    },
});
