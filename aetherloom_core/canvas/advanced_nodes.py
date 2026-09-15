"""Schemas and bounded local processing for compositing, text and mask nodes."""
import copy
import json
import math
from contextlib import ExitStack
from pathlib import Path


def field(key, title, editor, default, options=None):return key, title, editor, default, options
def choice(key, title, default, options):return field(key,title,'enum',default,options)


SCHEMAS = {
 'image_grid': ('图像拼接','image_tools',[
  choice('layout','排列','grid',[('grid','网格'),('horizontal','横排'),('vertical','竖排')]),
  field('columns','列数','int',2,(1,64)),field('cell_width','单元宽度','int',512,(1,4096)),
  field('cell_height','单元高度','int',512,(1,4096)),field('gap','间距','int',8,(0,512)),
  field('background','背景颜色','line','#00000000')]),
 'image_composite': ('图像叠加','image_tools',[
  field('x','横向位置','int',0,(-16384,16384)),field('y','纵向位置','int',0,(-16384,16384)),
  field('opacity','透明度','float',1.,(0.,1.)),field('resize_source','前景适应背景尺寸','bool',False)]),
 'image_format': ('图像格式转换','image_tools',[
  choice('format','输出格式','png',[('png','PNG'),('jpeg','JPEG'),('webp','WebP')]),
  field('quality','质量','int',95,(1,100)),field('background','去除透明时的背景','line','#ffffff')]),
 'image_to_mask': ('图像转 MASK','mask_tools',[
  choice('channel','读取通道','gray',[('gray','灰度'),('red','红'),('green','绿'),('blue','蓝'),('alpha','Alpha')]),
  field('invert','反相','bool',False)]),
 'attached_mask': ('读取图像遮罩','mask_tools',[]),
 'image_join_alpha': ('图像与遮罩合并（RGBA）','mask_tools',[]),
 'image_split_alpha': ('拆分图像与遮罩','mask_tools',[]),
 'image_mask_composite': ('按遮罩混合图像','mask_tools',[
  field('x','横向位置','int',0,(0,16384)),field('y','纵向位置','int',0,(0,16384)),
  field('resize_source','前景适应背景尺寸','bool',False)]),
 'mask_preview': ('遮罩与图像混合预览','mask_tools',[
  field('opacity','遮罩显示透明度','float',.5,(0.,1.))]),
 'mask_to_image': ('MASK 转图像','mask_tools',[]),
 'mask_invert': ('遮罩反相','mask_tools',[]),
 'mask_grow': ('遮罩扩张 / 收缩','mask_tools',[field('amount','像素（负值收缩）','int',8,(-64,64)),
  field('tapered_corners','削角扩张','bool',False)]),
 'mask_feather': ('遮罩羽化','mask_tools',[field('radius','模糊半径','float',4.,(0.,128.))]),
 'mask_grow_blur': ('遮罩高斯扩展','mask_tools',[
  field('amount','扩张像素（负值收缩）','int',8,(-64,64)),field('radius','高斯羽化半径','float',4.,(0.,128.)),
  field('tapered_corners','削角扩张','bool',False)]),
 'mask_edge_feather': ('遮罩四边羽化','mask_tools',[
  field('left','左侧宽度','int',0,(0,16384)),field('top','顶部宽度','int',0,(0,16384)),
  field('right','右侧宽度','int',0,(0,16384)),field('bottom','底部宽度','int',0,(0,16384))]),
 'mask_threshold': ('遮罩二值化','mask_tools',[field('threshold','阈值','float',.5,(0.,1.))]),
 'mask_resize': ('遮罩缩放','mask_tools',[
  field('width','宽度','int',512,(1,16384)),field('height','高度','int',512,(1,16384)),
  choice('interpolation','插值','nearest',[('nearest','最近邻（保留二值）'),('bilinear','双线性（柔和边缘）')])]),
 'text_replace': ('文本替换','text_tools',[
  field('text','文本','text',''),field('find','查找','line',''),field('replacement','替换为','line',''),
  field('count','替换次数（0 为全部）','int',0,(0,10000))]),
 'text_extract': ('文本提取','text_tools',[
  field('text','文本','text',''),field('start','起始标记（留空从头）','line',''),
  field('end','结束标记（留空到尾）','line',''),field('all_matches','提取所有匹配段','bool',False)]),
 'json_extract': ('JSON 字段提取','text_tools',[
  field('text','JSON 文本','text','{}'),field('pointer','字段路径（JSON Pointer）','line','/prompt'),
  choice('type','输出类型','text',[('text','文本'),('int','INT'),('float','FLOAT'),('boolean','布尔')])]),
 'number_math': ('数值计算','text_tools',[
  field('a','数值 A','float',0.,(-1e15,1e15)),field('b','数值 B','float',1.,(-1e15,1e15)),
  choice('operation','运算','add',[('add','加'),('subtract','减'),('multiply','乘'),('divide','除'),
   ('min','较小值'),('max','较大值'),('floor','向下取整 A'),('ceil','向上取整 A'),('round','就近取整 A'),('clamp','限制 A 范围')]),
  field('minimum','下限','float',0.,(-1e15,1e15)),field('maximum','上限','float',1.,(-1e15,1e15))]),
 'value_convert': ('类型转换','text_tools',[
  choice('type','输出类型','text',[('text','文本'),('int','INT'),('float','FLOAT'),('boolean','布尔')]),
  choice('rounding','转 INT 的取整方式','exact',[('exact','要求整数'),('floor','向下取整'),('ceil','向上取整'),('round','就近取整')])]),
 'value_compare': ('数值 / 文本比较','text_tools',[
  field('a','值 A','line',''),field('b','值 B','line',''),
  choice('operation','比较','equal',[('equal','相等'),('not_equal','不相等'),('greater','大于'),('less','小于'),('contains','包含文本')])]),
 'boolean': ('布尔','input',[field('value','值','bool',False)]),
 'manual_select': ('人工选择结果','output',[]),
 'branch': ('条件分支','utility',[field('condition','条件','bool',True)]),
 'video_frames': ('视频拆帧','video_tools',[
  choice('mode','提取方式','first',[('first','首帧'),('index','指定帧'),('interval','间隔采样')]),
  field('frame','帧索引（从 0 开始）','int',0,(0,10000000)),field('start','起始秒','float',0.,(0.,86400.)),
  field('interval','采样间隔（秒）','float',1.,(.01,3600.)),field('count','最多提取帧数','int',32,(1,256)),
  field('audio','同时提取音轨','bool',False)]),
 'video_assemble': ('帧合成视频','video_tools',[
  field('fps','帧率','float',24.,(.1,240.)),field('quality','CRF（越小越清晰）','int',18,(0,51))]),
 'subgraph': ('组合节点','utility',[]),
}
from . import bounding_nodes
SCHEMAS.update(bounding_nodes.SCHEMAS)
KINDS = frozenset(SCHEMAS)
IMAGE_KINDS = frozenset(k for k in KINDS if k.startswith(('image_', 'mask_')) or k == 'attached_mask')
VIDEO_KINDS = frozenset({'video_frames','video_assemble'})
PASS_KINDS = frozenset({'manual_select','branch'})


def inputs(node):
 if node['kind'] in bounding_nodes.KINDS:return bounding_nodes.inputs(node)
 kind=node['kind'];ports=[]
 if kind=='image_grid':ports=[('images','图像','image_input')]
 elif kind=='image_composite':ports=[('background','背景','image'),('foreground','前景','image'),('mask','遮罩','mask')]
 elif kind=='mask_preview':ports=[('image','图像','image'),('mask','遮罩','mask')]
 elif kind=='image_join_alpha':ports=[('image','图像','image'),('mask','遮罩','mask')]
 elif kind=='image_split_alpha':ports=[('image','图像','image')]
 elif kind=='image_mask_composite':ports=[('destination','背景','image'),('source','前景','image'),('mask','遮罩','mask')]
 elif kind in ('image_format','image_to_mask','attached_mask'):ports=[('image','图像','image')]
 elif kind.startswith('mask_'):ports=[('mask','遮罩','mask')]
 elif kind=='value_convert':ports=[('value','内容','any')]
 elif kind=='manual_select':ports=[('value','候选结果','any')]
 elif kind=='branch':ports=[('value','内容','any'),('condition','条件','boolean')]
 elif kind=='video_frames':ports=[('video','视频','video')]
 elif kind=='video_assemble':ports=[('images','图像','image_input'),('audio','音轨','audio')]
 for key,label,editor,_,_ in SCHEMAS[kind][2]:
  if kind in ('branch','boolean'):continue
  if editor in ('int','float') or key=='text' or kind=='value_compare' and key in ('a','b'):
   ports.append((key,label,'number' if editor=='float' else 'int' if editor=='int' else 'any' if kind=='value_compare' else 'text'))
 return [dict(key=k,label=l,type=t) for k,l,t in ports]


def output_type(node):
 kind=node['kind']
 if kind=='mask_preview':return 'image'
 if kind in ('image_to_mask','attached_mask') or kind.startswith('mask_') and kind!='mask_to_image':return 'mask'
 if kind.startswith('image_') or kind=='mask_to_image':return 'image'
 if kind in ('json_extract','value_convert'):return node.get('params',{}).get('type','text')
 if kind in ('boolean','value_compare'):return 'boolean'
 if kind=='number_math':return 'float'
 if kind=='video_assemble':return 'video'
 if kind.startswith('text_'):return 'text'
 return 'any'


def typed(value, kind, rounding='exact'):
 if kind=='text':return dict(type='text',text=value if isinstance(value,str) else json.dumps(value,ensure_ascii=False))
 if kind=='boolean':
  if isinstance(value,str):
   if value.strip().lower() not in ('true','false','0','1'):raise ValueError('布尔文本仅接受 true、false、0、1')
   value=value.strip().lower() in ('true','1')
  elif type(value) not in (bool,int,float) or value not in (0,1):raise ValueError('布尔值需要 true / false 或 0 / 1')
  return dict(type=kind,value=bool(value))
 if isinstance(value,bool):raise ValueError('布尔值不能隐式转换为数值')
 from decimal import Decimal, InvalidOperation, ROUND_FLOOR, ROUND_CEILING, ROUND_HALF_EVEN
 try:
  number=Decimal(str(value))
  if not number.is_finite():raise ValueError('数值必须有限')
  if kind=='int':
   if rounding=='exact' and number!=number.to_integral_value():raise ValueError('存在小数，请选择取整方式')
   number=number.to_integral_value(rounding={'floor':ROUND_FLOOR,'ceil':ROUND_CEILING,'round':ROUND_HALF_EVEN}.get(rounding,ROUND_HALF_EVEN))
   if not -(2**63)<=number<=2**63-1:raise ValueError('整数超过 INT64 范围')
   return dict(type='int',value=int(number))
  number=float(number)
  if not math.isfinite(number):raise ValueError('浮点数超限')
  return dict(type='float',value=number)
 except (InvalidOperation,TypeError):raise ValueError('无法转换为指定数值类型')


def scalar(result):
 from . import model
 kind=model.result_type(result)
 if kind not in model.VALUE_TYPES | {'text'}:raise ValueError('需要文本或数值，请先转换输入类型')
 if 'value' in result:return result['value']
 if 'text' in result:return result['text']
 if result.get('path') and Path(result['path']).stat().st_size<=8*1024*1024:return Path(result['path']).read_text('utf-8-sig')
 raise ValueError('文本结果不可读或超过 8 MiB')


def _size(size):
 if min(size)<1 or max(size)>16384 or size[0]*size[1]>40_000_000:raise ValueError('图像尺寸超限：每边最多 16384，总计最多 4000 万像素')


def image_operation(node, batch, params, directory, stop):
 if node['kind'] in bounding_nodes.KINDS:return bounding_nodes.operation(node,batch,params,directory,stop)
 from PIL import Image,ImageOps,ImageColor,ImageFilter,ImageChops
 from . import model
 from .utility_nodes import _image
 kind=node['kind'];directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
 result_metadata={}
 def open_image(key):return _image(batch[key]['path'])
 def open_mask():
  if model.result_type(batch['mask'])!='mask':raise ValueError('请先使用“图像转 MASK”')
  with Image.open(batch['mask']['path']) as image:
   _size(image.size);return image.convert('L')
 with ExitStack() as stack:
  if kind=='image_grid':
   source=batch['images'];items=model.batch_items(source) if model.result_type(source)=='batch' else [source]
   if any(model.result_type(r)!='image' for r in items):raise ValueError('拼接仅接收图像 Batch')
   count=len(items);columns=count if params['layout']=='horizontal' else 1 if params['layout']=='vertical' else int(params['columns'])
   columns=min(count,columns);rows=math.ceil(count/columns);w,h,gap=int(params['cell_width']),int(params['cell_height']),int(params['gap'])
   size=(columns*w+(columns-1)*gap,rows*h+(rows-1)*gap);_size(size)
   result=stack.enter_context(Image.new('RGBA',size,ImageColor.getcolor(params['background'],'RGBA')))
   for i,item in enumerate(items):
    check_stop(stop)
    from .utility_nodes import resize_dimensions
    with _image(item['path']) as image:
     with image.resize(resize_dimensions(image.size,w,h,'contain'),Image.Resampling.LANCZOS) as fitted:
      result.alpha_composite(fitted,((i%columns)*(w+gap)+(w-fitted.width)//2,(i//columns)*(h+gap)+(h-fitted.height)//2))
  elif kind=='image_composite':
   result=stack.enter_context(open_image('background'));foreground=stack.enter_context(open_image('foreground'))
   if params['resize_source']:foreground=stack.enter_context(foreground.resize(result.size,Image.Resampling.LANCZOS))
   alpha=stack.enter_context(foreground.getchannel('A'))
   if 'mask' in batch:
    mask=stack.enter_context(open_mask());mask=stack.enter_context(mask.resize(foreground.size,Image.Resampling.BILINEAR))
    alpha=stack.enter_context(ImageChops.multiply(alpha,mask))
   alpha=stack.enter_context(alpha.point(lambda x:round(x*float(params['opacity']))));foreground.putalpha(alpha)
   result.alpha_composite(foreground,(int(params['x']),int(params['y'])))
  elif kind in ('image_join_alpha','image_split_alpha'):
   if 'image' not in batch or kind=='image_join_alpha' and 'mask' not in batch:
    raise ValueError('请连接图像和 MASK' if kind=='image_join_alpha' else '请连接图像')
   image=stack.enter_context(open_image('image'))
   if kind=='image_join_alpha':
    mask=stack.enter_context(open_mask());mask=stack.enter_context(mask.resize(image.size,Image.Resampling.BILINEAR))
    result=image;alpha=stack.enter_context(ImageOps.invert(mask));result.putalpha(alpha)
   else:
    alpha=stack.enter_context(image.getchannel('A'));mask=stack.enter_context(ImageOps.invert(alpha))
    result=stack.enter_context(image.convert('RGB'))
   mask_path=directory/'mask.png';mask.save(mask_path)
   result_metadata.update(mask_path=str(mask_path),_processed_mask=True,_mask_nonempty=bool(mask.getbbox()))
  elif kind=='image_mask_composite':
   if 'destination' not in batch or 'source' not in batch:raise ValueError('请连接背景和前景图像')
   with Image.open(batch['destination']['path']) as original:
    mode='RGBA' if 'A' in original.getbands() or 'transparency' in original.info else 'RGB'
   destination=stack.enter_context(open_image('destination'));result=stack.enter_context(destination.convert(mode))
   source=stack.enter_context(open_image('source'));source=stack.enter_context(source.convert(mode))
   if params['resize_source']:source=stack.enter_context(source.resize(result.size,Image.Resampling.BILINEAR))
   mask=stack.enter_context(open_mask()) if 'mask' in batch else stack.enter_context(Image.new('L',source.size,255))
   mask=stack.enter_context(mask.resize(source.size,Image.Resampling.BILINEAR))
   # ComfyUI blends channels using MASK coverage; this is distinct from the
   # existing alpha-composited foreground/background node.
   result.paste(source,(int(params['x']),int(params['y'])),mask)
  elif kind=='mask_preview':
   image=stack.enter_context(open_image('image')) if 'image' in batch else None
   mask=stack.enter_context(open_mask()) if 'mask' in batch else None
   if mask is None and image is not None:
    attached=batch['image'].get('mask_path')
    if attached:
     with Image.open(attached) as original:
      _size(original.size);mask=stack.enter_context(original.convert('L'))
    else:mask=stack.enter_context(ImageOps.invert(image.getchannel('A')))
   if image is None:
    if mask is None:raise ValueError('请连接图像或 MASK')
    result=stack.enter_context(mask.convert('RGB'))
   else:
    mask=stack.enter_context(mask.resize(image.size,Image.Resampling.BILINEAR))
    overlay=stack.enter_context(Image.new('RGBA',image.size,(88,180,255,0)))
    alpha=stack.enter_context(mask.point(lambda value:round(value*float(params['opacity']))))
    overlay.putalpha(alpha)
    base=stack.enter_context(image.convert('RGB'));base=stack.enter_context(base.convert('RGBA'))
    result=stack.enter_context(Image.alpha_composite(base,overlay))
  elif kind=='attached_mask':
   path=batch['image'].get('mask_path')
   if not path or not Path(path).is_file():raise ValueError('此图像没有附带遮罩')
   with Image.open(path) as original:
    _size(original.size);result=stack.enter_context(original.convert('L'))
  elif kind=='image_to_mask':
   image=stack.enter_context(open_image('image'));channel=params['channel']
   result=stack.enter_context(image.convert('L') if channel=='gray' else image.getchannel({'red':'R','green':'G','blue':'B','alpha':'A'}[channel]))
   if params['invert']:result=stack.enter_context(ImageOps.invert(result))
  elif kind=='image_format':
   result=stack.enter_context(open_image('image'))
   if params['format']=='jpeg':
    flattened=stack.enter_context(Image.new('RGBA',result.size,ImageColor.getcolor(params['background'],'RGBA')))
    flattened.alpha_composite(result);result=stack.enter_context(flattened.convert('RGB'))
  else:
   mask=stack.enter_context(open_mask())
   if kind=='mask_to_image':result=stack.enter_context(mask.convert('RGB'))
   elif kind=='mask_invert':result=stack.enter_context(ImageOps.invert(mask))
   elif kind=='mask_grow':
    from .mask_processing import grow
    result=stack.enter_context(grow(mask,params['amount'],params['tapered_corners'],stop))
   elif kind=='mask_grow_blur':
    from .mask_processing import grow_blur
    result=stack.enter_context(grow_blur(mask,params['amount'],params['radius'],params['tapered_corners'],stop))
   elif kind=='mask_edge_feather':
    from .mask_processing import feather_edges
    result=stack.enter_context(feather_edges(mask,params,stop))
   elif kind=='mask_feather':result=stack.enter_context(mask.filter(ImageFilter.GaussianBlur(float(params['radius']))))
   elif kind=='mask_threshold':result=stack.enter_context(mask.point(lambda x:255 if x/255>=float(params['threshold']) else 0))
   elif kind=='mask_resize':
    size=(int(params['width']),int(params['height']));_size(size)
    result=stack.enter_context(mask.resize(size,Image.Resampling.NEAREST if params['interpolation']=='nearest' else Image.Resampling.BILINEAR))
   else:raise ValueError('不支持的图像处理')
  check_stop(stop)
  suffix=params['format'] if kind=='image_format' else 'png';target=directory/('result.'+suffix)
  result.save(target,quality=int(params.get('quality',95)))
 return [dict(type=output_type(node),path=str(target),**result_metadata)]


def check_stop(stop):
 if stop.is_set():
  from .save_results import SaveCanceled
  raise SaveCanceled('本地处理已取消')


def execute(node,directory,batches,stop):
 from . import model
 from .utility_nodes import defaults,_IMAGE_SLOT
 output=[];kind=node['kind']
 for index,batch in enumerate(batches):
  check_stop(stop);params=dict(defaults(kind),**node.get('params',{}))
  for key,_,editor,_,bounds in SCHEMAS[kind][2]:
   if key in batch:
    params[key]=scalar(batch[key])
    if editor in ('int','float'):
     params[key]=typed(params[key],editor)['value']
     if not bounds[0]<=params[key]<=bounds[1]:raise ValueError(key+' 超出范围')
  if kind in IMAGE_KINDS:
   while not _IMAGE_SLOT.acquire(timeout=.1):check_stop(stop)
   try:results=image_operation(node,batch,params,Path(directory)/str(index),stop)
   finally:_IMAGE_SLOT.release()
  elif kind in VIDEO_KINDS:
   from .video_nodes import execute as video_execute
   results=video_execute(kind,batch,params,Path(directory)/str(index),stop)
  elif kind=='text_replace':
   if not params['find']:raise ValueError('查找内容不能为空')
   results=[dict(type='text',text=str(params['text']).replace(params['find'],params['replacement'],int(params['count']) or -1))]
  elif kind=='text_extract':
   text=str(params['text']);start,end=params['start'],params['end'];parts=[];offset=0
   while offset<=len(text):
    left=text.find(start,offset) if start else offset
    if left<0:break
    left+=len(start);right=text.find(end,left) if end else len(text)
    if right<0:break
    parts.append(text[left:right]);offset=right+len(end)
    if not params['all_matches'] or not end or offset<=left:break
    if len(parts)>10000:raise ValueError('匹配条目过多')
   if not parts:raise ValueError('没有找到指定文本段')
   results=[dict(type='text',text=value) for value in parts]
  elif kind=='json_extract':
   raw=str(params['text']).strip()
   if raw.startswith('```') and raw.endswith('```'):raw=raw.split('\n',1)[1].rsplit('```',1)[0]
   value=json.loads(raw);pointer=params['pointer']
   if pointer and not pointer.startswith('/'):raise ValueError('字段路径使用 /prompt 或 /scenes/0/prompt；留空提取根值')
   try:
    for token in pointer.split('/')[1:] if pointer else []:
     token=token.replace('~1','/').replace('~0','~')
     if isinstance(value,list):
      if not token.isdigit() or len(token)>1 and token.startswith('0'):raise ValueError('无效数组索引')
      value=value[int(token)]
     else:value=value[token]
   except (KeyError,IndexError,ValueError,TypeError):raise ValueError('JSON 中不存在指定字段：'+pointer)
   values=value if isinstance(value,list) else [value]
   if not values or len(values)>10000:raise ValueError('JSON 数组需包含 1–10000 项')
   results=[typed(v,params['type']) for v in values]
  elif kind=='value_convert':results=[typed(scalar(batch['value']),params['type'],params['rounding'])]
  elif kind=='number_math':
   a,b=float(params['a']),float(params['b']);op=params['operation']
   if op=='divide' and b==0:raise ValueError('除数不能为零')
   if params['minimum']>params['maximum']:raise ValueError('下限不能大于上限')
   functions={'add':lambda:a+b,'subtract':lambda:a-b,'multiply':lambda:a*b,'divide':lambda:a/b,
    'min':lambda:min(a,b),'max':lambda:max(a,b),'floor':lambda:math.floor(a),'ceil':lambda:math.ceil(a),
    'round':lambda:round(a),'clamp':lambda:min(float(params['maximum']),max(float(params['minimum']),a))}
   results=[typed(functions[op](),'float')]
  elif kind=='value_compare':
   a,b=params['a'],params['b'];op=params['operation']
   if op in ('greater','less'):a,b=typed(a,'float')['value'],typed(b,'float')['value']
   answer=(a==b if op=='equal' else a!=b if op=='not_equal' else a>b if op=='greater' else a<b if op=='less' else str(b) in str(a))
   results=[dict(type='boolean',value=answer)]
  elif kind=='boolean':results=[dict(type='boolean',value=params['value'])]
  else:raise ValueError('此节点需由画布调度器执行')
  for result in results:
   # The crop's image, box and mask describe the same input item. Their common
   # origin must survive edits/filters so each patch returns to its own image.
   origin_index = index if kind == 'image_crop_mask' else len(output)
   origins = model.result_lineage(batch,node['id'],origin_index)
   if kind == 'image_crop_mask':origins['__bounding_group__:' + node['id']] = str(index)
   result.update(index=len(output),lineage=origins);output.append(result)
 check_stop(stop);return output
