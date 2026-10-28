import copy
import secrets

from .analysis import analyse
from .graph import render_prompt, snapshot
from .store import asset_by_id, binding, digest, invalidate, shot_bindings, version


async def analyse_native(store, project_id, chapter_number, model, style, density, revision):
    project = store.get(project_id)
    if not 1 <= chapter_number <= len(project["chapters"]):
        raise ValueError(f"本剧本共 {len(project['chapters'])} 章，请修改章节序号。")
    chapter = project["chapters"][chapter_number - 1]
    key = digest([project["source"]["hash"], chapter["id"], model, style, density, revision])
    job = next((j for j in store.jobs(project_id) if j.get("analysis_key") == key), None)
    if job and job["status"] != "succeeded":
        reason = job.get("error") or "本次分析尚未完成。"
        raise ValueError(f"{reason}\n请在分镜节点查看状态，失败后可点击重试继续分析。")
    if job is None:
        def settings(p):
            p["settings"].update(text_model=model, style=style, density=density)
        store.change(project_id, settings)
        job = store.add_job(project_id, {"kind": "text", "chapter_id": chapter["id"], "model": model,
                                        "style": style, "density": density, "analysis_key": key})
        await analyse(store, job["id"])
        job = store.job(job["id"])
    if job["status"] != "succeeded":
        raise RuntimeError(job.get("error", "分镜分析未完成。"))
    return {"project_id": project_id, "chapter_id": chapter["id"], "analysis_id": job["id"]}


def asset_shot(project, asset):
    parent_version = version(asset).get("parent_version_id")
    return {"id": asset["id"], "summary": asset["name"], "image_prompt": version(asset)["description"],
            "asset_description": version(asset)["description"], "participants": [],
            "props": [binding(project, asset["parent_id"], "locked" if parent_version else "follow_latest", parent_version)] if asset.get("parent_id") else [], "source_refs": asset.get("source_refs", [])}


def local_asset_job(store, project, asset, image):
    existing = next((j for j in store.jobs(project["id"]) if j.get("owner_id") == asset["id"] and j["status"] == "succeeded"
                     and j.get("result_image", {}).get("hash") == image["hash"]), None)
    if existing:
        return existing
    shot = asset_shot(project, asset)
    snap = snapshot(project, shot)
    folder, filename = image["file"].rsplit("/", 1)
    return store.add_job(project["id"], {"kind": "image", "native": True, "status": "succeeded", "owner_type": "asset",
                        "asset_version_id": asset["current_version_id"],
                        "owner_id": asset["id"], "prompt": shot["image_prompt"], "snapshot": snap, "settings": snap["settings"],
                        "references": [], "result_image": image, "saved_result": {"filename": filename, "subfolder": folder, "type": "input"}})


def native_canvas(store, project, chapter_id, analysis_id=None):
    shots = [s for s in project["shots"] if s["chapter_id"] == chapter_id and (not analysis_id or s.get("analysis_id") == analysis_id)]
    # Resolve dependencies even when the referenced frame belongs to another chapter.
    all_shots = {s["id"]: s for s in project["shots"]}
    needed = {s["id"] for s in shots}
    for shot in list(shots):
        previous = shot.get("previous_shot_id")
        while previous and previous not in needed:
            needed.add(previous)
            shots.insert(0, all_shots[previous])
            previous = all_shots[previous].get("previous_shot_id")
    assets = {}
    parents = {}
    def include_asset(asset_id, version_id=None):
        original = asset_by_id(project, asset_id)
        selected = version(original, version_id)
        key = asset_id if selected["id"] == original["current_version_id"] else f"{asset_id}@{selected['id']}"
        if key not in assets:
            if original.get("parent_id"):
                parents[key] = include_asset(original["parent_id"], selected.get("parent_version_id"))
            assets[key] = {**original, "current_version_id": selected["id"]}
        return key
    for shot in shots:
        for item in shot_bindings(shot):
            include_asset(item["asset_id"], item["version_id"] if item["policy"] == "locked" else None)
    jobs = store.jobs(project["id"])
    nodes, links = [], []
    colors = {"character": "#355168", "look": "#4d4268", "scene": "#385448", "scene_state": "#385448", "prop": "#635035", "shot": "#294d63"}

    def add(owner, kind, prompt, position, job=None, key=None):
        key = key or owner["id"]
        if job and job["status"] == "succeeded":
            prompt = job["snapshot"]["shot"]["image_prompt"]
        props = {"astrogate_project": project["id"], "astrogate_owner": owner["id"], "astrogate_owner_type": "shot" if kind == "shot" else "asset",
                 "astrogate_kind": kind, "astrogate_key": key, "astrogate_base_prompt": prompt,
                 "astrogate_images": [job["saved_result"]] if job and job.get("saved_result") else []}
        parent = asset_by_id(project, owner.get("parent_id"))
        purpose = {"character": "人物身份、脸型体貌，不套用服装", "look": "此人物的服装、发型和配饰",
                   "scene": "场景空间布局，不自动带入参考图中的人物", "scene_state": "场景时段、天气与光线",
                   "prop": "道具外形与材质", "shot": "前镜空间与光线，人物名单以本镜为准"}[kind]
        props["astrogate_reference_purpose"] = f"{parent['name'] + ' / ' if parent else ''}{owner.get('name', owner.get('summary', ''))}：{purpose}"
        if kind != "shot":
            props["astrogate_version"] = owner["current_version_id"]
            props["astrogate_locked"] = key != owner["id"]
        nodes.append({"id": key, "type": "AstroGateStoryboardFrame", "title": owner.get("summary", owner.get("name", "图片")),
                      "pos": position, "color": colors[kind], "properties": props,
                      "widgets": {"prompt": prompt, "job_id": job["id"] if job else "", "element_id": owner["id"],
                                  "aspect_ratio": project["settings"]["aspect_ratio"], "resolution": "4K", "quality": "high"}})
        if parent:
            nodes[-1]["title"] = f"{parent['name']} / {owner['name']}"
        links.append({"from": "$board", "slot": 0, "to": key, "input": "board", "type": "ASTROGATE_BOARD", "label": "所属分镜脚本"})

    positions = {}
    row = 0
    for root in [key for key in assets if key not in parents]:
        positions[root] = [900, 80 + row * 650]
        children = [key for key in assets if parents.get(key) == root]
        for index, child in enumerate(children):
            positions[child] = [1380, 80 + (row + index) * 650]
        row += max(1, len(children))
    for key, asset in assets.items():
        parent = parents.get(key)
        selected = version(asset)
        job = local_asset_job(store, project, asset, selected["image"]) if selected.get("image") else next(
            (j for j in jobs if j.get("owner_id") == asset["id"] and j["status"] == "succeeded" and
             (j.get("asset_version_id") == selected["id"] or j.get("snapshot", {}).get("shot", {}).get("asset_description") == selected["description"])), None)
        add(asset, asset["kind"], selected["description"], positions[key], job, key)
        if key != asset["id"]:
            nodes[-1]["title"] += " · 固定版本"
        if parent:
            links.append({"from": parent, "slot": 0, "to": key, "input": "reference_images.reference_1", "type": "IMAGE", "label": "身份/场景基准"})

    for index, shot in enumerate(shots):
        scene_id = (shot.get("scene") or {}).get("asset_id", "none")
        # Chronological rows stay readable while assets are shared on the left.
        job = next((j for j in jobs if j["id"] == shot.get("latest_job_id")), None)
        add(shot, "shot", shot["image_prompt"], [1940 + (index % 3) * 500, 80 + (index // 3) * 800], job)
        refs = []
        seen = set()
        for item in shot_bindings(shot):
            key = include_asset(item["asset_id"], item["version_id"] if item["policy"] == "locked" else None)
            if key not in seen:
                refs.append(key)
                seen.add(key)
        if shot.get("previous_shot_id"):
            refs.append(shot["previous_shot_id"])
        for ref_index, source in enumerate(refs):
            links.append({"from": source, "slot": 0, "to": shot["id"], "input": f"reference_images.reference_{ref_index + 1}", "type": "IMAGE", "label": "图像参考"})
        story_parent = shot.get("story_parent_id")
        if story_parent in needed:
            links.append({"from": story_parent, "slot": 1, "to": shot["id"], "input": "story_context", "type": "ASTROGATE_STORY", "label": shot.get("story_relation", "剧情承接")})
        elif index:
            links.append({"from": shots[index - 1]["id"], "slot": 1, "to": shot["id"], "input": "story_context", "type": "ASTROGATE_STORY", "label": "叙事顺序"})
        nodes[-1]["title"] = f"{index + 1:03d} · {shot['summary']}"
        nodes[-1]["properties"]["astrogate_scene"] = asset_by_id(project, scene_id)["name"] if scene_id != "none" else "空镜 / 未指定场景"
    return {"project_id": project["id"], "chapter_id": chapter_id, "nodes": nodes, "links": links}


def prepare_native(store, project, body):
    owner_id = body["owner_id"]
    kind = body["owner_type"]
    if kind == "shot":
        shot = next((s for s in project["shots"] if s["id"] == owner_id), None)
        if shot is None:
            raise ValueError("分镜已不存在。")
        shot = copy.deepcopy(shot)
    elif kind == "asset":
        asset = asset_by_id(project, owner_id)
        if asset is None:
            raise ValueError("素材已不存在。")
        asset = {**asset, "current_version_id": body.get("version_id") or asset["current_version_id"]}
        shot = asset_shot(project, asset)
    else:
        raise ValueError("节点归属类型无效。")
    shot["image_prompt"] = body["prompt"]
    if kind == "asset":
        shot["asset_description"] = body["prompt"]
    settings = {**project["settings"], **body["settings"]}
    if settings["resolution"] not in ("original", "1K", "2K", "3K", "4K") or settings["quality"] not in ("low", "medium", "high"):
        raise ValueError("图片参数无效。")
    refs = []
    for item in body.get("references", []):
        ref = {"purpose": str(item.get("purpose") or "参考构图与细节"), "slot": item["slot"]}
        if item.get("source_signature"):
            ref["source_signature"] = str(item["source_signature"])
        if item.get("job_id"):
            source = store.job(item["job_id"])
            if source["project_id"] != project["id"]:
                raise ValueError("参考任务不属于本项目。")
            ref["job_id"] = source["id"]
            ref["asset_id"] = source["owner_id"]
        refs.append(ref)
    story_id = body.get("story_parent_id")
    story = next((s for s in project["shots"] if s["id"] == story_id), None)
    if story_id and (story is None or story_id == owner_id):
        raise ValueError("剧情关联必须指向本项目的另一镜头。")
    snap = snapshot(project, shot)
    snap.update(settings=settings, story_parent_id=story_id, story_summary=story["summary"] if story else "")
    if story:
        snap["descriptions"].append(f"前情（只延续叙事，不自动画入前镜人物）：{story['summary']}")
    fingerprint = digest([shot["image_prompt"], settings, refs, story_id, snap["story_summary"], snap["descriptions"], snap["people"]])
    asset_version = asset["current_version_id"] if kind == "asset" else None
    jobs = [j for j in store.jobs(project["id"]) if j.get("owner_id") == owner_id and j.get("native") and j.get("asset_version_id") == asset_version]
    active = next((j for j in jobs if j["status"] in ("running", "prepared", "queued", "cancelling")), None)
    if active:
        raise ValueError("该图片节点已经有任务，请等待或停止后再生成。")
    reused = next((j for j in jobs if j.get("fingerprint") == fingerprint and j["status"] == "succeeded"), None)
    if reused and not body.get("force"):
        return reused
    if not body.get("force") and jobs and jobs[0]["status"] == "unknown":
        raise ValueError("上次结果未知。点击“重绘”会明确发起新请求。")
    if kind == "shot":
        def edited(p):
            current = next(s for s in p["shots"] if s["id"] == owner_id)
            if current["image_prompt"] != body["prompt"]:
                current["image_prompt"] = body["prompt"]
                current["revision"] += 1
                invalidate(p, [owner_id])
        store.change(project["id"], edited)
    return store.add_job(project["id"], {"kind": "image", "native": True, "owner_type": kind, "owner_id": owner_id,
                         "asset_version_id": asset_version,
                         "snapshot": snap, "settings": settings, "prompt": render_prompt(snap, refs), "fingerprint": fingerprint,
                         "references": refs, "gateway_request_id": secrets.token_hex(32)})
