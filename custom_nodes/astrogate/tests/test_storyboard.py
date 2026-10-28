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
    text, usage = await modules.analysis.text_request("gpt-5.6-terra", "system", {})
    assert modules.node.json.loads(text) == {"summary": "雨夜相遇"}
    assert usage == {"completion_tokens": 12}
    assert len(calls) == 1


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
    async def first(model, system, content):
        calls.append(system)
        if system == modules.analysis.EXTRACT_PROMPT:
            return modules.node.json.dumps({"assets": [{"local_id": "lin", "existing_id": "林夏", "kind": "character", "source_refs": ref}]}), {}
        raise RuntimeError("远端结果未知")
    monkeypatch.setattr(modules.analysis, "text_request", first)
    job = modules.store.add_job(project["id"], {"kind": "text", "chapter_id": project["chapters"][0]["id"],
                                               "model": "gpt-6-luna", "style": "写实", "density": "自然拆镜"})
    await modules.analysis.analyse(modules.store, job["id"])
    assert modules.store.job(job["id"])["status"] == "unknown"

    async def resume(model, system, content):
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
    async def request(model, system, content):
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


async def test_frame_queue_serializes_requests_and_releases_loop(modules):
    queue = modules.node.FrameQueue()
    running = 0
    peak = 0
    async def work():
        nonlocal running, peak
        async with queue.slot():
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.01)
            running -= 1
    await asyncio.gather(work(), work(), work())
    assert peak == 1
    assert queue.lock is None and queue.waiters == 0


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
    assert len(recipe['nodes']) == 7
    refs = [l for l in recipe['links'] if l['to'] == 'shot2' and l['type'] == 'IMAGE']
    assert len(refs) == 5
    assert {l['from'] for l in refs} == {'林夏', '蓝衣', '周野', '黑衣', '客栈'}
    story = next(l for l in recipe['links'] if l['to'] == 'shot2' and l['type'] == 'ASTROGATE_STORY')
    assert story['from'] == 'shot1' and story['slot'] == 1
    assert all(l['from'] != 'shot1' for l in refs)
    assert all(n['widgets']['resolution'] == '4K' and n['widgets']['quality'] == 'high' for n in recipe['nodes'])


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
    job = native.prepare_native(modules.store, project, {'owner_id': 'shot1', 'owner_type': 'shot', 'prompt': '两人抬头',
        'settings': {'resolution': '1K', 'quality': 'high', 'aspect_ratio': '16:9'},
        'references': [{'slot': 'reference_images.reference_1', 'purpose': '林夏身份'}, {'slot': 'reference_images.reference_2', 'purpose': '周野身份'}]})
    pending = m.AstroGateStoryboardFrame.check_lazy_status(job_id=job['id'], reference_images={'reference_1': (None, 'reference_images.reference_1')})
    assert pending == ['reference_images.reference_1']
    # Deliberately reverse dictionary insertion; socket order must win.
    inputs = {'reference_2': torch.ones(1, 8, 8, 3), 'reference_1': torch.zeros(1, 8, 8, 3)}
    output = await m.AstroGateStoryboardFrame.execute('两人抬头', job['id'], inputs, element_id='shot1')
    assert output.result[1]['element_id'] == 'shot1'
    assert sent[0]['prompt'].index('图1：林夏身份') < sent[0]['prompt'].index('图2：周野身份')
    saved = modules.store.job(job['id'])
    assert all(r['image']['hash'] for r in saved['references'])
    assert m.AstroGateStoryboardFrame.check_lazy_status(job_id=job['id'], reference_images={'reference_1': (None, 'reference_images.reference_1')}) == []
    await m.AstroGateStoryboardFrame.execute('两人抬头', job['id'], element_id='shot1')
    assert len(sent) == 1


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
    async def text_request(model, system, content):
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
