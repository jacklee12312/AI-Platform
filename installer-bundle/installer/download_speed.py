"""Display-only download throughput from successive public progress snapshots."""
from __future__ import annotations

import time


def format_rate(bytes_per_second):
    if bytes_per_second >= 1_000_000:
        return f'{bytes_per_second / 1_000_000:.1f} MB/s'
    if bytes_per_second >= 1_000:
        return f'{bytes_per_second / 1_000:.0f} KB/s'
    return f'{bytes_per_second:.0f} B/s'


class DownloadSpeedMeter:
    def __init__(self):
        self.reset()

    def reset(self):
        self.file = None
        self.bytes = None
        self.at = None
        self.rate = None

    def label(self, status, transfer, now=None):
        if status != 'running':
            self.reset()
            return '下载速度：—（已暂停）' if status == 'paused' else '下载速度：—（未在下载）'
        transfer = transfer if isinstance(transfer, dict) else {}
        phase = transfer.get('phase')
        if phase != '下载':
            self.reset()
            reason = {'校验': '正在校验', '读取': '正在读取本地文件', '等待重试': '等待重试'}.get(phase, '非下载阶段')
            return f'下载速度：—（{reason}）'
        file = transfer.get('file')
        count = transfer.get('file_bytes')
        if not isinstance(file, str) or not file or type(count) is not int or count < 0:
            self.reset()
            return '下载速度：计算中…'
        now = time.monotonic() if now is None else now
        if file != self.file or self.bytes is None or count < self.bytes or now < self.at:
            self.file, self.bytes, self.at, self.rate = file, count, now, None
            return '下载速度：计算中…'
        if count > self.bytes and now - self.at >= 0.2:
            instant = (count - self.bytes) / (now - self.at)
            self.rate = instant if self.rate is None else 0.6 * self.rate + 0.4 * instant
            self.bytes, self.at = count, now
        if self.rate is None:
            return '下载速度：计算中…'
        if now - self.at >= 3:
            return '下载速度：0 KB/s（等待数据）'
        return '下载速度：' + format_rate(self.rate) + '（近几秒）'
