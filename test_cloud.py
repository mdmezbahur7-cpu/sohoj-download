import io
import json
import socket
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import cloud
import egress


class CloudTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data = cloud.DATA
        cloud.DATA = Path(self.temp.name)
        with cloud.LOCK:
            cloud.JOBS.clear()
        self.client = cloud.app.test_client()
        self.other = cloud.app.test_client()
        self.origin = {'Origin': 'http://localhost'}
    def tearDown(self):
        cloud.DATA = self.data
        self.temp.cleanup()
    def owner(self, client):
        client.get('/api/health')
        with client.session_transaction() as session:
            return session['owner']
    def job(self, owner, **extra):
        jid = 'a'*32
        cloud.JOBS[jid] = dict(id=jid, owner=owner, status='queued', mode='audio',
                               quality='192', url='https://youtu.be/test', title='Clip',
                               progress=0, created=time.time(), **extra)
        return jid
    def test_unsafe_sources_rejected(self):
        for url in ['https://127.0.0.1/video','file:///etc/passwd','https://youtube.com.evil.test/x',
                    'https://u:p@youtube.com/x','https://youtu.be:8888/x','http://youtu.be/x']:
            with self.assertRaises(ValueError):
                cloud.validate_url(url)
        self.assertEqual(cloud.validate_url('https://youtu.be/test'),'https://youtu.be/test')
        self.assertEqual(cloud.validate_url('https://example.org/clip.mp4'),'https://example.org/clip.mp4')
        with self.assertRaises(ValueError): cloud.validate_url('https://127.0.0.1/clip.mp4')
    def test_proxy_blocks_private_dns_before_connection(self):
        for ip in ['127.0.0.1','10.0.0.2','169.254.169.254','::1','192.168.1.1']:
            with patch.object(egress.socket,'getaddrinfo', return_value=[(socket.AF_INET,socket.SOCK_STREAM,0,'',(ip,443))]):
                with self.assertRaises(ValueError):
                    egress.connect_public('example.test',443)
        with self.assertRaises(ValueError):
            egress.connect_public('example.test',22)
    def test_proxy_pins_resolved_numeric_address(self):
        connection=Mock()
        with patch.object(egress.socket,'getaddrinfo',return_value=[(socket.AF_INET,socket.SOCK_STREAM,0,'',('93.184.216.34',443))]), patch.object(egress.socket,'socket',return_value=connection):
            self.assertIs(egress.connect_public('example.test',443),connection)
        connection.connect.assert_called_once_with(('93.184.216.34',443))
    def test_separate_browsers_cannot_see_save_or_cancel_others_jobs(self):
        a=self.owner(self.client);self.owner(self.other)
        jid=self.job(a)
        file=cloud.DATA/'clip.mp3';file.write_bytes(b'example')
        cloud.JOBS[jid].update(status='completed',file=str(file))
        self.assertEqual(len(self.client.get('/api/jobs').json),1)
        self.assertEqual(self.other.get('/api/jobs').json,[])
        self.assertEqual(self.other.get('/api/file/'+jid).status_code,404)
        self.assertEqual(self.other.post('/api/jobs/'+jid+'/cancel',json={},headers=self.origin).status_code,404)
        response=self.client.get('/api/file/'+jid)
        self.assertEqual(response.data,b'example')
        self.assertIn('attachment',response.headers['Content-Disposition'])
        response.close()
    def test_reject_cross_origin_invalid_url_and_oversize_body(self):
        self.assertEqual(self.client.post('/api/jobs',json={},headers={'Origin':'https://evil.test'}).status_code,403)
        self.assertEqual(self.client.post('/api/jobs',json={'url':'https://127.0.0.1'},headers=self.origin).status_code,400)
        self.assertEqual(self.client.post('/api/jobs',data='x'*9000,headers={**self.origin,'Content-Type':'application/json'}).status_code,413)
    def test_queue_admission_and_cancel(self):
        with patch.object(cloud.POOL,'submit') as submit,patch.object(cloud.shutil,'which',return_value='/usr/bin/ffmpeg'):
            response=self.client.post('/api/jobs',json={'url':'https://youtu.be/test','mode':'video','quality':'720'},headers=self.origin)
            self.assertEqual(response.status_code,202)
            jid=response.json['id'];submit.assert_called_once_with(cloud.run_job,jid)
            self.assertEqual(self.client.post('/api/jobs/'+jid+'/cancel',json={},headers=self.origin).status_code,200)
            self.assertEqual(cloud.JOBS[jid]['status'],'cancelled')
    def test_successful_job_progress_and_format_options(self):
        jid=self.job('owner');folder=cloud.DATA/jid;folder.mkdir();file=folder/'clip.mp3';file.write_bytes(b'mp3')
        process=Mock();process.stdout=io.StringIO('SOHOJ_TITLE:"Test clip"\nSOHOJ_PROGRESS:50%\nSOHOJ_FILE:'+json.dumps(str(file))+'\n');process.wait.return_value=0;process.poll.return_value=0
        with patch.object(cloud.subprocess,'Popen',return_value=process) as create:
            cloud.run_job(jid)
        self.assertEqual(cloud.JOBS[jid]['status'],'completed')
        args=create.call_args.args[0]
        self.assertIn('--proxy',args);self.assertIn('native',args);self.assertIn('mp3',args)
        from yt_dlp import parse_options
        options=parse_options(args[3:])
        self.assertEqual(options.ydl_opts['proxy'],cloud.PROXY_URL)
    def test_failed_job_cleans_partial_media(self):
        jid=self.job('owner');folder=cloud.DATA/jid;folder.mkdir();(folder/'x.part').write_text('partial')
        process=Mock();process.stdout=io.StringIO('ERROR: This media requires login\n');process.wait.return_value=1;process.poll.return_value=0
        with patch.object(cloud.subprocess,'Popen',return_value=process):
            cloud.run_job(jid)
        self.assertEqual(cloud.JOBS[jid]['status'],'failed')
        self.assertFalse(folder.exists())

if __name__=='__main__':
    unittest.main()
