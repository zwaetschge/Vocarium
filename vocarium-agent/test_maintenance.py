import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from maintenance import Maintenance, MODEL_ID


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.environment = patch.dict(os.environ)
        self.environment.start(); self.addCleanup(self.environment.stop)
        self.root = Path(self.temp.name)
        self.lock = threading.Lock()
        self.service = Maintenance(self.root, self.lock, lambda: ('', ''), lambda: False)

    def test_active_run_blocks_update(self):
        self.lock.acquire()
        with self.assertRaises(BlockingIOError): self.service.start('update', 'codex')
        self.assertIsNone(self.service.state['job'])
        self.lock.release()

    def test_unknown_cli_and_action_are_rejected(self):
        for args in [('update','codex;echo secret'),('shell',None),('update','zai')]:
            with self.assertRaises(ValueError): self.service.start(*args)
        self.assertFalse(self.lock.locked())

    def test_failed_install_keeps_executable_and_removes_staging(self):
        bindir=self.root/'bin';bindir.mkdir();old=self.root/'old';old.write_text('old');(bindir/'codex').symlink_to(old)
        with patch('maintenance.fetch_json',return_value={'version':'1.2.3'}), patch('maintenance.run_install',side_effect=RuntimeError('secret')):
            with self.assertRaises(RuntimeError):self.service.install('codex')
        self.assertEqual((bindir/'codex').resolve(),old)
        self.assertEqual(list((self.root/'releases').iterdir()),[])

    def test_install_only_activates_after_version_check(self):
        self.service.state['job']={}
        with patch('maintenance.fetch_json',return_value={'version':'1.2.3'}),patch('maintenance.run_install') as run,patch.object(self.service,'version',return_value='0.0.1'):
            with self.assertRaises(ValueError):self.service.install('claude')
            command=run.call_args.args[0]
            self.assertEqual(command[-1],'@anthropic-ai/claude-code@1.2.3')
        self.assertFalse((self.root/'bin'/'claude').exists())

    def test_refresh_retains_previous_models_on_failure_and_retirement(self):
        self.service.state['catalogs']={'codex':{'models':[{'value':'previous','label':'Previous'}],'refreshed_at':'before'}}
        with patch.object(self.service,'discover',side_effect=ValueError('secret')):self.service.refresh()
        self.assertEqual(self.service.state['catalogs']['codex']['refreshed_at'],'before')
        self.assertIn('previous',self.service.known_models('codex'))
        self.assertNotIn('secret',json.dumps(self.service.snapshot()))
        with patch.object(self.service,'discover',return_value=[{'value':'new','label':'New'}]):self.service.refresh()
        self.assertEqual(self.service.known_models('codex'),{'new','previous'})
        restored=Maintenance(self.root,self.lock,lambda:('',''),lambda:False)
        self.assertEqual(restored.known_models('codex'),{'new','previous'})
        self.assertTrue(MODEL_ID.fullmatch('opus[1m]'))

    def test_interrupted_job_is_not_left_running(self):
        self.service.state['job']={'status':'running'};self.service.save()
        restored=Maintenance(self.root,self.lock,lambda:('',''),lambda:False)
        self.assertEqual(restored.state['job']['status'],'failed')

    def test_worker_error_releases_inference_lock_and_redacts_error(self):
        self.lock.acquire();self.service.state['job']={'status':'running'}
        with patch.object(self.service,'install',side_effect=ValueError('secret')):self.service.work('update','codex')
        self.assertFalse(self.lock.locked())
        self.assertEqual(self.service.state['job']['status'],'failed')
        self.assertNotIn('secret',json.dumps(self.service.snapshot()))

if __name__ == '__main__': unittest.main()
