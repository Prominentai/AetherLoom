"""Cancelable FFmpeg jobs; frame files stay inside the current run directory."""
import subprocess
import tempfile
from pathlib import Path
from .advanced_nodes import check_stop


def command(arguments,stop):
 import imageio_ffmpeg
 with tempfile.TemporaryFile() as log:
  process=subprocess.Popen([imageio_ffmpeg.get_ffmpeg_exe(),'-nostdin','-hide_banner','-loglevel','error',*arguments],
   stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=log,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
  try:
   while process.poll() is None:
    if stop.wait(.1):check_stop(stop)
   if process.returncode:
    log.seek(0);raise ValueError('视频处理失败：'+log.read(3000).decode('utf8','replace'))
  finally:
   if process.poll() is None:process.terminate()
   try:process.wait(timeout=3)
   except subprocess.TimeoutExpired:process.kill();process.wait()


def execute(kind,batch,params,directory,stop):
 from . import model
 from .advanced_nodes import _size
 from PIL import Image
 directory.mkdir(parents=True,exist_ok=True)
 if kind=='video_frames':
  source=batch['video']['path']
  from moviepy.video.io.ffmpeg_reader import ffmpeg_parse_infos
  info=ffmpeg_parse_infos(source);_size(tuple(info.get('video_size') or (0,0)))
  mode=params['mode'];options=[]
  if mode=='interval':options=['-ss',str(float(params['start']))]
  filters='select=eq(n\\,%d)' % int(params['frame']) if mode=='index' else 'fps=1/%s' % float(params['interval']) if mode=='interval' else 'select=eq(n\\,0)'
  count=int(params['count']) if mode=='interval' else 1
  command([*options,'-i',source,'-an','-vf',filters,'-vsync','0','-frames:v',str(count),str(directory/'frame_%06d.png')],stop)
  files=sorted(directory.glob('frame_*.png'))
  if not files:raise ValueError('所选时间或帧索引没有对应图像')
  results=[dict(type='image',path=str(path)) for path in files]
  if params['audio'] and info.get('audio_found'):
   audio=directory/'audio.wav';command(['-i',source,'-vn','-c:a','pcm_s16le',str(audio)],stop)
   results.append(dict(type='audio',path=str(audio)))
  return results
 source=batch['images'];items=model.batch_items(source) if model.result_type(source)=='batch' else [source]
 if any(model.result_type(item)!='image' for item in items):raise ValueError('帧合成视频只接受图像或图像 Batch')
 size=None
 # FFmpeg reads a sequential disk list, not an in-memory stack of frames.
 for index,item in enumerate(items):
  check_stop(stop)
  from .utility_nodes import _image
  with _image(item['path']) as image:
   _size(image.size)
   if size and image.size!=size:raise ValueError('帧尺寸不一致，请先使用图像缩放节点统一尺寸')
   size=image.size
   with Image.new('RGBA',size,(0,0,0,255)) as opaque:
    opaque.alpha_composite(image)
    with opaque.convert('RGB') as rgb:rgb.save(directory/('input_%06d.png'%index))
 result=directory/'video.mp4'
 options=['-framerate',str(float(params['fps'])),'-i',str(directory/'input_%06d.png')]
 if 'audio' in batch:options+=['-i',batch['audio']['path']]
 duration=len(items)/float(params['fps'])
 options+=['-map','0:v:0','-vf','pad=ceil(iw/2)*2:ceil(ih/2)*2','-c:v','libx264','-pix_fmt','yuv420p','-crf',str(int(params['quality']))]
 if 'audio' in batch:options+=['-map','1:a:0','-c:a','aac','-af','apad']
 options+=['-t',str(duration),'-movflags','+faststart',str(result)]
 command(options,stop)
 for path in directory.glob('input_*.png'):path.unlink(missing_ok=True)
 return [dict(type='video',path=str(result))]
