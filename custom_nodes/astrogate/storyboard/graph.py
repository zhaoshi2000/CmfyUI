import copy
import secrets

from .store import asset_by_id, digest, shot_bindings, version


def references(project, shot):
    result, missing, descriptions = [], [], []
    seen = set()
    for item in shot_bindings(shot):
        asset = asset_by_id(project, item["asset_id"])
        selected = version(asset, item["version_id"] if item["policy"] == "locked" else None)
        parent = asset_by_id(project, asset.get("parent_id"))
        target = f"{parent['name']} / {asset['name']}" if parent else asset["name"]
        kind = {"character": "人物身份（只参考体貌，不套用服装）", "look": "该人物的服装发型配饰",
                "scene": "场景空间布局", "scene_state": "场景时段、布局和光线", "prop": "道具外形"}[asset["kind"]]
        descriptions.append(f"{target}，{kind}：{selected['description']}")
        if (asset["id"], selected["id"]) in seen:
            continue
        seen.add((asset["id"], selected["id"]))
        if selected.get("image"):
            result.append({"asset_id": asset["id"], "version_id": selected["id"], "purpose": f"{target}：{kind}",
                           "image": selected["image"]})
        else:
            missing.append(target)
    return result, missing, descriptions


def snapshot(project, shot):
    refs, missing, descriptions = references(project, shot)
    settings = {key: project["settings"][key] for key in ("aspect_ratio", "resolution", "quality", "style")}
    people = []
    for person in shot.get("participants", []):
        if person.get("visible") is not True:
            continue
        name = asset_by_id(project, person["identity"]["asset_id"])["name"]
        people.append(f"{name}：" + "，".join(str(person.get(k, "")) for k in ("screen_position", "action", "expression", "facing")))
    return {"shot": copy.deepcopy(shot), "references": refs, "missing": missing, "descriptions": descriptions,
            "people": people, "settings": settings, "source_id": project["source"]["id"]}


def semantic_hash(value):
    value = copy.deepcopy(value)
    for field in ("stale", "latest_job_id", "revision"):
        value.get("shot", {}).pop(field, None)
    return digest(value)


def render_prompt(snap, refs):
    lines = [snap["shot"]["image_prompt"], "画风：" + snap["settings"]["style"]]
    lines += snap["descriptions"] + snap["people"]
    if snap["shot"].get("atmosphere"):
        lines.append("本镜特效与氛围：" + snap["shot"]["atmosphere"])
    if refs:
        lines.append("参考图用途（按实际输入顺序）：")
        lines.extend(f"图{index + 1}：{ref['purpose']}" for index, ref in enumerate(refs))
        lines.append("每张图仅用于指定对象和用途，不混合不同人物的脸和服装。场景参考中的路人不自动入画。上一帧只参考空间关系，人物名单以本镜为准。")
    lines.append("只绘制指定的设定素材；默认无文字标签和水印。" if "asset_description" in snap["shot"] else "只画本镜列出的可见人物；默认无字幕、文字标签和水印。")
    if snap["shot"].get("asset_kind") in ("character", "look"):
        lines.append("人物设定参考图：仅绘制一个指定人物，完整展示人物与服饰；背景为干净的纯白或浅灰单色，无场景、家具、道具陈列、其他人物、文字或水印。保持参考图人物身份及本次指定造型。")
    if snap["shot"].get("asset_kind") in ("scene", "scene_state"):
        lines.append("无人环境素材：只绘制建筑、空间、陈设与光照。画面中禁止出现任何人物、路人、人体、剪影或人物倒影；参考图中即使有人也必须移除。保留同一场景的空间布局，不绘制剧情事件。")
    prompt = "\n".join(lines)
    if len(prompt) > 16000:
        raise ValueError("合并素材说明后的提示词超过16000字符，请精简设定。")
    return prompt


def plan(store, project, shot_ids, allow_missing=False, force=False, asset_id=None):
    shots = {shot["id"]: shot for shot in project["shots"]}
    if asset_id:
        asset = asset_by_id(project, asset_id)
        if asset is None:
            raise ValueError("素材不存在。")
        parent = asset_by_id(project, asset.get("parent_id"))
        synthetic = {"id": asset_id, "image_prompt": f"生成单独一张{asset['name']}设定参考图。{version(asset)['description']}，清晰完整展示对象，不含文字标签。",
                     "asset_description": version(asset)["description"], "participants": [], "props": [], "source_refs": asset.get("source_refs", [])}
        if parent:
            synthetic["props"].append({"asset_id": parent["id"], "version_id": parent["current_version_id"], "policy": "follow_latest"})
        shots = {asset_id: synthetic}
        shot_ids = [asset_id]
    if not shot_ids or any(s not in shots for s in shot_ids):
        raise ValueError("请选择存在的镜头。")
    jobs = store.jobs(project["id"])
    entries, visited = [], set()

    def visit(shot_id):
        if shot_id in visited:
            return
        visited.add(shot_id)
        shot = shots[shot_id]
        snap = snapshot(project, shot)
        fingerprint = semantic_hash(snap)
        reusable = next((j for j in jobs if j.get("owner_id") == shot_id and j.get("fingerprint") == fingerprint
                         and j["status"] == "succeeded" and j.get("result_image")), None)
        if not asset_id and shot.get("stale"):
            reusable = None
        if reusable and not (force and shot_id in shot_ids):
            entries.append({"owner_id": shot_id, "title": shot.get("summary", "素材"), "reuse": reusable["id"], "missing": [], "snapshot": snap})
            return
        previous = shot.get("previous_shot_id")
        if previous:
            if previous not in shots:
                raise ValueError("上一帧已删除，请修改引用。")
            visit(previous)
        refs = copy.deepcopy(snap["references"])
        if previous:
            refs.append({"previous_shot_id": previous, "purpose": "上一帧的空间位置与光线；不继承未列出的人物"})
        if len(refs) > 8:
            raise ValueError(f"{shot.get('summary', '镜头')} 需要 {len(refs)} 张参考，接口最多8张；请减少绑定，不会静默丢弃人物。")
        for ref in refs:
            if ref.get("image") and not store.image_path(ref["image"]["file"]).is_file():
                raise ValueError("定稿素材文件缺失，请恢复文件；不会自动重新生成人物。")
        entries.append({"owner_id": shot_id, "title": shot.get("summary", asset["name"] if asset_id else "镜头"),
                        "snapshot": snap, "fingerprint": fingerprint, "references": refs, "prompt": render_prompt(snap, refs),
                        "missing": snap["missing"], "reuse": None, "owner_type": "asset" if asset_id else "shot"})

    for shot_id in shot_ids:
        visit(shot_id)
    missing = sorted({m for entry in entries if not entry["reuse"] for m in entry["missing"]})
    unresolved = [j["id"] for j in jobs if j.get("owner_id") in visited and j["status"] in ("unknown", "running", "cancelling", "prepared", "queued")]
    return {"project_id": project["id"], "revision": project["revision"], "entries": entries,
            "requests": sum(not e["reuse"] for e in entries), "reused": sum(bool(e["reuse"]) for e in entries),
            "missing": missing, "blocked": bool(missing and not allow_missing), "unresolved_jobs": unresolved,
            "selected": shot_ids, "asset_id": asset_id}


def prepare(store, project, planned, acknowledge_unknown=False):
    if planned["blocked"]:
        raise ValueError("请先上传/生成并定稿缺失素材，或明确选择允许缺失素材按文本生成。")
    active = [store.job(j) for j in planned["unresolved_jobs"]]
    if any(j["status"] in ("running", "cancelling", "prepared", "queued") for j in active):
        raise ValueError("所选镜头已有待执行或运行任务，请先完成或取消它。")
    if active and not acknowledge_unknown:
        raise ValueError("存在结果未知的任务。确认可能产生重复费用后，才能手动发起新请求。")
    mapping, records = {}, []
    for entry in planned["entries"]:
        if entry["reuse"]:
            job = store.job(entry["reuse"])
        else:
            refs = copy.deepcopy(entry["references"])
            for ref in refs:
                if ref.get("previous_shot_id"):
                    ref["job_id"] = mapping[ref["previous_shot_id"]]
            job = store.add_job(project["id"], {"kind": "image", "owner_type": entry["owner_type"], "owner_id": entry["owner_id"],
                                "snapshot": entry["snapshot"], "fingerprint": entry["fingerprint"], "references": refs,
                                "prompt": entry["prompt"], "gateway_request_id": secrets.token_hex(32),
                                "settings": entry["snapshot"]["settings"]})
        mapping[entry["owner_id"]] = job["id"]
        records.append(job)
    return compile_jobs(store, project, records, planned["selected"])


def compile_jobs(store, project, jobs, selected):
    nodes, links, api, keys = [], [], {}, {}

    def add(key, kind, title, widgets, props=None):
        if key in keys:
            return keys[key]
        node_id = str(len(nodes) + 1)
        keys[key] = node_id
        nodes.append({"id": node_id, "key": key, "type": kind, "title": title, "widgets": widgets,
                      "properties": {"astrogate_project": project["id"], **(props or {})}})
        api[node_id] = {"class_type": kind, "inputs": dict(widgets)}
        return node_id

    def load(ref):
        image = ref["image"]
        return add(image["file"], "LoadImage", ref["purpose"], {"image": image["file"]})

    job_nodes = {}
    for job in jobs:
        owner_id = job["owner_id"]
        shot = job["snapshot"]["shot"]
        frame = add(owner_id, "AstroGateStoryboardFrame", shot.get("summary", "素材设定图"),
                    {"prompt": shot["image_prompt"], "job_id": job["id"]}, {"astrogate_owner": owner_id, "astrogate_job": job["id"],
                    "astrogate_base_prompt": shot["image_prompt"],
                    "astrogate_images": [job["saved_result"]] if job.get("saved_result") else []})
        job_nodes[job["id"]] = frame
        # Successful jobs read their own durable result; references need not execute again.
        if job["status"] == "succeeded":
            continue
        for index, ref in enumerate(job["references"]):
            if ref.get("job_id"):
                upstream = store.job(ref["job_id"])
                if upstream["id"] in job_nodes:
                    source = job_nodes[upstream["id"]]
                elif upstream["status"] == "succeeded":
                    source = load({"image": upstream["result_image"], "purpose": ref["purpose"]})
                else:
                    raise ValueError("缺少前帧执行节点，请重新预览生成计划。")
            else:
                source = load(ref)
            socket = f"reference_images.reference_{index + 1}"
            api[frame]["inputs"][socket] = [source, 0]
            links.append({"from": source, "to": frame, "input": socket})
    return {"nodes": nodes, "links": links, "prompt": api, "targets": [keys[s] for s in selected],
            "jobs": [j["id"] for j in jobs], "project_id": project["id"]}


def draft_canvas(store, project, chapter_id):
    all_shots = {s["id"]: s for s in project["shots"]}
    expanded = {}
    def include(shot):
        if shot["id"] in expanded:
            return
        expanded[shot["id"]] = shot
        if shot.get("previous_shot_id"):
            include(all_shots[shot["previous_shot_id"]])
    for shot in project["shots"]:
        if shot["chapter_id"] == chapter_id:
            include(shot)
    shots = list(expanded.values())
    nodes, links = [], []
    keys = {}
    for shot in shots:
        snap = snapshot(project, shot)
        refs = snap["references"]
        if shot.get("previous_shot_id"):
            refs.append({"previous_shot_id": shot["previous_shot_id"], "purpose": "上一帧空间关系"})
        if len(refs) > 8:
            raise ValueError("镜头参考图片超过接口8张上限，请先调整引用。")
        frame = str(len(nodes) + 1)
        keys[shot["id"]] = frame
        completed = store.job(shot["latest_job_id"]) if shot.get("latest_job_id") else None
        ready = completed and completed["status"] == "succeeded" and not shot.get("stale")
        nodes.append({"id": frame, "key": shot["id"], "type": "AstroGateStoryboardFrame", "title": shot["summary"],
                      "widgets": {"prompt": shot["image_prompt"], "job_id": completed["id"] if ready else ""},
                      "properties": {"astrogate_project": project["id"], "astrogate_owner": shot["id"],
                                     "astrogate_base_prompt": shot["image_prompt"],
                                     "astrogate_images": [completed["saved_result"]] if completed and completed.get("saved_result") else []}})
        for index, ref in enumerate(refs):
            if ref.get("previous_shot_id"):
                continue
            file = ref["image"]["file"]
            if file not in keys:
                keys[file] = str(len(nodes) + 1)
                nodes.append({"id": keys[file], "key": file, "type": "LoadImage", "title": ref["purpose"], "widgets": {"image": file},
                              "properties": {"astrogate_project": project["id"]}})
            links.append({"from": keys[file], "to": frame, "input": f"reference_images.reference_{index + 1}"})
    for shot in shots:
        previous = shot.get("previous_shot_id")
        if previous in keys:
            refs, _, _ = references(project, shot)
            links.append({"from": keys[previous], "to": keys[shot["id"]], "input": f"reference_images.reference_{len(refs) + 1}"})
    return {"nodes": nodes, "links": links, "jobs": [], "project_id": project["id"]}
