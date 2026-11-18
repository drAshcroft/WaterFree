"""ComfyUI's public HTTP API, using core nodes and existing registered weights."""
from __future__ import annotations

import io
import json
import secrets
import time
import uuid
from pathlib import Path
from urllib import error, parse, request

from backend.imagegen.backend_diffusers import BackendUnavailable, GenerationFailed


def _url(base, path):
    parts = parse.urlsplit(base)
    if parts.scheme not in ('http', 'https') or not parts.netloc or parts.username or parts.password:
        raise ValueError('ComfyUI URL must be an HTTP(S) endpoint without embedded credentials.')
    return base.rstrip('/') + path


def _http(base, path, data=None, *, content_type='application/json', timeout=15, binary=False):
    body = json.dumps(data).encode() if data is not None and not isinstance(data, bytes) else data
    req = request.Request(_url(base, path), data=body,
                          headers={'Content-Type': content_type} if body is not None else {})
    try:
        with request.urlopen(req, timeout=timeout) as response:
            payload = response.read()
        return payload if binary else json.loads(payload)
    except (error.URLError, OSError, ValueError) as exc:
        detail = exc.read().decode(errors='replace')[:1500] if isinstance(exc, error.HTTPError) else str(exc)
        raise GenerationFailed(f'ComfyUI {path}: {detail}') from exc


def is_available(spec):
    try:
        _http(spec.comfy_url, '/system_stats', timeout=5)
        return True, ''
    except (GenerationFailed, ValueError) as exc:
        return False, f'{exc}. Start ComfyUI or configure comfyUrl.'


def require_available(spec):
    usable, reason = is_available(spec)
    if not usable:
        raise BackendUnavailable(reason)


def free(spec) -> bool:
    """Ask ComfyUI to unload its models and free cached memory (``POST /free``); never raises.

    ComfyUI keeps models resident between jobs by design. With ``comfyFreeAfter``
    WaterFree asks it to let go, so the next job in the GPU queue finds the
    memory. A failure only means ComfyUI keeps its models; the images are done.
    """
    try:
        # /free answers 200 with an empty body, so read it raw rather than as JSON.
        _http(spec.comfy_url, '/free', {'unload_models': True, 'free_memory': True}, binary=True)
        return True
    except (GenerationFailed, ValueError):
        return False


def _choices(info, node, field):
    try:
        return info[node]['input']['required'][field][0]
    except (KeyError, IndexError, TypeError) as exc:
        raise GenerationFailed(f'ComfyUI is missing required core node {node}.{field}.') from exc


def load_pipeline(spec, *, hf_token=''):
    info = _http(spec.comfy_url, '/object_info')
    checkpoints = _choices(info, 'CheckpointLoaderSimple', 'ckpt_name')
    checkpoint = spec.comfy_checkpoint
    if not checkpoint and len(checkpoints) == 1:
        checkpoint = checkpoints[0]
    if checkpoint not in checkpoints:
        raise GenerationFailed('Select --checkpoint from the registered ComfyUI checkpoints: ' + ', '.join(checkpoints))
    loras = []
    for value in spec.loras:
        available = _choices(info, 'LoraLoader', 'lora_name')
        name = value['path']
        if name not in available and Path(name).is_file():
            matches = [x for x in available if Path(x).name == Path(name).name]
            if len(matches) == 1:
                name = matches[0]
        if name not in available:
            raise GenerationFailed(f'LoRA is not registered in ComfyUI: {value["path"]}. Place it in a configured models/loras folder.')
        loras.append({**value, 'path': name})
    return {'checkpoint': checkpoint, 'loras': loras}


def _upload(spec, path, *, mask=False):
    from PIL import Image, ImageOps
    with Image.open(path) as source:
        pixels = source.convert('L').convert('RGB') if mask else ImageOps.exif_transpose(source).convert('RGB')
        pixels = pixels.resize((spec.width, spec.height), Image.Resampling.NEAREST if mask else Image.Resampling.LANCZOS)
        stream = io.BytesIO()
        pixels.save(stream, format='PNG')
    boundary = 'WaterFree' + uuid.uuid4().hex
    filename = 'waterfree_' + uuid.uuid4().hex + '.png'
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="{filename}"\r\n'
            'Content-Type: image/png\r\n\r\n').encode() + stream.getvalue()
    body += (f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="type"\r\n\r\ninput'
             f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="overwrite"\r\n\r\nfalse'
             f'\r\n--{boundary}--\r\n').encode()
    result = _http(spec.comfy_url, '/upload/image', body, content_type=f'multipart/form-data; boundary={boundary}')
    return (result.get('subfolder', '').strip('/') + '/' + result['name']).lstrip('/')


def build_workflow(spec, context, prompt, count, *, image='', mask='', seed=None):
    graph = {}
    def node(kind, **inputs):
        key = str(len(graph) + 1)
        graph[key] = {'class_type': kind, 'inputs': inputs}
        return key
    checkpoint = node('CheckpointLoaderSimple', ckpt_name=context['checkpoint'])
    model, clip, vae = [checkpoint, 0], [checkpoint, 1], [checkpoint, 2]
    for lora in context['loras']:
        key = node('LoraLoader', model=model, clip=clip, lora_name=lora['path'],
                   strength_model=lora['scale'], strength_clip=lora['scale'])
        model, clip = [key, 0], [key, 1]
    positive = node('CLIPTextEncode', text=prompt, clip=clip)
    negative = node('CLIPTextEncode', text=spec.negative_prompt, clip=clip)
    input_pixels = mask_pixels = None
    if image:
        input_pixels = [node('LoadImage', image=image), 0]
        if mask:
            mask_image = node('LoadImage', image=mask)
            mask_pixels = [node('ImageToMask', image=[mask_image, 0], channel='red'), 0]
            latent = node('VAEEncodeForInpaint', pixels=input_pixels, vae=vae, mask=mask_pixels, grow_mask_by=6)
        else:
            latent = node('VAEEncode', pixels=input_pixels, vae=vae)
        if count > 1:
            latent = node('RepeatLatentBatch', samples=[latent, 0], amount=count)
    else:
        latent = node('EmptyLatentImage', width=spec.width, height=spec.height, batch_size=count)
    sampler = node('KSampler', model=model, positive=[positive, 0], negative=[negative, 0],
                   latent_image=[latent, 0], seed=seed if seed is not None else spec.seed,
                   # ComfyUI's CFG=1 disables guidance; Diffusers uses 0.
                   steps=spec.steps, cfg=spec.guidance if spec.guidance > 0 else 1.0, sampler_name='euler', scheduler='normal',
                   denoise=spec.strength if image else 1.0)
    decoded = node('VAEDecode', samples=[sampler, 0], vae=vae)
    pixels = [decoded, 0]
    if mask:
        if count > 1:
            input_pixels = [node('RepeatImageBatch', image=input_pixels, amount=count), 0]
        pixels = [node('ImageCompositeMasked', destination=input_pixels, source=pixels,
                       mask=mask_pixels, x=0, y=0, resize_source=False), 0]
    output = node('SaveImage', images=pixels, filename_prefix='WaterFree/' + uuid.uuid4().hex)
    return graph, output


def run_pipeline(context, spec, prompt, *, count):
    image = _upload(spec, spec.image) if spec.image else ''
    mask = _upload(spec, spec.mask, mask=True) if spec.mask else ''
    workflow, output = build_workflow(spec, context, prompt, count, image=image, mask=mask,
                                     seed=spec.seed if spec.seed >= 0 else secrets.randbelow(2**63))
    queued = _http(spec.comfy_url, '/prompt', {'prompt': workflow, 'client_id': uuid.uuid4().hex})
    if queued.get('error') or queued.get('node_errors') or not queued.get('prompt_id'):
        raise GenerationFailed('ComfyUI rejected the workflow: ' + json.dumps(queued)[:2000])
    prompt_id = queued['prompt_id']
    deadline = time.monotonic() + spec.comfy_timeout
    while time.monotonic() < deadline:
        remaining = max(.1, deadline - time.monotonic())
        history = _http(spec.comfy_url, '/history/' + parse.quote(prompt_id, safe=''), timeout=min(15, remaining))
        entry = history.get(prompt_id)
        if entry:
            status = entry.get('status', {})
            if status.get('status_str') == 'error':
                raise GenerationFailed('ComfyUI execution failed: ' + json.dumps(status.get('messages', []))[:2000])
            descriptors = entry.get('outputs', {}).get(output, {}).get('images', [])
            if descriptors:
                from PIL import Image
                images = []
                for value in descriptors:
                    query = parse.urlencode({k: value[k] for k in ('filename', 'subfolder', 'type') if k in value})
                    data = _http(spec.comfy_url, '/view?' + query, binary=True)
                    with Image.open(io.BytesIO(data)) as pixels:
                        images.append(pixels.copy())
                return images
            if status.get('completed'):
                raise GenerationFailed(f'ComfyUI job {prompt_id} completed without image output.')
        time.sleep(min(.5, max(0, deadline - time.monotonic())))
    raise GenerationFailed(f'ComfyUI job {prompt_id} exceeded {spec.comfy_timeout:g}s. It may still be running; other users’ jobs were not interrupted.')
