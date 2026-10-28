import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import vm from "node:vm";
import test from "node:test";
import { webcrypto } from "node:crypto";

const source = await readFile(new URL("../web/storyboard.js", import.meta.url), "utf8");
async function load(app, extras = {}, api = {}) {
    const context = vm.createContext({queueMicrotask, structuredClone, console, JSON, Set, Map, URLSearchParams, TextEncoder, crypto: webcrypto, ...extras});
    const module = new vm.SourceTextModule(source + "\nexport { installFold, mountGraph, changed, generate, installFrame, analysisContext, startAnalysis, showAnalysisState, poll, panelWidget };", {context});
    await module.link(async specifier => {
        const name = specifier.includes("app.js") ? "app" : "api";
        return new vm.SyntheticModule([name], function () { this.setExport(name, name === "app" ? app : api); }, {context});
    });
    await module.evaluate();
    return module.namespace;
}
function graphMock() {
    let id = 0, linkId = 0;
    const graph = {_nodes: [], links: {}, add(node) { node.id = ++id; this._nodes.push(node); }, setDirtyCanvas() {}, getNodeById(id) { return this._nodes.find(n => n.id == id); }};
    const LiteGraph = {createNode(type) {
        return {type, properties: {}, widgets: ["prompt", "job_id", "element_id", "aspect_ratio", "resolution", "quality"].map(name => ({name, value: ""})), inputs: [],
            findInputSlot(name) { return this.inputs.findIndex(i => i.name === name); },
            addInput(name, type) { this.inputs.push({name, type}); }, setDirtyCanvas() {},
            connect(output, target, slot) { const id = ++linkId; graph.links[id] = {origin_id: this.id, origin_slot: output, target_id: target.id}; target.inputs[slot].link = id; },
        };
    }};
    return {graph, LiteGraph};
}
test("legacy folding preserves values, references and output previews", async () => {
    const app = {registerExtension() {}, nodeOutputs: {}};
    const {installFold} = await load(app);
    class Node {
        constructor() {
            this.id = 12; this.properties = {}; this.flags = {};
            this.widgets = [{name: "prompt", value: "两个人"}, {name: "consistency", value: "蓝衣与黑衣"}];
            this.inputs = [{name: "reference_images.reference_1", link: 77}];
        }
        addWidget(type, name, value, callback, options) { const w = {type, name, value, callback, options}; this.widgets.push(w); return w; }
        setDirtyCanvas() {}
    }
    installFold(Node);
    const node = new Node(); node.onNodeCreated(); node.widgets.at(-1).callback();
    assert.equal(node.widgets[0].hidden, true);
    assert.equal(node.widgets[0].value, "两个人");
    assert.equal(node.inputs[0].link, 77);
    assert.equal(node.flags.collapsed, undefined);
    node.onExecuted({images: [{filename: "frame.png", type: "output", subfolder: "AstroGate"}]});
    node.onConfigure();
    await new Promise(resolve => queueMicrotask(resolve));
    assert.equal(app.nodeOutputs[12].images[0].filename, "frame.png");
    node.widgets.at(-1).callback(); assert.equal(node.widgets[0].hidden, false);
});
test("native board creates real image/story wires, shares assets and preserves manual edits on restore", async () => {
    const {graph, LiteGraph} = graphMock();
    const board = LiteGraph.createNode("AstroGateStoryboard"); board.pos = [100, 100]; graph.add(board);
    const {mountGraph, changed} = await load({registerExtension() {}, graph, nodeOutputs: {}}, {LiteGraph});
    const recipe = {project_id: "p", chapter_id: "c", nodes: ["asset", "shot1", "shot2"].map((id, i) => ({id, type: "AstroGateStoryboardFrame", title: id, pos: [i * 500, 100], widgets: {prompt: id, job_id: "j-" + id, aspect_ratio: "16:9", resolution: "4K", quality: "high"}, properties: {astrogate_project: "p", astrogate_key: id, astrogate_owner: id, astrogate_base_prompt: id}})), links: [
        {from: "$board", slot: 0, to: "shot1", input: "board", type: "ASTROGATE_BOARD"},
        {from: "asset", slot: 0, to: "shot1", input: "reference_images.reference_1", type: "IMAGE"},
        {from: "asset", slot: 0, to: "shot2", input: "reference_images.reference_1", type: "IMAGE"},
        {from: "shot1", slot: 1, to: "shot2", input: "story_context", type: "ASTROGATE_STORY"},
    ]};
    const map = mountGraph(board, recipe);
    const first = map.get("shot1"), second = map.get("shot2"), asset = map.get("asset");
    const getLink = (n, name) => graph.links[n.inputs.find(i => i.name === name).link];
    assert.equal(getLink(first, "reference_images.reference_1").origin_id, getLink(second, "reference_images.reference_1").origin_id);
    assert.equal(getLink(second, "story_context").origin_slot, 1);
    assert.equal(changed(second), false);
    asset.widgets[0].value = "修改身份";
    assert.equal(changed(first), true);
    assert.equal(changed(second), true);
    first.widgets[0].value = "手动改词"; first.pos = [900, 420];
    first.inputs.find(i => i.name === "reference_images.reference_1").link = null;
    mountGraph(board, recipe);
    assert.equal(graph._nodes.length, 4);
    assert.equal(first.widgets[0].value, "手动改词");
    assert.deepEqual(first.pos, [900, 420]);
    assert.equal(first.inputs.find(i => i.name === "reference_images.reference_1").link, null);
});
test("story edges never prepare or regenerate the preceding image", async () => {
    const {graph, LiteGraph} = graphMock();
    const make = (name, job) => { const n = LiteGraph.createNode("AstroGateStoryboardFrame"); graph.add(n); n.properties = {astrogate_project: "p", astrogate_owner: name, astrogate_owner_type: "shot", astrogate_base_prompt: name}; n.widgets[0].value = name; n.widgets[1].value = job; return n; };
    const previous = make("previous", "unknown-old"), target = make("target", "");
    target.addInput("story_context", "ASTROGATE_STORY"); previous.connect(1, target, 0);
    const calls = [];
    const api = {fetchApi: async (path, options) => {
        calls.push([path, options?.body]);
        return {ok: true, json: async () => path.endsWith("/native/prepare") ? {id: "new", status: "prepared"} : {}};
    }};
    const app = {registerExtension() {}, graph, nodeOutputs: {}, graphToPrompt: async () => ({output: {}, workflow: {}})};
    const {generate} = await load(app, {LiteGraph, FormData: class {}, crypto: {randomUUID: () => "prompt-id"}}, api);
    await generate([target]);
    assert.equal(calls.filter(([p]) => p.endsWith("/native/prepare")).length, 1);
    assert.equal(calls.some(([p]) => p.includes("unknown-old")), false);
    const prepared = JSON.parse(calls.find(([p]) => p.endsWith("/native/prepare"))[1]);
    assert.equal(prepared.story_parent_id, "previous");
    assert.deepEqual(prepared.references, []);
    const queued = JSON.parse(calls.find(([p]) => p === "/prompt")[1]);
    assert.deepEqual(queued.partial_execution_targets, [String(target.id)]);
});

function analysisGraph() {
    const {graph, LiteGraph} = graphMock();
    const file = LiteGraph.createNode('AstroGateTXT');
    file.widgets = [{name:'project_id', value:'project'}];
    graph.add(file);
    const board = LiteGraph.createNode('AstroGateStoryboard');
    board.widgets = Object.entries({text_model:'gpt-5.6-terra', chapter:1, style:'电影光线，雨夜\n第二行', density:'精细拆镜 😀', analysis_revision:0}).map(([name,value])=>({name,value}));
    board.widgets.push({name:'$$node-text-preview',value:'AI 正在分析剧本…'});
    board.agAnalysisButton = {};
    board.agRetryButton = {};
    board.agStatus = {textContent:'',classList:{toggle(){}}};
    board.pos=[0,0];
    graph.add(board);
    board.addInput('script','ASTROGATE_SCRIPT');
    file.connect(0,board,0);
    graph.change=()=>{};
    graph.serialize=()=>({});
    return {graph,LiteGraph,board};
}
const expectedAnalysisKey='78060fd40dc38dda3541669d10b4300be7c7b25787fb93ed76ef8e570975ac96';
const failedAnalysis={id:'failed-job',kind:'text',status:'failed',error:'文本接口 HTTP 503，未自动重试或切换模型。',analysis_key:expectedAnalysisKey,chapter_id:'chapter-id'};
function analysisAPI(calls, jobs) {
    return {fetchApi:async(path,options)=>{
        calls.push([path,options?.body]);
        let data={};
        if (path.endsWith('/projects/project')) data={project:{source:{hash:'source-hash'},chapters:[{id:'chapter-id'}]}};
        if (path.endsWith('?summary=1')) data={jobs};
        if (path.endsWith('/native/canvas')) data={project_id:'project',chapter_id:'chapter-id',nodes:[],links:[]};
        return {ok:true,json:async()=>data};
    }};
}
test('analysis key matches Python digest with Chinese text, newline and emoji',async()=>{
    const {graph,board}=analysisGraph();
    const {analysisContext}=await load({registerExtension(){},graph},{FormData:class{}},analysisAPI([],[]));
    const context=await analysisContext(board);
    assert.equal(context.key,expectedAnalysisKey);
    assert.equal(context.chapterId,'chapter-id');
});
test('failed analysis reveals original reason and hides stale progress without submitting again',async()=>{
    const {graph,board}=analysisGraph();
    const calls=[];
    const {startAnalysis}=await load({registerExtension(){},graph},{FormData:class{}},analysisAPI(calls,[failedAnalysis]));
    await startAnalysis(board);
    assert.match(board.agStatus.textContent,/HTTP 503/);
    assert.equal(board.agRetryButton.hidden,false);
    assert.equal(board.widgets.at(-1).hidden,true);
    assert.equal(calls.some(([p])=>p==='/prompt'||p.endsWith('/resume')),false);
});
test('explicit retry resumes the failed job and keeps completed stage checkpoints',async()=>{
    const {graph,board}=analysisGraph();
    const calls=[];
    const {startAnalysis}=await load({registerExtension(){},graph},{FormData:class{}},analysisAPI(calls,[failedAnalysis]));
    await startAnalysis(board,true);
    assert.equal(calls.filter(([p])=>p.endsWith('/jobs/failed-job/resume')).length,1);
    assert.equal(calls.some(([p])=>p==='/prompt'),false);
    assert.equal(board.widgets.find(w=>w.name==='analysis_revision').value,0);
    assert.equal(board.agAnalysisButton.disabled,true);
});
test('unknown results require an explicit retry and are labelled before submission',async()=>{
    const {graph,board}=analysisGraph();
    const calls=[];
    const job={...failedAnalysis,status:'unknown',error:'远端结果未知'};
    const {startAnalysis}=await load({registerExtension(){},graph},{FormData:class{}},analysisAPI(calls,[job]));
    await startAnalysis(board);
    assert.equal(board.agRetryButton.textContent,'结果未知，重新提交');
    assert.equal(calls.some(([p])=>p.endsWith('/resume')),false);
});

test('analysis progress shows the current phase and elapsed waiting time',async()=>{
    const {graph,board}=analysisGraph();
    const {showAnalysisState}=await load({registerExtension(){},graph},{Date:{now:()=>120000}});
    showAnalysisState(board,{status:'analysing',stage:'shots',stage_started_at:60,completed_chunks:0,total_chunks:1});
    assert.match(board.agStatus.textContent,/正在编写分镜与提示词/);
    assert.match(board.agStatus.textContent,/已等待 60 秒/);
    assert.equal(board.agAnalysisButton.disabled,true);
    assert.equal(board.agRetryButton.hidden,true);
});
test('completed resumed analysis automatically builds native nodes without another API analysis',async()=>{
    const {graph,LiteGraph,board}=analysisGraph();
    const calls=[];
    const {poll}=await load({registerExtension(){},graph,nodeOutputs:{}},{LiteGraph,FormData:class{}},analysisAPI(calls,[{...failedAnalysis,status:'succeeded',error:null}]));
    await poll();
    assert.equal(board.properties.astrogate_board.analysis_id,'failed-job');
    assert.equal(calls.filter(([p])=>p.endsWith('/native/canvas')).length,1);
    assert.equal(calls.some(([p])=>p.endsWith('/resume')||p==='/prompt'),false);
    await poll();
    assert.equal(calls.filter(([p])=>p.endsWith('/native/canvas')).length,1);
});

test('middle drag over card controls is forwarded to native canvas while left click stays editable',async()=>{
    const handlers={};
    const root={addEventListener:(name,handler)=>handlers[name]=handler};
    const dispatched=[];
    class Pointer { constructor(type,init){Object.assign(this,init);this.type=type;} }
    const node={addDOMWidget:()=>({options:{}})};
    const {panelWidget}=await load({registerExtension(){},canvas:{canvas:{dispatchEvent:event=>dispatched.push(event)}}},
        {PointerEvent:Pointer,document:{createElement:()=>({})}});
    panelWidget(node,root,()=>100);
    const event={type:'pointerdown',button:1,buttons:4,pointerId:7,isPrimary:true,target:{closest:()=>true},preventDefault(){this.prevented=true},stopPropagation(){this.stopped=true}};
    handlers.pointerdown(event);
    assert.equal(event.prevented,true);
    assert.equal(dispatched.length,1);
    assert.equal(dispatched[0].pointerId,7);
    const left={...event,button:0,buttons:1,prevented:false,stopped:false};
    handlers.pointerdown(left);
    assert.equal(dispatched.length,1);
    assert.equal(left.stopped,true);
    assert.equal(left.prevented,false);
});
