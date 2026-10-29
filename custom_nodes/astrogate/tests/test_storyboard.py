import asyncio
import copy
import importlib.util
import sys
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
import torch
from aiohttp import web
from PIL import Image

import nodes
from execution import PromptExecutor


@pytest.fixture
def modules(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("astrogate_story_test", Path(__file__).parents[1] / "__init__.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    store_module = sys.modules[spec.name + ".storyboard.store"]
    graph = sys.modules[spec.name + ".storyboard.graph"]
    analysis = sys.modules[spec.name + ".storyboard.analysis"]
    store = store_module.Store(tmp_path / "user", tmp_path / "input")
    monkeypatch.setattr(module, "get_store", lambda: store)
    monkeypatch.setenv("ASTROGATE_IMG_API_KEY", "test-only-image")
    monkeypatch.setattr(module.AstroGateStoryboardFrame, "hidden", SimpleNamespace(unique_id=None, prompt=None, extra_pnginfo=None), raising=False)
    return SimpleNamespace(node=module, domain=store_module, graph=graph, analysis=analysis, store=store)


@pytest.fixture
def project(modules):
    d = modules.domain
    p = d.parse_novel("第一章 客栈\r\n林夏穿蓝衣，周野穿黑衣，两人在客栈相遇。\r\n林夏提到不在场的阿明。\r\n第二章 街道\r\n林夏换上雨衣走到街道。".encode(), "测试小说")
    source = [{"paragraph_id": p["chapters"][0]["paragraphs"][1]["id"], "quote": "两人在客栈相遇"}]
    for name, kind, parent in [("林夏", "character", None), ("蓝衣", "look", "林夏"), ("周野", "character", None),
                               ("黑衣", "look", "周野"), ("客栈", "scene", None)]:
        p["assets"].append({"id": name, "name": name, "kind": kind, "parent_id": parent, "aliases": [], "source_refs": source,
                            "current_version_id": name + "1", "versions": [{"id": name + "1", "description": name, "image": None}]})
    shot = {"id": "shot1", "chapter_id": p["chapters"][0]["id"], "summary": "相遇", "source_refs": source, "revision": 1,
            "image_prompt": "两人在客栈相遇", "scene": d.binding(p, "客栈"), "participants": [
                {"visible": True, "identity": d.binding(p, "林夏"), "look": d.binding(p, "蓝衣"), "action": "抬头"},
                {"visible": True, "identity": d.binding(p, "周野"), "look": d.binding(p, "黑衣"), "action": "推门"}],
            "props": [], "previous_shot_id": None, "stale": True}
    p["shots"] = [shot]
    modules.store.create("default", p)
    return p


@pytest.mark.parametrize("encoding", ["utf-8-sig", "gb18030", "utf-16"])
def test_import_keeps_exact_source_offsets(modules, encoding):
    text = "第一章 雨夜\r\n  林夏走出门。\n第二章 晨光\n街道无人。\n"
    p = modules.domain.parse_novel(text.encode(encoding), "小说")
    assert len(p["chapters"]) == 2
    for c in p["chapters"]:
        for paragraph in c["paragraphs"]:
            assert p["source"]["text"][paragraph["start"]:paragraph["end"]] == paragraph["text"]


def test_long_paragraph_chunks_keep_identity_without_duplicate_text(modules):
    p = [{"id": "p", "text": "甲" * 14000}]
    result = list(modules.domain.chunks(p))
    assert [len(c[0]["text"]) for c in result] == [6000, 6000, 2000]
    assert all(c[0]["id"] == "p" for c in result)
    assert "".join(c[0]["text"] for c in result) == p[0]["text"]


def test_version_updates_only_following_shots_and_explicit_descendants(modules, project):
    p, d = project, modules.domain
    locked = copy.deepcopy(p["shots"][0])
    locked["id"] = "locked"
    locked["stale"] = False
    locked["participants"][0]["look"]["policy"] = "locked"
    p["shots"][0]["stale"] = False
    following = copy.deepcopy(locked)
    following.update(id="following", previous_shot_id="shot1")
    p["shots"].extend([locked, following])
    v = d.publish_version(p, "蓝衣", "蓝衣有新饰品")
    assert p["shots"][0]["stale"] and following["stale"]
    assert not locked["stale"]
    assert locked["participants"][0]["look"]["version_id"] == "蓝衣1"
    assert p["shots"][0]["participants"][0]["look"]["version_id"] == v["id"]


def test_bad_look_character_pair_and_mentioned_people_rejected(modules, project):
    shot = project["shots"][0]
    shot["participants"][0]["look"] = modules.domain.binding(project, "黑衣")
    with pytest.raises(ValueError, match="造型"):
        modules.domain.validate_shot(project, shot)
    shot["participants"][0]["look"] = None
    shot["participants"][1]["visible"] = False
    with pytest.raises(ValueError, match="出镜"):
        modules.domain.validate_shot(project, shot)


def test_cycle_and_fake_evidence_rejected(modules, project):
    shot = project["shots"][0]
    shot["previous_shot_id"] = "shot1"
    with pytest.raises(ValueError, match="循环"):
        modules.domain.validate_shot(project, shot)
    shot["previous_shot_id"] = None
    shot["source_refs"][0]["quote"] = "这句话不在原文"
    with pytest.raises(ValueError, match="引文"):
        modules.domain.validate_shot(project, shot)


def test_project_revision_and_owner_protection(modules, project):
    store = modules.store
    with pytest.raises(ValueError, match="用户"):
        store.get(project["id"], "other-user")
    store.change(project["id"], lambda p: p.update(title="改名"), 0)
    with pytest.raises(ValueError, match="更新"):
        store.change(project["id"], lambda p: p.update(title="覆盖"), 0)
    assert store.get(project["id"])["title"] == "改名"
    with pytest.raises(ValueError, match="路径"):
        store.image_path("../private")


def save_reference(modules, project, name, color):
    data = BytesIO()
    Image.new("RGB", (8, 8), color).save(data, format="PNG")
    image = modules.store.save_image(project["id"], data.getvalue())
    asset = modules.domain.asset_by_id(project, name)
    modules.domain.version(asset)["image"] = image


def test_semantic_reference_order_matches_links_and_prompt(modules, project):
    for name, color in [("客栈", "gray"), ("林夏", "red"), ("蓝衣", "blue"), ("周野", "green"), ("黑衣", "black")]:
        save_reference(modules, project, name, color)
    planned = modules.graph.plan(modules.store, project, ["shot1"])
    assert not planned["blocked"]
    recipe = modules.graph.prepare(modules.store, project, planned)
    job = modules.store.job(recipe["jobs"][0])
    assert len(job["references"]) == len(recipe["links"]) == 5
    for i, ref in enumerate(job["references"]):
        assert f"图{i + 1}：{ref['purpose']}" in job["prompt"]
        assert recipe["links"][i]["input"] == f"reference_images.reference_{i + 1}"
    assert recipe["prompt"][recipe["targets"][0]]["class_type"] == "AstroGateStoryboardFrame"


def test_no_reference_requires_explicit_text_only_override(modules, project):
    planned = modules.graph.plan(modules.store, project, ["shot1"])
    assert planned["blocked"] and len(planned["missing"]) == 5
    with pytest.raises(ValueError, match="素材"):
        modules.graph.prepare(modules.store, project, planned)
    allowed = modules.graph.plan(modules.store, project, ["shot1"], allow_missing=True)
    recipe = modules.graph.prepare(modules.store, project, allowed)
    assert recipe["links"] == [] and len(recipe["targets"]) == 1


def test_unknown_jobs_never_automatically_retry(modules, project):
    planned = modules.graph.plan(modules.store, project, ["shot1"], allow_missing=True)
    recipe = modules.graph.prepare(modules.store, project, planned)
    modules.store.update_job(recipe["jobs"][0], status="unknown")
    planned = modules.graph.plan(modules.store, project, ["shot1"], allow_missing=True)
    with pytest.raises(ValueError, match="未知"):
        modules.graph.prepare(modules.store, project, planned)
    fresh = modules.graph.prepare(modules.store, project, planned, acknowledge_unknown=True)
    assert fresh["jobs"] != recipe["jobs"]


def test_restart_marks_inflight_unknown_and_preserves_success(modules, project):
    first = modules.store.add_job(project["id"], {"status": "running"})
    second = modules.store.add_job(project["id"], {"status": "succeeded"})
    restarted = modules.domain.Store(modules.store.root, modules.store.input_dir)
    restarted.recover()
    assert restarted.job(first["id"])["status"] == "unknown"
    assert restarted.job(second["id"])["status"] == "succeeded"


async def test_generation_persists_and_reuses_after_restart(modules, project, monkeypatch, tmp_path):
    m = modules.node
    monkeypatch.setattr(m.folder_paths, "output_directory", str(tmp_path / "output"))
    (tmp_path / "output").mkdir()
    calls = []

    async def generate(payload, edit):
        calls.append(payload)
        data = BytesIO()
        Image.new("RGB", (16, 16), "red").save(data, format="PNG")
        return {"data": [{"b64_json": m.base64.b64encode(data.getvalue()).decode()}]}

    monkeypatch.setattr(m, "generate_image", generate)
    recipe = modules.graph.prepare(modules.store, project, modules.graph.plan(modules.store, project, ["shot1"], allow_missing=True))
    job = modules.store.job(recipe["jobs"][0])
    result = await m.AstroGateStoryboardFrame.execute(job["prompt"], job["id"])
    assert result.result[0].shape[0] == 1
    saved = modules.store.job(job["id"])
    assert saved["status"] == "succeeded"
    assert modules.store.image_path(saved["result_image"]["file"]).is_file()
    assert (modules.store.root / "requests" / f"{job['id']}.json").is_file()
    reloaded = modules.domain.Store(modules.store.root, modules.store.input_dir)
    monkeypatch.setattr(m, "get_store", lambda: reloaded)
    await m.AstroGateStoryboardFrame.execute(job["prompt"], job["id"])
    assert len(calls) == 1
    assert not modules.store.get(project["id"])["shots"][0]["stale"]


async def test_changed_reference_blocks_before_network(modules, project, monkeypatch):
    save_reference(modules, project, "客栈", "red")
    planned = modules.graph.plan(modules.store, project, ["shot1"], allow_missing=True)
    recipe = modules.graph.prepare(modules.store, project, planned)
    job = modules.store.job(recipe["jobs"][0])
    with pytest.raises(ValueError, match="顺序或内容"):
        await modules.node.AstroGateStoryboardFrame.execute(job["prompt"], job["id"], {"reference_1": torch.zeros(1, 8, 8, 3)})
    assert modules.store.job(job["id"])["status"] == "prepared"


async def test_timeout_becomes_unknown_with_no_retry(modules, project, monkeypatch):
    async def generate(*args):
        raise RuntimeError("图片请求超时")
    monkeypatch.setattr(modules.node, "generate_image", generate)
    recipe = modules.graph.prepare(modules.store, project, modules.graph.plan(modules.store, project, ["shot1"], allow_missing=True))
    job = modules.store.job(recipe["jobs"][0])
    with pytest.raises(RuntimeError, match="超时"):
        await modules.node.AstroGateStoryboardFrame.execute(job["prompt"], job["id"])
    assert modules.store.job(job["id"])["status"] == "unknown"
    with pytest.raises(RuntimeError, match="不能重复"):
        await modules.node.AstroGateStoryboardFrame.execute(job["prompt"], job["id"])


async def test_image_timeout_is_configurable_and_does_not_retry(modules, monkeypatch, aiohttp_server):
    m = modules.node
    monkeypatch.delenv('ASTROGATE_IMG_TIMEOUT', raising=False)
    assert m.image_timeout().total == 900 and m.image_timeout().connect == 15
    calls = []
    async def handle(request):
        calls.append(request.path)
        await asyncio.sleep(.2)
        return web.json_response({'data':[]})
    app = web.Application()
    app.router.add_post('/images/generations', handle)
    server = await aiohttp_server(app)
    monkeypatch.setattr(m, 'API_BASE', str(server.make_url('/')).rstrip('/'))
    monkeypatch.setenv('ASTROGATE_IMG_TIMEOUT', '.05')
    with pytest.raises(RuntimeError, match='超时'):
        await m.generate_image({'prompt':'test'}, False)
    assert calls == ['/images/generations']
    monkeypatch.setenv('ASTROGATE_IMG_TIMEOUT', '0')
    with pytest.raises(ValueError, match='正数'):
        m.image_timeout()


async def test_cancelled_inflight_image_remains_unknown_and_is_not_retried(modules, project, monkeypatch):
    m = modules.node
    native = sys.modules[m.__name__ + '.storyboard.native']
    job = native.prepare_native(modules.store,project,{'owner_id':'shot1','owner_type':'shot','prompt':'客栈',
        'settings':{},'references':[]})
    entered = asyncio.Event()
    calls = []
    async def generate(*args):
        calls.append('sent')
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(m,'generate_image',generate)
    task = asyncio.create_task(m.AstroGateStoryboardFrame.execute('客栈',job['id']))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls == ['sent']
    assert modules.store.job(job['id'])['status'] == 'unknown'
    with pytest.raises(ValueError, match='未知'):
        native.prepare_native(modules.store,modules.store.get(project['id']),{'owner_id':'shot1','owner_type':'shot',
            'prompt':'客栈','settings':{},'references':[]})


async def test_text_model_configuration_keeps_channels_separate(modules, aiohttp_server, monkeypatch):
    calls = []
    async def handle(request):
        assert request.headers["Authorization"] == "Bearer text-only"
        data = await request.json()
        calls.append(data)
        return web.json_response({"choices": [{"message": {"content": '{"assets":[]}'}, "finish_reason": "stop"}]})
    app = web.Application()
    app.router.add_post("/v1/chat/completions", handle)
    server = await aiohttp_server(app)
    monkeypatch.setattr(modules.analysis, "TEXT_BASE", str(server.make_url("/v1")))
    monkeypatch.setenv("ASTROGATE_TEXT_API_KEY", "text-only")
    await modules.analysis.text_request("gpt-5.6-terra", "system", {})
    await modules.analysis.text_request("gpt-6-luna", "system", {})
    assert calls[0]["reasoning_effort"] == "xhigh"
    assert "reasoning_effort" not in calls[1]
    assert all(c["stream"] is True for c in calls)
    assert all("response_format" not in c for c in calls)


def test_job_events_survive_reload_without_logging_every_progress_update(modules, project):
    store = modules.store
    job = store.add_job(project['id'], {'kind': 'text'})
    store.update_job(job['id'], status='analysing', stage='assets', event='发送素材提取请求')
    for count in range(5):
        store.update_job(job['id'], received_chars=count, last_activity_at=123)
    saved = store.job(job['id'])
    assert len(saved['events']) == 2
    assert saved['events'][-1]['stage'] == 'assets'
    store.update_job(job['id'], status='failed', error='文本接口 HTTP 503')
    reloaded = modules.domain.Store(store.root, store.input_dir).job(job['id'])
    assert reloaded['events'][-1]['error'] == '文本接口 HTTP 503'
    for count in range(125):
        store.update_job(job['id'], event=f'阶段 {count}')
    assert len(store.job(job['id'])['events']) == 120


async def test_text_stream_handles_split_utf8_keepalive_and_usage(modules, aiohttp_server, monkeypatch):
    calls = []
    async def handle(request):
        calls.append(await request.json())
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        events = [
            {"choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}}]},
            {"choices": [{"index": 0, "delta": {"content": '{"summary":"雨夜'}}]},
            {"choices": [{"index": 0, "delta": {"content": '相遇"}'}}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            {"choices": [], "usage": {"completion_tokens": 12}},
        ]
        wire = b": keep-alive\n\n" + b"".join(("data: " + modules.node.json.dumps(e, ensure_ascii=False) + "\r\n\r\n").encode() for e in events) + b"data: [DONE]\n\n"
        for offset in range(0, len(wire), 7):
            await response.write(wire[offset:offset + 7])
        return response
    app = web.Application()
    app.router.add_post("/chat/completions", handle)
    server = await aiohttp_server(app)
    monkeypatch.setattr(modules.analysis, "TEXT_BASE", str(server.make_url("/")).rstrip("/"))
    monkeypatch.setenv("ASTROGATE_TEXT_API_KEY", "text-only")
    progress = []
    text, usage = await modules.analysis.text_request("gpt-5.6-terra", "system", {}, on_progress=lambda event, **fields: progress.append((event, fields)))
    assert modules.node.json.loads(text) == {"summary": "雨夜相遇"}
    assert usage == {"completion_tokens": 12}
    assert len(calls) == 1
    assert progress[0][1]['response_status'] == 200
    assert any(fields.get('request_state') == 'receiving' for _, fields in progress)
    assert progress[-1][1]['received_chars'] == len(text)
    assert all('雨夜' not in (event or '') for event, _ in progress)


@pytest.mark.parametrize("ending,error", [
    ("", "结果未知"),
    ('data: {"choices":[{"delta":{},"finish_reason":"length"}]}\n\n', "被截断"),
    ('data: {"error":{"message":"upstream failed"}}\n\n', "流式错误"),
])
async def test_incomplete_text_stream_never_retries(modules, aiohttp_server, monkeypatch, ending, error):
    calls = []
    async def handle(request):
        calls.append(await request.json())
        return web.Response(text='data: {"choices":[{"delta":{"content":"partial"}}]}\n\n' + ending, content_type="text/event-stream")
    app = web.Application()
    app.router.add_post("/chat/completions", handle)
    server = await aiohttp_server(app)
    monkeypatch.setattr(modules.analysis, "TEXT_BASE", str(server.make_url("/")).rstrip("/"))
    monkeypatch.setenv("ASTROGATE_TEXT_API_KEY", "text-only")
    with pytest.raises(RuntimeError, match=error):
        await modules.analysis.text_request("gpt-5.6-terra", "system", {})
    assert len(calls) == 1


async def test_configured_text_timeout_stays_unknown_without_retry(modules, aiohttp_server, monkeypatch):
    calls = []
    async def handle(request):
        calls.append(await request.json())
        await asyncio.sleep(0.1)
        return web.json_response({"choices": []})
    app = web.Application()
    app.router.add_post("/chat/completions", handle)
    server = await aiohttp_server(app)
    monkeypatch.setattr(modules.analysis, "TEXT_BASE", str(server.make_url("/")).rstrip("/"))
    monkeypatch.setenv("ASTROGATE_TEXT_API_KEY", "text-only")
    monkeypatch.setenv("ASTROGATE_TEXT_TIMEOUT", "0.03")
    with pytest.raises(RuntimeError, match="结果未知"):
        await modules.analysis.text_request("gpt-5.6-terra", "system", {})
    assert len(calls) == 1


async def test_analysis_resume_keeps_applied_asset_checkpoint(modules, project, monkeypatch):
    ref = project["shots"][0]["source_refs"]
    calls = []
    async def first(model, system, content, on_progress=None):
        calls.append(system)
        if system == modules.analysis.EXTRACT_PROMPT:
            return modules.node.json.dumps({"assets": [{"local_id": "lin", "existing_id": "林夏", "kind": "character", "source_refs": ref}]}), {}
        raise RuntimeError("远端结果未知")
    monkeypatch.setattr(modules.analysis, "text_request", first)
    job = modules.store.add_job(project["id"], {"kind": "text", "chapter_id": project["chapters"][0]["id"],
                                               "model": "gpt-6-luna", "style": "写实", "density": "自然拆镜"})
    await modules.analysis.analyse(modules.store, job["id"])
    assert modules.store.job(job["id"])["status"] == "unknown"

    async def resume(model, system, content, on_progress=None):
        calls.append(system)
        assert system == modules.analysis.SHOT_PROMPT
        return '{"shots":[],"summary":"两人相遇"}', {}
    monkeypatch.setattr(modules.analysis, "text_request", resume)
    await modules.analysis.analyse(modules.store, job["id"])
    assert modules.store.job(job["id"])["status"] == "succeeded"
    assert calls.count(modules.analysis.EXTRACT_PROMPT) == 1
    saved = modules.store.get(project["id"])
    assert len(modules.domain.asset_by_id(saved, "林夏")["suggestions"]) == 1
    assert len(saved["analysis_checkpoints"]) == 2


async def test_analysis_keeps_paragraph_when_ai_paraphrases_quote(modules, project, monkeypatch):
    paragraph_id = project["chapters"][0]["paragraphs"][1]["id"]
    refs = [{"paragraph_id": paragraph_id, "quote": "林夏穿蓝衣，两人在客栈相遇"}]
    extraction = modules.node.json.dumps({"assets": [{"local_id": "lin", "existing_id": "林夏",
                                                     "kind": "character", "source_refs": refs}]})
    storyboard = modules.node.json.dumps({"shots": [{"local_id": "s1", "summary": "客栈",
        "source_refs": refs, "scene_id": "客栈", "participants": [], "image_prompt": "客栈空镜"}]})
    job = modules.store.add_job(project["id"], {"kind": "text", "chapter_id": project["chapters"][0]["id"],
        "model": "gpt-5.6-terra", "style": "写实", "density": "精细", "status": "failed",
        "responses": [{"chunk": 0, "stage": "assets", "raw": extraction, "applied": False}]})
    async def request(model, system, content, on_progress=None):
        assert system == modules.analysis.SHOT_PROMPT
        return storyboard, {}
    monkeypatch.setattr(modules.analysis, "text_request", request)
    await modules.analysis.analyse(modules.store, job["id"])
    saved = modules.store.job(job["id"])
    assert saved["status"] == "succeeded", saved.get("error")
    assert [r["raw"] for r in saved["responses"]] == [extraction, storyboard]
    p = modules.store.get(project["id"])
    expected = [{"paragraph_id": paragraph_id, "quote": None}]
    assert p["shots"][-1]["source_refs"] == expected
    assert modules.domain.asset_by_id(p, "林夏")["suggestions"][-1]["source_refs"] == expected
    assert p["shots"][0]["source_refs"][0]["quote"] == "两人在客栈相遇"


@pytest.mark.parametrize(('atmosphere','expected'), [(None, ''), ('雨后薄雾', '雨后薄雾'), (['细雨', '暖灯'], '细雨，暖灯')])
def test_analysis_saves_optional_atmosphere_without_creating_image_assets(modules, project, atmosphere, expected):
    paragraph = project['chapters'][0]['paragraphs'][1]['id']
    draft = {'shots':[{'local_id':'new','summary':'客栈','source_refs':[{'paragraph_id':paragraph,'quote':None}],
        'scene_id':'客栈','participants':[],'image_prompt':'客栈空镜','atmosphere':atmosphere}]}
    modules.analysis.merge_shots(project,project['chapters'][0]['id'],draft,{paragraph},'analysis')
    assert project['shots'][-1]['atmosphere']==expected
    assert len(project['assets'])==5


@pytest.mark.parametrize("outside_chapter", [False, True])
def test_quote_fallback_does_not_accept_invalid_paragraphs(modules, project, outside_chapter):
    paragraph_id = project["chapters"][1]["paragraphs"][1]["id"] if outside_chapter else "invented"
    item = {"source_refs": [{"paragraph_id": paragraph_id, "quote": "改写的摘录"}]}
    modules.analysis.normalize_draft_sources(project, [item])
    allowed = {p["id"] for p in project["chapters"][0]["paragraphs"]}
    with pytest.raises(ValueError, match="不在本次正文"):
        modules.analysis.merge_assets(project, {"assets": [{"kind": "scene", **item}]}, allowed)


async def test_native_analysis_surfaces_original_failure(modules, project):
    native = sys.modules[modules.node.__name__ + ".storyboard.native"]
    key = modules.domain.digest([project["source"]["hash"], project["chapters"][0]["id"], "gpt-5.6-terra", "写实", "精细", 0])
    modules.store.add_job(project["id"], {"kind": "text", "analysis_key": key, "status": "failed", "error": "文本接口 HTTP 503"})
    with pytest.raises(ValueError, match="文本接口 HTTP 503"):
        await native.analyse_native(modules.store, project["id"], 1, "gpt-5.6-terra", "写实", "精细", 0)


async def test_invalid_draft_reference_finishes_with_failure(modules, project):
    raw = modules.node.json.dumps({"assets": [{"kind": "scene", "source_refs": ["invalid"]}]})
    job = modules.store.add_job(project["id"], {"kind": "text", "chapter_id": project["chapters"][0]["id"],
        "responses": [{"chunk": 0, "stage": "assets", "raw": raw, "applied": False}]})
    await modules.analysis.analyse(modules.store, job["id"])
    assert modules.store.job(job["id"])["status"] == "failed"
    assert "原文定位" in modules.store.job(job["id"])["error"]


@pytest.mark.parametrize("limit", [1, 3])
async def test_frame_queue_limits_concurrency_and_releases_loop(modules, limit):
    queue = modules.node.FrameQueue(limit)
    running = 0
    peak = 0
    async def work():
        nonlocal running, peak
        async with queue.slot():
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.01)
            running -= 1
    await asyncio.gather(*(work() for _ in range(6)))
    assert peak == limit
    assert queue.semaphore is None and queue.waiters == 0


async def test_frame_queue_cancellation_releases_waiting_and_active_slots(modules):
    queue = modules.node.FrameQueue(1)
    entered = asyncio.Event()
    async def work():
        async with queue.slot():
            entered.set()
            await asyncio.Event().wait()
    running = asyncio.create_task(work())
    await entered.wait()
    waiting = asyncio.create_task(work())
    await asyncio.sleep(0)
    waiting.cancel()
    running.cancel()
    await asyncio.gather(waiting, running, return_exceptions=True)
    assert queue.semaphore is None and queue.waiters == 0
    async with queue.slot():
        assert queue.waiters == 1


def test_merging_aliases_preserves_locked_versions(modules, project):
    duplicate = copy.deepcopy(modules.domain.asset_by_id(project, "客栈"))
    duplicate.update(id="旅店", name="旅店", current_version_id="旅店1")
    duplicate["versions"][0]["id"] = "旅店1"
    project["assets"].append(duplicate)
    shot = project["shots"][0]
    shot["scene"] = modules.domain.binding(project, "旅店", "locked", "旅店1")
    modules.domain.merge_asset(project, "旅店", "客栈")
    assert shot["scene"]["asset_id"] == "客栈"
    assert shot["scene"]["version_id"] == "旅店1"
    assert duplicate["redirect_to"] == "客栈"
    assert "旅店" in modules.domain.asset_by_id(project, "客栈")["aliases"]
    modules.domain.validate_shot(project, shot)

def test_native_canvas_wires_draft_assets_and_separates_story(modules, project):
    native = sys.modules[modules.node.__name__ + '.storyboard.native']
    second = copy.deepcopy(project['shots'][0])
    second.update(id='shot2', story_parent_id='shot1', story_relation='对话回应')
    project['shots'].append(second)
    recipe = native.native_canvas(modules.store, project, project['chapters'][0]['id'])
    assert len(recipe['nodes']) == 9
    refs = [l for l in recipe['links'] if l['to'] == 'shot2' and l['type'] == 'IMAGE']
    assert len(refs) == 3
    assert {l['from'] for l in refs} == {'蓝衣', '黑衣', '客栈'}
    story = next(l for l in recipe['links'] if l['to'] == 'shot2' and l['type'] == 'ASTROGATE_STORY')
    assert story['from'] == 'shot1' and story['slot'] == 1
    assert all(l['from'] != 'shot1' for l in refs)
    assert all(n['widgets']['resolution'] == '4K' and n['widgets']['quality'] == 'high' for n in recipe['nodes'])


def test_native_canvas_places_nested_props_beyond_look_and_scene(modules, project):
    native = sys.modules[modules.node.__name__ + '.storyboard.native']
    for name, parent in [('皮包', '黑衣'), ('皮包挂饰', '皮包'), ('木桌', '客栈'), ('油灯', '木桌')]:
        project['assets'].append({'id': name, 'name': name, 'kind': 'prop', 'parent_id': parent,
            'current_version_id': name + '1', 'versions': [{'id': name + '1', 'description': name, 'image': None}]})
    project['shots'][0]['props'] = [modules.domain.binding(project, name) for name in ('皮包挂饰', '油灯')]
    recipe = native.native_canvas(modules.store, project, project['chapters'][0]['id'])
    positions = {n['id']: n['pos'] for n in recipe['nodes']}
    assert len(set(map(tuple, positions.values()))) == len(positions)
    for parent, child in [('周野', '黑衣'), ('黑衣', '皮包'), ('皮包', '皮包挂饰'), ('客栈', '木桌'), ('木桌', '油灯')]:
        assert positions[parent][0] < positions[child][0]
        assert any(link['from'] == parent and link['to'] == child and link['type'] == 'IMAGE' for link in recipe['links'])
    assert positions['shot1'][0] >= max(pos[0] for key, pos in positions.items() if key != 'shot1') + 460


def test_character_pairs_have_stable_ids_layout_and_visible_cast_only(modules, project):
    native = sys.modules[modules.node.__name__ + '.storyboard.native']
    project['settings']['aspect_ratio'] = '2.39:1'
    hidden = copy.deepcopy(project['assets'][0])
    hidden.update(id='mentioned',name='未出场的人')
    project['assets'].append(hidden)
    recipe = native.native_canvas(modules.store, project, project['chapters'][0]['id'])
    by_id = {n['id']:n for n in recipe['nodes']}
    assert 'mentioned' not in by_id
    for name in ('林夏','周野'):
        front, left = by_id[name], by_id[name+'::left']
        assert front['pos'][1] == left['pos'][1] and left['pos'][0] > front['pos'][0]
        assert front['widgets']['aspect_ratio'] == left['widgets']['aspect_ratio'] == '2:3'
        assert front['properties']['astrogate_prompt_folded']
        assert '正视' in front['widgets']['prompt'] and '左视' in left['widgets']['prompt']
        assert any(l['from']==name and l['to']==name+'::left' and l['type']=='IMAGE' for l in recipe['links'])
    assert all(n['widgets']['aspect_ratio']=='2.39:1' for n in recipe['nodes'] if n['properties']['astrogate_kind']!='character')
    assert len({tuple(n['pos']) for n in recipe['nodes']}) == len(recipe['nodes'])


def test_shot_uses_each_visible_characters_look_or_front_and_look_uses_both_views(modules, project):
    native = sys.modules[modules.node.__name__ + '.storyboard.native']
    project['shots'][0]['participants'][1]['look'] = None
    recipe = native.native_canvas(modules.store, project, project['chapters'][0]['id'])
    shot_refs = [link['from'] for link in recipe['links'] if link['to']=='shot1' and link['type']=='IMAGE']
    assert shot_refs == ['客栈', '蓝衣', '周野']
    look_refs = [link['from'] for link in recipe['links'] if link['to']=='蓝衣' and link['type']=='IMAGE']
    assert look_refs == ['林夏', '林夏::left']
    assert '周野::left' not in shot_refs and '林夏' not in shot_refs


def test_character_and_look_requests_require_clean_background_without_affecting_shots(modules, project):
    native = sys.modules[modules.node.__name__ + '.storyboard.native']
    bodies = [('林夏','asset','front'), ('林夏','asset','left'), ('蓝衣','asset',''), ('客栈','asset',''), ('shot1','shot','')]
    jobs = [native.prepare_native(modules.store, project, {'owner_id':owner, 'owner_type':kind,
        'view':view, 'prompt':'原提示词', 'settings':{}}) for owner,kind,view in bodies]
    for job in jobs[:3]:
        assert '人物设定参考图：' in job['prompt'] and '干净的纯白或浅灰单色' in job['prompt']
        assert '其他人物' in job['prompt'] and '无场景' in job['prompt']
    assert all('人物设定参考图：' not in job['prompt'] for job in jobs[3:])


async def test_prop_request_keeps_story_context_but_only_draws_the_object(modules, project, monkeypatch, tmp_path):
    native = sys.modules[modules.node.__name__ + '.storyboard.native']
    description = '一盏点燃的铜制油灯，初始置于窗边木桌上；后文位于林夏与周野之间。'
    prop = {'id': '油灯', 'name': '铜制油灯', 'kind': 'prop', 'parent_id': None, 'aliases': [],
            'source_refs': project['shots'][0]['source_refs'], 'current_version_id': '油灯1',
            'versions': [{'id': '油灯1', 'description': description, 'image': None}]}
    project['assets'].append(prop)
    modules.store.change(project['id'], lambda p: p['assets'].append(copy.deepcopy(prop)))
    planned = modules.graph.plan(modules.store, project, [], asset_id='油灯', allow_missing=True)
    assert '独立物品设定参考图：' in planned['entries'][0]['prompt']
    job = native.prepare_native(modules.store, project, {'owner_id': '油灯', 'owner_type': 'asset',
        'prompt': description, 'settings': {'resolution': 'original'}})
    modules.store.update_job(job['id'], prompt=description)
    sent = []

    async def generate(payload, edit):
        sent.append(payload)
        data = BytesIO()
        Image.new('RGB', (16, 16), 'white').save(data, format='PNG')
        return {'data': [{'b64_json': modules.node.base64.b64encode(data.getvalue()).decode()}]}

    monkeypatch.setattr(modules.node, 'generate_image', generate)
    monkeypatch.setattr(modules.node.folder_paths, 'output_directory', str(tmp_path / 'output'))
    (tmp_path / 'output').mkdir()
    await modules.node.AstroGateStoryboardFrame.execute(description, job['id'], element_id='油灯')
    actual = sent[0]['prompt']
    assert actual.splitlines()[0] == '绘制任务：生成「铜制油灯」的单幅独立物品参考图，空背景。'
    assert '不是场景绘制指令' in actual
    assert description in actual
    assert '干净的纯白或浅灰单色' in actual and '无人物、手部、家具、场景、无关物品' in actual
    assert '不拼贴、不分屏' in actual and '前后剧情只用于理解物品，不画入画面' in actual
    assert modules.store.get(project['id'])['assets'][-1]['versions'][0]['description'] == description
    shot = copy.deepcopy(project['shots'][0])
    shot['props'] = [modules.domain.binding(project, '油灯')]
    frame = modules.graph.render_prompt(modules.graph.snapshot(project, shot), [])
    assert description in frame and '林夏' in frame and '周野' in frame
    assert '独立物品设定参考图：' not in frame


def test_front_and_left_jobs_do_not_conflict_or_reuse_each_other(modules, project):
    native = sys.modules[modules.node.__name__ + '.storyboard.native']
    body = {'owner_id':'林夏','owner_type':'asset','prompt':'正视','settings':{'aspect_ratio':'2:3'},'view':'front'}
    front = native.prepare_native(modules.store, project, body)
    left = native.prepare_native(modules.store, project, {**body,'view':'left','prompt':'左视',
        'references':[{'slot':'reference_images.reference_1','job_id':front['id'],'purpose':'林夏正视'}]})
    assert front['id'] != left['id']
    assert left['references'][0]['asset_view'] == 'front'
    assert left['snapshot']['shot']['asset_description'] == '林夏'
    with pytest.raises(ValueError,match='已有任务|已经有任务'):
        native.prepare_native(modules.store, project, body)


def test_scene_views_enforce_empty_environments_without_removing_story_cast(modules, project):
    native = sys.modules[modules.node.__name__ + '.storyboard.native']
    body = {'owner_id':'客栈','owner_type':'asset','prompt':'客栈大堂','settings':{'aspect_ratio':'16:9'}}
    base = native.prepare_native(modules.store, project, body)
    assert '禁止出现任何人物' in base['prompt']
    left = native.prepare_native(modules.store, project, {**body,'view':'scene_left','prompt':'左侧机位',
        'references':[{'slot':'reference_images.reference_1','job_id':base['id'],'purpose':'场景基准'}]})
    assert left['id'] != base['id'] and left['asset_view']=='scene_left'
    assert left['references'][0]['job_id']==base['id']
    assert '参考图中即使有人也必须移除' in left['prompt']
    frame = modules.graph.render_prompt(modules.graph.snapshot(project,project['shots'][0]),[])
    assert '禁止出现任何人物' not in frame
    assert '林夏' in frame and '周野' in frame


def test_reference_purpose_does_not_mix_style_with_character_identity(modules, project):
    native = sys.modules[modules.node.__name__ + '.storyboard.native']
    character = native.prepare_native(modules.store, project, {'owner_id':'林夏','owner_type':'asset',
        'prompt':'正视','settings':{},'view':'front'})
    job = native.prepare_native(modules.store, project, {'owner_id':'shot1','owner_type':'shot',
        'prompt':'两人在客栈相遇','settings':{},'references':[
            {'slot':'reference_images.reference_1','job_id':character['id'],'role':'style','purpose':'只参考画风与色彩，不参考人物身份'},
            {'slot':'reference_images.reference_2','job_id':character['id'],'role':'character','purpose':'林夏身份'},
        ]})
    style, identity = job['references']
    assert style['role']=='style' and 'character_id' not in style and 'asset_view' not in style
    assert identity['role']=='character' and identity['character_id']=='林夏' and identity['asset_view']=='front'
    assert '图1：只参考画风与色彩' in job['prompt']
    assert '图2：林夏身份' in job['prompt']
    buffer = BytesIO()
    Image.new('RGB',(8,8),'blue').save(buffer,format='PNG')
    image = {'b64_json':modules.node.base64.b64encode(buffer.getvalue()).decode()}
    packed, purposes = modules.node.pack_character_views([image,image],job['references'])
    assert len(packed)==2 and [p['role'] for p in purposes]==['style','character']


def test_editing_atmosphere_preserves_old_job_and_changes_only_the_shot(modules, project):
    native = sys.modules[modules.node.__name__ + '.storyboard.native']
    body = {'owner_id':'shot1','owner_type':'shot','prompt':'两人在客栈相遇','settings':{},'atmosphere':'窗外细雨'}
    first = native.prepare_native(modules.store, project, body)
    modules.store.update_job(first['id'], status='succeeded')
    assert native.prepare_native(modules.store, modules.store.get(project['id']), body)['id']==first['id']
    second = native.prepare_native(modules.store, modules.store.get(project['id']), {**body,'atmosphere':'夕阳与尘埃'})
    assert second['id'] != first['id']
    assert '窗外细雨' in modules.store.job(first['id'])['prompt']
    assert '夕阳与尘埃' in second['prompt']
    saved = modules.store.get(project['id'])
    assert saved['shots'][0]['atmosphere']=='夕阳与尘埃'
    assert len(saved['assets'])==len(project['assets'])


async def test_global_style_changes_without_reanalysis_and_marks_frames_stale(modules, project, monkeypatch, aiohttp_client):
    service = sys.modules[modules.node.__name__ + '.storyboard.service']
    modules.store.change(project['id'],lambda p:p['shots'][0].update(stale=False))
    routes = web.RouteTableDef()
    monkeypatch.setattr(service,'get_store',lambda:modules.store)
    monkeypatch.setattr(service,'PromptServer',SimpleNamespace(instance=SimpleNamespace(routes=routes,
        user_manager=SimpleNamespace(get_request_user_id=lambda request:'default'))))
    service.register_routes()
    app=web.Application()
    app.add_routes(routes)
    client=await aiohttp_client(app)
    response=await client.post('/astrogate/storyboard/projects/'+project['id']+'/edit',
        json={'action':'frame_aspect_ratio','aspect_ratio':'16:9','style':'水彩绘本'})
    assert response.status==200,await response.text()
    saved=modules.store.get(project['id'])
    assert saved['settings']['style']=='水彩绘本' and saved['shots'][0]['stale']
    assert not modules.store.jobs(project['id'])
    assert '画风：水彩绘本' in modules.graph.render_prompt(modules.graph.snapshot(saved,saved['shots'][0]),[])


async def test_image_recovery_requires_unknown_job_and_project_owner(modules, project, monkeypatch, aiohttp_client):
    service = sys.modules[modules.node.__name__ + '.storyboard.service']
    unknown = modules.store.add_job(project['id'], {'kind':'image','status':'unknown'})
    complete = modules.store.add_job(project['id'], {'kind':'image','status':'succeeded'})
    text = modules.store.add_job(project['id'], {'kind':'text','status':'unknown'})
    calls = []
    async def recover(store, job):
        calls.append((store,job['id']))
        return {'status':'pending'}
    def create_app(callback):
        routes = web.RouteTableDef()
        monkeypatch.setattr(service,'PromptServer',SimpleNamespace(instance=SimpleNamespace(routes=routes,
            user_manager=SimpleNamespace(get_request_user_id=lambda request:request.headers.get('X-Test-User','default')))))
        monkeypatch.setattr(service,'get_store',lambda:modules.store)
        service.register_routes(callback)
        app = web.Application()
        app.add_routes(routes)
        return app
    client = await aiohttp_client(create_app(recover))
    response = await client.post('/astrogate/storyboard/jobs/'+unknown['id']+'/recover')
    assert response.status==200 and (await response.json())['status']=='pending'
    assert calls==[(modules.store,unknown['id'])]
    for job in (complete,text):
        response = await client.post('/astrogate/storyboard/jobs/'+job['id']+'/recover')
        assert response.status==400
    response = await client.post('/astrogate/storyboard/jobs/'+unknown['id']+'/recover',headers={'X-Test-User':'other'})
    assert response.status==400 and len(calls)==1
    assert modules.store.job(unknown['id'])['status']=='unknown'
    unsupported = await aiohttp_client(create_app(None))
    response = await unsupported.post('/astrogate/storyboard/jobs/'+unknown['id']+'/recover')
    assert response.status==400 and '不支持' in (await response.json())['error']


async def test_approving_character_views_preserves_locked_versions_and_reuses_the_pair(modules, project, monkeypatch, aiohttp_client):
    native = sys.modules[modules.node.__name__ + '.storyboard.native']
    service = sys.modules[modules.node.__name__ + '.storyboard.service']
    body = {'owner_id':'林夏','owner_type':'asset','prompt':'正视','settings':{'aspect_ratio':'2:3'},'view':'front'}
    front = native.prepare_native(modules.store, project, body)
    front_image = {'file':'astrogate/front.png','hash':'front'}
    modules.store.update_job(front['id'],status='succeeded',result_image=front_image)
    left = native.prepare_native(modules.store, project, {**body,'view':'left','prompt':'左视',
        'references':[{'slot':'reference_images.reference_1','job_id':front['id'],'purpose':'正视'}]})
    left_image = {'file':'astrogate/left.png','hash':'left'}
    modules.store.update_job(left['id'],status='succeeded',result_image=left_image)
    def lock(p):
        shot = copy.deepcopy(p['shots'][0])
        shot.update(id='locked',stale=False)
        shot['participants'][0]['identity']['policy']='locked'
        p['shots'].append(shot)
    modules.store.change(project['id'],lock)
    routes=web.RouteTableDef()
    monkeypatch.setattr(service,'get_store',lambda:modules.store)
    monkeypatch.setattr(service,'PromptServer',SimpleNamespace(instance=SimpleNamespace(routes=routes,
        user_manager=SimpleNamespace(get_request_user_id=lambda request:'default'))))
    service.register_routes()
    app=web.Application()
    app.add_routes(routes)
    client=await aiohttp_client(app)
    for job in (front,left):
        response=await client.post('/astrogate/storyboard/projects/'+project['id']+'/edit',json={'action':'approve','job_id':job['id']})
        assert response.status==200,await response.text()
    saved=modules.store.get(project['id'])
    asset=modules.domain.asset_by_id(saved,'林夏')
    selected=modules.domain.version(asset)
    assert selected['view_images']=={'front':front_image,'left':left_image}
    assert asset['versions'][0]['image'] is None
    assert saved['shots'][1]['participants'][0]['identity']['version_id']=='林夏1'
    assert not saved['shots'][1]['stale']
    recipe=native.native_canvas(modules.store,saved,saved['chapters'][0]['id'])
    for view,key in [('front','林夏'),('left','林夏::left')]:
        node=next(n for n in recipe['nodes'] if n['id']==key)
        job=modules.store.job(node['widgets']['job_id'])
        assert job['asset_version_id']==selected['id'] and job['asset_view']==view
        assert job['result_image']==selected['view_images'][view]


@pytest.mark.parametrize('ratio', ['2.39:1','9：16',' 16:9 '])
def test_custom_frame_aspect_dimensions(modules, ratio):
    normalized = modules.domain.aspect_ratio(ratio)
    w, h = map(float, normalized.split(':'))
    width, height = modules.node.output_dimensions(ratio, '4K')
    assert max(width,height) == 3840
    assert abs(width / height - w / h) < 0.003


@pytest.mark.parametrize('ratio',['0:9','nan:1','1:inf','16','16:9:1'])
def test_invalid_frame_aspect_is_actionable(modules, ratio):
    with pytest.raises(ValueError,match='画幅'):
        modules.domain.aspect_ratio(ratio)


def test_character_reference_sheet_keeps_people_and_view_order_separate(modules):
    m = modules.node
    images=[]
    for color in ('red','blue','green'):
        buffer=BytesIO()
        Image.new('RGB',(10,20),color).save(buffer,format='PNG')
        images.append({'b64_json':m.base64.b64encode(buffer.getvalue()).decode()})
    refs=[{'asset_id':'a','version_id':'v1','asset_view':'left','purpose':'甲左视'},
          {'asset_id':'a','version_id':'v1','asset_view':'front','purpose':'甲正视'},
          {'asset_id':'b','version_id':'v2','asset_view':'left','purpose':'乙左视'}]
    packed, purposes=m.pack_character_views(images,refs)
    assert len(packed)==len(purposes)==2
    with Image.open(BytesIO(m.base64.b64decode(packed[0]['b64_json']))) as sheet:
        assert sheet.size==(20,20)
        assert sheet.getpixel((0,0))==(0,0,255) and sheet.getpixel((10,0))==(255,0,0)
    assert purposes[0]['asset_id']=='a' and purposes[1]['asset_id']=='b'
    assert '同一人物' in purposes[0]['purpose']


async def test_native_pair_workflow_uses_both_views_for_look_and_sends_looks_to_shot(modules, project, monkeypatch, tmp_path):
    m = modules.node
    native = sys.modules[m.__name__ + '.storyboard.native']
    recipe = native.native_canvas(modules.store, project, project['chapters'][0]['id'])
    jobs, prompt, completed = {}, {}, set()
    for node in recipe['nodes']:
        key = node['id']
        links = [l for l in recipe['links'] if l['to']==key and l['type']=='IMAGE']
        refs = [{'slot':l['input'],'job_id':jobs[l['from']]['id'],'purpose':l['from']} for l in links]
        jobs[key] = native.prepare_native(modules.store, project, {'owner_id':node['properties']['astrogate_owner'],
            'owner_type':node['properties']['astrogate_owner_type'],'view':node['properties']['astrogate_view'],
            'prompt':node['widgets']['prompt'],'settings':{'resolution':'original','quality':'high'},'references':refs})
        prompt[key] = {'class_type':'AstroGateStoryboardFrame','inputs':{'prompt':node['widgets']['prompt'],'job_id':jobs[key]['id'],
            **{l['input']:[l['from'],0] for l in links}}}
    by_request = {j['gateway_request_id']:key for key,j in jobs.items()}
    async def generate(payload, edit):
        key = by_request[payload['gateway_request_id']]
        if key.endswith('::left'):
            assert key.removesuffix('::left') in completed
            assert edit and len(payload['images'])==1
        if key in ('蓝衣','黑衣'):
            assert edit and len(payload['images'])==1
            assert '同一人物双视图图册' in payload['prompt']
        if key=='shot1':
            assert {'蓝衣','黑衣'} <= completed
            assert len(payload['images'])==3
            assert '同一人物双视图图册' not in payload['prompt']
        await asyncio.sleep(.01)
        data=BytesIO()
        Image.new('RGB',(16,24),'blue').save(data,format='PNG')
        completed.add(key)
        return {'data':[{'b64_json':m.base64.b64encode(data.getvalue()).decode()}]}
    monkeypatch.setattr(m,'generate_image',generate)
    monkeypatch.setattr(m,'PromptServer',SimpleNamespace(instance=SimpleNamespace(send_progress_text=lambda *args:None)))
    monkeypatch.setattr(m.folder_paths,'output_directory',str(tmp_path/'output'))
    (tmp_path/'output').mkdir()
    m.AstroGateStoryboardFrame.FINALIZE_SCHEMA()
    monkeypatch.setitem(nodes.NODE_CLASS_MAPPINGS,'AstroGateStoryboardFrame',m.AstroGateStoryboardFrame)
    events=[]
    server=SimpleNamespace(client_id=None,last_node_id=None,send_sync=lambda event,data,*args:events.append((event,data)))
    executor=PromptExecutor(server,cache_args={'ram':0,'ram_inactive':0})
    await executor.execute_async(prompt,'character-views',{'client_id':'test'},['shot1'])
    assert executor.success,events
    assert len(completed)==len(recipe['nodes'])


def test_prop_atlas_preserves_every_reference_with_numbers(modules):
    m=modules.node
    buffer=BytesIO()
    Image.new('RGB',(20,30),'red').save(buffer,format='PNG')
    image={'b64_json':m.base64.b64encode(buffer.getvalue()).decode()}
    refs=[{'purpose':f'prop-{i}','asset_kind':'prop','asset_id':str(i)} for i in range(10)]
    packed, purposes=m.pack_character_views([image]*10,refs)
    assert len(packed)==1 and len(purposes)==1
    assert all(f'{i+1}号：prop-{i}' in purposes[0]['purpose'] for i in range(10))
    with Image.open(BytesIO(m.base64.b64decode(packed[0]['b64_json']))) as atlas:
        assert atlas.width*atlas.height<=24_000_000


async def test_native_frame_orders_actual_inputs_and_durably_reuses(modules, project, monkeypatch, tmp_path):
    native = sys.modules[modules.node.__name__ + '.storyboard.native']
    m = modules.node
    monkeypatch.setattr(m.folder_paths, 'output_directory', str(tmp_path / 'output'))
    (tmp_path / 'output').mkdir()
    sent = []
    async def generate(payload, edit):
        sent.append(payload)
        data = BytesIO()
        Image.new('RGB', (16, 9), 'blue').save(data, format='PNG')
        return {'data': [{'b64_json': m.base64.b64encode(data.getvalue()).decode()}]}
    monkeypatch.setattr(m, 'generate_image', generate)
    job = native.prepare_native(modules.store, project, {'owner_id': 'shot1', 'owner_type': 'shot', 'prompt': '两人抬头', 'atmosphere': '窗外细雨与柔和暖光',
        'settings': {'resolution': '1K', 'quality': 'high', 'aspect_ratio': '16:9'},
        'references': [{'slot': 'reference_images.reference_1', 'purpose': '林夏身份'}, {'slot': 'reference_images.reference_2', 'purpose': '周野身份'}]})
    pending = m.AstroGateStoryboardFrame.check_lazy_status(job_id=job['id'], reference_images={'reference_1': (None, 'reference_images.reference_1')})
    assert pending == ['reference_images.reference_1']
    # Deliberately reverse dictionary insertion; socket order must win.
    inputs = {'reference_2': torch.ones(1, 8, 8, 3), 'reference_1': torch.zeros(1, 8, 8, 3)}
    output = await m.AstroGateStoryboardFrame.execute('两人抬头', job['id'], inputs, element_id='shot1')
    assert output.result[1]['element_id'] == 'shot1'
    assert sent[0]['prompt'].index('图1：林夏身份') < sent[0]['prompt'].index('图2：周野身份')
    assert '本镜特效与氛围：窗外细雨与柔和暖光' in sent[0]['prompt']
    assert modules.store.get(project['id'])['shots'][0]['atmosphere'] == '窗外细雨与柔和暖光'
    saved_project = modules.store.get(project['id'])
    recipe = native.native_canvas(modules.store, saved_project, saved_project['chapters'][0]['id'])
    shot = next(n for n in recipe['nodes'] if n['id'] == 'shot1')
    assert shot['properties']['astrogate_atmosphere'] == '窗外细雨与柔和暖光'
    saved = modules.store.job(job['id'])
    assert all(r['image']['hash'] for r in saved['references'])
    assert m.AstroGateStoryboardFrame.check_lazy_status(job_id=job['id'], reference_images={'reference_1': (None, 'reference_images.reference_1')}) == []
    await m.AstroGateStoryboardFrame.execute('两人抬头', job['id'], element_id='shot1')
    assert len(sent) == 1


@pytest.mark.parametrize('status', ['succeeded','pending','missing','expired','unavailable'])
async def test_recover_result_only_queries_gateway_without_generating(modules, project, monkeypatch, tmp_path, aiohttp_server, status):
    native = sys.modules[modules.node.__name__ + '.storyboard.native']
    m = modules.node
    job = native.prepare_native(modules.store, project, {'owner_id':'shot1','owner_type':'shot',
        'prompt':'两人在客栈相遇','settings':{'resolution':'original'},'references':[]})
    modules.store.update_job(job['id'],status='unknown',error='结果未知')
    buffer=BytesIO()
    Image.new('RGB',(16,9),'blue').save(buffer,format='PNG')
    calls=[]
    async def lookup(request):
        calls.append((request.method,request.match_info['request_id']))
        assert request.headers['Authorization']=='Bearer test-only-image'
        return web.json_response({'status':status,'result':{'data':[{'b64_json':m.base64.b64encode(buffer.getvalue()).decode()}]}})
    app=web.Application()
    app.router.add_get('/images/jobs/{request_id}',lookup)
    server=await aiohttp_server(app)
    monkeypatch.setattr(m,'API_BASE',str(server.make_url('/')).rstrip('/'))
    monkeypatch.setattr(m.folder_paths,'output_directory',str(tmp_path/'output'))
    (tmp_path/'output').mkdir()
    result=await m.recover_storyboard_image(modules.store,modules.store.job(job['id']))
    assert result['status']==status and calls==[('GET',job['gateway_request_id'])]
    saved=modules.store.job(job['id'])
    if status=='succeeded':
        assert saved['status']=='succeeded' and saved['error'] is None
        assert (tmp_path/'output'/result['saved_result']['subfolder']/result['saved_result']['filename']).is_file()
        assert modules.store.image_path(saved['result_image']['file']).is_file()
        assert modules.store.get(project['id'])['shots'][0]['latest_job_id']==job['id']
    else:
        assert saved['status']=='unknown' and 'saved_result' not in saved


async def test_missing_saved_image_can_be_recovered_without_regenerating(modules, project, monkeypatch):
    native = sys.modules[modules.node.__name__ + '.storyboard.native']
    m = modules.node
    job = native.prepare_native(modules.store, project, {'owner_id':'shot1','owner_type':'shot',
        'prompt':'两人在客栈相遇','settings':{},'references':[]})
    modules.store.update_job(job['id'],status='succeeded',result_image={'file':'astrogate/missing.png'},
                             saved_result={'filename':'missing.png','subfolder':'','type':'output'})
    async def fail_if_called(*args):
        raise AssertionError('The missing cache must not trigger image generation')
    monkeypatch.setattr(m,'generate_image',fail_if_called)
    await m.AstroGateStoryboardFrame.execute('两人在客栈相遇',job['id'],element_id='shot1')
    saved=modules.store.job(job['id'])
    assert saved['status']=='unknown' and '查询并取回' in saved['error']


async def test_native_analysis_reuses_result_without_text_call(modules, project, monkeypatch):
    native = sys.modules[modules.node.__name__ + '.storyboard.native']
    calls = []
    async def analyse(store, job_id):
        calls.append(job_id)
        store.update_job(job_id, status='succeeded')
    monkeypatch.setattr(native, 'analyse', analyse)
    first = await native.analyse_native(modules.store, project['id'], 1, 'gpt-5.6-terra', '写实', '精细', 0)
    assert first == await native.analyse_native(modules.store, project['id'], 1, 'gpt-5.6-terra', '写实', '精细', 0)
    assert len(calls) == 1
    await native.analyse_native(modules.store, project['id'], 1, 'gpt-5.6-terra', '写实', '精细', 1)
    assert len(calls) == 2


def test_native_unknown_requires_explicit_redraw(modules, project):
    native = sys.modules[modules.node.__name__ + '.storyboard.native']
    body = {'owner_id': 'shot1', 'owner_type': 'shot', 'prompt': '相遇', 'settings': {}, 'references': []}
    job = native.prepare_native(modules.store, project, body)
    modules.store.update_job(job['id'], status='unknown')
    with pytest.raises(ValueError, match='未知'):
        native.prepare_native(modules.store, project, body)
    assert native.prepare_native(modules.store, project, {**body, 'force': True})['id'] != job['id']


async def test_native_cancel_stops_before_next_text_stage(modules, project, monkeypatch):
    job = modules.store.add_job(project['id'], {'kind': 'text', 'chapter_id': project['chapters'][0]['id'], 'model': 'gpt-5.6-terra', 'style': '写实', 'density': '精细'})
    calls = []
    async def text_request(model, system, content, on_progress=None):
        calls.append(system)
        modules.store.update_job(job['id'], cancel_requested=True)
        return '{"assets":[],"summary":"已提取"}', {}
    monkeypatch.setattr(modules.analysis, 'text_request', text_request)
    await modules.analysis.analyse(modules.store, job['id'])
    assert modules.store.job(job['id'])['status'] == 'cancelled'
    assert calls == [modules.analysis.EXTRACT_PROMPT]

@pytest.mark.parametrize("has_reference", [False, True])
async def test_real_executor_resolves_image_but_not_story_or_analysis(modules, project, monkeypatch, tmp_path, has_reference):
    native = sys.modules[modules.node.__name__ + '.storyboard.native']
    m = modules.node
    previous = copy.deepcopy(project['shots'][0])
    previous.update(id='earlier', summary='之前的对白')
    project['shots'].append(previous)
    save_reference(modules, project, '林夏', 'red')
    modules.store.change(project['id'], lambda p: p.update(assets=project['assets'], shots=project['shots']))
    asset = modules.domain.asset_by_id(project, '林夏')
    reference = native.local_asset_job(modules.store, project, asset, modules.domain.version(asset)['image'])
    job = native.prepare_native(modules.store, project, {'owner_id':'shot1', 'owner_type':'shot', 'prompt':'两人相遇',
        'settings':{'resolution':'1K', 'quality':'high'}, 'story_parent_id':'earlier',
        'references':[{'slot':'reference_images.reference_1','purpose':'林夏身份','job_id':reference['id']}] if has_reference else []})
    requests = []
    async def generate(payload, edit):
        assert edit == has_reference
        assert len(payload.get('images', [])) == int(has_reference)
        requests.append(payload)
        data = BytesIO()
        Image.new('RGB', (16, 9), 'blue').save(data, format='PNG')
        return {'data':[{'b64_json':m.base64.b64encode(data.getvalue()).decode()}]}
    async def no_analysis(*args, **kwargs):
        raise AssertionError('剧情和脚本元数据不能触发文本请求')
    monkeypatch.setattr(m, 'generate_image', generate)
    monkeypatch.setattr(m.AstroGateStoryboard, 'execute', classmethod(no_analysis))
    monkeypatch.setattr(m, 'PromptServer', SimpleNamespace(instance=SimpleNamespace(send_progress_text=lambda *args: None)))
    monkeypatch.setattr(m.folder_paths, 'output_directory', str(tmp_path / 'output'))
    (tmp_path / 'output').mkdir()
    for cls in [m.AstroGateTXT, m.AstroGateStoryboard, m.AstroGateStoryboardFrame]:
        cls.FINALIZE_SCHEMA()
        monkeypatch.setitem(nodes.NODE_CLASS_MAPPINGS, cls.GET_SCHEMA().node_id, cls)
    prompt = {
        '1':{'class_type':'AstroGateTXT','inputs':{'project_id':project['id']}},
        '2':{'class_type':'AstroGateStoryboard','inputs':{'script':['1',0],'chapter':1,'text_model':'gpt-5.6-terra','style':'写实','density':'精细','analysis_revision':0}},
        '3':{'class_type':'AstroGateStoryboardFrame','inputs':{'prompt':'尚未画出的上一镜','job_id':'','element_id':'earlier'}},
        '4':{'class_type':'AstroGateStoryboardFrame','inputs':{'prompt':'两人相遇','job_id':job['id'],'element_id':'shot1','board':['2',0],'story_context':['3',1],'reference_images.reference_1':['5',0]}},
        '5':{'class_type':'AstroGateStoryboardFrame','inputs':{'prompt':reference['snapshot']['shot']['image_prompt'],'job_id':reference['id'],'element_id':'林夏'}},
    }
    if not has_reference:
        del prompt['4']['inputs']['reference_images.reference_1']
    events = []
    server = SimpleNamespace(client_id=None, last_node_id=None, send_sync=lambda event, data, *args: events.append((event,data)))
    executor = PromptExecutor(server, cache_args={'ram':0,'ram_inactive':0})
    await executor.execute_async(prompt, 'native-test', {'client_id':'test'}, ['4'])
    assert executor.success, events
    assert len(requests) == 1
    assert set(executor.history_result['outputs']) == ({'4','5'} if has_reference else {'4'})

async def test_native_executor_parallel_images_wait_for_their_references(modules, project, monkeypatch, tmp_path):
    m = modules.node
    native = sys.modules[m.__name__ + '.storyboard.native']
    project['shots'] = [{**copy.deepcopy(project['shots'][0]), 'id':name} for name in ('a', 'b', 'c', 'target')]
    modules.store.change(project['id'], lambda p: p.update(shots=project['shots']))
    jobs, prompt = {}, {}
    for name in ('a', 'b', 'c', 'target'):
        refs = [{'slot':f'reference_images.reference_{i}', 'purpose':parent, 'job_id':jobs[parent]['id']}
                for i, parent in enumerate(('a', 'b'), 1)] if name == 'target' else []
        jobs[name] = native.prepare_native(modules.store, project, {'owner_id':name, 'owner_type':'shot',
            'prompt':name, 'settings':{'resolution':'original', 'quality':'high'}, 'references':refs})
        inputs = {'prompt':name, 'job_id':jobs[name]['id'], 'element_id':name}
        if name == 'target':
            inputs.update({'reference_images.reference_1':['a',0], 'reference_images.reference_2':['b',0]})
        prompt[name] = {'class_type':'AstroGateStoryboardFrame', 'inputs':inputs}
    all_started = asyncio.Event()
    running, peak = 0, 0
    finished = set()
    names = {job['gateway_request_id']:name for name, job in jobs.items()}
    async def generate(payload, edit):
        nonlocal running, peak
        name = names[payload['gateway_request_id']]
        if name == 'target':
            assert {'a','b'} <= finished
            assert edit and len(payload['images']) == 2
        running += 1
        peak = max(peak, running)
        if running == 2:
            all_started.set()
        await asyncio.wait_for(all_started.wait(), 2)
        await asyncio.sleep(0.01)
        data = BytesIO()
        Image.new('RGB', (16,9), 'blue').save(data, format='PNG')
        running -= 1
        finished.add(name)
        return {'data':[{'b64_json':m.base64.b64encode(data.getvalue()).decode()}]}
    monkeypatch.setattr(m, 'frame_queue', m.FrameQueue(2))
    monkeypatch.setattr(m, 'generate_image', generate)
    monkeypatch.setattr(m, 'PromptServer', SimpleNamespace(instance=SimpleNamespace(send_progress_text=lambda *args: None)))
    monkeypatch.setattr(m.folder_paths, 'output_directory', str(tmp_path / 'output'))
    (tmp_path / 'output').mkdir()
    m.AstroGateStoryboardFrame.FINALIZE_SCHEMA()
    monkeypatch.setitem(nodes.NODE_CLASS_MAPPINGS, 'AstroGateStoryboardFrame', m.AstroGateStoryboardFrame)
    events = []
    server = SimpleNamespace(client_id=None, last_node_id=None, send_sync=lambda event,data,*args:events.append((event,data)))
    executor = PromptExecutor(server, cache_args={'ram':0,'ram_inactive':0})
    await executor.execute_async(prompt, 'parallel-images', {'client_id':'test'}, ['a','b','c','target'])
    assert executor.success, events
    assert peak == 2 and finished == {'a','b','c','target'}
    assert all(modules.store.job(job['id'])['status'] == 'succeeded' for job in jobs.values())
    assert len({modules.store.job(job['id'])['result_image']['file'] for job in jobs.values()}) == 4


@pytest.mark.parametrize('failure', ['before_request', 'unknown'])
async def test_failed_native_image_blocks_only_dependent_branch(modules, project, monkeypatch, tmp_path, aiohttp_client, failure):
    m = modules.node
    native = sys.modules[m.__name__ + '.storyboard.native']
    service = sys.modules[m.__name__ + '.storyboard.service']
    project['shots'] = [{**copy.deepcopy(project['shots'][0]), 'id':name} for name in ('bad','dependent','independent','story')]
    modules.store.change(project['id'],lambda p:p.update(shots=project['shots']))
    jobs, prompt = {}, {}
    for name in ('bad','dependent','independent','story'):
        refs = [{'slot':'reference_images.reference_1','purpose':'出错的参考图','job_id':jobs['bad']['id']}] if name=='dependent' else []
        body = {'owner_id':name,'owner_type':'shot','prompt':name,'settings':{'resolution':'original'},'references':refs}
        if name=='story':
            body['story_parent_id']='bad'
        jobs[name] = native.prepare_native(modules.store,project,body)
        inputs = {'prompt':name,'job_id':jobs[name]['id'],'element_id':name}
        if name=='dependent':
            inputs['reference_images.reference_1']=['bad',0]
        if name=='story':
            inputs['story_context']=['bad',1]
        if name=='bad' and failure=='before_request':
            inputs['prompt']='已修改的提示词'
        prompt[name] = {'class_type':'AstroGateStoryboardFrame','inputs':inputs}
    sent = []
    names = {job['gateway_request_id']:name for name,job in jobs.items()}
    async def generate(payload, edit):
        name = names[payload['gateway_request_id']]
        sent.append(name)
        if name=='bad':
            raise RuntimeError('图片请求超时')
        buffer = BytesIO()
        Image.new('RGB',(16,9),'blue').save(buffer,format='PNG')
        return {'data':[{'b64_json':m.base64.b64encode(buffer.getvalue()).decode()}]}
    monkeypatch.setattr(m,'generate_image',generate)
    monkeypatch.setattr(m,'PromptServer',SimpleNamespace(instance=SimpleNamespace(send_progress_text=lambda *args:None)))
    monkeypatch.setattr(m.folder_paths,'output_directory',str(tmp_path/'output'))
    (tmp_path/'output').mkdir()
    m.AstroGateStoryboardFrame.FINALIZE_SCHEMA()
    monkeypatch.setitem(nodes.NODE_CLASS_MAPPINGS,'AstroGateStoryboardFrame',m.AstroGateStoryboardFrame)
    events=[]
    server=SimpleNamespace(client_id=None,last_node_id=None,send_sync=lambda event,data,*args:events.append((event,data)))
    executor=PromptExecutor(server,cache_args={'ram':0,'ram_inactive':0})
    await executor.execute_async(prompt,'branch-failure',{'client_id':'test'},['dependent','independent','story'])
    assert executor.success,events
    bad = modules.store.job(jobs['bad']['id'])
    assert bad['status']==('failed' if failure=='before_request' else 'unknown')
    assert sent.count('bad') == (0 if failure=='before_request' else 1)
    assert 'dependent' not in sent and {'independent','story'} <= set(sent)
    assert modules.store.job(jobs['independent']['id'])['status']=='succeeded'
    assert modules.store.job(jobs['story']['id'])['status']=='succeeded'
    dependent = modules.store.job(jobs['dependent']['id'])
    assert dependent['status']=='prepared'
    blocked_events = [(event, data) for event, data in events if event=='execution_error' and data.get('exception_type')=='ExecutionBlocked']
    assert any(data['node_id']=='dependent' for _, data in blocked_events)
    history_entry = {'prompt':[0,'branch-failure',prompt],'status':{'messages':blocked_events}}
    state, reason = service.queue_failure(dependent,history_entry, {bad['id']:bad})
    assert state=='blocked' and '出错的参考图' in reason
    state, reason = service.queue_failure({'id':'unrelated'},history_entry)
    assert state=='blocked' and 'dependent' not in reason and 'Execution Blocked' not in reason
    state, reason = service.queue_failure(dependent,history_entry)
    assert state=='blocked' and '参考图' in reason
    modules.store.update_job(dependent['id'],prompt_id='branch-failure')
    routes = web.RouteTableDef()
    history = {'branch-failure':history_entry}
    monkeypatch.setattr(service,'get_store',lambda:modules.store)
    monkeypatch.setattr(service,'PromptServer',SimpleNamespace(instance=SimpleNamespace(routes=routes,
        user_manager=SimpleNamespace(get_request_user_id=lambda request:'default'),
        prompt_queue=SimpleNamespace(get_history=lambda:history))))
    service.register_routes()
    app = web.Application()
    app.add_routes(routes)
    client = await aiohttp_client(app)
    response = await client.get('/astrogate/storyboard/projects/'+project['id']+'?summary=1')
    assert response.status==200
    state = {job['id']:job for job in (await response.json())['jobs']}
    assert state[dependent['id']]['status']=='blocked' and '出错的参考图' in state[dependent['id']]['error']
    assert state[bad['id']]['status']==bad['status']
    assert state[jobs['independent']['id']]['status']=='succeeded'


def test_native_locked_look_keeps_its_own_image_node(modules, project):
    native = sys.modules[modules.node.__name__ + '.storyboard.native']
    locked = copy.deepcopy(project['shots'][0])
    locked['id'] = 'locked-shot'
    locked['participants'][0]['look']['policy'] = 'locked'
    project['shots'].append(locked)
    modules.domain.publish_version(project, '蓝衣', '蓝衣加银色饰品')
    recipe = native.native_canvas(modules.store, project, project['chapters'][0]['id'])
    old = next(n for n in recipe['nodes'] if n['id'] == '蓝衣@蓝衣1')
    assert old['properties']['astrogate_locked']
    assert old['widgets']['prompt'] == '蓝衣'
    assert next(n for n in recipe['nodes'] if n['id'] == '蓝衣')['widgets']['prompt'] == '蓝衣加银色饰品'
    old_links = [l for l in recipe['links'] if l['to']=='locked-shot' and l['type']=='IMAGE']
    assert any(l['from']=='蓝衣@蓝衣1' for l in old_links)
    assert not any(l['from']=='蓝衣' for l in old_links)

@pytest.mark.parametrize('owner,status', [('failing-job','failed'),('later-job','blocked')])
def test_queue_failure_names_the_actual_failing_node(modules, owner, status):
    service = sys.modules[modules.node.__name__ + '.storyboard.service']
    history = {'prompt':[0,'prompt-id',{'86':{'inputs':{'job_id':'failing-job'},'_meta':{'title':'红伞'}}}],
               'status':{'messages':[['execution_error',{'node_id':'86','exception_message':"'tuple' object has no attribute 'values'\n"}]]}}
    actual, message = service.queue_failure({'id':owner}, history)
    assert actual == status
    assert '红伞' in message and 'tuple' in message
    assert '上游未完成' not in message


def test_queue_cancel_is_not_reported_as_upstream_failure(modules):
    service = sys.modules[modules.node.__name__ + '.storyboard.service']
    history = {'status':{'messages':[['execution_interrupted',{}]]}}
    assert service.queue_failure({'id':'job'}, history)[0] == 'cancelled'
