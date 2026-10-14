"""Progress display tests; no downloads or installation are started."""
import sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from download_speed import DownloadSpeedMeter,format_rate


class DownloadSpeedTests(unittest.TestCase):
    def test_rate_uses_only_new_bytes_from_same_download(self):
        meter=DownloadSpeedMeter()
        current={'phase':'下载','file':'OCI/model-a','file_bytes':300_000_000}
        self.assertEqual(meter.label('running',current,10),'下载速度：计算中…')
        current['file_bytes']=310_000_000
        self.assertEqual(meter.label('running',current,12),'下载速度：5.0 MB/s（近几秒）')
        self.assertEqual(meter.label('running',current,16),'下载速度：0 KB/s（等待数据）')

    def test_new_file_and_resume_do_not_count_existing_bytes(self):
        meter=DownloadSpeedMeter()
        meter.label('running',{'phase':'下载','file':'OCI/a','file_bytes':1_000_000},1)
        self.assertEqual(meter.label('running',{'phase':'下载','file':'OCI/b','file_bytes':500_000_000},2),'下载速度：计算中…')
        self.assertEqual(meter.label('running',{'phase':'下载','file':'OCI/b','file_bytes':5_000_000},3),'下载速度：计算中…')
        self.assertEqual(meter.label('running',{'phase':'下载','file':'OCI/b','file_bytes':6_000_000},4),'下载速度：1.0 MB/s（近几秒）')

    def test_local_or_stopped_tasks_never_show_network_rate(self):
        meter=DownloadSpeedMeter()
        meter.label('running',{'phase':'下载','file':'OCI/a','file_bytes':0},1)
        meter.label('running',{'phase':'下载','file':'OCI/a','file_bytes':1_000_000},2)
        self.assertEqual(meter.label('running',{'phase':'校验','file':'OCI/a','file_bytes':1_000_000},3),'下载速度：—（正在校验）')
        self.assertEqual(meter.label('paused',{'phase':'下载','file':'OCI/a','file_bytes':1_000_000},4),'下载速度：—（已暂停）')
        self.assertEqual(meter.label('running',{'phase':'下载','file':'OCI/a','file_bytes':1_000_000},5),'下载速度：计算中…')

    def test_units(self):
        self.assertEqual(format_rate(500),'500 B/s')
        self.assertEqual(format_rate(12_000),'12 KB/s')
        self.assertEqual(format_rate(12_500_000),'12.5 MB/s')


if __name__=='__main__':unittest.main(verbosity=2)
