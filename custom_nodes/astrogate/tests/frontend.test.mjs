import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import vm from "node:vm";
import test from "node:test";
import { webcrypto } from "node:crypto";

const source = await readFile(new URL("../web/storyboard.js", import.meta.url), "utf8");
async function load(app, extras = {}, api = {}) {
    const context = vm.createContext({queueMicrotask, structuredClone, console, JSON, Set, Map, URLSearchParams, TextEncoder, crypto: webcrypto, ...extras});
    const module = new vm.SourceTextModule(source + "\nexport { installFold, mountGraph, changed, generate, installFrame, analysisContext, startAnalysis, showAnalysisState, poll, panelWidget, activityStatus, activityLines, message, createActivityPanel, buildBoard, applyFrameAspect, batchProgress, addSceneAngle, nodeSignature, upgradeSignature };", {context});
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

test('activity logs distinguish stages and hide credentials when copied',async()=>{
    const {activityLines,activityStatus}=await load({registerExtension(){}});
    const job={status:'unknown',kind:'text',stage:'shots',created_at:1,updated_at:4,error:'Bearer private-token sk-1234567890abcdef',events:[
        {time:1,status:'analysing',stage:'assets'}, {time:2,message:'接口响应 HTTP 200'}, {time:3,stage:'shots'}]};
    const log=activityLines(job);
    assert.match(log,/提取人物/);
    assert.match(log,/HTTP 200/);
    assert.match(log,/编写分镜/);
    assert.match(log,/结果未知/);
    assert.doesNotMatch(log,/private-token|1234567890abcdef/);
    assert.equal(activityStatus({...job,status:'succeeded'}),'已完成');
});

test('unchanged task status does not keep invalidating the canvas',async()=>{
    let dirty=0;
    const node={properties:{},agStatus:{textContent:'',classList:{toggle(){}}},setDirtyCanvas(){dirty++;}};
    const {message}=await load({registerExtension(){}});
    message(node,'已完成');
    message(node,'已完成');
    assert.equal(dirty,1);
    message(node,'正在生成',true);
    assert.equal(dirty,2);
});

test('compact log panel keeps logs when collapsed and cancels only the selected text task',async()=>{
    const elements=[];
    class Element {
        constructor(tag){this.tag=tag;this.children=[];this.events={};this.textContent='';this.scrollTop=0;this.scrollHeight=0;this.clientHeight=0;this.classList={toggle(){}};elements.push(this);}
        append(...items){this.children.push(...items);}
        replaceChildren(...items){this.children=items;}
        setAttribute(){}
        addEventListener(name,fn){this.events[name]=fn;}
    }
    const calls=[];
    const document={createElement:tag=>new Element(tag),body:new Element('body')};
    const api={fetchApi:async(path)=>{calls.push(path);return {ok:true,json:async()=>({id:'job-b',status:'cancelling',kind:'text',created_at:1})};}};
    const {createActivityPanel}=await load({registerExtension(){}},{document,FormData:class{},localStorage:{getItem:()=>null,setItem(){}},navigator:{clipboard:{writeText:async text=>calls.push(text)}}},api);
    const panel=createActivityPanel();
    const a={node:{id:1,title:'剧本 A'},job:{id:'job-a',kind:'text',status:'analysing',created_at:1,updated_at:2}};
    const b={node:{id:2,title:'剧本 B'},job:{id:'job-b',kind:'text',status:'analysing',created_at:1,updated_at:3,stage:'shots'}};
    panel.update([a,b]);
    const root=elements.find(e=>e.tag==='details');
    const log=elements.find(e=>e.tag==='pre');
    const collapse=elements.find(e=>e.textContent==='收起');
    const stop=elements.find(e=>e.textContent==='停止此任务');
    assert.equal(root.open,false);
    const before=log.textContent;
    root.open=true;
    await collapse.events.click();
    assert.equal(root.open,false);
    assert.equal(log.textContent,before);
    assert.equal(collapse.disabled,false);
    await stop.events.click();
    assert.deepEqual(calls,['/astrogate/storyboard/jobs/job-b/cancel']);
    assert.equal(stop.disabled,true);
    assert.equal(a.job.status,'analysing');
    panel.update([1,2,3,4].map(id=>({node:{id,title:'图片 '+id},job:{id:'image-'+id,kind:'image',status:id<4?'running':'queued',created_at:1}})));
    assert.ok(elements.some(e=>e.textContent==='分镜日志 · 3 张生成中 · 1 张等待'));
    assert.equal(stop.textContent,'停止此批次');
    panel.update([]);
    assert.match(log.textContent,/选择 TXT/);
});

test('building a chapter queues images once and the saved auto marker survives restore',async()=>{
    const {graph,LiteGraph,board}=analysisGraph();
    const recipe={project_id:'project',chapter_id:'chapter-id',nodes:['front','left','shot'].map((id,i)=>({id,type:'AstroGateStoryboardFrame',title:id,pos:[i*500,100],
        widgets:{prompt:id,job_id:'',aspect_ratio:i<2?'2:3':'16:9',resolution:'1K',quality:'high'},
        properties:{astrogate_project:'project',astrogate_owner:id==='shot'?'shot':'actor',astrogate_owner_type:id==='shot'?'shot':'asset',astrogate_key:id,astrogate_kind:id==='shot'?'shot':'character',astrogate_view:id==='shot'?'':id,astrogate_base_prompt:id}})),
        links:[...['front','left','shot'].map(to=>({from:'$board',slot:0,to,input:'board',type:'ASTROGATE_BOARD'})),
            {from:'front',slot:0,to:'left',input:'reference_images.reference_1',type:'IMAGE'},
            {from:'front',slot:0,to:'shot',input:'reference_images.reference_1',type:'IMAGE'},
            {from:'left',slot:0,to:'shot',input:'reference_images.reference_2',type:'IMAGE'}]};
    const calls=[];
    const api={fetchApi:async(path,options)=>{
        const body=options?.body?JSON.parse(options.body):null;
        calls.push([path,body]);
        const data=path.endsWith('/native/canvas')?recipe:path.endsWith('/native/prepare')?{id:'job-'+body.prompt,status:'prepared'}:{};
        return {ok:true,json:async()=>data};
    }};
    const app={registerExtension(){},graph,nodeOutputs:{},canvas:{ds:{},centerOnNode(){},selectNode(){}},graphToPrompt:async()=>({output:{},workflow:{}})};
    const {buildBoard}=await load(app,{LiteGraph,FormData:class{},crypto:{randomUUID:()=> 'auto-images'}},api);
    const info={project_id:'project',chapter_id:'chapter-id',analysis_id:'analysis'};
    await Promise.all([buildBoard(board,info),buildBoard(board,info)]);
    assert.equal(calls.filter(([p])=>p.endsWith('/native/canvas')).length,1);
    assert.equal(calls.filter(([p])=>p==='/prompt').length,1);
    const prepared=calls.filter(([p])=>p.endsWith('/native/prepare')).map(([,b])=>b);
    assert.deepEqual(prepared.map(b=>b.prompt),['front','left','shot']);
    assert.equal(prepared[1].references[0].job_id,'job-front');
    assert.equal(prepared[1].view,'left');
    assert.equal(board.properties.astrogate_generation_jobs.length,3);
    await buildBoard(board,info,true);
    assert.equal(calls.filter(([p])=>p==='/prompt').length,1);
    board.properties.astrogate_autogenerate=false;
    await buildBoard(board,{...info,analysis_id:'another'});
    assert.equal(calls.filter(([p])=>p==='/prompt').length,1);
});

test('global aspect changes scene frames while keeping character designs and old images',async()=>{
    const {graph,LiteGraph,board}=analysisGraph();
    board.widgets.push({name:'frame_aspect_ratio',value:'2.390：1'});
    const frames=['character','scene','shot'].map(kind=>{
        const n=LiteGraph.createNode('AstroGateStoryboardFrame');graph.add(n);
        n.properties={astrogate_kind:kind,astrogate_images:[{filename:'old.png'}]};
        n.widgets.find(w=>w.name==='aspect_ratio').value='2:3';
        n.addInput('board','ASTROGATE_BOARD');board.connect(0,n,0);return n;
    });
    const calls=[];
    const {applyFrameAspect}=await load({registerExtension(){},graph},{FormData:class{}},analysisAPI(calls,[]));
    await applyFrameAspect(board);
    assert.deepEqual(frames.map(n=>n.widgets.find(w=>w.name==='aspect_ratio').value),['2:3','2.39:1','2.39:1']);
    assert.ok(frames.every(n=>n.properties.astrogate_images[0].filename==='old.png'));
    assert.equal(JSON.parse(calls[0][1]).action,'frame_aspect_ratio');
    assert.equal(JSON.parse(calls[0][1]).style,'电影光线，雨夜\n第二行');
});

test('image card controls precede a folded prompt and batch progress counts only saved images',async()=>{
    const elements=[];
    class Element {
        constructor(tag){this.tag=tag;this.children=[];this.events={};this.classList={toggle(){}};elements.push(this);}
        append(...items){this.children.push(...items);}
        replaceChildren(...items){this.children=items;}
        addEventListener(name,fn){this.events[name]=fn;}
        getAttribute(name){return this[name];}
        removeAttribute(name){delete this[name];}
    }
    const {graph,LiteGraph}=graphMock();graph.change=()=>{};
    const node=LiteGraph.createNode('AstroGateStoryboardFrame');graph.add(node);
    node.properties.astrogate_images=[{filename:'old.png',type:'output'}];
    node.widgets.forEach(w=>{w.options={values:['16:9','4K','high']};});
    node.widgets[0].value='保留提示词';
    node.inputs=[{name:'reference_images.reference_1',link:11},{name:'reference_images.reference_2',link:22},{name:'board',link:33}];
    node.addDOMWidget=()=>({options:{}});node.setSize=size=>node.size=size;node.computeSize=()=>[430,440];
    let resize, disconnected=false;
    class ResizeObserver { constructor(callback){resize=callback;} observe(){} disconnect(){disconnected=true;} }
    const {installFrame,batchProgress}=await load({registerExtension(){},graph,nodeOutputs:{}},{ResizeObserver,document:{createElement:tag=>new Element(tag)}},{apiURL:path=>path});
    installFrame(node);
    const root=elements.find(e=>e.className==='ag-native ag-image-card').children[0];
    const detail=elements.find(e=>e.tag==='details');
    assert.equal(root.children[3].className,'ag-native-actions');
    assert.equal(root.children[5],detail);
    assert.match(root.children[0].textContent,/参考图 2/);
    assert.equal(node.properties.astrogate_references_folded,true);
    assert.equal(node.agStatus.hidden,true);
    const inputs=node.inputs;
    root.children[0].events.click();
    assert.equal(node.properties.astrogate_references_folded,false);
    assert.ok(node.inputs.every(i=>i.pos===undefined));
    root.children[0].events.click();
    assert.equal(node.inputs,inputs);
    assert.deepEqual(node.inputs.map(i=>i.link),[11,22,33]);
    assert.equal(detail.open,false);
    assert.equal(node.agImage.hidden,false);
    assert.equal(elements.find(e=>e.tag==='textarea').value,'保留提示词');
    detail.open=true;detail.events.toggle();root.offsetHeight=500;resize();
    const expandedHeight=node.size[1];
    detail.open=false;detail.events.toggle();root.offsetHeight=335;resize();
    assert.ok(expandedHeight-node.size[1]>=160);
    assert.ok(node.size[1]<440);
    node.widgets.push({name:'$$canvas-image-preview'},{name:'$$node-text-preview'});
    node.onDrawBackground();
    assert.ok(node.widgets.slice(-2).every(w=>w.hidden));
    assert.equal(node.widgets[0].value,'保留提示词');
    assert.equal(node.agImage.hidden,false);
    node.properties.astrogate_generation_jobs=['a','b','c','d'];
    batchProgress(node,new Map([['a',{status:'succeeded'}],['b',{status:'running'}],['c',{status:'queued'}],['d',{status:'unknown'}]]));
    assert.equal(node.agProgress.bar.value,1);assert.equal(node.agProgress.bar.max,4);
    assert.match(node.agProgress.label.textContent,/已完成 1\/4.*未完成\/异常 1/);
    node.onRemoved();assert.equal(disconnected,true);
});

test('legacy cards preserve cached images while style, atmosphere and reference purposes affect generation',async()=>{
    const elements=[];
    class Element {
        constructor(tag){this.tag=tag;this.children=[];this.events={};this.classList={toggle(){}};elements.push(this);}
        append(...items){this.children.push(...items);}
        replaceChildren(...items){this.children=items;}
        addEventListener(name,fn){this.events[name]=fn;}
        getAttribute(name){return this[name];}
        removeAttribute(name){delete this[name];}
    }
    class ResizeObserver {observe(){} disconnect(){}}
    const {graph,LiteGraph,board}=analysisGraph();
    const image=LiteGraph.createNode('LoadImage');graph.add(image);image.title='参考图';
    const node=LiteGraph.createNode('AstroGateStoryboardFrame');graph.add(node);
    node.title='第 1 镜';node.properties={astrogate_kind:'shot',astrogate_project:'project',astrogate_owner:'shot',astrogate_owner_type:'shot',astrogate_base_prompt:'两人交谈'};
    Object.assign(node.widgets[0],{value:'两人交谈'});node.widgets[1].value='old-job';
    node.widgets[3].value='16:9';node.widgets[4].value='4K';node.widgets[5].value='high';
    node.properties.astrogate_base_settings=['16:9','4K','high'];
    node.widgets.forEach(w=>{w.options={values:['16:9','4K','high']};});
    node.addInput('reference_images.reference_1','IMAGE');image.connect(0,node,0);
    node.addInput('board','ASTROGATE_BOARD');board.connect(0,node,1);
    node.addDOMWidget=()=>({options:{}});node.setSize=size=>node.size=size;node.computeSize=()=>[430,440];
    const calls=[];
    const api={apiURL:path=>path,fetchApi:async(path,options)=>{
        const body=options?.body ? JSON.parse(options.body) : null;
        calls.push([path,body]);
        const data=path.endsWith('/native/prepare')?{id:'new-job',status:'prepared'}:path.endsWith('/jobs/old-job')?{id:'old-job',status:'succeeded'}:{};
        return {ok:true,json:async()=>data};
    }};
    const app={registerExtension(){},graph,nodeOutputs:{},graphToPrompt:async()=>({output:{},workflow:{}})};
    const {installFrame,changed,generate,nodeSignature}=await load(app,{ResizeObserver,document:{createElement:tag=>new Element(tag)},FormData:class{},crypto:{randomUUID:()=> 'prompt-id'}},api);
    node.properties.astrogate_signature=JSON.stringify(JSON.parse(nodeSignature(node)).slice(0,6));
    installFrame(node);
    assert.equal(changed(node),false);
    assert.equal(JSON.parse(node.properties.astrogate_signature).length,9);
    const style=board.widgets.find(w=>w.name==='style');
    const originalStyle=style.value;
    style.value='水墨风格';assert.equal(changed(node),true);
    style.value=originalStyle;assert.equal(changed(node),false);
    const root=elements.find(e=>e.className==='ag-native ag-image-card').children[0];
    assert.match(root.children[0].textContent,/分镜.*参考图 1/);
    root.children[0].events.click();
    const row=root.children[1].children[0];
    row.children[1].value='style';row.children[1].events.change();
    row.children[2].value='仅参考中国水墨笔触';row.children[2].events.input();
    const atmosphere=elements.find(e=>e.placeholder==='可选，例如雨雾、烛光或魔法光效');
    atmosphere.value='窗外雨雾';atmosphere.events.input();
    assert.equal(changed(node),true);
    await generate([node]);
    const prepared=calls.find(([path])=>path.endsWith('/native/prepare'))[1];
    assert.equal(prepared.atmosphere,'窗外雨雾');
    assert.equal(prepared.references[0].role,'style');
    assert.match(prepared.references[0].purpose,/整体画风.*仅参考中国水墨笔触/);
    assert.equal(calls.find(([path])=>path.endsWith('/edit'))[1].style,originalStyle);
    assert.equal(changed(node),false);
    const other=LiteGraph.createNode('LoadImage');graph.add(other);other.title='另一张参考图';other.connect(0,node,0);
    assert.equal(changed(node),true);
    await generate([node]);
    const replaced=calls.filter(([path])=>path.endsWith('/native/prepare')).at(-1)[1].references[0];
    assert.equal(replaced.role,'auto');
    assert.doesNotMatch(replaced.purpose,/中国水墨笔触/);
});

test('legacy signature migration does not hide edits or reference changes',async()=>{
    const {graph,LiteGraph}=graphMock();
    const node=LiteGraph.createNode('AstroGateStoryboardFrame');graph.add(node);
    node.properties={astrogate_kind:'shot',astrogate_base_prompt:'原提示词'};
    node.widgets[0].value='原提示词';
    const {changed,nodeSignature,upgradeSignature}=await load({registerExtension(){},graph});
    node.properties.astrogate_signature=JSON.stringify(JSON.parse(nodeSignature(node)).slice(0,6));
    const previous=node.properties.astrogate_signature;
    node.widgets[0].value='修改后的提示词';
    upgradeSignature(node);
    assert.equal(node.properties.astrogate_signature,previous);
    assert.equal(changed(node),true);
});

test('changing global style marks shots changed but keeps completed assets reusable',async()=>{
    const {graph,LiteGraph,board}=analysisGraph();
    const asset=LiteGraph.createNode('AstroGateStoryboardFrame');graph.add(asset);
    asset.properties={astrogate_kind:'character',astrogate_base_prompt:'人物'};
    asset.widgets[0].value='人物';asset.addInput('board','ASTROGATE_BOARD');board.connect(0,asset,0);
    const shot=LiteGraph.createNode('AstroGateStoryboardFrame');graph.add(shot);
    shot.properties={astrogate_kind:'shot',astrogate_base_prompt:'分镜'};
    shot.widgets[0].value='分镜';shot.addInput('board','ASTROGATE_BOARD');board.connect(0,shot,0);
    const {changed,nodeSignature}=await load({registerExtension(){},graph});
    asset.properties.astrogate_signature=nodeSignature(asset);
    shot.properties.astrogate_signature=nodeSignature(shot);
    board.widgets.find(w=>w.name==='style').value='水墨风格';
    assert.equal(changed(asset),false);
    assert.equal(changed(shot),true);
});

test('extra scene angles are manual independent nodes referencing one base scene',async()=>{
    const {graph,LiteGraph,board}=analysisGraph();
    board.properties.astrogate_board={project_id:'project',chapter_id:'chapter'};
    const source=LiteGraph.createNode('AstroGateStoryboardFrame');graph.add(source);
    source.title='客栈';source.pos=[500,100];source.size=[430,400];
    source.properties={astrogate_project:'project',astrogate_key:'scene',astrogate_owner:'scene',astrogate_owner_type:'asset',astrogate_kind:'scene',astrogate_version:'v1',astrogate_images:[{filename:'base.png'}]};
    source.widgets[0].value='无人客栈';source.widgets[1].value='base-job';
    source.addInput('board','ASTROGATE_BOARD');board.connect(0,source,0);
    const app={registerExtension(){},graph,canvas:{ds:{},centerOnNode(){},selectNode(){}}};
    const calls=[];
    const {addSceneAngle}=await load(app,{LiteGraph,FormData:class{}},analysisAPI(calls,[]));
    assert.equal(graph._nodes.length,3);
    const left=await addSceneAngle(source,'scene_left');
    assert.equal(left.properties.astrogate_owner,'scene');
    assert.equal(left.properties.astrogate_view,'scene_left');
    assert.equal(left.widgets[1].value,'');
    assert.equal(left.properties.astrogate_images.length,0);
    assert.match(left.widgets[0].value,/左侧机位.*仅改变摄影机/);
    const ref=left.inputs.find(i=>i.name==='reference_images.reference_1');
    assert.equal(graph.links[ref.link].origin_id,source.id);
    assert.equal(await addSceneAngle(source,'scene_left'),left);
    assert.equal(graph._nodes.length,4);
    assert.equal(calls.filter(([p])=>p.endsWith('/native/prepare')).length,0);
    assert.equal(source.widgets[1].value,'base-job');
});
