
import unittest
import os
import sys
from pathlib import Path
from moviepy import (
    VideoFileClip,
)
# add project root to python path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from app.models.schema import MaterialInfo
from app.services import video as vd
from app.utils import utils

resources_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "resources")

class TestVideoService(unittest.TestCase):
    def setUp(self):
        self.test_img_path = os.path.join(resources_dir, "1.png")
    
    def tearDown(self):
        pass
    
    def test_preprocess_video(self):
        if not os.path.exists(self.test_img_path):
            self.fail(f"test image not found: {self.test_img_path}")
        
        # test preprocess_video function
        m = MaterialInfo()
        m.url = self.test_img_path
        m.provider = "local"
        print(m)
        
        materials = vd.preprocess_video([m], clip_duration=4)
        print(materials)
        
        # verify result
        self.assertIsNotNone(materials)
        self.assertEqual(len(materials), 1)
        self.assertTrue(materials[0].url.endswith(".mp4"))
        
        # moviepy get video info
        clip = VideoFileClip(materials[0].url)
        print(clip)
        
        # clean generated test video file
        if os.path.exists(materials[0].url):
            os.remove(materials[0].url)
    
    def test_wrap_text(self):
        """test text wrapping function"""
        try:
            font_path = os.path.join(utils.font_dir(), "STHeitiMedium.ttc")
            if not os.path.exists(font_path):
                self.fail(f"font file not found: {font_path}")
                
            # test english text wrapping
            test_text_en = "This is a test text for wrapping long sentences in english language"
            
            wrapped_text_en, text_height_en = vd.wrap_text(
                text=test_text_en,
                max_width=300,
                font=font_path,
                fontsize=30
            )
            print(wrapped_text_en, text_height_en)
            # verify text is wrapped
            self.assertIn("\n", wrapped_text_en)
            
            # test chinese text wrapping
            test_text_zh = "这是一段用来测试中文长句换行的文本内容，应该会根据宽度限制进行换行处理"
            wrapped_text_zh, text_height_zh = vd.wrap_text(
                text=test_text_zh,
                max_width=300,
                font=font_path,
                fontsize=30
            )   
            print(wrapped_text_zh, text_height_zh)
            # verify chinese text is wrapped
            self.assertIn("\n", wrapped_text_zh)
        except Exception as e:
            self.fail(f"test wrap_text failed: {str(e)}")

class TestFfmpegPipeline(unittest.TestCase):
    """ffmpeg 快速合成流程的回归测试（效果需与原 MoviePy 流程一致）"""

    def setUp(self):
        import tempfile

        self.tmp_dir = tempfile.mkdtemp()
        self.source_video = os.path.join(self.tmp_dir, "landscape.mp4")
        # 横屏测试素材，用于验证竖屏输出时的等比缩放与黑边
        vd._image_to_video_with_ffmpeg(
            os.path.join(resources_dir, "1.png"), self.source_video, 3
        )

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_image_to_video(self):
        clip = VideoFileClip(self.source_video)
        self.assertAlmostEqual(clip.duration, 3, delta=0.1)
        self.assertEqual(clip.size[0] % 2, 0)
        self.assertEqual(clip.size[1] % 2, 0)
        clip.close()

    def test_render_clip_all_transitions(self):
        for transition in [None, "FadeIn", "FadeOut", "SlideIn", "SlideOut"]:
            output = os.path.join(self.tmp_dir, f"clip-{transition}.mp4")
            duration = vd._render_clip_with_ffmpeg(
                self.source_video, 0.5, 2, output, 540, 960, transition, "left", 2
            )
            clip = VideoFileClip(output)
            self.assertEqual(clip.size, [540, 960])
            self.assertEqual(clip.fps, vd.fps)
            self.assertAlmostEqual(clip.duration, duration, delta=0.1)
            clip.close()

    def test_generate_video_trims_to_audio(self):
        from moviepy import AudioClip
        from app.models.schema import VideoParams

        video = os.path.join(self.tmp_dir, "video.mp4")
        vd._render_clip_with_ffmpeg(self.source_video, 0, 3, video, 1080, 1920, None, "left", 2)
        audio = os.path.join(self.tmp_dir, "voice.mp3")
        AudioClip(lambda t: 0 * t, duration=2, fps=44100).write_audiofile(audio, logger=None)
        subtitle = os.path.join(self.tmp_dir, "sub.srt")
        with open(subtitle, "w", encoding="utf-8") as fp:
            fp.write("1\n00:00:00,000 --> 00:00:01,900\nPhụ đề tiếng Việt\n\n")

        params = VideoParams(video_subject="test")
        params.video_aspect = "9:16"
        params.bgm_type = ""
        output = os.path.join(self.tmp_dir, "final.mp4")
        vd.generate_video(video, audio, subtitle, output, params)

        clip = VideoFileClip(output)
        # 成片时长应与配音一致，而不是更长的素材时长
        self.assertAlmostEqual(clip.duration, 2, delta=0.1)
        self.assertEqual(clip.size, [1080, 1920])
        self.assertIsNotNone(clip.audio)
        clip.close()


if __name__ == "__main__":
    unittest.main() 