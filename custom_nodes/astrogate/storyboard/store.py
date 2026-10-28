import hashlib
import json
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageOps


def new_id():
    return uuid.uuid4().hex


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def image_hash(image):
    image = image.convert("RGB")
    return hashlib.sha256(str(image.size).encode() + image.tobytes()).hexdigest()


def decode_novel(data):
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16"), "UTF-16"
    try:
        return data.decode("utf-8-sig"), "UTF-8"
    except UnicodeDecodeError:
        return data.decode("gb18030"), "GB18030"


def parse_novel(data, title):
    text, encoding = decode_novel(data)
    source_id = new_id()
    headings = re.compile(r"^\s*(第[零〇一二三四五六七八九十百千万两\d]+[章回卷节].*|chapter\s+\d+.*)\s*$", re.I)
    chapters = []
    current = None
    offset = 0
    for line in text.splitlines(keepends=True):
        content = line.strip()
        if content:
            if headings.match(content) or current is None or (current["title"].startswith("未分章") and len(current["paragraphs"]) >= 100):
                current = {"id": new_id(), "title": content if headings.match(content) else f"未分章 {len(chapters) + 1}", "paragraphs": []}
                chapters.append(current)
            start = offset + len(line) - len(line.lstrip())
            current["paragraphs"].append({"id": new_id(), "start": start, "end": start + len(content), "text": content})
        offset += len(line)
    if not chapters:
        raise ValueError("TXT 中没有可分析的正文。")
    return {"id": new_id(), "title": title, "schema_version": 1, "revision": 0,
            "source": {"id": source_id, "text": text, "encoding": encoding, "hash": hashlib.sha256(data).hexdigest()},
            "chapters": chapters, "assets": [], "shots": [], "workflows": {},
            "settings": {"text_model": "gpt-5.6-terra", "style": "写实电影分镜，自然摄影质感；所有人物和场景使用同一画风", "density": "精细拆镜，保留关键动作、对话反应、空间关系和必要空镜，每镜一张独立关键帧",
                         "aspect_ratio": "16:9", "resolution": "4K", "quality": "high"}}


def chunks(paragraphs, limit=6000):
    batch, size = [], 0
    for paragraph in paragraphs:
        # Split oversized paragraphs without inventing new source paragraph IDs.
        for offset in range(0, len(paragraph["text"]), limit):
            piece = {**paragraph, "text": paragraph["text"][offset:offset + limit], "slice_start": offset}
            if batch and size + len(piece["text"]) > limit:
                yield batch
                batch, size = [], 0
            batch.append(piece)
            size += len(piece["text"])
    if batch:
        yield batch


def asset_by_id(project, asset_id):
    return next((a for a in project["assets"] if a["id"] == asset_id), None)


def version(asset, version_id=None):
    wanted = version_id or asset["current_version_id"]
    return next(v for v in asset["versions"] if v["id"] == wanted)


def binding(project, asset_id, policy="follow_latest", version_id=None):
    asset = asset_by_id(project, asset_id)
    if asset is None:
        raise ValueError("引用的素材不存在。")
    if policy not in ("follow_latest", "locked"):
        raise ValueError("引用方式应为 follow_latest 或 locked。")
    selected = version(asset, version_id if policy == "locked" else None)
    return {"asset_id": asset_id, "version_id": selected["id"], "policy": policy}


def check_sources(project, refs, allowed=None):
    paragraphs = {p["id"]: p for c in project["chapters"] for p in c["paragraphs"]}
    if not isinstance(refs, list) or not refs:
        raise ValueError("分析条目缺少原文定位。")
    for ref in refs:
        if not isinstance(ref, dict):
            raise ValueError("原文定位必须是包含段落ID的对象。")
        p = paragraphs.get(ref.get("paragraph_id"))
        if p is None or (allowed is not None and p["id"] not in allowed):
            raise ValueError("分析结果引用了不在本次正文中的段落。")
        if ref.get("quote") and ref["quote"] not in p["text"]:
            raise ValueError("分析结果的引文与原文不符，请编辑草稿。")


def shot_bindings(shot):
    bindings = []
    if shot.get("scene"):
        bindings.append(shot["scene"])
    for person in shot.get("participants", []):
        if person.get("visible") is True:
            bindings.extend([person["identity"], person["look"]] if person.get("look") else [person["identity"]])
    bindings.extend(shot.get("props", []))
    return bindings


def validate_shot(project, shot):
    if shot["chapter_id"] not in {c["id"] for c in project["chapters"]}:
        raise ValueError("镜头所属章节不存在。")
    check_sources(project, shot["source_refs"])
    if not isinstance(shot.get("image_prompt"), str) or not shot["image_prompt"].strip():
        raise ValueError("请填写镜头提示词。")
    for item in shot_bindings(shot):
        resolved = binding(project, item["asset_id"], item["policy"], item.get("version_id"))
        item.update(resolved)
    if shot.get("scene") and asset_by_id(project, shot["scene"]["asset_id"])["kind"] not in ("scene", "scene_state"):
        raise ValueError("场景引用类型不正确。")
    seen = set()
    for person in shot.get("participants", []):
        if person.get("visible") is not True:
            raise ValueError("participants 只允许实际出镜的人物；提及者放在 mentioned 中。")
        character = asset_by_id(project, person["identity"]["asset_id"])
        if character["kind"] != "character" or character["id"] in seen:
            raise ValueError("镜头包含重复或无效的人物身份。")
        seen.add(character["id"])
        if person.get("look"):
            look = asset_by_id(project, person["look"]["asset_id"])
            if look["kind"] != "look" or look.get("parent_id") != character["id"]:
                raise ValueError("人物和造型不匹配。")
    for prop in shot.get("props", []):
        if asset_by_id(project, prop["asset_id"])["kind"] != "prop":
            raise ValueError("道具引用类型不正确。")
    shots = {s["id"]: s for s in project["shots"]}
    shots[shot["id"]] = shot
    previous = shot.get("previous_shot_id")
    visited = {shot["id"]}
    while previous:
        if previous not in shots or previous in visited:
            raise ValueError("上一帧引用不存在或形成循环。")
        visited.add(previous)
        previous = shots[previous].get("previous_shot_id")


def invalidate(project, shot_ids):
    affected = set(shot_ids)
    while True:
        additional = {s["id"] for s in project["shots"] if s.get("previous_shot_id") in affected} - affected
        if not additional:
            break
        affected.update(additional)
    for shot in project["shots"]:
        if shot["id"] in affected:
            shot["stale"] = True


def publish_version(project, asset_id, description, image=None, parent_version_id=None):
    asset = asset_by_id(project, asset_id)
    if asset is None:
        raise ValueError("素材不存在。")
    parent = asset_by_id(project, asset.get("parent_id"))
    record = {"id": new_id(), "description": description, "image": image, "created_at": time.time(),
              "parent_version_id": parent_version_id or (parent["current_version_id"] if parent else None)}
    asset["versions"].append(record)
    asset["current_version_id"] = record["id"]
    changed = []
    for shot in project["shots"]:
        for item in shot_bindings(shot):
            if item["asset_id"] == asset_id and item["policy"] == "follow_latest":
                item["version_id"] = record["id"]
                changed.append(shot["id"])
    invalidate(project, changed)
    return record


def merge_asset(project, source_id, target_id):
    source, target = asset_by_id(project, source_id), asset_by_id(project, target_id)
    if not source or not target or source_id == target_id or source.get("redirect_to") or target.get("redirect_to"):
        raise ValueError("请选择两个不同的有效素材。")
    if source["kind"] != target["kind"] or source.get("parent_id") != target.get("parent_id"):
        raise ValueError("只能合并同类别、同归属的重复素材。")
    if source["kind"] == "character":
        for shot in project["shots"]:
            identities = {p["identity"]["asset_id"] for p in shot.get("participants", [])}
            if {source_id, target_id}.issubset(identities):
                raise ValueError("两个人物同在一个镜头，请先修正该镜重复的出镜人物，再合并身份。")
    target["aliases"] = sorted(set(target.get("aliases", []) + source.get("aliases", []) + [source["name"]]))
    target["versions"].extend(source["versions"])
    source["redirect_to"] = target_id
    for asset in project["assets"]:
        if asset.get("parent_id") == source_id:
            asset["parent_id"] = target_id
    affected = []
    for shot in project["shots"]:
        for item in shot_bindings(shot):
            if item["asset_id"] == source_id:
                item["asset_id"] = target_id
                if item["policy"] == "follow_latest":
                    item["version_id"] = target["current_version_id"]
                affected.append(shot["id"])
    invalidate(project, affected)


class Store:
    def __init__(self, root, input_dir):
        self.root = Path(root)
        self.input_dir = Path(input_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self.database = self.root / "storyboard.sqlite3"
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS projects (id TEXT PRIMARY KEY, owner TEXT NOT NULL, data TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, data TEXT NOT NULL)")
            db.execute("CREATE INDEX IF NOT EXISTS job_project ON jobs(project_id)")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.database, timeout=15)
        try:
            with db:
                yield db
        finally:
            db.close()

    def create(self, owner, project):
        with self.connect() as db:
            db.execute("INSERT INTO projects VALUES (?, ?, ?)", (project["id"], owner, json.dumps(project, ensure_ascii=False)))
        return project

    def get(self, project_id, owner=None):
        with self.connect() as db:
            row = db.execute("SELECT owner, data FROM projects WHERE id=?", (project_id,)).fetchone()
        if not row or (owner is not None and owner != row[0]):
            raise ValueError("项目不存在或不属于当前用户。")
        return json.loads(row[1])

    def list(self, owner):
        with self.connect() as db:
            rows = db.execute("SELECT data FROM projects WHERE owner=?", (owner,)).fetchall()
        return [{"id": p["id"], "title": p["title"]} for (raw,) in rows for p in [json.loads(raw)]]

    def change(self, project_id, change, expected=None):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT data FROM projects WHERE id=?", (project_id,)).fetchone()
            if not row:
                raise ValueError("项目不存在。")
            project = json.loads(row[0])
            if expected is not None and project["revision"] != expected:
                raise ValueError("项目已更新，请刷新后再保存，当前修改未覆盖。")
            change(project)
            project["revision"] += 1
            db.execute("UPDATE projects SET data=? WHERE id=?", (json.dumps(project, ensure_ascii=False), project_id))
        return project

    def add_job(self, project_id, data):
        job = {"id": new_id(), "project_id": project_id, "created_at": time.time(), "status": "prepared", **data}
        job["events"] = [{"time": job["created_at"], "status": job["status"]}]
        with self.connect() as db:
            db.execute("INSERT INTO jobs VALUES (?, ?, ?)", (job["id"], project_id, json.dumps(job, ensure_ascii=False)))
        return job

    def job(self, job_id):
        with self.connect() as db:
            row = db.execute("SELECT data FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise ValueError("任务不存在，请点击图片卡片上的生成按钮。")
        return json.loads(row[0])

    def jobs(self, project_id):
        with self.connect() as db:
            rows = db.execute("SELECT data FROM jobs WHERE project_id=? ORDER BY rowid DESC", (project_id,)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def update_job(self, job_id, expected_status=None, **fields):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT data FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise ValueError("任务不存在。")
            job = json.loads(row[0])
            if expected_status is not None and job["status"] not in expected_status:
                raise ValueError("任务已执行或取消，不能重复提交；请查看历史结果。")
            now = time.time()
            event = {"time": now}
            message = fields.pop("event", None)
            for field in ("status", "stage", "error"):
                if fields.get(field) is not None and fields[field] != job.get(field):
                    event[field] = fields[field]
            if message:
                event["message"] = message
            if len(event) > 1:
                job["events"] = [*job.get("events", []), event][-120:]
            job.update(fields, updated_at=now)
            db.execute("UPDATE jobs SET data=? WHERE id=?", (json.dumps(job, ensure_ascii=False), job_id))
        return job

    def recover(self):
        with self.connect() as db:
            rows = db.execute("SELECT id, data FROM jobs").fetchall()
            for job_id, raw in rows:
                job = json.loads(raw)
                if job["status"] in ("running", "analysing", "cancelling"):
                    job.update(status="unknown", error="服务已重启，远端结果未知。未自动重试。")
                    db.execute("UPDATE jobs SET data=? WHERE id=?", (json.dumps(job, ensure_ascii=False), job_id))

    def image_path(self, relative):
        root = (self.input_dir / "astrogate").resolve()
        path = (self.input_dir / relative).resolve()
        if not path.is_relative_to(root):
            raise ValueError("图片路径必须位于项目素材目录。")
        return path

    def save_image(self, project_id, data):
        if not re.fullmatch(r"[0-9a-f]{32}", project_id):
            raise ValueError("无效项目 ID。")
        with Image.open(BytesIO(data)) as opened:
            if opened.width * opened.height > 24_000_000 or getattr(opened, "n_frames", 1) != 1:
                raise ValueError("素材必须是单帧图片，且不超过 2400 万像素。")
            image = ImageOps.exif_transpose(opened).convert("RGB")
        relative = f"astrogate/{project_id}/{new_id()}.png"
        path = self.image_path(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        image.save(path, format="PNG")
        return {"file": relative, "hash": image_hash(image), "width": image.width, "height": image.height}
