"""文字保护、原帧裁切、时间码对齐和模型输入的离线回归。"""
import asyncio
import base64
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from test_autonomous_ranges import _install_astrbot_stub

_install_astrbot_stub()

from core.domain.video_watch import VideoWatch
from core.server_ext.media import storyboard


def source_image(tmp_path, name="frame.png", size=(360, 200)):
    path = tmp_path / name
    Image.new("RGB", size, (80, 110, 140)).save(path)
    return str(path)


def test_ordinary_frames_compact_and_uncertain_keeps_size(tmp_path):
    paths = [source_image(tmp_path)]
    with patch.object(storyboard, "_text_regions", return_value=[]):
        result = storyboard._render_storyboard(paths, 360, 200, 3, 3, str(tmp_path))
    with Image.open(result["grid_path"]) as im:
        assert im.size == (720, 400)
    assert result["resize_reason"] == "compact"
    Path(result["grid_path"]).unlink()
    with patch.object(storyboard, "np", None):
        result = storyboard._render_storyboard(paths, 360, 200, 3, 3, str(tmp_path))
    assert result["grid_size"] == [1080, 600]
    assert result["resize_reason"] == "text_detection_unavailable"
    Path(result["grid_path"]).unlink()


def test_text_detector_finds_menu_but_not_flat_frame(tmp_path):
    path = source_image(tmp_path, size=(640, 360))
    assert storyboard._text_regions(path) == []
    image = Image.new("RGB", (640, 360), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype("DejaVuSans.ttf", 18)
    for y, text in zip((60, 95, 130, 165), (
        "Settings and preferences", "Choose your account", "Download the plugin", "Open the control panel",
    )):
        draw.text((100, y), text, font=font, fill="black")
    image.save(path)
    assert len(storyboard._text_regions(path)) >= 3
    result = storyboard._render_storyboard([path], 360, 200, 3, 3, str(tmp_path))
    assert result["resize_reason"] == "dense_text"
    assert result["grid_size"] == [1080, 600]
    Path(result["grid_path"]).unlink()


def test_text_detail_preserves_original_pixels(tmp_path):
    pixels = np.random.default_rng(7).integers(0, 256, (800, 1200, 3), dtype=np.uint8)
    path = tmp_path / "original.png"
    Image.fromarray(pixels).save(path)
    region = {"box": [400, 500, 900, 540], "glyph_height": 20}
    with patch.object(storyboard, "_text_regions", return_value=[region]):
        result = storyboard._render_storyboard([str(path)], 360, 200, 3, 3, str(tmp_path))
    with Image.open(result["text_detail"]["path"]) as crop:
        assert crop.size == (500 + storyboard.TEXT_DETAIL_LABEL_W, 40)
        # 来自 1200 宽原帧，文字区域仍为 500 像素宽；左侧另加帧编号。
        assert np.asarray(crop)[:, :storyboard.TEXT_DETAIL_LABEL_W].max() > 200
    assert result["grid_size"] == [720, 400]
    assert "宫格[1]" in result["text_detail"]["manifest"]
    Path(result["grid_path"]).unlink()


def test_striped_menu_is_dense_text(tmp_path):
    path = tmp_path / "striped_menu.png"
    image = Image.new("RGB", (360, 202), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype("DejaVuSans.ttf", 11)
    for index, text in enumerate(("Flip Walker", "Turntable Walker", "Long Walking Model",
                                  "Wagon Walker", "Simple Walker", "Chebyshev Walker")):
        y = 20 + index * 26
        draw.rectangle((100, y, 280, y + 21), fill=(45, 60, 75))
        draw.text((110, y + 4), text, font=font, fill="white")
    image.save(path)
    assert len(storyboard._text_regions(str(path))) >= 2
    result = storyboard._render_storyboard([str(path)], 360, 200, 3, 3, str(tmp_path))
    assert result["resize_reason"] == "dense_text"
    assert result["grid_size"] == [1080, 600]
    Path(result["grid_path"]).unlink()


def test_excess_text_falls_back_instead_of_truncating(tmp_path):
    path = source_image(tmp_path)
    region = {"box": [30, 150, 330, 180], "glyph_height": 8}
    with patch.object(storyboard, "_text_regions", return_value=[region]):
        result = storyboard._render_storyboard([path] * 9, 360, 200, 3, 3, str(tmp_path))
    assert result["resize_reason"] == "text_detail_budget"
    assert result["grid_size"] == [1080, 600]
    assert result["text_detail"] is None
    Path(result["grid_path"]).unlink()
    assert storyboard._build_text_detail([path] * 9, [(i, region) for i in range(9)], str(tmp_path)) is None


def test_low_resolution_dense_frame_does_not_duplicate_pixels(tmp_path):
    path = source_image(tmp_path)
    regions = [{"box": [100, y, 250, y + 15], "glyph_height": 8} for y in (30, 60, 90)]
    with patch.object(storyboard, "_text_regions", return_value=regions):
        result = storyboard._render_storyboard([path], 360, 200, 3, 3, str(tmp_path))
    assert result["resize_reason"] == "dense_text"
    assert result["text_detail"] is None
    Path(result["grid_path"]).unlink()


def test_caption_crop_keeps_whole_line(tmp_path):
    path = source_image(tmp_path)
    region = {"box": [80, 20, 280, 38], "glyph_height": 12}
    detail = storyboard._build_text_detail([path], [(0, region)], str(tmp_path))
    with Image.open(detail["path"]) as image:
        assert image.size == (360 + storyboard.TEXT_DETAIL_LABEL_W, 18)


def test_failed_hires_frame_keeps_its_timestamp_and_disables_shrink(tmp_path):
    paths = [source_image(tmp_path, f"dense{i}.png") for i in range(3)]
    video = tmp_path / "clip.mp4"
    captured = {}

    def render(selected, *args, **kwargs):
        captured.update(selected=selected, **kwargs)
        return {"grid_path": None}

    with patch.object(storyboard, "_ffprobe_duration", return_value=3), \
         patch.object(storyboard, "_sample_dense_frames", return_value=list(enumerate(paths))), \
         patch.object(storyboard, "_extract_hires_frames", return_value=[paths[0], None, paths[2]]), \
         patch.object(storyboard, "_render_storyboard", side_effect=render):
        result = storyboard.build_storyboard(str(video), tmp_dir=str(tmp_path))
    assert captured["selected"] == paths
    assert captured["source_complete"] is False
    assert result["manifest"] == "[1] 0:00\n[2] 0:01\n[3] 0:02"


def test_hires_ffmpeg_has_no_scale_and_preserves_failure_slots(tmp_path):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if len(calls) == 1:
            Path(command[-1]).write_bytes(b"frame")
        return SimpleNamespace(returncode=0 if len(calls) == 1 else 1)

    with patch.object(storyboard.subprocess, "run", side_effect=run):
        paths = storyboard._extract_hires_frames("clip.mp4", [0, 1], 360, 200, str(tmp_path))
    assert len(paths) == 2 and paths[0] and paths[1] is None
    assert all("-vf" not in command for command in calls)


def test_model_receives_crop_mapping_warning_and_cleanup(tmp_path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"0" * 2048)
    grid = source_image(tmp_path, "grid.jpg")
    detail = source_image(tmp_path, "detail.jpg", (300, 30))
    board = {"grid_path": grid, "frames": [], "manifest": "[1] 0:03",
             "text_detail": {"path": detail, "manifest": "文字裁切[1] 对应宫格[1]"},
             "ocr": {"frames": [{"frame_id": 1, "texts": [], "status": "unavailable"}]}}
    watcher = SimpleNamespace(settings=SimpleNamespace(bool=lambda _: True),
                              _cleanup_storyboard=VideoWatch._cleanup_storyboard)
    with patch.object(storyboard, "build_storyboard", return_value=board):
        content, returned = asyncio.run(VideoWatch._storyboard_content(watcher, str(video), "看片"))
    assert returned is board
    assert [part["type"] for part in content] == ["image_url", "image_url", "text"]
    assert base64.b64decode(content[1]["image_url"]["url"].split(",", 1)[1]) == Path(detail).read_bytes()
    assert "文字无法可靠辨认" in content[-1]["text"]
    assert '"frame_id"' in content[-1]["text"]
    assert '"at"' not in content[-1]["text"]
    assert "对应宫格[1]" in content[-1]["text"]
    assert "不能把全部文字归到最后一帧" in content[-1]["text"]
    VideoWatch._cleanup_storyboard(board)
    assert not Path(grid).exists() and not Path(detail).exists()
    assert video.exists()


def test_storyboard_keeps_persona_and_comment_protocol(tmp_path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"0" * 2048)
    grid = source_image(tmp_path, "grid.jpg")
    board = {"grid_path": grid, "frames": [], "manifest": "[1] 0:03",
             "selected_timestamps": [3],
             "ocr": {"frames": [{"frame_id": 1, "texts": [], "status": "no_text"}]}}
    captured = {}

    class Vision:
        async def _astrbot_multimodal_generate(self, provider, content, **kwargs):
            captured.update(kwargs)
            assert '"comment"' in content[-1]["text"] and '"frame_id"' in content[-1]["text"]
            return json.dumps({"comment": {"decision": "skip", "text": ""},
                               "timeline": [{"frame_id": 1, "visual": "坦克转动炮塔"}],
                               "impression": "积木拼装过程", "audio": "听不到",
                               "sensitive": False, "comment_hint": "聊拼装"}, ensure_ascii=False)

    host = SimpleNamespace(config={}, vision=Vision(),
                           settings=SimpleNamespace(bool=lambda k: k == "VIDEO_STORYBOARD_ENABLED",
                                                    get=lambda _: False),
                           video_files=SimpleNamespace(_cleanup_video_artifacts=lambda *a: None))
    with patch.object(storyboard, "build_storyboard", return_value=board):
        result = asyncio.run(VideoWatch(host)._analyze_single_segment(
            str(video), {"title": "拼装"}, "none", "fake-provider", None, None,
            offset=10, length=5, comment={"system_prompt": "当前人格", "prompt": "选择是否评论"}))
    assert captured["system_prompt"] == "当前人格" and captured["max_tokens"] == 1500
    assert result["timeline"] == [(13, "坦克转动炮塔")]
    # 沿用旧解析器的存储契约：skip 的评论正文为空字符串。
    assert result["comment"] == ""
    assert not Path(grid).exists() and video.exists()
