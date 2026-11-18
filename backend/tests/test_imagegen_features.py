import io
import json
import tempfile
import threading
import unittest
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch, MagicMock
from PIL import Image
from backend.imagegen.config import ImageGenConfig
from backend.imagegen import backend_comfy, backend_diffusers
from backend.imagegen.generate import generate
from backend.imagegen.progress import DownloadProgress


class EditingTests(unittest.TestCase):
    def test_validation_precedes_model_loading(self):
        for config in [ImageGenConfig(mask='mask.png'), ImageGenConfig(strength=float('nan')),
                       ImageGenConfig(image='does-not-exist.png'), ImageGenConfig(loras=['missing.safetensors'])]:
            with patch.object(backend_diffusers, 'load_pipeline') as load:
                with self.assertRaises(ValueError):
                    generate('test', config=config)
                load.assert_not_called()

    def test_mask_preserves_original_pixels_and_metadata_hashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            original=Path(tmp)/'input.png'; mask=Path(tmp)/'mask.png'
            Image.new('RGB',(8,8),'blue').save(original)
            pixels=Image.new('L',(8,8),0); pixels.putpixel((0,0),255); pixels.save(mask)
            config=ImageGenConfig(image=str(original),mask=str(mask),width=8,height=8)
            with patch.object(backend_diffusers,'require_available'), patch.object(backend_diffusers,'load_pipeline'), \
                 patch.object(backend_diffusers,'run_pipeline',return_value=[Image.new('RGB',(8,8),'red')]):
                result=generate('test',config=config,output_dir=tmp)
            with Image.open(result['files'][0]['image']) as image:
                self.assertEqual(image.getpixel((0,0)),(255,0,0))
                self.assertEqual(image.getpixel((1,1)),(0,0,255))
            metadata=json.loads(Path(result['files'][0]['metadata']).read_text())
            self.assertEqual(metadata['mode'],'inpaint')
            self.assertEqual(len(metadata['inputs']['image']['sha256']),64)

    def test_diffusers_passes_editing_inputs_and_converts_pipeline_for_lora(self):
        with tempfile.TemporaryDirectory() as tmp:
            image=Path(tmp)/'image.png'; Image.new('RGB',(16,16)).save(image)
            lora=Path(tmp)/'lora.safetensors'; lora.write_bytes(b'test')
            spec=ImageGenConfig(image=str(image),loras=[str(lora)+'=0.4'],width=16,height=16).merged()
            base=MagicMock(); pipeline=MagicMock()
            loader=MagicMock(); loader.from_pretrained.return_value=base
            conversion=MagicMock(); conversion.from_pipe.return_value=pipeline
            with patch.object(backend_diffusers,'require_available'), patch.object(backend_diffusers,'_require_peft'), patch.object(backend_diffusers,'_pipeline_class',side_effect=[loader,conversion]), \
                 patch.object(backend_diffusers,'DownloadProgress'), patch.object(backend_diffusers,'_apply_memory_strategy'), \
                 patch.object(backend_diffusers,'unload'):
                backend_diffusers._PIPELINE_CACHE.clear()
                loaded=backend_diffusers.load_pipeline(spec)
            conversion.from_pipe.assert_called_once_with(base)
            pipeline.load_lora_weights.assert_called_once_with(str(lora.parent),weight_name=lora.name,adapter_name='waterfree_lora_0')
            pipeline.set_adapters.assert_called_once_with(['waterfree_lora_0'],adapter_weights=[0.4])
            pipeline.return_value.images=[Image.new('RGB',(16,16))]
            backend_diffusers.run_pipeline(loaded,spec,'test',count=1)
            self.assertEqual(pipeline.call_args.kwargs['strength'],.75)
            self.assertEqual(pipeline.call_args.kwargs['image'].size,(16,16))
            backend_diffusers._PIPELINE_CACHE.clear()

    def test_progress_reports_measured_bytes_without_fake_percentage(self):
        with tempfile.TemporaryDirectory() as tmp:
            events=[]
            progress=DownloadProgress('test/model',cache_root=tmp,emit=events.append)
            progress.blobs.mkdir(parents=True)
            (progress.blobs/'weights.incomplete').write_bytes(b'x'*123)
            progress.sample()
            self.assertEqual(events[-1]['newCacheBytes'],123)
            self.assertEqual(events[-1]['incompleteCacheBytes'],123)
            self.assertEqual(events[-1]['phase'],'downloading')
            self.assertIsNone(events[-1]['totalBytes'])


class ComfyHttpTests(unittest.TestCase):
    def test_real_http_roundtrip_uploads_edit_inputs_and_reports_execution_error(self):
        calls=[]; graph={}; mode={'error':False}
        png=io.BytesIO(); Image.new('RGB',(8,8),'red').save(png,format='PNG')
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def reply(self,payload,binary=False):
                self.send_response(200); self.end_headers()
                self.wfile.write(payload if binary else json.dumps(payload).encode())
            def do_GET(self):
                calls.append(self.path)
                if self.path=='/system_stats': return self.reply({})
                if self.path=='/object_info':
                    return self.reply({'CheckpointLoaderSimple':{'input':{'required':{'ckpt_name':[['model.safetensors']]}}},
                                       'LoraLoader':{'input':{'required':{'lora_name':[['style.safetensors']]}}}})
                if self.path.startswith('/history/'):
                    if mode['error']: return self.reply({'job':{'status':{'status_str':'error','messages':['bad node']}}})
                    output=next(k for k,v in graph.items() if v['class_type']=='SaveImage')
                    return self.reply({'job':{'outputs':{output:{'images':[{'filename':'result.png','type':'output','subfolder':''}]}}}})
                if self.path.startswith('/view?'): return self.reply(png.getvalue(),True)
                raise AssertionError(self.path)
            def do_POST(self):
                body=self.rfile.read(int(self.headers['Content-Length'])); calls.append(self.path)
                if self.path=='/upload/image':
                    assert b'Content-Type: image/png' in body
                    return self.reply({'name':'input.png','subfolder':''})
                graph.update(json.loads(body)['prompt']); self.reply({'prompt_id':'job'})
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                image=Path(tmp)/'input.png'; Image.new('RGB',(8,8),'blue').save(image)
                spec=ImageGenConfig(backend='comfy',comfy_url=f'http://127.0.0.1:{server.server_port}',
                    image=str(image),mask=str(image),width=8,height=8,loras=['style.safetensors=0.5']).merged()
                backend_comfy.require_available(spec)
                context=backend_comfy.load_pipeline(spec)
                result=backend_comfy.run_pipeline(context,spec,'test',count=1)
                self.assertEqual(len(result),1)
                self.assertEqual(calls.count('/upload/image'),2)
                types=[x['class_type'] for x in graph.values()]
                self.assertIn('VAEEncodeForInpaint',types)
                self.assertIn('ImageCompositeMasked',types)
                self.assertIn('LoraLoader',types)
                mode['error']=True
                with self.assertRaisesRegex(backend_comfy.GenerationFailed,'bad node'):
                    backend_comfy.run_pipeline(context,replace(spec,image='',mask=''),'test',count=1)
        finally:
            server.shutdown(); server.server_close(); thread.join()

    def test_free_asks_comfyui_to_unload_and_tolerates_failure(self):
        """GPU-008: POST /free answers 200 with an empty body; an unreachable ComfyUI must not fail the job."""
        posted = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass

            def do_POST(self):
                posted.append((self.path, json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
                self.send_response(200); self.end_headers()   # ComfyUI's /free sends no body

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            spec = ImageGenConfig(backend='comfy', comfy_url=f'http://127.0.0.1:{server.server_port}').merged()
            self.assertTrue(backend_comfy.free(spec))
            self.assertEqual(posted, [('/free', {'unload_models': True, 'free_memory': True})])
        finally:
            server.shutdown(); server.server_close(); thread.join()
        self.assertFalse(backend_comfy.free(ImageGenConfig(backend='comfy', comfy_url='http://127.0.0.1:9').merged()))

    def test_checkpoint_and_lora_selection_require_registered_weights(self):
        info={'CheckpointLoaderSimple':{'input':{'required':{'ckpt_name':[['one','two']]}}}}
        with patch.object(backend_comfy,'_http',return_value=info):
            with self.assertRaisesRegex(backend_comfy.GenerationFailed,'Select --checkpoint'):
                backend_comfy.load_pipeline(ImageGenConfig(backend='comfy').merged())
            with self.assertRaisesRegex(backend_comfy.GenerationFailed,'LoraLoader'):
                backend_comfy.load_pipeline(ImageGenConfig(backend='comfy',comfy_checkpoint='one',loras=['missing']).merged())
