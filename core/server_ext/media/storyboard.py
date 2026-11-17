"""分镜分析（storyboard）：HSV 镜头检测 + 密度驱动选帧 + 宫格拼图 + 时间码清单。

改编自 dsh-video-storyboard（https://github.com/SAOS-3624/dsh-video-storyboard）的低成本工作流，
但完全用 ffmpeg + numpy + Pillow 在本地实现，不依赖任何外部 JS 插件：

1. ffmpeg 按固定 fps 密集抽帧（降到小尺寸，仅用于镜头差异检测）；
2. numpy 把相邻帧转成灰度/HSV 直方图，计算帧间差异，识别镜头切换点；
3. 密度驱动选帧：优先在镜头切换密集处取代表帧，同时保证时间轴上均匀覆盖；
4. Pillow 把选出的代表帧拼成一张编号宫格图（默认 3×3）；
5. 生成带时间码的文本清单，供视觉模型一次调用理解全片脉络。

对外只暴露一个入口：build_storyboard(video_path, ...)，返回 dict：
    grid_path   宫格图路径（临时文件，调用方负责清理）
    frames      代表帧路径列表
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
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None


# ── 默认参数 ──────────────────────────────────────────────────────────────
DEFAULT_SAMPLE_FPS = 1.0        # 密集抽帧率（帧/秒），用于镜头检测
DEFAULT_DOWNSCALE_W = 160       # 检测帧缩小后的宽度
DEFAULT_GRID_COLS = 3           # 宫格列数
DEFAULT_GRID_ROWS = 3           # 宫格行数（总格数 = cols * rows）
DEFAULT_CELL_W = 360            # 宫格每格宽度（喂给视觉模型的分辨率）
DEFAULT_CELL_H = 200            # 宫格每格高度（等比缩放，取上限）

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

    HSV 明度通道对光照更鲁棒，这里用 Pillow 的 L 通道 + numpy 均值池化，
    等效于把整帧压到 64 维的亮度指纹。
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

    对每个时间戳用 ffmpeg 抽取一帧，缩放到不超过 (cell_w, cell_h) 但保留原始比例，
    并统一转成 JPEG。返回帧路径列表（与 timestamps 顺序一致）。
    """
    paths = []
    target_w = max(320, int(cell_w))     # 宫格每格至少要这个宽度才够清晰
    for i, ts in enumerate(timestamps):
        out_path = os.path.join(tmp_dir, f"hires_{i:03d}.jpg")
        cmd = [
            "ffmpeg", "-y", "-ss", f"{ts:.3f}", "-i", video_path,
            "-vframes", "1", "-vf", f"scale={target_w}:-2", "-q:v", "5",
            out_path,
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if proc.returncode == 0 and os.path.exists(out_path):
            paths.append(out_path)
    return paths


def _build_grid(selected, cell_w, cell_h, cols, rows):
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

    fd, grid_path = tempfile.mkstemp(suffix=".jpg", prefix="bili_storyboard_")
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
    selected = hires if hires else [frames[i][1] for i in picked_idx]

    grid_path = _build_grid(selected, int(cell_w), int(cell_h), int(grid_cols), int(grid_rows))

    # 时间码清单
    manifest_lines = []
    for order, ts in enumerate(picked_timestamps, start=1):
        manifest_lines.append(f"[{order}] {_format_ts(ts)}")
    manifest = "\n".join(manifest_lines)

    return {
        "grid_path": grid_path,
        "frames": [p for _, p in frames],   # 所有密集帧，调用方统一清理
        "manifest": manifest,
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
