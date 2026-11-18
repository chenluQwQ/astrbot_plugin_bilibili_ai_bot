"""本地 OCR：原帧两次识别一致且高置信度才入档，低清文字不能交给视觉模型猜。"""
import math
import threading
import unicodedata

OCR_MIN_SCORE = 0.95
OCR_SCALES = (2, 3)
OCR_MAX_LINE_CHARS = 160
OCR_MAX_FRAME_CHARS = 900
_engine = None
_lock = threading.Lock()


def _normalized(text):
    return "".join(unicodedata.normalize("NFKC", str(text)).casefold().split())


def _records(result, scale):
    records = []
    for box, text, score in result or ():
        points = [[float(x) / scale, float(y) / scale] for x, y in box]
        coords = [min(p[0] for p in points), min(p[1] for p in points),
                  max(p[0] for p in points), max(p[1] for p in points)]
        score = float(score)
        if text and math.isfinite(score):
            records.append({"box": coords, "text": str(text).strip(), "score": score})
    return records


def _overlap(a, b):
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[2], b[2]), min(a[3], b[3])
    area = max(0, x1 - x0) * max(0, y1 - y0)
    smallest = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))
    return area / smallest if smallest > 0 else 0


def consensus_records(first, second):
    """同时检查文字、置信度、位置，不根据上下文修词，不把另一行的同词拿来凑一致。"""
    accepted, used = [], set()
    for record in sorted(first, key=lambda row: (row["box"][1], row["box"][0])):
        for index, other in enumerate(second):
            if index in used or _overlap(record["box"], other["box"]) < 0.7:
                continue
            if (all(math.isfinite(r["score"]) and OCR_MIN_SCORE <= r["score"] <= 1 for r in (record, other))
                    and _normalized(record["text"]) == _normalized(other["text"])
                    and 0 < len(record["text"]) <= OCR_MAX_LINE_CHARS):
                accepted.append(record)
                used.add(index)
                break
    return accepted


def confirmed_records(image, first, second, engine):
    """原帧行裁切两次一致，并得到至少一次整帧识别支持；分歧时也不靠多数猜字。"""
    from PIL import Image
    accepted = []
    for region in first:
        candidates = [region] + [r for r in second if _overlap(region["box"], r["box"]) >= 0.7]
        if not any(r["score"] >= OCR_MIN_SCORE for r in candidates):
            continue
        x0, y0, x1, y1 = region["box"]
        box = (max(0, math.floor(x0) - 2), max(0, math.floor(y0) - 2),
               min(image.width, math.ceil(x1) + 2), min(image.height, math.ceil(y1) + 2))
        crop = image.crop(box)
        confirmations = []
        for scale in OCR_SCALES:
            result, _ = engine(crop.resize((crop.width * scale, crop.height * scale), Image.LANCZOS),
                               use_det=False, use_cls=False)
            if not result or len(result) != 1:
                break
            text, score = result[0]
            confirmations.append({"box": region["box"], "text": str(text).strip(), "score": float(score)})
        if len(confirmations) != 2 or not consensus_records(confirmations[:1], confirmations[1:]):
            continue
        # 保留整帧读到的原文，不使用提示词/真值词典改字；裁切只作复核。
        match = next((r for r in candidates if r["score"] >= OCR_MIN_SCORE
                      and _normalized(r["text"]) == _normalized(confirmations[0]["text"])), None)
        if match and len(match["text"]) <= OCR_MAX_LINE_CHARS:
            accepted.append(match)
    return sorted(accepted, key=lambda r: (r["box"][1], r["box"][0]))


def read_storyboard_text(paths):
    """缺依赖/缺原帧/识别失败均拒识；引擎延迟加载、限线程、串行复用，避免重复占内存。"""
    from PIL import Image
    global _engine
    frames = []
    with _lock:
        try:
            if _engine is None:
                from rapidocr_onnxruntime import RapidOCR
                _engine = RapidOCR(text_score=0.0, intra_op_num_threads=2, inter_op_num_threads=1,
                                   det_limit_type="max", det_limit_side_len=1280)
        except Exception:
            return {"engine": "unavailable", "frames": [
                {"frame_id": index, "texts": [], "status": "unavailable"}
                for index, _ in enumerate(paths, 1)
            ]}
        for frame_id, path in enumerate(paths, 1):
            row = {"frame_id": frame_id, "texts": [], "status": "source_missing"}
            if path:
                try:
                    with Image.open(path) as source:
                        image = source.convert("RGB")
                    results = []
                    for scale in OCR_SCALES:
                        enlarged = image.resize((image.width * scale, image.height * scale), Image.LANCZOS)
                        result, _ = _engine(enlarged, use_cls=False)
                        results.append(_records(result, scale))
                    accepted = confirmed_records(image, *results, _engine)
                    # 空白帧没有拒识；只在检测到但无法确认的文字处明确标记。
                    row["status"] = "no_text" if not any(results) else "ok"
                    row["detected_lines"] = max(map(len, results))
                    row["accepted_lines"] = len(accepted)
                    row["texts"] = [r["text"] for r in accepted]
                    if len(accepted) < row["detected_lines"]:
                        row["status"] = "partial"
                    if sum(map(len, row["texts"])) > OCR_MAX_FRAME_CHARS:
                        row["texts"], row["accepted_lines"], row["status"] = [], 0, "budget_exceeded"
                except Exception:
                    row["status"] = "failed"
            frames.append(row)
    return {"engine": "rapidocr_onnxruntime", "min_score": OCR_MIN_SCORE,
            "scales": list(OCR_SCALES), "confirmation": "two_crops_and_one_full_frame", "frames": frames}
