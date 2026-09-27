import glob
import itertools
import os
import random
import re
import gc
import shutil
import subprocess

import numpy as np
from typing import List, Optional
from loguru import logger
from moviepy import (
    AudioFileClip,
    ColorClip,
    CompositeAudioClip,
    CompositeVideoClip,
    ImageClip,
    TextClip,
    VideoFileClip,
    afx,
)
from moviepy.video.tools.subtitles import SubtitlesClip
from PIL import Image, ImageFont

from app.models import const
from app.models.schema import (
    MaterialInfo,
    VideoAspect,
    VideoConcatMode,
    VideoParams,
    VideoTransitionMode,
)
from app.services.utils import video_effects
from app.utils import utils

class SubClippedVideoClip:
    def __init__(self, file_path, start_time=None, end_time=None, width=None, height=None, duration=None):
        self.file_path = file_path
        self.start_time = start_time
        self.end_time = end_time
        self.width = width
        self.height = height
        if duration is None:
            self.duration = end_time - start_time
        else:
            self.duration = duration

    def __str__(self):
        return f"SubClippedVideoClip(file_path={self.file_path}, start_time={self.start_time}, end_time={self.end_time}, duration={self.duration}, width={self.width}, height={self.height})"


audio_codec = "aac"
# Docker 里的 ffmpeg/AAC 组合在默认配置下更容易出现音频质量波动，
# 这里显式抬高音频码率，避免成片阶段因为默认值过低而引入明显失真。
audio_bitrate = "192k"
video_codec = "libx264"
fps = 30


def get_ffmpeg_binary():
    # 优先复用配置里显式指定的 ffmpeg，可避免不同环境下 PATH 不一致。
    configured_ffmpeg = os.environ.get("IMAGEIO_FFMPEG_EXE")
    if configured_ffmpeg:
        return configured_ffmpeg
    if shutil.which("ffmpeg"):
        return "ffmpeg"
    # 系统未安装 ffmpeg 时，回退到 MoviePy 自带的 imageio-ffmpeg 二进制，
    # 否则多片段视频在最后拼接时会失败。
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


def _escape_ffmpeg_concat_path(file_path: str) -> str:
    # concat demuxer 使用单引号包裹路径，路径中的单引号需要先转义。
    return file_path.replace("'", "'\\''")


def concat_video_clips_with_ffmpeg(
    clip_files: List[str], output_file: str, threads: int, output_dir: str
):
    concat_list_file = os.path.join(output_dir, "ffmpeg-concat-list.txt")
    with open(concat_list_file, "w", encoding="utf-8") as fp:
        for clip_file in clip_files:
            absolute_path = os.path.abspath(clip_file)
            fp.write(f"file '{_escape_ffmpeg_concat_path(absolute_path)}'\n")

    # 所有中间片段编码参数一致，优先直接复制流拼接（几乎瞬间完成且无损），
    # 失败时再回退到重新编码。
    copy_command = [
        get_ffmpeg_binary(),
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        concat_list_file,
        "-c",
        "copy",
        output_file,
    ]

    command = [
        get_ffmpeg_binary(),
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        concat_list_file,
        "-c:v",
        video_codec,
        "-threads",
        str(threads or 2),
        "-pix_fmt",
        "yuv420p",
        output_file,
    ]

    try:
        copy_result = subprocess.run(
            copy_command,
            capture_output=True,
            text=True,
            check=False,
        )
        if copy_result.returncode == 0:
            return
        logger.warning("ffmpeg stream copy concat failed, fallback to re-encoding")

        # 使用 ffmpeg 只做一次串联与编码，避免 MoviePy 逐段合并时反复重编码，
        # 从而降低画质劣化与颜色偏移风险。
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            error_message = (result.stderr or result.stdout or "").strip()
            raise RuntimeError(error_message or "ffmpeg concat failed")
    finally:
        delete_files(concat_list_file)


def _sanitize_image_file(image_path: str) -> str:
    # 某些本地图片虽然能被 Pillow 打开，但会因为损坏的 EXIF/eXIf 元数据导致
    # ImageClip 在解析阶段直接抛异常。这里重新导出一份“干净图片”，把坏元数据剥离掉。
    image_root, _ = os.path.splitext(image_path)
    sanitized_path = f"{image_root}.sanitized.png"

    with Image.open(image_path) as image:
        image.load()
        # 统一导出为 PNG，避免 JPEG/PNG 不同元数据路径继续把坏块带过去。
        cleaned_image = Image.new(image.mode, image.size)
        cleaned_image.putdata(list(image.getdata()))
        cleaned_image.save(sanitized_path)

    return sanitized_path


def _open_image_clip_with_fallback(image_path: str):
    # 优先直接打开原始图片；如果因为损坏元数据失败，再尝试生成无元数据副本。
    try:
        return ImageClip(image_path), image_path
    except Exception as exc:
        logger.warning(
            f"failed to open image directly, trying sanitized copy: {image_path}, error: {str(exc)}"
        )
        sanitized_path = _sanitize_image_file(image_path)
        return ImageClip(sanitized_path), sanitized_path

def close_clip(clip):
    if clip is None:
        return
        
    try:
        # close main resources
        if hasattr(clip, 'reader') and clip.reader is not None:
            clip.reader.close()
            
        # close audio resources
        if hasattr(clip, 'audio') and clip.audio is not None:
            if hasattr(clip.audio, 'reader') and clip.audio.reader is not None:
                clip.audio.reader.close()
            del clip.audio
            
        # close mask resources
        if hasattr(clip, 'mask') and clip.mask is not None:
            if hasattr(clip.mask, 'reader') and clip.mask.reader is not None:
                clip.mask.reader.close()
            del clip.mask
            
        # handle child clips in composite clips
        if hasattr(clip, 'clips') and clip.clips:
            for child_clip in clip.clips:
                if child_clip is not clip:  # avoid possible circular references
                    close_clip(child_clip)
            
        # clear clip list
        if hasattr(clip, 'clips'):
            clip.clips = []
            
    except Exception as e:
        logger.error(f"failed to close clip: {str(e)}")
    
    del clip
    gc.collect()

def delete_files(files: List[str] | str):
    if isinstance(files, str):
        files = [files]

    for file in files:
        try:
            os.remove(file)
        except Exception as e:
            logger.debug(f"failed to delete file {file}: {str(e)}")

def get_bgm_file(bgm_type: str = "random", bgm_file: str = ""):
    if not bgm_type:
        return ""

    if bgm_file and os.path.exists(bgm_file):
        return bgm_file

    if bgm_type == "random":
        suffix = "*.mp3"
        song_dir = utils.song_dir()
        files = glob.glob(os.path.join(song_dir, suffix))
        # 当背景音乐目录为空时，直接回退为“不使用 BGM”，避免 random.choice([]) 抛异常。
        if not files:
            logger.warning(f"no bgm files found in song directory: {song_dir}")
            return ""
        return random.choice(files)

    return ""


TRANSITION_DURATION = 1
# 合并视频时单次 ffmpeg 调用最多打开的输入数量，超过则分组合并，避免占用过多内存
MERGE_MAX_INPUTS = 12


def _run_ffmpeg(command: List[str]):
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        error_message = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(error_message[-2000:] or "ffmpeg failed")


def _render_clip_with_ffmpeg(
    file_path: str,
    start_time: float,
    duration: float,
    output_file: str,
    video_width: int,
    video_height: int,
    transition: Optional[str],
    side: str,
    threads: int,
):
    """
    用 ffmpeg 一次完成：截取片段、等比缩放并补黑边、统一帧率、转场效果、编码。
    与原 MoviePy 实现效果一致（等比缩放居中、黑色背景、1 秒淡入淡出/滑入滑出），
    但不再逐帧在 Python 中处理，速度快很多。
    """
    w, h, d, t = video_width, video_height, duration, TRANSITION_DURATION
    base = (
        f"[0:v]scale={w}:{h}:force_original_aspect_ratio=decrease,"
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps={fps}"
    )
    if transition == VideoTransitionMode.fade_in.value:
        filter_graph = f"{base},fade=t=in:st=0:d={t},format=yuv420p[v]"
    elif transition == VideoTransitionMode.fade_out.value:
        filter_graph = f"{base},fade=t=out:st={max(d - t, 0):.3f}:d={t},format=yuv420p[v]"
    elif transition in (
        VideoTransitionMode.slide_in.value,
        VideoTransitionMode.slide_out.value,
    ):
        if transition == VideoTransitionMode.slide_in.value:
            progress = f"min(t/{t},1)"
            offsets = {
                "left": (f"-W+W*{progress}", "0"),
                "right": (f"W-W*{progress}", "0"),
                "top": ("0", f"-H+H*{progress}"),
                "bottom": ("0", f"H-H*{progress}"),
            }
        else:
            progress = f"min(max((t-{max(d - t, 0):.3f})/{t},0),1)"
            offsets = {
                "left": (f"-W*{progress}", "0"),
                "right": (f"W*{progress}", "0"),
                "top": ("0", f"-H*{progress}"),
                "bottom": ("0", f"H*{progress}"),
            }
        x_expr, y_expr = offsets.get(side, ("0", "0"))
        filter_graph = (
            f"color=c=black:s={w}x{h}:r={fps}:d={d:.3f}[bg];{base}[fg];"
            f"[bg][fg]overlay=x='{x_expr}':y='{y_expr}':eval=frame:shortest=1,"
            f"format=yuv420p[v]"
        )
    else:
        filter_graph = f"{base},format=yuv420p[v]"

    _run_ffmpeg(
        [
            get_ffmpeg_binary(),
            "-y",
            "-ss",
            f"{start_time:.3f}",
            "-t",
            f"{d:.3f}",
            "-i",
            file_path,
            "-filter_complex",
            filter_graph,
            "-map",
            "[v]",
            "-an",
            "-c:v",
            video_codec,
            "-preset",
            "ultrafast",
            "-crf",
            "18",
            "-threads",
            str(threads or 2),
            output_file,
        ]
    )
    return d


def _probe_media(file_path: str):
    """返回 (时长秒数, 是否包含音频)。"""
    result = subprocess.run(
        [get_ffmpeg_binary(), "-hide_banner", "-i", file_path],
        capture_output=True,
        text=True,
        check=False,
    )
    info = result.stderr or ""
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", info)
    if not match:
        raise RuntimeError(f"cannot read media duration: {file_path}")
    hours, minutes, seconds = match.groups()
    duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    has_audio = re.search(r"Stream #\S+.*Audio:", info) is not None
    return duration, has_audio


def crossfade_junction(previous_duration: float, next_duration: float) -> float:
    """两段之间的溶解时长：默认 1 秒，片段较短时缩短，保证不超过任一片段的一半。"""
    return max(0.05, min(TRANSITION_DURATION, previous_duration / 2, next_duration / 2))


def _merge_group(
    files: List[str],
    durations: List[float],
    audio_flags: List[bool],
    output_file: str,
    width: int,
    height: int,
    transition: str,
    with_audio: bool,
    encode_args: List[str],
):
    filters = []
    for i, (duration, has_audio) in enumerate(zip(durations, audio_flags)):
        filters.append(
            f"[{i}:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps={fps},"
            f"format=yuv420p,tpad=stop_mode=clone:stop_duration=2,"
            # xfade 要求固定帧率，trim/setpts 之后需要再次声明帧率
            f"trim=duration={duration:.3f},setpts=PTS-STARTPTS,fps={fps},settb=AVTB[v{i}]"
        )
        if with_audio:
            source = f"[{i}:a]" if has_audio else "anullsrc=r=44100:cl=stereo,"
            filters.append(
                f"{source}aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo,"
                f"apad,atrim=duration={duration:.3f},asetpts=PTS-STARTPTS[a{i}]"
            )

    if transition == "crossfade" and len(files) > 1:
        video_label, audio_label, length = "v0", "a0", durations[0]
        for k in range(1, len(files)):
            junction = crossfade_junction(durations[k - 1], durations[k])
            offset = max(length - junction, 0)
            filters.append(
                f"[{video_label}][v{k}]xfade=transition=fade:duration={junction:.3f}"
                f":offset={offset:.3f}[vx{k}]"
            )
            video_label = f"vx{k}"
            if with_audio:
                filters.append(f"[{audio_label}][a{k}]acrossfade=d={junction:.3f}[ax{k}]")
                audio_label = f"ax{k}"
            length = length + durations[k] - junction
    else:
        streams = "".join(
            f"[v{i}][a{i}]" if with_audio else f"[v{i}]" for i in range(len(files))
        )
        filters.append(
            f"{streams}concat=n={len(files)}:v=1:a={1 if with_audio else 0}"
            + ("[vcat][acat]" if with_audio else "[vcat]")
        )
        video_label, audio_label = "vcat", "acat"

    filter_file = f"{output_file}.filter.txt"
    with open(filter_file, "w", encoding="utf-8") as fp:
        fp.write(";\n".join(filters))
    command = [get_ffmpeg_binary(), "-y"]
    for file_path in files:
        command += ["-i", file_path]
    command += ["-filter_complex_script", filter_file, "-map", f"[{video_label}]"]
    if with_audio:
        command += ["-map", f"[{audio_label}]", "-c:a", audio_codec, "-b:a", audio_bitrate]
    else:
        command += ["-an"]
    command += ["-c:v", video_codec] + encode_args + ["-movflags", "+faststart", output_file]
    try:
        _run_ffmpeg(command)
    finally:
        delete_files(filter_file)


def merge_video_files(
    files: List[str],
    output_file: str,
    transition: str = "none",
    width: int = 0,
    height: int = 0,
    with_audio: bool = True,
    threads: int = 2,
    final_quality: bool = True,
) -> str:
    """
    把多个视频首尾相接合并为一个视频。
    - transition: "none" 直接切换；"crossfade" 相邻两段画面（和声音）互相溶解，不经过黑屏
    - width/height 为 0 时使用第一个视频的尺寸；尺寸不同的视频会等比缩放并补黑边
    - with_audio: 是否保留音频（无音轨的视频会自动补静音）
    - 输入较多时分组合并，组与组之间同样使用所选的转场
    """
    if not files:
        raise ValueError("no video files to merge")
    probes = [_probe_media(f) for f in files]
    if not width or not height:
        first = VideoFileClip(files[0])
        width, height = first.size
        close_clip(first)
    width, height = width // 2 * 2, height // 2 * 2

    final_args = (
        ["-preset", "medium", "-crf", "23"]
        if final_quality
        else ["-preset", "ultrafast", "-crf", "18"]
    )
    final_args += ["-threads", str(threads or 2)]

    if len(files) <= MERGE_MAX_INPUTS:
        _merge_group(
            files,
            [p[0] for p in probes],
            [p[1] for p in probes],
            output_file,
            width,
            height,
            transition,
            with_audio,
            final_args,
        )
        return output_file

    # 分组合并：组内先合并成高质量中间文件，再递归合并各组
    group_files = []
    intermediate_args = ["-preset", "ultrafast", "-crf", "18", "-threads", str(threads or 2)]
    try:
        for start in range(0, len(files), MERGE_MAX_INPUTS):
            group = files[start : start + MERGE_MAX_INPUTS]
            group_probes = probes[start : start + MERGE_MAX_INPUTS]
            # 递归合并时每一层使用不同的文件名，避免覆盖上一层正在读取的中间文件
            group_file = f"{output_file}.{os.urandom(4).hex()}.part{len(group_files) + 1}.mp4"
            _merge_group(
                group,
                [p[0] for p in group_probes],
                [p[1] for p in group_probes],
                group_file,
                width,
                height,
                transition,
                with_audio,
                intermediate_args,
            )
            group_files.append(group_file)
        return merge_video_files(
            group_files,
            output_file,
            transition=transition,
            width=width,
            height=height,
            with_audio=with_audio,
            threads=threads,
            final_quality=final_quality,
        )
    finally:
        delete_files(group_files)


def combine_videos(
    combined_video_path: str,
    video_paths: List[str],
    audio_file: str,
    video_aspect: VideoAspect = VideoAspect.portrait,
    video_concat_mode: VideoConcatMode = VideoConcatMode.random,
    video_transition_mode: VideoTransitionMode = None,
    max_clip_duration: int = 5,
    threads: int = 2,
) -> str:
    audio_clip = AudioFileClip(audio_file)
    audio_duration = audio_clip.duration
    logger.info(f"audio duration: {audio_duration} seconds")
    logger.info(f"maximum clip duration: {max_clip_duration} seconds")

    # 兼容 API 直接调用时未传转场模式的情况，避免后续访问 .value 时崩溃。
    transition_value = getattr(video_transition_mode, "value", video_transition_mode)
    output_dir = os.path.dirname(combined_video_path)

    aspect = VideoAspect(video_aspect)
    video_width, video_height = aspect.to_resolution()

    processed_clips = []
    subclipped_items = []
    video_duration = 0
    for video_path in video_paths:
        clip = VideoFileClip(video_path)
        clip_duration = clip.duration
        clip_w, clip_h = clip.size
        close_clip(clip)
        
        start_time = 0

        while start_time < clip_duration:
            end_time = min(start_time + max_clip_duration, clip_duration)

            # 保留所有有效分段。
            # 这样既不会丢掉“整段视频本身就短于 max_clip_duration”的素材，
            # 也不会吞掉长视频最后剩下的一小段尾部内容。
            if end_time > start_time:
                subclipped_items.append(
                    SubClippedVideoClip(
                        file_path=video_path,
                        start_time=start_time,
                        end_time=end_time,
                        width=clip_w,
                        height=clip_h,
                    )
                )

            start_time = end_time
            if video_concat_mode.value == VideoConcatMode.sequential.value:
                break

    # random subclipped_items order
    if video_concat_mode.value == VideoConcatMode.random.value:
        random.shuffle(subclipped_items)
        
    logger.debug(f"total subclipped items: {len(subclipped_items)}")
    
    # 溶解转场会让相邻片段重叠，累计时长时需要减去重叠部分
    is_crossfade = transition_value == VideoTransitionMode.crossfade.value

    def add_processed_clip(clip_info):
        nonlocal video_duration
        if is_crossfade and processed_clips:
            video_duration -= crossfade_junction(
                processed_clips[-1].duration, clip_info.duration
            )
        processed_clips.append(clip_info)
        video_duration += clip_info.duration

    # Add downloaded clips over and over until the duration of the audio (max_duration) has been reached
    for i, subclipped_item in enumerate(subclipped_items):
        if video_duration > audio_duration:
            break
        
        logger.debug(f"processing clip {i+1}: {subclipped_item.width}x{subclipped_item.height}, current duration: {video_duration:.2f}s, remaining: {audio_duration - video_duration:.2f}s")
        
        clip_file = f"{output_dir}/temp-clip-{i+1}.mp4"
        shuffle_side = random.choice(["left", "right", "top", "bottom"])
        effective_transition = transition_value
        if is_crossfade:
            # 溶解转场在拼接阶段处理，单个片段不加效果
            effective_transition = None
        elif transition_value == VideoTransitionMode.shuffle.value:
            effective_transition = random.choice(
                [
                    VideoTransitionMode.fade_in.value,
                    VideoTransitionMode.fade_out.value,
                    VideoTransitionMode.slide_in.value,
                    VideoTransitionMode.slide_out.value,
                ]
            )

        # 优先用 ffmpeg 直接处理片段；失败时回退到原 MoviePy 逐帧处理
        try:
            clip_duration_saved = _render_clip_with_ffmpeg(
                file_path=subclipped_item.file_path,
                start_time=subclipped_item.start_time,
                duration=min(
                    subclipped_item.end_time - subclipped_item.start_time,
                    max_clip_duration,
                ),
                output_file=clip_file,
                video_width=video_width,
                video_height=video_height,
                transition=effective_transition,
                side=shuffle_side,
                threads=threads,
            )
            add_processed_clip(
                SubClippedVideoClip(
                    file_path=clip_file,
                    duration=clip_duration_saved,
                    width=subclipped_item.width,
                    height=subclipped_item.height,
                )
            )
            continue
        except Exception as e:
            logger.warning(f"ffmpeg failed to process clip, fallback to moviepy: {str(e)}")

        try:
            clip = VideoFileClip(subclipped_item.file_path).subclipped(subclipped_item.start_time, subclipped_item.end_time)
            clip_duration = clip.duration
            # Not all videos are same size, so we need to resize them
            clip_w, clip_h = clip.size
            if clip_w != video_width or clip_h != video_height:
                clip_ratio = clip.w / clip.h
                video_ratio = video_width / video_height
                logger.debug(f"resizing clip, source: {clip_w}x{clip_h}, ratio: {clip_ratio:.2f}, target: {video_width}x{video_height}, ratio: {video_ratio:.2f}")
                
                if clip_ratio == video_ratio:
                    clip = clip.resized(new_size=(video_width, video_height))
                else:
                    if clip_ratio > video_ratio:
                        scale_factor = video_width / clip_w
                    else:
                        scale_factor = video_height / clip_h

                    new_width = int(clip_w * scale_factor)
                    new_height = int(clip_h * scale_factor)

                    background = ColorClip(size=(video_width, video_height), color=(0, 0, 0)).with_duration(clip_duration)
                    clip_resized = clip.resized(new_size=(new_width, new_height)).with_position("center")
                    clip = CompositeVideoClip([background, clip_resized])
                    
            if effective_transition in (None, VideoTransitionMode.none.value):
                clip = clip
            elif effective_transition == VideoTransitionMode.fade_in.value:
                clip = video_effects.fadein_transition(clip, TRANSITION_DURATION)
            elif effective_transition == VideoTransitionMode.fade_out.value:
                clip = video_effects.fadeout_transition(clip, TRANSITION_DURATION)
            elif effective_transition == VideoTransitionMode.slide_in.value:
                clip = video_effects.slidein_transition(clip, TRANSITION_DURATION, shuffle_side)
            elif effective_transition == VideoTransitionMode.slide_out.value:
                clip = video_effects.slideout_transition(clip, TRANSITION_DURATION, shuffle_side)

            if clip.duration > max_clip_duration:
                clip = clip.subclipped(0, max_clip_duration)
                
            # wirte clip to temp file
            # 中间片段只用于后续拼接，最终视频还会再编码一次：
            # 使用 ultrafast + 高质量 CRF 大幅缩短编码时间，同时基本不损失画质；
            # 原素材音轨最终会被配音替换，这里不写音频，保证各片段流格式一致以便无损拼接。
            clip.write_videofile(
                clip_file,
                logger=None,
                fps=fps,
                codec=video_codec,
                audio=False,
                preset="ultrafast",
                threads=threads,
                ffmpeg_params=["-crf", "18", "-pix_fmt", "yuv420p"],
            )

            # Store clip duration before closing
            clip_duration_saved = clip.duration
            close_clip(clip)

            add_processed_clip(SubClippedVideoClip(file_path=clip_file, duration=clip_duration_saved, width=clip_w, height=clip_h))
            
        except Exception as e:
            logger.error(f"failed to process clip: {str(e)}")
    
    # loop processed clips until the video duration matches or exceeds the audio duration.
    if video_duration < audio_duration:
        logger.warning(f"video duration ({video_duration:.2f}s) is shorter than audio duration ({audio_duration:.2f}s), looping clips to match audio length.")
        base_clips = processed_clips.copy()
        for clip in itertools.cycle(base_clips):
            if video_duration >= audio_duration:
                break
            add_processed_clip(clip)
        logger.info(f"video duration: {video_duration:.2f}s, audio duration: {audio_duration:.2f}s, looped {len(processed_clips)-len(base_clips)} clips")
     
    # merge video clips progressively, avoid loading all videos at once to avoid memory overflow
    logger.info("starting clip merging process")
    if not processed_clips:
        logger.warning("no clips available for merging")
        return combined_video_path
    
    # if there is only one clip, use it directly
    if len(processed_clips) == 1:
        logger.info("using single clip directly")
        shutil.copy(processed_clips[0].file_path, combined_video_path)
        delete_files([processed_clips[0].file_path])
        logger.info("video combining completed")
        return combined_video_path

    clip_files = [clip.file_path for clip in processed_clips]
    if is_crossfade:
        logger.info(f"merging {len(clip_files)} clips with crossfade")
        merge_video_files(
            clip_files,
            combined_video_path,
            transition="crossfade",
            width=video_width,
            height=video_height,
            with_audio=False,
            threads=threads,
            final_quality=False,
        )
    else:
        logger.info(f"concatenating {len(clip_files)} clips with ffmpeg")
        concat_video_clips_with_ffmpeg(
            clip_files=clip_files,
            output_file=combined_video_path,
            threads=threads,
            output_dir=output_dir,
        )

    # clean temp files
    delete_files(list(set(clip_files)))
            
    logger.info("video combining completed")
    return combined_video_path


def wrap_text(text, max_width, font="Arial", fontsize=60):
    # Create ImageFont
    font = ImageFont.truetype(font, fontsize)

    def get_text_size(inner_text):
        inner_text = inner_text.strip()
        left, top, right, bottom = font.getbbox(inner_text)
        return right - left, bottom - top

    width, height = get_text_size(text)
    if width <= max_width:
        return text, height

    processed = True

    _wrapped_lines_ = []
    words = text.split(" ")
    _txt_ = ""
    for word in words:
        _before = _txt_
        _txt_ += f"{word} "
        _width, _height = get_text_size(_txt_)
        if _width <= max_width:
            continue
        else:
            if _txt_.strip() == word.strip():
                processed = False
                break
            _wrapped_lines_.append(_before)
            _txt_ = f"{word} "
    _wrapped_lines_.append(_txt_)
    if processed:
        _wrapped_lines_ = [line.strip() for line in _wrapped_lines_]
        result = "\n".join(_wrapped_lines_).strip()
        height = len(_wrapped_lines_) * height
        return result, height

    _wrapped_lines_ = []
    chars = list(text)
    _txt_ = ""
    for word in chars:
        _txt_ += word
        _width, _height = get_text_size(_txt_)
        if _width <= max_width:
            continue
        else:
            _wrapped_lines_.append(_txt_)
            _txt_ = ""
    _wrapped_lines_.append(_txt_)
    result = "\n".join(_wrapped_lines_).strip()
    height = len(_wrapped_lines_) * height
    return result, height


def _resolve_clip_position(clip, video_width: int, video_height: int):
    # 与 MoviePy 合成时的定位规则一致："center" 表示居中，数值表示左上角坐标
    x, y = clip.pos(0)
    if isinstance(x, str):
        x = {"left": 0, "center": (video_width - clip.w) / 2, "right": video_width - clip.w}[x]
    if isinstance(y, str):
        y = {"top": 0, "center": (video_height - clip.h) / 2, "bottom": video_height - clip.h}[y]
    return int(x), int(y)


def _render_text_clip_png(clip, video_width: int, video_height: int, output_file: str):
    """把一条字幕渲染成与视频同尺寸的透明 PNG（字幕已放在最终位置）。"""
    rgb = clip.get_frame(0).astype("uint8")
    if clip.mask is not None:
        alpha = (clip.mask.get_frame(0) * 255).clip(0, 255).astype("uint8")
    else:
        alpha = np.full(rgb.shape[:2], 255, dtype="uint8")
    text_image = Image.fromarray(np.dstack([rgb, alpha]), "RGBA")
    canvas = Image.new("RGBA", (video_width, video_height), (0, 0, 0, 0))
    canvas.paste(text_image, _resolve_clip_position(clip, video_width, video_height), text_image)
    canvas.save(output_file)


def _compose_final_video_with_ffmpeg(
    video_path: str,
    audio_clip,
    text_clips: list,
    output_file: str,
    video_width: int,
    video_height: int,
    duration: float,
    audio_fps: int,
    threads: int,
):
    work_dir = os.path.join(
        os.path.dirname(output_file), f"compose-{os.path.basename(output_file)}"
    )
    os.makedirs(work_dir, exist_ok=True)
    try:
        audio_file = os.path.join(work_dir, "audio.m4a")
        audio_clip.write_audiofile(
            audio_file,
            fps=audio_fps,
            codec=audio_codec,
            bitrate=audio_bitrate,
            logger=None,
        )

        command = [get_ffmpeg_binary(), "-y", "-i", video_path]
        if text_clips:
            # 字幕轨：按时间排列的透明图片序列，空档处放全透明图片
            blank_file = os.path.join(work_dir, "blank.png")
            Image.new("RGBA", (video_width, video_height), (0, 0, 0, 0)).save(blank_file)
            entries = []
            cursor = 0.0
            for index, clip in enumerate(sorted(text_clips, key=lambda c: c.start)):
                start = max(float(clip.start), cursor)
                end = min(float(clip.end), duration)
                if end - start <= 0.001:
                    continue
                if start - cursor > 0.001:
                    entries.append((blank_file, start - cursor))
                png_file = os.path.join(work_dir, f"sub-{index + 1}.png")
                _render_text_clip_png(clip, video_width, video_height, png_file)
                entries.append((png_file, end - start))
                cursor = end
            entries.append((blank_file, max(duration - cursor, 0.001)))

            list_file = os.path.join(work_dir, "subtitles.txt")
            with open(list_file, "w", encoding="utf-8") as fp:
                for file_path, entry_duration in entries:
                    fp.write(f"file '{_escape_ffmpeg_concat_path(file_path)}'\n")
                    fp.write(f"duration {entry_duration:.3f}\n")
                # concat demuxer 会忽略最后一项的 duration，需要重复最后一个文件
                fp.write(f"file '{_escape_ffmpeg_concat_path(entries[-1][0])}'\n")

            command += ["-f", "concat", "-safe", "0", "-i", list_file, "-i", audio_file]
            command += [
                "-filter_complex",
                f"[0:v]fps={fps}[base];[1:v]format=rgba[subs];"
                f"[base][subs]overlay=0:0:eof_action=repeat,format=yuv420p[v]",
                "-map",
                "[v]",
                "-map",
                "2:a",
            ]
        else:
            command += [
                "-i",
                audio_file,
                "-vf",
                f"fps={fps},format=yuv420p",
                "-map",
                "0:v",
                "-map",
                "1:a",
            ]

        command += [
            "-t",
            f"{duration:.3f}",
            "-c:v",
            video_codec,
            "-preset",
            "medium",
            "-threads",
            str(threads or 2),
            "-c:a",
            "copy",
            "-movflags",
            "+faststart",
            output_file,
        ]
        _run_ffmpeg(command)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def generate_video(
    video_path: str,
    audio_path: str,
    subtitle_path: str,
    output_file: str,
    params: VideoParams,
):
    aspect = VideoAspect(params.video_aspect)
    video_width, video_height = aspect.to_resolution()

    logger.info(f"generating video: {video_width} x {video_height}")
    logger.info(f"  ① video: {video_path}")
    logger.info(f"  ② audio: {audio_path}")
    logger.info(f"  ③ subtitle: {subtitle_path}")
    logger.info(f"  ④ output: {output_file}")

    # https://github.com/harry0703/MoneyPrinterTurbo/issues/217
    # PermissionError: [WinError 32] The process cannot access the file because it is being used by another process: 'final-1.mp4.tempTEMP_MPY_wvf_snd.mp3'
    # write into the same directory as the output file
    output_dir = os.path.dirname(output_file)

    font_path = ""
    if params.subtitle_enabled:
        if not params.font_name:
            # 默认使用支持越南语声调字符的字体（Liberation Serif，与 Times New Roman 字形度量兼容）
            params.font_name = "LiberationSerif-Bold.ttf"
        font_path = os.path.join(utils.font_dir(), params.font_name)
        if os.name == "nt":
            font_path = font_path.replace("\\", "/")

        logger.info(f"  ⑤ font: {font_path}")

    def resolve_subtitle_background_color():
        # 兼容历史参数：API 里 `text_background_color` 既可能是布尔值，
        # 也可能是实际颜色字符串。统一在这里归一化，避免把 True/False
        # 直接传给 TextClip 后出现不可预期的渲染结果。
        if isinstance(params.text_background_color, bool):
            return "#000000" if params.text_background_color else None
        return params.text_background_color

    def create_text_clip(subtitle_item):
        params.font_size = int(params.font_size)
        params.stroke_width = int(params.stroke_width)
        phrase = subtitle_item[1]
        max_width = video_width * 0.9
        wrapped_txt, txt_height = wrap_text(
            phrase, max_width=max_width, font=font_path, fontsize=params.font_size
        )
        interline = int(params.font_size * 0.25)
        line_count = wrapped_txt.count("\n") + 1
        vertical_padding = int(params.font_size * 0.35)
        # MoviePy 在 `method=label` 下会自动收缩文本框高度，遇到多行字幕、
        # 描边或背景色时，容易把最后一行的下半部分裁掉。这里显式传入
        # 一个更保守的高度，把行间距和额外上下留白一并算进去，保证字幕
        # 背景框与文字本身都能完整渲染出来。
        size = (
            int(max_width),
            int(txt_height + vertical_padding + (interline * line_count)),
        )

        _clip = TextClip(
            text=wrapped_txt,
            font=font_path,
            font_size=params.font_size,
            color=params.text_fore_color,
            bg_color=resolve_subtitle_background_color(),
            stroke_color=params.stroke_color,
            stroke_width=params.stroke_width,
            interline=interline,
            size=size,
            text_align="center",
        )
        duration = subtitle_item[0][1] - subtitle_item[0][0]
        _clip = _clip.with_start(subtitle_item[0][0])
        _clip = _clip.with_end(subtitle_item[0][1])
        _clip = _clip.with_duration(duration)
        if params.subtitle_position == "bottom":
            _clip = _clip.with_position(("center", video_height * 0.95 - _clip.h))
        elif params.subtitle_position == "top":
            _clip = _clip.with_position(("center", video_height * 0.05))
        elif params.subtitle_position == "custom":
            # Ensure the subtitle is fully within the screen bounds
            margin = 10  # Additional margin, in pixels
            max_y = video_height - _clip.h - margin
            min_y = margin
            custom_y = (video_height - _clip.h) * (params.custom_position / 100)
            custom_y = max(
                min_y, min(custom_y, max_y)
            )  # Constrain the y value within the valid range
            _clip = _clip.with_position(("center", custom_y))
        else:  # center
            _clip = _clip.with_position(("center", "center"))
        return _clip

    video_clip = VideoFileClip(video_path).without_audio()
    audio_clip = AudioFileClip(audio_path).with_effects(
        [afx.MultiplyVolume(params.voice_volume)]
    )
    # 成片时长以配音为准：拼接素材时为了覆盖配音会多出一段，
    # 这里裁掉多余部分，避免结尾出现只有背景音乐、没有配音和字幕的画面。
    final_duration = min(video_clip.duration, audio_clip.duration)
    video_clip = video_clip.subclipped(0, final_duration)

    def make_textclip(text):
        return TextClip(
            text=text,
            font=font_path,
            font_size=params.font_size,
        )

    text_clips = []
    if subtitle_path and os.path.exists(subtitle_path):
        sub = SubtitlesClip(
            subtitles=subtitle_path, encoding="utf-8", make_textclip=make_textclip
        )
        for item in sub.subtitles:
            clip = create_text_clip(subtitle_item=item)
            text_clips.append(clip)

    bgm_file = get_bgm_file(bgm_type=params.bgm_type, bgm_file=params.bgm_file)
    if bgm_file:
        try:
            bgm_clip = AudioFileClip(bgm_file).with_effects(
                [
                    afx.MultiplyVolume(params.bgm_volume),
                    afx.AudioFadeOut(3),
                    afx.AudioLoop(duration=final_duration),
                ]
            )
            audio_clip = CompositeAudioClip([audio_clip, bgm_clip])
        except Exception as e:
            logger.error(f"failed to add bgm: {str(e)}")

    # 显式沿用输入音频的采样率；如果取不到，再回退到 MoviePy 默认的 44100Hz。
    # 这样可以减少不同运行环境，尤其是 Docker 环境中再次重采样带来的音质波动。
    output_audio_fps = int(getattr(audio_clip, "fps", 0) or 44100)
    audio_clip = audio_clip.with_duration(final_duration)

    # 优先用 ffmpeg 合成字幕与音频（字幕预渲染为透明图片后叠加），
    # 避免 MoviePy 逐帧合成；失败时回退到原来的 MoviePy 流程。
    try:
        _compose_final_video_with_ffmpeg(
            video_path=video_path,
            audio_clip=audio_clip,
            text_clips=text_clips,
            output_file=output_file,
            video_width=video_width,
            video_height=video_height,
            duration=final_duration,
            audio_fps=output_audio_fps,
            threads=params.n_threads or 2,
        )
        video_clip.close()
        return
    except Exception as e:
        logger.warning(f"ffmpeg failed to compose final video, fallback to moviepy: {str(e)}")

    if text_clips:
        video_clip = CompositeVideoClip([video_clip, *text_clips]).with_duration(
            final_duration
        )
    video_clip = video_clip.with_audio(audio_clip)
    video_clip.write_videofile(
        output_file,
        audio_codec=audio_codec,
        audio_fps=output_audio_fps,
        audio_bitrate=audio_bitrate,
        temp_audiofile_path=output_dir,
        threads=params.n_threads or 2,
        logger=None,
        fps=fps,
    )
    video_clip.close()
    del video_clip


def _image_to_video_with_ffmpeg(image_path: str, output_file: str, clip_duration: float):
    """
    用 ffmpeg 把图片转成缓慢放大的视频片段（与原 MoviePy 实现相同：每秒放大 3%，居中）。
    先把图片缩到长边不超过 1920（成片最大尺寸），再放大 2 倍做 zoompan，避免缩放抖动。
    """
    with Image.open(image_path) as image:
        width, height = image.size
    scale = min(1.0, 1920 / max(width, height))
    base_w = max(2, int(width * scale) // 2 * 2)
    base_h = max(2, int(height * scale) // 2 * 2)
    frames = max(1, int(round(clip_duration * fps)))
    zoom_rate = clip_duration * 0.03
    filter_graph = (
        f"scale={base_w * 2}:{base_h * 2}:flags=bicubic,"
        f"zoompan=z='1+{zoom_rate}*on/{frames}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
        f":d={frames}:s={base_w}x{base_h}:fps={fps},setsar=1,format=yuv420p"
    )
    _run_ffmpeg(
        [
            get_ffmpeg_binary(),
            "-y",
            "-loop",
            "1",
            "-framerate",
            str(fps),
            "-t",
            f"{clip_duration:.3f}",
            "-i",
            image_path,
            "-vf",
            filter_graph,
            "-frames:v",
            str(frames),
            "-c:v",
            video_codec,
            "-preset",
            "ultrafast",
            "-crf",
            "18",
            output_file,
        ]
    )


def preprocess_video(materials: List[MaterialInfo], clip_duration=4):
    # WebUI 在某些二次生成场景下可能传入空素材列表，这里直接返回空结果，避免抛出 NoneType 异常。
    if not materials:
        return []

    # 仅返回通过预处理校验的素材，避免低分辨率图片继续进入后续的视频合成流程。
    valid_materials = []

    for material in materials:
        if not material.url:
            continue

        ext = utils.parse_extension(material.url)
        material_source_path = material.url
        try:
            # 图片素材直接按图片方式读取，避免先走 VideoFileClip 误判后触发不稳定的回退分支。
            if ext in const.FILE_TYPE_IMAGES:
                clip, material_source_path = _open_image_clip_with_fallback(material.url)
            else:
                clip = VideoFileClip(material.url)
        except Exception:
            # 非标准扩展名或探测失败时再回退到图片模式，兼容历史上直接传本地图片路径的情况。
            try:
                clip, material_source_path = _open_image_clip_with_fallback(material.url)
            except Exception as exc:
                logger.warning(
                    f"skip unreadable local material: {material.url}, error: {str(exc)}"
                )
                continue
        try:
            width = clip.size[0]
            height = clip.size[1]
            if width < 480 or height < 480:
                logger.warning(f"low resolution material: {width}x{height}, minimum 480x480 required")
                # 探测到低分辨率素材后立即关闭资源，并且不要把该素材返回给后续流程。
                close_clip(clip)
                continue

            if ext in const.FILE_TYPE_IMAGES:
                logger.info(f"processing image: {material_source_path}")
                # 探测尺寸时已经打开过一次素材，这里先释放探测句柄，再重新创建用于导出的图片 clip。
                close_clip(clip)
                video_file = f"{material_source_path}.mp4"
                # 优先用 ffmpeg 生成放大效果视频，失败时回退到 MoviePy 逐帧处理
                try:
                    _image_to_video_with_ffmpeg(material_source_path, video_file, clip_duration)
                    material.url = video_file
                    logger.success(f"image processed: {video_file}")
                    valid_materials.append(material)
                    continue
                except Exception as e:
                    logger.warning(f"ffmpeg failed to process image, fallback to moviepy: {str(e)}")
                # Create an image clip and set its duration to 3 seconds
                clip = (
                    ImageClip(material_source_path)
                    .with_duration(clip_duration)
                    .with_position("center")
                )
                # Apply a zoom effect using the resize method.
                # A lambda function is used to make the zoom effect dynamic over time.
                # The zoom effect starts from the original size and gradually scales up to 120%.
                # t represents the current time, and clip.duration is the total duration of the clip (3 seconds).
                # Note: 1 represents 100% size, so 1.2 represents 120% size.
                zoom_clip = clip.resized(
                    lambda t: 1 + (clip_duration * 0.03) * (t / clip.duration)
                )

                # Optionally, create a composite video clip containing the zoomed clip.
                # This is useful when you want to add other elements to the video.
                final_clip = CompositeVideoClip([zoom_clip])

                # Output the video to a file.
                video_file = f"{material_source_path}.mp4"
                final_clip.write_videofile(video_file, fps=30, logger=None)
                close_clip(clip)
                close_clip(final_clip)
                material.url = video_file
                logger.success(f"image processed: {video_file}")
            else:
                # 普通视频素材只需要读取尺寸做校验，校验完成后立即释放句柄即可。
                close_clip(clip)
        except Exception:
            close_clip(clip)
            raise

        valid_materials.append(material)

    return valid_materials
