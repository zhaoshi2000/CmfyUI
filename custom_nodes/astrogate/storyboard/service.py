import asyncio
import json
import zipfile
from functools import wraps
from pathlib import Path

from aiohttp import web

import folder_paths
from server import PromptServer

from .analysis import MODELS, analyse
from .graph import draft_canvas, plan, prepare
from .native import native_canvas, prepare_native
from .store import Store, asset_by_id, invalidate, merge_asset, new_id, parse_novel, publish_version, validate_shot, version


_store = None


def get_store():
    global _store
    if _store is None:
        _store = Store(folder_paths.get_system_user_directory("astrogate"), folder_paths.get_input_directory())
        _store.recover()
    return _store


def queue_failure(job, history):
    messages = history.get("status", {}).get("messages", [])
    for event, details in reversed(messages):
        if event == "execution_error":
            node_id = str(details["node_id"])
            node = history["prompt"][2].get(node_id, {})
            title = node.get("_meta", {}).get("title") or f"节点 {node_id}"
            reason = details.get("exception_message", "执行失败").strip()[:400]
            if node.get("inputs", {}).get("job_id") == job["id"]:
                return "failed", f"{title}：{reason}。图片请求尚未发出。"
            return "blocked", f"本批次被“{title}”报错中止：{reason}。此图片尚未执行。"
        if event == "execution_interrupted":
            return "cancelled", "此批次已停止，图片请求尚未发出。"
    return "blocked", "队列已结束，此图片尚未执行。请检查参考图是否已生成。"


def register_routes():
    server = PromptServer.instance
    store = get_store()
    tasks = {}

    def owner(request):
        return server.user_manager.get_request_user_id(request)

    def project_for(request):
        return store.get(request.match_info["project"], owner(request))

    def job_for(request):
        job = store.job(request.match_info["job"])
        store.get(job["project_id"], owner(request))
        return job

    def route(method, path):
        def decorate(handler):
            @wraps(handler)
            async def safe(request):
                try:
                    return await handler(request)
                except (ValueError, KeyError, TypeError, StopIteration) as error:
                    message = str(error) if isinstance(error, ValueError) else "数据不完整，请刷新后检查输入。"
                    return web.json_response({"error": message}, status=400)
            server.routes.route(method, "/astrogate/storyboard" + path)(safe)
            return safe
        return decorate

    @route("GET", "/projects")
    async def projects(request):
        return web.json_response({"projects": store.list(owner(request)), "models": MODELS})

    @route("POST", "/projects")
    async def create(request):
        reader = await request.multipart()
        field = await reader.next()
        if field is None or not field.filename or not field.filename.lower().endswith(".txt"):
            raise ValueError("请选择 TXT 小说文件。")
        data = await field.read()
        if len(data) > 20 * 1024 * 1024:
            raise ValueError("TXT 超过20MB，请拆成多个项目导入。")
        project = parse_novel(bytes(data), Path(field.filename).stem)
        return web.json_response(store.create(owner(request), project))

    @route("GET", "/projects/{project}")
    async def get_project(request):
        project = project_for(request)
        history = server.prompt_queue.get_history()
        for job in store.jobs(project["id"]):
            if job["status"] in ("prepared", "blocked") and job.get("prompt_id") in history:
                status, error = queue_failure(job, history[job["prompt_id"]])
                if (status, error) != (job["status"], job.get("error")):
                    store.update_job(job["id"], status=status, error=error)
        jobs = [{k: v for k, v in j.items() if k not in ("snapshot", "responses", "prompt", "references")}
                for j in store.jobs(project["id"])]
        if request.query.get("summary") == "1":
            return web.json_response({"revision": project["revision"], "jobs": jobs})
        return web.json_response({"project": project, "jobs": jobs})

    @route("POST", "/projects/{project}/edit")
    async def edit(request):
        project = project_for(request)
        body = await request.json()

        def change(p):
            action = body["action"]
            if action == "settings":
                values = body["settings"]
                if values["text_model"] not in MODELS:
                    raise ValueError("文本模型不在可选列表内。")
                if values["aspect_ratio"] not in ("1:1", "16:9", "9:16", "4:3", "3:4", "2:3", "3:2", "21:9"):
                    raise ValueError("画幅比例无效。")
                if values["resolution"] not in ("original", "1K", "2K", "3K", "4K") or values["quality"] not in ("low", "medium", "high"):
                    raise ValueError("图片设置无效。")
                if any(values[k] != p["settings"][k] for k in ("style", "aspect_ratio", "resolution", "quality")):
                    invalidate(p, [s["id"] for s in p["shots"]])
                p["settings"] = {k: values[k] for k in p["settings"]}
            elif action == "shot":
                shot = next(s for s in p["shots"] if s["id"] == body["shot"]["id"])
                replacement = {**body["shot"], "id": shot["id"], "chapter_id": shot["chapter_id"], "revision": shot["revision"] + 1}
                validate_shot(p, replacement)
                shot.update(replacement)
                invalidate(p, [shot["id"]])
            elif action == "asset":
                asset = asset_by_id(p, body["asset_id"])
                asset["name"] = body["name"]
                publish_version(p, asset["id"], body["description"])
            elif action == "merge_asset":
                merge_asset(p, body["source_id"], body["target_id"])
            elif action == "new_asset":
                kind = body["kind"]
                if kind not in ("character", "look", "scene", "scene_state", "prop"):
                    raise ValueError("未知素材类别。")
                parent = asset_by_id(p, body.get("parent_id"))
                expected_parent = {"look": "character", "scene_state": "scene"}.get(kind)
                if expected_parent and (parent is None or parent["kind"] != expected_parent):
                    raise ValueError("请选择正确的人物或场景归属。")
                asset = {"id": new_id(), "name": body["name"], "kind": kind, "parent_id": parent["id"] if expected_parent else None,
                         "aliases": [], "source_refs": [], "manual": True, "versions": []}
                p["assets"].append(asset)
                publish_version(p, asset["id"], body["description"])
            elif action == "approve":
                job = store.job(body["job_id"])
                if job["project_id"] != p["id"] or job.get("owner_type") != "asset" or job["status"] != "succeeded":
                    raise ValueError("该任务没有可定稿的素材图片。")
                asset = asset_by_id(p, job["owner_id"])
                parent = job["snapshot"]["shot"].get("props", [])
                publish_version(p, asset["id"], job["snapshot"]["shot"]["asset_description"], job["result_image"],
                                parent[0]["version_id"] if parent else None)
            elif action == "workflow":
                chapter_id = body["chapter_id"]
                if chapter_id not in {c["id"] for c in p["chapters"]}:
                    raise ValueError("章节不存在。")
                p["workflows"][chapter_id] = body["workflow"]
            else:
                raise ValueError("未知编辑操作。")
        result = store.change(project["id"], change, body.get("revision"))
        return web.json_response(result)

    @route("POST", "/projects/{project}/assets/{asset}/image")
    async def upload(request):
        project = project_for(request)
        asset = asset_by_id(project, request.match_info["asset"])
        if asset is None:
            raise ValueError("素材不存在。")
        reader = await request.multipart()
        field = await reader.next()
        if field is None:
            raise ValueError("请选择图片。")
        data = await field.read()
        if len(data) > 32 * 1024 * 1024:
            raise ValueError("参考图片不能超过32MB。")
        image = store.save_image(project["id"], bytes(data))
        updated = store.change(project["id"], lambda p: publish_version(p, asset["id"], version(asset)["description"], image), project["revision"])
        return web.json_response(updated)

    @route("POST", "/projects/{project}/analyse")
    async def start_analysis(request):
        project = project_for(request)
        body = await request.json()
        chapter_id = body["chapter_id"]
        if chapter_id not in {c["id"] for c in project["chapters"]}:
            raise ValueError("章节不存在。")
        previous = [j for j in store.jobs(project["id"]) if j["kind"] == "text" and j["chapter_id"] == chapter_id]
        if any(j["status"] in ("prepared", "analysing") for j in previous):
            raise ValueError("本章正在分析，请等待或停止当前任务。")
        if previous and not body.get("reanalyze"):
            raise ValueError("本章已有分析记录。可继续编辑现有镜头，或明确选择重新分析（保留旧镜头）。")
        settings = project["settings"]
        job = store.add_job(project["id"], {"kind": "text", "chapter_id": chapter_id, "model": settings["text_model"],
                                             "style": settings["style"], "density": settings["density"]})
        task = asyncio.create_task(analyse(store, job["id"]))
        tasks[job["id"]] = task
        task.add_done_callback(lambda _: tasks.pop(job["id"], None))
        return web.json_response(job)

    @route("POST", "/projects/{project}/plan")
    async def make_plan(request):
        project = project_for(request)
        body = await request.json()
        planned = plan(store, project, body.get("shot_ids", []), body.get("allow_missing", False), body.get("force", False), body.get("asset_id"))
        return web.json_response(planned)

    @route("POST", "/projects/{project}/canvas")
    async def canvas(request):
        project = project_for(request)
        body = await request.json()
        return web.json_response(draft_canvas(store, project, body["chapter_id"]))

    @route("POST", "/projects/{project}/native/canvas")
    async def native_graph(request):
        project = project_for(request)
        body = await request.json()
        return web.json_response(native_canvas(store, project, body["chapter_id"], body.get("analysis_id")))

    @route("POST", "/projects/{project}/native/prepare")
    async def native_job(request):
        project = project_for(request)
        return web.json_response(prepare_native(store, project, await request.json()))

    @route("POST", "/projects/{project}/prepare")
    async def prepare_jobs(request):
        project = project_for(request)
        body = await request.json()
        if body["revision"] != project["revision"]:
            raise ValueError("计划预览后项目已改变，请重新查看生成计划。")
        planned = plan(store, project, body.get("shot_ids", []), body.get("allow_missing", False), body.get("force", False), body.get("asset_id"))
        return web.json_response(prepare(store, project, planned, body.get("acknowledge_unknown", False)))

    @route("GET", "/jobs/{job}")
    async def get_job(request):
        return web.json_response(job_for(request))

    @route("POST", "/jobs/{job}/resume")
    async def resume_analysis(request):
        job = job_for(request)
        if job["kind"] != "text" or job["status"] not in ("failed", "unknown") or job["id"] in tasks:
            raise ValueError("仅失败或已停止的文本分析可以继续。")
        body = await request.json()
        if "response_index" in body:
            responses = job.get("responses", [])
            index = int(body["response_index"])
            if index < 0 or index >= len(responses) or responses[index].get("applied"):
                raise ValueError("已经应用的草稿不可覆盖，请修改项目中的素材或镜头。")
            responses[index]["raw"] = body["raw"]
            store.update_job(job["id"], responses=responses)
        store.update_job(job["id"], status="prepared")
        task = asyncio.create_task(analyse(store, job["id"]))
        tasks[job["id"]] = task
        task.add_done_callback(lambda _: tasks.pop(job["id"], None))
        return web.json_response({"status": "prepared"})

    @route("POST", "/jobs/{job}/queue")
    async def mark_queued(request):
        job = job_for(request)
        data = await request.json()
        # Keep the prompt ID before submission, so a lost browser response is recoverable.
        return web.json_response(store.update_job(job["id"], expected_status=("prepared", "succeeded"),
                                                  prompt_id=data["prompt_id"], status=job["status"]))

    @route("POST", "/jobs/{job}/cancel")
    async def cancel(request):
        job = job_for(request)
        if job["id"] in tasks:
            tasks[job["id"]].cancel()
        elif job["kind"] == "text" and job["status"] in ("analysing", "cancelling"):
            store.update_job(job["id"], status="cancelling", cancel_requested=True,
                             error="正在等待已发出的文本请求返回；已完成草稿会保留，不再发送下一阶段。")
        elif job["status"] in ("prepared", "queued", "running", "cancelling"):
            prompt_id = job.get("prompt_id")
            if prompt_id:
                server.prompt_queue.delete_queue_item(lambda entry: entry[1] == prompt_id)
                server.prompt_queue.interrupt_if_running(prompt_id)
            # A prompt can contain several frames. Mark every unsent frame in that prompt cancelled.
            for related in store.jobs(job["project_id"]):
                if related["id"] == job["id"] or (prompt_id and related.get("prompt_id") == prompt_id):
                    if related["status"] in ("prepared", "queued"):
                        store.update_job(related["id"], status="cancelled")
                    elif related["status"] == "running":
                        store.update_job(related["id"], status="cancelling", cancel_requested=True,
                                         error="本地已请求停止；远端请求可能仍在处理，返回的成功图片仍会保存。")
        return web.json_response(store.job(job["id"]))

    @route("GET", "/projects/{project}/export")
    async def export(request):
        project = project_for(request)
        jobs = store.jobs(project["id"])
        destination = store.root / "exports" / f"{new_id()}.zip"
        destination.parent.mkdir(exist_ok=True)
        with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("source.txt", project["source"]["text"])
            archive.writestr("project.json", json.dumps(project, ensure_ascii=False, indent=2))
            archive.writestr("generations.json", json.dumps(jobs, ensure_ascii=False, indent=2))
            images = {v["image"]["file"] for a in project["assets"] for v in a["versions"] if v.get("image")}
            images.update(j["result_image"]["file"] for j in jobs if j.get("result_image"))
            for relative in sorted(images):
                source = store.image_path(relative)
                if source.is_file():
                    archive.write(source, "input/" + relative)
            for chapter_id, workflow in project["workflows"].items():
                archive.writestr(f"workflows/{chapter_id}.json", json.dumps(workflow, ensure_ascii=False))
            for index, shot in enumerate(project["shots"], 1):
                job = next((j for j in jobs if j.get("id") == shot.get("latest_job_id") and j.get("result_image")), None)
                if job:
                    source = store.image_path(job["result_image"]["file"])
                    if source.is_file():
                        archive.write(source, f"frames/{index:04}_{shot['id'][:8]}.png")
        return web.FileResponse(destination, headers={"Content-Disposition": 'attachment; filename="astrogate-storyboard.zip"'})
