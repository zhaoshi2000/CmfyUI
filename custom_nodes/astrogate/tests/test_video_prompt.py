import copy

import pytest

from test_storyboard import modules as modules, project as project


def test_distinct_character_sheets_and_voice_follow_identity(modules, project):
    from astrogate_story_test.storyboard.video import video_prompt

    first, second = project["shots"][0], copy.deepcopy(project["shots"][0])
    second["id"] = "shot2"
    second["summary"] = "林夏反应"
    second["participants"] = [second["participants"][0]]
    second["video_action"] = "切到林夏面部近景，转身看向门口。"
    project["shots"].append(second)
    for name in ("林夏", "周野"):
        asset = modules.domain.asset_by_id(project, name)
        asset["versions"][0]["view_images"] = {"sheet": {"file": f"astrogate/{project['id']}/{name}.png"}}
    speaker = modules.domain.asset_by_id(project, "周野")
    speaker["voice_versions"] = [{"id": "voice1", "file": f"astrogate/{project['id']}/voices/voice.wav"}]
    speaker["current_voice_id"] = "voice1"
    project["dialogues"] = [{"id": "d1", "chapter_id": first["chapter_id"], "character_id": "周野",
                             "type": "dialogue", "source": "original", "status": "confirmed", "text": "你先走，我来挡住他们。"}]
    first["dialogue_links"] = [{"dialogue_id": "d1", "start": 0, "end": 3, "mode": "start"}]
    second["dialogue_links"] = [{"dialogue_id": "d1", "start": 4, "end": 11, "mode": "finish"}]
    result = video_prompt(project, first["chapter_id"])

    assert [item["name"] for item in result["images"]] == ["林夏", "周野"]
    assert [item["name"] for item in result["audios"]] == ["周野"]
    assert result["prompt"].count("图1 林夏 [image]") == 1
    assert "周野对白（声线音频1）：{你先走}" in result["prompt"]
    assert "周野画外对白（声线音频1）：{我来挡住他们。}" in result["prompt"]
    assert "BGM" in result["prompt"] and "字幕" in result["prompt"]
    assert "04:01" not in result["prompt"]


def test_offscreen_speaker_does_not_add_character_picture_or_unconfirmed_adaptation(modules, project):
    from astrogate_story_test.storyboard.video import video_prompt

    shot = project["shots"][0]
    shot["participants"] = []
    project["dialogues"] = [{"id": "d", "chapter_id": shot["chapter_id"], "character_id": "周野",
                             "type": "voiceover", "source": "adapted", "status": "pending", "text": "快走"}]
    shot["dialogue_links"] = [{"dialogue_id": "d", "start": 0, "end": 2, "mode": "start"}]
    result = video_prompt(project, shot["chapter_id"])

    assert not any(item["name"] == "周野" for item in result["images"])
    assert "{快走}" not in result["prompt"]
    assert result["warnings"]


def test_voice_upload_keeps_old_versions_and_rejects_other_files(modules, project):
    store = modules.store
    wav = b"RIFF" + (40).to_bytes(4, "little") + b"WAVEfmt " + b"\0" * 40
    first = store.save_voice(project["id"], wav)
    second = store.save_voice(project["id"], wav)
    character = modules.domain.asset_by_id(project, "周野")
    character["voice_versions"] = [first, second]
    character["current_voice_id"] = second["id"]

    assert first["id"] != second["id"]
    assert store.image_path(first["file"]).read_bytes() == wav
    assert len(character["voice_versions"]) == 2
    with pytest.raises(ValueError, match="声线音频请选择"):
        store.save_voice(project["id"], b"not audio")


def test_audio_only_continuation_needs_no_new_text_range(modules, project):
    from astrogate_story_test.storyboard.video import video_prompt

    shot = project["shots"][0]
    shot["participants"] = []
    project["dialogues"] = [{"id": "d", "chapter_id": shot["chapter_id"], "character_id": "周野",
                             "type": "dialogue", "source": "original", "status": "confirmed", "text": "你先走，我来挡住他们。"}]
    modules.domain.save_dialogue_links(project, shot["id"], [{"dialogue_id": "d", "start": None, "end": None,
                                                              "mode": "audio_only", "show_subtitle": False}])
    result = video_prompt(project, shot["chapter_id"])

    assert not shot["dialogue_links"][0]["needs_review"]
    assert "周野的声音从上一镜自然延续，不重新朗读整句" in result["prompt"]
    assert "{你先走" not in result["prompt"]
    assert result["warnings"] == []


def test_video_prompt_node_keeps_existing_video_node_name(modules):
    schema = modules.node.AstroGateVideoPrompt.define_schema()
    assert schema.node_id == "AstroGateVideoPrompt"
    assert schema.category == "video/AstroGate"
    assert [output.io_type for output in schema.outputs] == ["STRING", "STRING"]
    assert modules.node.AstroGateJimengImageToVideo.define_schema().node_id == "AstroGateJimengImageToVideo"


def test_video_prompt_omits_ai_music_and_subtitle_directions(modules, project):
    from astrogate_story_test.storyboard.video import video_prompt

    shot = project["shots"][0]
    shot["video_action"] = "镜头推近，并添加字幕和BGM。"
    shot["sound_cues"] = ["脚步声", "背景音乐渐强"]
    result = video_prompt(project, shot["chapter_id"])

    assert "现场声：脚步声" in result["prompt"]
    assert "背景音乐渐强" not in result["prompt"]
    assert "镜头推近，并添加字幕" not in result["prompt"]
    assert len(result["warnings"]) == 2
