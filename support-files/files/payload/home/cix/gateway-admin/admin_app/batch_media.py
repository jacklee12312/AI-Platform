"""Bounded, local-only media conversion in a disposable process, no model calls."""
from __future__ import annotations

import argparse
import json
import math
import wave
from pathlib import Path

MAX_PAGES = 50
MAX_PIXELS = 20_000_000
MAX_EDGE = 2048
MAX_AUDIO_SECONDS = 1800
IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.webp'}
AUDIO_FORMATS = {'.wav': 'wav', '.mp3': 'mp3', '.m4a': 'mov', '.flac': 'flac', '.ogg': 'ogg', '.aac': 'aac'}


def inspect(path: Path, extension: str) -> dict:
    if extension == '.pdf':
        import pypdfium2 as pdfium
        with pdfium.PdfDocument(path) as pdf:
            if not 1 <= len(pdf) <= MAX_PAGES:
                raise ValueError(f'每个 PDF 须为 1–{MAX_PAGES} 页')
            return {'pages': len(pdf)}
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    with Image.open(path) as im:
        if im.format not in {'PNG', 'JPEG', 'WEBP'} or im.width * im.height > MAX_PIXELS or getattr(im, 'n_frames', 1) != 1:
            raise ValueError('请使用不超过 2000 万像素的单张 PNG、JPEG 或 WebP 图片')
        im.verify()
    return {'pages': 1}


def image_page(path: Path, extension: str, page: int, output: Path) -> dict:
    from PIL import Image, ImageOps
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    if extension == '.pdf':
        import pypdfium2 as pdfium
        with pdfium.PdfDocument(path) as pdf:
            if not 1 <= len(pdf) <= MAX_PAGES or not 0 <= page < len(pdf):
                raise ValueError('PDF 页数或页码超出范围')
            pdf_page = pdf[page]
            width, height = pdf_page.get_size()
            if not all(math.isfinite(n) and n > 0 for n in (width, height)):
                raise ValueError('PDF 页面尺寸无效')
            bitmap = pdf_page.render(scale=min(2.0, MAX_EDGE / max(width, height)))
            try:
                im = bitmap.to_pil().convert('RGB')
                try: im.save(output, format='JPEG', quality=94)
                finally: im.close()
            finally:
                bitmap.close(); pdf_page.close()
    else:
        inspect(path, extension)
        with Image.open(path) as im:
            im.draft('RGB', (MAX_EDGE, MAX_EDGE))
            ImageOps.exif_transpose(im, in_place=True)
            im.thumbnail((MAX_EDGE, MAX_EDGE))
            if im.mode in ('RGBA', 'LA') or 'transparency' in im.info:
                rgba = im.convert('RGBA')
                background = Image.new('RGB', rgba.size, 'white')
                background.paste(rgba, mask=rgba.getchannel('A'))
                im = background
            else: im = im.convert('RGB')
            im.save(output, format='JPEG', quality=94)
            im.close()
    return {'page': page + 1}


def audio_wave(path: Path, extension: str, output: Path) -> dict:
    import av
    if extension not in AUDIO_FORMATS:
        raise ValueError('不支持的音频格式')
    samples = 0
    with path.open('rb') as source, av.open(source, format=AUDIO_FORMATS[extension], options={
        'protocol_whitelist': 'file,pipe', 'enable_drefs': '0', 'use_absolute_path': '0',
    }) as container, wave.open(str(output), 'wb') as target:
        if not container.streams.audio:
            raise ValueError('文件没有可解码的音轨')
        container.streams.audio[0].thread_count = 1
        target.setnchannels(1); target.setsampwidth(2); target.setframerate(16000)
        resampler = av.AudioResampler(format='s16', layout='mono', rate=16000)
        def write(frames):
            nonlocal samples
            for frame in frames:
                samples += frame.samples
                if samples > MAX_AUDIO_SECONDS * 16000:
                    raise ValueError('单个音频最长 30 分钟，请拆分后提交')
                target.writeframesraw(bytes(frame.planes[0])[:frame.samples * 2])
        for frame in container.decode(audio=0): write(resampler.resample(frame))
        write(resampler.resample(None))
        if not samples: raise ValueError('文件没有有效音频样本')
    return {'duration': round(samples / 16000, 3)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['inspect', 'image', 'audio'])
    parser.add_argument('source', type=Path)
    parser.add_argument('extension')
    parser.add_argument('output', type=Path)
    parser.add_argument('--page', type=int, default=0)
    args = parser.parse_args()
    try:
        if args.action == 'inspect': result = inspect(args.source, args.extension)
        elif args.action == 'image': result = image_page(args.source, args.extension, args.page, args.output)
        else: result = audio_wave(args.source, args.extension, args.output)
        print(json.dumps(result))
    except Exception as error:
        # Do not expose local paths or parser internals to business users.
        message = str(error) if isinstance(error, ValueError) else '文件损坏、加密或格式不受支持，请检查文件后重试'
        print(json.dumps({'error': message[:300]}))
        raise SystemExit(1)


if __name__ == '__main__': main()
