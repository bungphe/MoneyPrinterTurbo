"""
批量生成视频：根据主题列表（每行一个主题，或 CSV 文件）依次生成多个视频，
并为每个视频生成发布用的标题和话题标签，最后汇总为 CSV 结果文件。
"""

import copy
import csv
import io
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, List, Optional
from uuid import uuid4

from loguru import logger

from app.models.schema import VideoParams
from app.services import llm
from app.services import task as tm
from app.utils import utils

# CSV 表头支持越南语 / 英语 / 中文列名
SUBJECT_COLUMNS = ("subject", "topic", "chu_de", "chủ đề", "chu de", "主题")
SCRIPT_COLUMNS = ("script", "kich_ban", "kịch bản", "kich ban", "文案")
KEYWORDS_COLUMNS = ("keywords", "terms", "tu_khoa", "từ khóa", "tu khoa", "关键词")

RESULT_COLUMNS = ["stt", "chu_de", "trang_thai", "video", "tieu_de", "hashtag", "loi"]

STATUS_SUCCESS = "thanh_cong"
STATUS_FAILED = "that_bai"

SAMPLE_CSV = (
    "chu_de,kich_ban,tu_khoa\n"
    "5 mẹo tiết kiệm tiền mỗi tháng,,\n"
    "Lợi ích của việc uống đủ nước,,\n"
    'Cà phê sữa đá Việt Nam,"Cà phê sữa đá là thức uống quen thuộc của người Việt.","vietnamese coffee, iced coffee"\n'
)


@dataclass
class BatchItem:
    subject: str
    script: str = ""
    keywords: str = ""


@dataclass
class BatchResult:
    index: int
    subject: str
    status: str
    videos: List[str] = field(default_factory=list)
    title: str = ""
    hashtags: List[str] = field(default_factory=list)
    error: str = ""


def _pick(row: dict, names) -> str:
    for key, value in row.items():
        if key and key.strip().lower() in names:
            return (value or "").strip()
    return ""


def parse_batch_input(text: str = "", csv_content: Optional[bytes] = None) -> List[BatchItem]:
    """
    解析批量输入：
    - text: 每行一个主题，空行和以 # 开头的行会被忽略
    - csv_content: CSV 文件内容，需包含主题列（chu_de / subject），可选文案列和关键词列
    两者可同时提供，结果按 CSV 在前、文本在后合并，并去除重复主题。
    """
    items: List[BatchItem] = []

    if csv_content:
        content = csv_content.decode("utf-8-sig", errors="replace")
        reader = csv.DictReader(io.StringIO(content))
        headers = [h.strip().lower() for h in (reader.fieldnames or []) if h]
        if not any(h in SUBJECT_COLUMNS for h in headers):
            raise ValueError("CSV must contain a subject column (chu_de / subject)")
        for row in reader:
            subject = _pick(row, SUBJECT_COLUMNS)
            if subject:
                items.append(
                    BatchItem(
                        subject=subject,
                        script=_pick(row, SCRIPT_COLUMNS),
                        keywords=_pick(row, KEYWORDS_COLUMNS),
                    )
                )

    for line in (text or "").splitlines():
        subject = line.strip()
        if subject and not subject.startswith("#"):
            items.append(BatchItem(subject=subject))

    unique_items = []
    seen = set()
    for item in items:
        key = item.subject.lower()
        if key not in seen:
            seen.add(key)
            unique_items.append(item)
    return unique_items


def _build_params(base_params: VideoParams, item: BatchItem) -> VideoParams:
    params = copy.deepcopy(base_params)
    params.video_subject = item.subject
    params.video_script = item.script
    params.video_terms = item.keywords
    return params


def write_results_csv(results: List[BatchResult], output_file: str) -> str:
    # utf-8-sig 让 Excel 正确显示越南语
    with open(output_file, "w", encoding="utf-8-sig", newline="") as fp:
        writer = csv.writer(fp)
        writer.writerow(RESULT_COLUMNS)
        for r in results:
            writer.writerow(
                [
                    r.index,
                    r.subject,
                    r.status,
                    " | ".join(r.videos),
                    r.title,
                    " ".join(r.hashtags),
                    r.error,
                ]
            )
    return output_file


def run_batch(
    items: List[BatchItem],
    base_params: VideoParams,
    batch_id: str = "",
    on_progress: Optional[Callable[[int, int, BatchResult], None]] = None,
) -> dict:
    """
    依次为每个主题生成视频。单个主题失败不会中断整个批次。
    on_progress(done, total, result) 在每个主题完成后回调，用于更新界面进度。
    返回 {"batch_id", "results", "csv_file"}。
    """
    if not batch_id:
        batch_id = f"batch-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:6]}"
    batch_dir = utils.task_dir(batch_id)
    csv_file = os.path.join(batch_dir, "ket_qua.csv")

    results: List[BatchResult] = []
    total = len(items)
    logger.info(f"start batch {batch_id}, total: {total}")

    for index, item in enumerate(items, start=1):
        logger.info(f"\n\n## batch {batch_id}: [{index}/{total}] {item.subject}")
        result = BatchResult(index=index, subject=item.subject, status=STATUS_FAILED)
        params = _build_params(base_params, item)
        try:
            task_result = tm.start(task_id=str(uuid4()), params=params)
            videos = (task_result or {}).get("videos") or []
            if videos:
                result.status = STATUS_SUCCESS
                result.videos = videos
                metadata = llm.generate_post_metadata(
                    video_subject=item.subject,
                    video_script=task_result.get("script", "") or item.script,
                    language=params.video_language,
                )
                result.title = metadata.get("title") or item.subject
                result.hashtags = metadata.get("hashtags") or []
            else:
                result.error = "video generation failed"
        except Exception as e:
            logger.exception(f"batch item failed: {item.subject}")
            result.error = str(e)

        results.append(result)
        # 每完成一个就写一次，批次中途中断也能保留已完成的结果
        write_results_csv(results, csv_file)
        if on_progress:
            on_progress(index, total, result)

    success = sum(1 for r in results if r.status == STATUS_SUCCESS)
    logger.success(f"batch {batch_id} finished: {success}/{total} succeeded, results: {csv_file}")
    return {"batch_id": batch_id, "results": results, "csv_file": csv_file}
