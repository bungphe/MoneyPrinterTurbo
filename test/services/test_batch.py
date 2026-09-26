import csv
import os
import shutil
import sys
import unittest
from pathlib import Path
from unittest import mock

# add project root to python path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.models.schema import VideoParams
from app.services import batch, llm
from app.utils import utils


class TestParseBatchInput(unittest.TestCase):
    def test_text_lines(self):
        items = batch.parse_batch_input(
            text="Chủ đề 1\n\n  Chủ đề 2  \n# ghi chú\nchủ đề 1\n"
        )
        self.assertEqual([i.subject for i in items], ["Chủ đề 1", "Chủ đề 2"])

    def test_csv_vietnamese_headers(self):
        content = batch.SAMPLE_CSV.encode("utf-8-sig")
        items = batch.parse_batch_input(csv_content=content)
        self.assertEqual(len(items), 3)
        self.assertEqual(items[0].subject, "5 mẹo tiết kiệm tiền mỗi tháng")
        self.assertEqual(items[2].script, "Cà phê sữa đá là thức uống quen thuộc của người Việt.")
        self.assertEqual(items[2].keywords, "vietnamese coffee, iced coffee")

    def test_csv_english_headers_and_text_merge(self):
        content = "Subject,Script\nTopic A,Hello\n".encode("utf-8")
        items = batch.parse_batch_input(text="Topic B", csv_content=content)
        self.assertEqual([i.subject for i in items], ["Topic A", "Topic B"])
        self.assertEqual(items[0].script, "Hello")

    def test_csv_without_subject_column(self):
        with self.assertRaises(ValueError):
            batch.parse_batch_input(csv_content=b"foo,bar\n1,2\n")


class TestRunBatch(unittest.TestCase):
    def setUp(self):
        self.batch_id = "batch-unittest"

    def tearDown(self):
        shutil.rmtree(utils.task_dir(self.batch_id), ignore_errors=True)

    def test_run_batch_continues_after_failures(self):
        def fake_start(task_id, params):
            if params.video_subject == "boom":
                raise RuntimeError("tts failed")
            if params.video_subject == "empty":
                return None
            return {"videos": [f"/tmp/{params.video_subject}.mp4"], "script": "kịch bản"}

        base = VideoParams(video_subject="giữ nguyên")
        items = batch.parse_batch_input(text="ok1\nboom\nempty\nok2")
        progress = []
        with mock.patch.object(batch.tm, "start", side_effect=fake_start), mock.patch.object(
            batch.llm,
            "generate_post_metadata",
            return_value={"title": "Tiêu đề", "hashtags": ["#a", "#b"]},
        ):
            output = batch.run_batch(
                items,
                base,
                batch_id=self.batch_id,
                on_progress=lambda done, total, r: progress.append((done, total, r.status)),
            )

        statuses = [r.status for r in output["results"]]
        self.assertEqual(
            statuses,
            [batch.STATUS_SUCCESS, batch.STATUS_FAILED, batch.STATUS_FAILED, batch.STATUS_SUCCESS],
        )
        self.assertEqual(output["results"][1].error, "tts failed")
        self.assertEqual([p[0] for p in progress], [1, 2, 3, 4])
        # base params must not be modified by the batch
        self.assertEqual(base.video_subject, "giữ nguyên")

        with open(output["csv_file"], encoding="utf-8-sig") as fp:
            rows = list(csv.DictReader(fp))
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0]["video"], "/tmp/ok1.mp4")
        self.assertEqual(rows[0]["tieu_de"], "Tiêu đề")
        self.assertEqual(rows[0]["hashtag"], "#a #b")
        self.assertEqual(rows[1]["trang_thai"], batch.STATUS_FAILED)


class TestGeneratePostMetadata(unittest.TestCase):
    def test_parses_json_wrapped_in_text(self):
        response = 'Đây là kết quả:\n{"title": "5 mẹo tiết kiệm", "hashtags": ["tietkiem", "#meo hay"]}'
        with mock.patch.object(llm, "_generate_response", return_value=response):
            data = llm.generate_post_metadata("tiết kiệm", "kịch bản")
        self.assertEqual(data["title"], "5 mẹo tiết kiệm")
        self.assertEqual(data["hashtags"], ["#tietkiem", "#meohay"])

    def test_fallback_on_error(self):
        with mock.patch.object(llm, "_generate_response", return_value="Error: no api key"):
            data = llm.generate_post_metadata("tiết kiệm", "kịch bản")
        self.assertEqual(data, {"title": "tiết kiệm", "hashtags": []})


if __name__ == "__main__":
    unittest.main()
