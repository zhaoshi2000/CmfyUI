import copy
import sys

import pytest

from test_storyboard import modules as modules, project as project


def candidate(project, local_id, **changes):
    paragraph = project["chapters"][0]["paragraphs"][1]
    return {"local_id": local_id, "summary": local_id, "image_prompt": "客栈中的独立分镜",
            "source_refs": [{"paragraph_id": paragraph["id"], "quote": None}], "participants": [],
            "scene_id": "客栈", "story_time": "当晚", "prop_ids": [], **changes}


def merge(modules, project, shots, analysis_id="analysis", chapter_id=None):
    chapter_id = chapter_id or project["chapters"][0]["id"]
    modules.analysis.merge_shots(project, chapter_id, {"shots": shots},
                                 {p["id"] for c in project["chapters"] for p in c["paragraphs"]}, analysis_id)


def test_consecutive_same_scene_shots_wire_previous_image(modules, project):
    project["shots"] = []
    merge(modules, project, [candidate(project, "s1"), candidate(project, "s2")])
    first, second = project["shots"]
    assert first["previous_shot_id"] is None
    assert second["previous_shot_id"] == first["id"]
    native = sys.modules[modules.node.__name__ + ".storyboard.native"]
    recipe = native.native_canvas(modules.store, project, first["chapter_id"], "analysis")
    assert any(link["from"] == first["id"] and link["to"] == second["id"] and link["type"] == "IMAGE"
               for link in recipe["links"])


@pytest.mark.parametrize("changes", [
    {"continue_previous": False}, {"story_time": "次日"}, {"scene_id": None},
    {"story_relation": "转场"}, {"story_relation": "时间推进"}, {"story_relation": "回忆"},
])
def test_cut_or_time_change_does_not_chain_images(modules, project, changes):
    project["shots"] = []
    merge(modules, project, [candidate(project, "s1"), candidate(project, "s2", **changes)])
    assert project["shots"][1]["previous_shot_id"] is None


def test_different_scene_and_empty_scenes_remain_independent(modules, project):
    street = copy.deepcopy(project["assets"][-1])
    street.update(id="街道", name="街道", current_version_id="街道1")
    street["versions"][0]["id"] = "街道1"
    project["assets"].append(street)
    project["shots"] = []
    merge(modules, project, [candidate(project, "s1"), candidate(project, "s2", scene_id="街道"),
                             candidate(project, "s3", scene_id=None), candidate(project, "s4", scene_id=None)])
    assert all(shot["previous_shot_id"] is None for shot in project["shots"])


def test_explicit_previous_is_preserved_and_explicit_cut_disconnects(modules, project):
    project["shots"] = []
    merge(modules, project, [candidate(project, "s1"), candidate(project, "s2", continue_previous=False),
                             candidate(project, "s3", previous_local_id="s1", previous_reason="延续空间"),
                             candidate(project, "s4", previous_local_id="s3", continue_previous=False)])
    assert project["shots"][2]["previous_shot_id"] == project["shots"][0]["id"]
    assert project["shots"][2]["previous_reason"] == "延续空间"
    assert project["shots"][3]["previous_shot_id"] is None


def test_chunk_continuity_only_uses_same_analysis_and_chapter(modules, project):
    project["shots"] = []
    merge(modules, project, [candidate(project, "s1")])
    prior = copy.deepcopy(project["shots"][0])
    merge(modules, project, [candidate(project, "s1", previous_local_id="previous_chunk")])
    assert project["shots"][-1]["previous_shot_id"] == prior["id"]
    assert project["shots"][0] == prior
    merge(modules, project, [candidate(project, "s1")], analysis_id="other-analysis")
    assert project["shots"][-1]["previous_shot_id"] is None
    merge(modules, project, [candidate(project, "s1")], chapter_id=project["chapters"][1]["id"])
    assert project["shots"][-1]["previous_shot_id"] is None


def test_invalid_previous_chunk_is_rejected_without_prior_analysis(modules, project):
    with pytest.raises(ValueError, match="上一帧依赖"):
        merge(modules, project, [candidate(project, "s1", previous_local_id="previous_chunk")])


@pytest.mark.parametrize(("include_props", "job_include_props"), [(None, None), (False, None), (True, None), (True, False), (False, True)])
async def test_analysis_passes_prop_generation_setting_to_both_stages(modules, project, monkeypatch, include_props, job_include_props):
    prop = {"id": "油灯", "name": "油灯", "kind": "prop", "parent_id": None, "aliases": [],
            "current_version_id": "油灯1", "versions": [{"id": "油灯1", "description": "铜制油灯", "image": None}]}
    def configure(p):
        p["assets"].append(prop)
        if include_props is not None:
            p["settings"]["include_props"] = include_props
    modules.store.change(project["id"], configure)
    calls = []
    async def request(model, system, content, on_progress=None):
        calls.append((system, copy.deepcopy(content)))
        return ('{"assets":[]}' if system == modules.analysis.EXTRACT_PROMPT else '{"shots":[]}'), {}
    monkeypatch.setattr(modules.analysis, "text_request", request)
    fields = {"kind": "text", "chapter_id": project["chapters"][0]["id"], "model": "gpt-6-luna", "style": "写实", "density": "精细"}
    if job_include_props is not None:
        fields["include_props"] = job_include_props
    job = modules.store.add_job(project["id"], fields)
    await modules.analysis.analyse(modules.store, job["id"])
    assert modules.store.job(job["id"])["status"] == "succeeded"
    assert [system for system, _ in calls] == [modules.analysis.EXTRACT_PROMPT, modules.analysis.SHOT_PROMPT]
    for _, content in calls:
        assert content["generate_prop_assets"] is (job_include_props if job_include_props is not None else include_props is True)
        assert any(asset["id"] == "油灯" and asset["kind"] == "prop" for asset in content["known_assets"])
    assert modules.domain.asset_by_id(modules.store.get(project["id"]), "油灯")["versions"] == prop["versions"]
