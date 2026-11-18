"""分镜分析：镜头检测、选帧、按文字密度调整宫格并补充原帧文字裁切。

改编自 dsh-video-storyboard（https://github.com/SAOS-3624/dsh-video-storyboard）的低成本工作流，
但完全用 ffmpeg + numpy + Pillow 在本地实现，不依赖任何外部 JS 插件：

1. ffmpeg 按固定 fps 密集抽帧（降到小尺寸，仅用于镜头差异检测）；
2. numpy 比较相邻帧的 8×8 灰度指纹，识别镜头切换点；
3. 密度驱动选帧：优先在镜头切换密集处取代表帧，同时保证时间轴上均匀覆盖；
4. Pillow 拼成宫格（默认 3×3），按文字密度保留尺寸或补充原帧文字裁切；
5. 生成带时间码的文本清单，供视觉模型一次调用理解全片脉络。

对外只暴露一个入口：build_storyboard(video_path, ...)，返回 dict：
    grid_path   宫格图路径（临时文件，调用方负责清理）
    frames      密集采样帧路径列表（供清理）
    manifest    带时间码的文本清单
    duration    视频时长（秒）
"""

import os
import subprocess

try:
    import numpy as np
except ImportError:  # pragma: no cover - 运行时 numpy 一定存在，这里仅防御
    np = None

try:
    from PIL import Image, ImageDraw, ImageFilter
except ImportError:  # pragma: no cover
    Image = None


# ── 默认参数 ──────────────────────────────────────────────────────────────
DEFAULT_SAMPLE_FPS = 1.0        # 密集抽帧率（帧/秒），用于镜头检测
DEFAULT_DOWNSCALE_W = 160       # 检测帧缩小后的宽度
DEFAULT_GRID_COLS = 3           # 宫格列数
DEFAULT_GRID_ROWS = 3           # 宫格行数（总格数 = cols * rows）
DEFAULT_CELL_W = 360            # 宫格每格宽度（喂给视觉模型的分辨率）
DEFAULT_CELL_H = 200            # 宫格每格高度（等比缩放，取上限）
TEXT_DETAIL_MAX_REGIONS = 4
TEXT_DETAIL_MAX_SIZE = (720, 200)
TEXT_DETAIL_LABEL_W = 32
TEXT_READABILITY_PROMPT = (
    "画面文字只能记录确实看清的内容；姓名、数字、菜单和字幕看不清时写"
    "“文字无法辨认”，不要凭上下文猜测。文字裁切与对应宫格属于同一帧，不是新镜头。"
)

# 帧间差异阈值：超过该值判定为镜头切换（0~255 的灰度均方根差）
DEFAULT_CUT_THRESHOLD = 28.0


def _ffprobe_duration(video_path):
    """读取视频时长（秒），失败返回 0.0。"""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", video_path],
            capture_output=True, text=True, timeout=60,
        )
        if out.returncode == 0 and out.stdout.strip():
            return float(out.stdout.strip())
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    return 0.0


def _format_ts(seconds):
    """把秒转成 mm:ss 或 h:mm:ss 时间码。"""
    seconds = max(0, int(round(seconds)))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _sample_dense_frames(video_path, fps, downscale_w, tmp_dir):
    """密集抽帧：返回 [(timestamp_sec, frame_path), ...]。

    用 ffmpeg 的 fps 滤镜按时间均匀抽帧并缩小，帧以 tmp_dir 下的顺序编号命名。
    """
    pattern = os.path.join(tmp_dir, "dense_%05d.jpg")
    cmd = [
        "ffmpeg", "-y", "-i", video_path,
        "-vf", f"fps={fps},scale={downscale_w}:-2",
        "-q:v", "6", pattern,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    frames = []
    if proc.returncode == 0:
        names = sorted(n for n in os.listdir(tmp_dir) if n.startswith("dense_") and n.endswith(".jpg"))
        step = 1.0 / float(fps)
        for idx, name in enumerate(names):
            frames.append((idx * step, os.path.join(tmp_dir, name)))
    return frames


def _frame_signature(path):
    """把一帧转成 8×8 的灰度缩略向量，用于快速差异计算。

    用 Pillow 的 L 通道把整帧压到 64 维亮度指纹。
    """
    if Image is None or np is None:
        return None
    with Image.open(path) as im:
        gray = im.convert("L").resize((8, 8))
        arr = np.asarray(gray, dtype=np.float32)
    return arr.ravel()


def _detect_cuts(frames):
    """在密集帧序列上做镜头切换检测。

    返回布尔列表，与 frames 等长；True 表示该帧相对上一帧发生了明显画面切换。
    无 numpy/Pillow 时退化为按固定间隔均匀取样（此时不判定镜头）。
    """
    if Image is None or np is None:
        return [False] * len(frames)

    sigs = [_frame_signature(p) for _, p in frames]
    cuts = [False] * len(frames)
    prev = None
    for i, sig in enumerate(sigs):
        if sig is None:
            continue
        if prev is not None:
            diff = float(np.sqrt(np.mean((sig - prev) ** 2)))
            if diff > DEFAULT_CUT_THRESHOLD:
                cuts[i] = True
        prev = sig
    return cuts


def _select_representative_frames(frames, cuts, grid_cells):
    """密度驱动选帧：保证时间轴均匀覆盖 + 镜头切换密集处优先。

    策略：
    - 把视频按时长均分成 grid_cells 个桶；
    - 每个桶内，若有镜头切换点，优先取第一个切换帧（画面突变处信息量最大）；
    - 否则取桶中点附近的一帧；
    - 返回选中的帧下标列表（按时间升序，去重）。
    """
    n = len(frames)
    if n == 0:
        return []
    if n <= grid_cells:
        return list(range(n))

    picks = []
    bucket_size = n / float(grid_cells)
    for b in range(grid_cells):
        start = int(round(b * bucket_size))
        end = int(round((b + 1) * bucket_size))
        if end <= start:
            end = start + 1
        if end > n:
            end = n
        seg_cuts = [i for i in range(start, end) if cuts[i]]
        if seg_cuts:
            picks.append(seg_cuts[0])
        else:
            mid = (start + end) // 2
            if mid >= n:
                mid = n - 1
            picks.append(mid)

    # 去重并保持时间顺序
    seen = set()
    result = []
    for i in picks:
        if i not in seen:
            seen.add(i)
            result.append(i)
    result.sort()
    return result


def _extract_hires_frames(video_path, timestamps, cell_w, cell_h, tmp_dir):
    """按给定时间戳重新抽取高分辨率帧，用于宫格图（避免用检测用的小尺寸帧放大）。

    保留源视频像素，宫格缩放放到后面；文字裁切必须来自这些原帧。
    返回等长列表，单帧失败用 None 占位，避免后续时间码错位。
    """
    paths = []
    for i, ts in enumerate(timestamps):
        out_path = os.path.join(tmp_dir, f"hires_{i:03d}.jpg")
        cmd = [
            "ffmpeg", "-y", "-ss", f"{ts:.3f}", "-i", video_path,
            "-vframes", "1", "-q:v", "2",
            out_path,
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        paths.append(out_path if proc.returncode == 0 and os.path.exists(out_path) else None)
    return paths


def _component_boxes(mask):
    """逐行合并相邻像素段，找局部对比度形成的笔画；不做 OCR，也不推测文字内容。"""
    parents, boxes = [], []

    def root(i):
        while parents[i] != i:
            parents[i] = parents[parents[i]]
            i = parents[i]
        return i

    previous = []
    for y, row in enumerate(mask):
        padded = np.pad(row.astype(np.int8), (1, 1))
        changes = np.diff(padded)
        runs = zip(np.flatnonzero(changes == 1), np.flatnonzero(changes == -1))
        current = []
        for x0, x1 in runs:
            touched = {root(i) for a, b, i in previous if a <= x1 and b >= x0}
            if touched:
                i = min(touched)
                for other in touched - {i}:
                    parents[other] = i
                    a, b, c, d = boxes[other]
                    old = boxes[i]
                    boxes[i] = [min(old[0], a), min(old[1], b), max(old[2], c), max(old[3], d)]
                old = boxes[i]
                boxes[i] = [min(old[0], int(x0)), old[1], max(old[2], int(x1)), y + 1]
            else:
                i = len(parents)
                parents.append(i)
                boxes.append([int(x0), y, int(x1), y + 1])
            current.append((int(x0), int(x1), i))
        previous = current
    return [box for i, box in enumerate(boxes) if root(i) == i]


def _text_regions(path):
    """保守检测成行的小笔画，返回原帧坐标。误报只会多保留尺寸，漏报仍可能发生。"""
    if Image is None or np is None:
        return None
    with Image.open(path) as source:
        original_size = source.size
        gray = source.convert("L")
    # 固定检测尺度；不放大低清源图。裁切仍从原帧取像素。
    gray.thumbnail((640, 640), Image.LANCZOS)
    width, height = gray.size
    pixels = np.asarray(gray, dtype=np.int16)
    local = np.asarray(gray.filter(ImageFilter.GaussianBlur(2)), dtype=np.int16)
    contrast = np.abs(pixels - local) > 18
    candidates = []
    for polarity, mask in enumerate((pixels - local > 18, local - pixels > 18)):
        for box in _component_boxes(mask):
            x0, y0, x1, y1 = box
            w, h = x1 - x0, y1 - y0
            if 2 <= w <= 32 and 4 <= h <= 32 and w <= 2.5 * h:
                candidates.append([*box, polarity])
    candidates.sort(key=lambda b: (b[1] + b[3], b[0]))
    lines = []
    for box in candidates:
        x0, y0, x1, y1, polarity = box
        cy = (y0 + y1) / 2
        matched = None
        for line in lines:
            if polarity == line["polarity"] and abs(cy - line["cy"]) <= max(2, min(y1 - y0, line["height"]) * 0.35):
                matched = line
                break
        if matched is None:
            matched = {"cy": cy, "height": y1 - y0, "boxes": [], "polarity": polarity}
            lines.append(matched)
        matched["boxes"].append(box)
    regions = []
    for line in lines:
        glyphs = sorted(line["boxes"], key=lambda b: b[0])
        groups = [[]]
        for box in glyphs:
            if groups[-1] and box[0] - max(b[2] for b in groups[-1]) > 24:
                groups.append([])
            groups[-1].append(box)
        for group in groups:
            # 两种极性的笔画会重叠，按不同 x 位置计数，不能当成两倍文字。
            if len({b[0] // 3 for b in group}) < 5:
                continue
            x0, y0 = min(b[0] for b in group), min(b[1] for b in group)
            x1, y1 = max(b[2] for b in group), max(b[3] for b in group)
            if x1 - x0 < max(30, width * 0.10):
                continue
            glyph_height = float(np.median([b[3] - b[1] for b in group]))
            # 字符应排在同一条窄行；随机纹理的组件高低不齐、上下仍有大量边缘。
            if y1 - y0 > glyph_height * 1.8:
                continue
            band_h = y1 - y0
            neighbors = np.concatenate((
                contrast[max(0, y0 - band_h):y0, x0:x1].ravel(),
                contrast[y1:min(height, y1 + band_h), x0:x1].ravel(),
            ))
            if neighbors.size:
                ink = float(contrast[y0:y1, x0:x1].mean())
                background = float(neighbors.mean())
                # 深浅相间的菜单行本身也有边缘；高占比、排列整齐的字行仍应保留。
                if ink < max(0.08, background * 1.8) and not (ink >= 0.70 and ink >= background * 1.2):
                    continue
            # 左上角极小水印不参与缩图决策，避免同一角标在九帧中耗尽裁切预算。
            if x1 < width * 0.22 and y1 < height * 0.12 and glyph_height < 6:
                continue
            sx, sy = original_size[0] / width, original_size[1] / height
            region = {
                "box": [max(0, int((x0 - 4) * sx)), max(0, int((y0 - 3) * sy)),
                        min(original_size[0], int((x1 + 4) * sx)),
                        min(original_size[1], int((y1 + 3) * sy))],
                "glyph_height": glyph_height * sy,
            }
            # 同一行的明暗笔画只算一次；不同字幕仍按不同帧保护。
            a, b, c, d = region["box"]
            if any(abs((b + d) - (r["box"][1] + r["box"][3])) < 6 * sy
                   and min(c, r["box"][2]) - max(a, r["box"][0]) > min(c - a, r["box"][2] - r["box"][0]) * 0.5
                   for r in regions):
                continue
            regions.append(region)
    return regions


def _build_text_detail(selected, regions, tmp_dir):
    """把小字行按原像素裁切后纵向排列；空间不足就返回 None，不能缩字或截断。"""
    crops, manifest = [], []
    for frame_index, region in regions:
        with Image.open(selected[frame_index]) as source:
            box = list(region["box"])
            # 顶部/底部字幕可能只有部分笔画被检测到，保留整行，避免裁掉句首或句尾。
            if (box[1] < source.height * 0.25 or box[3] > source.height * 0.75) and box[2] - box[0] >= source.width * 0.25:
                box[0], box[2] = 0, source.width
            crop = source.convert("RGB").crop(box)
        if crop.width + TEXT_DETAIL_LABEL_W > TEXT_DETAIL_MAX_SIZE[0]:
            return None
        crops.append(crop)
        manifest.append(f"文字裁切[{len(crops)}] 对应宫格[{frame_index + 1}]")
    if not crops:
        return None
    height = sum(max(16, c.height) for c in crops) + 4 * (len(crops) - 1)
    if height > TEXT_DETAIL_MAX_SIZE[1]:
        return None
    canvas = Image.new("RGB", (max(c.width for c in crops) + TEXT_DETAIL_LABEL_W, height), (18, 18, 22))
    draw = ImageDraw.Draw(canvas)
    y = 0
    for crop, (frame_index, _) in zip(crops, regions):
        draw.text((2, y), f"[{frame_index + 1}]", fill=(255, 255, 255))
        canvas.paste(crop, (TEXT_DETAIL_LABEL_W, y))
        y += max(16, crop.height) + 4
    path = os.path.join(tmp_dir, "text_detail.jpg")
    canvas.save(path, "JPEG", quality=95)
    return {"path": path, "manifest": "\n".join(manifest)}


def _render_storyboard(selected, cell_w, cell_h, cols, rows, tmp_dir, *, source_complete=True):
    """默认普通画面缩图；检测不确定、文字密集或补充裁切放不下时保留原宫格。"""
    normal_size = (cols * cell_w, rows * cell_h)
    compact_size = (min(normal_size[0], 720), min(normal_size[1], 400))
    regions = [_text_regions(path) for path in selected]
    reason = "compact"
    if not source_complete or any(r is None for r in regions):
        reason = "text_detection_unavailable"
    elif any(len(r) >= 2 for r in regions):
        reason = "dense_text"
    details = []
    for index, found in enumerate(regions):
        with Image.open(selected[index]) as source:
            scale = min(cell_w / source.width, cell_h / source.height)
        for region in found or ():
            if (reason == "compact" or scale < 0.95) and region["glyph_height"] * scale * compact_size[0] / normal_size[0] < 10:
                details.append((index, region))
    detail = None
    if reason == "compact" and details:
        if len(details) <= TEXT_DETAIL_MAX_REGIONS:
            detail = _build_text_detail(selected, details, tmp_dir)
        if detail is None:
            reason = "text_detail_budget"
    elif reason == "dense_text" and details:
        # 大图仍会缩小高清源帧时才补裁切；低清源已经原像素呈现，不重复花图像 token。
        smallest = min(details, key=lambda item: item[1]["glyph_height"])
        detail = _build_text_detail(selected, [smallest], tmp_dir)
    grid_path = _build_grid(selected, cell_w, cell_h, cols, rows, tmp_dir=tmp_dir)
    if grid_path and reason == "compact":
        with Image.open(grid_path) as canvas:
            canvas.resize(compact_size, Image.LANCZOS).save(grid_path, "JPEG", quality=85)
    return {
        "grid_path": grid_path, "text_detail": detail, "resize_reason": reason,
        "grid_size": list(compact_size if reason == "compact" else normal_size),
        "text_region_counts": [len(r) if r is not None else None for r in regions],
    }


def _build_grid(selected, cell_w, cell_h, cols, rows, *, tmp_dir=None):
    """把选中的代表帧拼成一张宫格图，返回临时文件路径。

    每格先等比缩放到不超过 (cell_w, cell_h)，并居中贴到固定尺寸的灰底格子上，
    保证整张宫格图对齐。空白格（选中帧数不足 cols*rows 时）留灰底。
    """
    import tempfile

    if Image is None:
        return None

    cells = []
    for path in selected:
        with Image.open(path) as im:
            im = im.convert("RGB")
            im.thumbnail((cell_w, cell_h), Image.LANCZOS)
            cell = Image.new("RGB", (cell_w, cell_h), (18, 18, 22))
            cell.paste(im, ((cell_w - im.width) // 2, (cell_h - im.height) // 2))
            cells.append(cell)

    total = cols * rows
    while len(cells) < total:
        cells.append(Image.new("RGB", (cell_w, cell_h), (18, 18, 22)))

    canvas = Image.new("RGB", (cols * cell_w, rows * cell_h), (18, 18, 22))
    for idx, cell in enumerate(cells):
        r, c = divmod(idx, cols)
        canvas.paste(cell, (c * cell_w, r * cell_h))

    fd, grid_path = tempfile.mkstemp(suffix=".jpg", prefix="bili_storyboard_", dir=tmp_dir)
    os.close(fd)
    canvas.save(grid_path, "JPEG", quality=85)
    return grid_path


def build_storyboard(
    video_path,
    *,
    sample_fps=DEFAULT_SAMPLE_FPS,
    downscale_w=DEFAULT_DOWNSCALE_W,
    grid_cols=DEFAULT_GRID_COLS,
    grid_rows=DEFAULT_GRID_ROWS,
    cell_w=DEFAULT_CELL_W,
    cell_h=DEFAULT_CELL_H,
    cut_threshold=DEFAULT_CUT_THRESHOLD,
    tmp_dir=None,
):
    """构建分镜宫格图与时间码清单。

    参数：
      video_path   视频文件路径
      sample_fps   密集抽帧率（镜头检测用，越高越精细，默认 1fps）
      grid_cols/rows  宫格行列数，决定代表帧数量上限
      cell_w/cell_h   宫格每格分辨率（喂给视觉模型）

    返回 dict：
      grid_path   宫格图路径（None 表示失败）
      frames      代表帧路径列表（原始密集帧文件，供清理）
      manifest    时间码文本清单
      duration    视频时长（秒）
    """
    duration = _ffprobe_duration(video_path)
    tmp_dir = tmp_dir or os.path.join(
        os.path.dirname(video_path),
        os.path.splitext(os.path.basename(video_path))[0] + "_storyboard",
    )
    os.makedirs(tmp_dir, exist_ok=True)

    frames = _sample_dense_frames(video_path, sample_fps, downscale_w, tmp_dir)
    if not frames:
        return {
            "grid_path": None, "frames": [], "manifest": "",
            "duration": duration, "tmp_dir": tmp_dir,
        }

    cuts = _detect_cuts(frames)
    grid_cells = int(grid_cols) * int(grid_rows)
    picked_idx = _select_representative_frames(frames, cuts, grid_cells)
    picked_timestamps = [frames[i][0] for i in picked_idx]

    # 用原始分辨率重新抽取代表帧，保证宫格图清晰（检测用的是小尺寸帧，直接放大会糊）
    hires = _extract_hires_frames(
        video_path, picked_timestamps, int(cell_w), int(cell_h), tmp_dir
    )
    # 失败帧在原位置回退，不能把后面的画面对应到前面的时间码。
    selected = [path or frames[picked_idx[i]][1] for i, path in enumerate(hires)]
    rendered = _render_storyboard(
        selected, int(cell_w), int(cell_h), int(grid_cols), int(grid_rows), tmp_dir,
        source_complete=all(hires),
    )
    # OCR只用原尺寸帧；抽取失败的低清检测帧不能被当成可靠文字来源。
    from .storyboard_text import read_storyboard_text
    ocr = read_storyboard_text(hires)

    # 时间码清单
    manifest_lines = []
    for order, ts in enumerate(picked_timestamps, start=1):
        manifest_lines.append(f"[{order}] {_format_ts(ts)}")
    manifest = "\n".join(manifest_lines)

    return {
        **rendered,
        "frames": [p for _, p in frames],   # 所有密集帧，调用方统一清理
        "manifest": manifest,
        "selected_timestamps": picked_timestamps,
        "ocr": ocr,
        "duration": duration,
        "tmp_dir": tmp_dir,                 # 密集帧所在临时目录，调用方清理
    }


def extract_audio_segments(video_path, segment_seconds, tmp_dir):
    """从视频抽取音轨并按固定时长切成 wav 段，供语音转文字（ASR）逐段处理。

    参数：
      video_path      视频文件路径
      segment_seconds 每段时长（秒），默认与视频分析的切片长度对齐
      tmp_dir         输出目录（段文件 segment_000.wav / segment_001.wav ...）

    返回：
      [(start_sec, wav_path), ...]  按时间顺序排列的分段列表；无音轨时返回空列表。
    """
    try:
        segment_seconds = int(segment_seconds)
    except (TypeError, ValueError):
        segment_seconds = 300
    segment_seconds = max(60, min(1800, segment_seconds))

    duration = _ffprobe_duration(video_path)
    if duration <= 0:
        return []

    os.makedirs(tmp_dir, exist_ok=True)
    segments = []
    start = 0.0
    index = 0
    while start < duration - 1.0:  # 剩余不足 1 秒不值得单独切
        out_path = os.path.join(tmp_dir, f"segment_{index:03d}.wav")
        cmd = [
            "ffmpeg", "-y",
            "-ss", f"{start:.3f}",
            "-i", video_path,
            "-t", str(segment_seconds),
            "-vn",                       # 只要音频，丢弃视频流
            "-ac", "1",                  # 单声道，ASR 通常够用且省流量
            "-ar", "16000",              # 16kHz，语音转写标准采样率
            "-c:a", "pcm_s16le",         # 未压缩 PCM，兼容性最好
            out_path,
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if proc.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            segments.append((start, out_path))
        start += segment_seconds
        index += 1
    return segments
