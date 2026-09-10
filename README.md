<svg width="220" height="220" viewBox="0 0 220 220" xmlns="http://www.w3.org/2000/svg">
    <defs>
        <linearGradient id="al_grad" x1="0" y1="0" x2="1" y2="1">
            <stop offset="0%" stop-color="#12c2e9"/>
            <stop offset="60%" stop-color="#6a00f4"/>
            <stop offset="100%" stop-color="#00d4ff"/>
        </linearGradient>
        <radialGradient id="al_core" cx="50%" cy="45%" r="60%">
            <stop offset="0%" stop-color="#ffffff" stop-opacity="0.08"/>
            <stop offset="70%" stop-color="#0b1220" stop-opacity="0.9"/>
        </radialGradient>
        <filter id="al_shadow" x="-30%" y="-30%" width="160%" height="160%">
            <feDropShadow dx="0" dy="6" stdDeviation="10" flood-color="#071425" flood-opacity="0.6"/>
        </filter>
    </defs>
    <!-- outer ring -->
    <circle cx="110" cy="110" r="94" fill="none" stroke="url(#al_grad)" stroke-width="12" stroke-linecap="round" filter="url(#al_shadow)"/>
    <!-- core background -->
    <circle cx="110" cy="110" r="82" fill="url(#al_core)" stroke="#071425" stroke-width="3"/>
    <!-- woven A emblem: two interlaced strokes forming an A -->
    <g transform="translate(0,6)">
        <path d="M70 150 L110 60 L150 150" fill="none" stroke="url(#al_grad)" stroke-width="14" stroke-linecap="round" stroke-linejoin="round"/>
        <path d="M86 122 L134 122" fill="none" stroke="#ffffff" stroke-width="10" stroke-linecap="round" stroke-linejoin="round" opacity="0.9"/>
        <!-- woven detail -->
        <path d="M95 120 L110 80 L125 120" fill="none" stroke="#0b1220" stroke-width="6" stroke-linecap="round" stroke-linejoin="round" opacity="0.18"/>
    </g>
    <!-- subtle highlight triangle to suggest play/loom -->
    <path d="M104 98 L104 132 L136 115 Z" fill="#ffffff" fill-opacity="0.06"/>
</svg>

![home_emblem](https://github.com/user-attachments/assets/f3fb2234-9484-48ff-8e94-4a2f104142ac)


# AetherLoom - Cloud AI Apps / ComfyUI Workflows Local Interactive Interface and View Management
# - 云端AI应用/comfyui工作流本地交互界面与视图管理

当前版本：**0.2**

<img width="3154" height="1809" alt="image" src="https://github.com/user-attachments/assets/806a0280-a4a0-4070-8af5-87e064eb566b" />


国内下载链接：https://pan.quark.cn/s/f0a699748a8e

视频教程：https://www.bilibili.com/video/BV1bfByBNEcv

本应用程序旨在通过利用API访问在线模型/工作流来减轻部署本地模型的计算负担，并提供一个用户友好的本地交互界面，尽可能在简化操作的同时丰富AI画图功能。

目前已实现的功能：

1. 输入Runninghub AI应用网址生成本地UI界面，任意修改参数与上传文件进行工作流运行
2. 本地视图管理
3. 本地视图GRC解码
4. 提示词自动补全
5. 提示词翻译与扩写
6. 图像反推（支持批量）
7. RH 模型库：检索公共模型、选择版本、复制 model name；本地收藏支持置顶、编辑和自建私有模型快捷项，与 App 和画布的模型选择器共用

模型库提供“公共模型”“本地收藏”“我的上传”三个分页。“本地收藏”和“我的上传”按 `.cn` / `.ai` 站点和模型类型独立保存，重启后保留，可在没有 API Key 时查看和选用。星标只增删本地收藏，不修改官网收藏或上传记录。“自建收藏”创建常用模型快捷项；“登记上传模型”创建已上传模型的本地记录，不上传模型文件。两组数据分别位于 `model_library/favorites.sqlite3` 和 `model_library/uploads.sqlite3`，不包含在发布包中。

自建收藏支持拖入封面图片，保存后存放在 `model_library/covers/`，再次替换会覆盖原封面。收藏界面仅展示图片，不展示封面路径。

两组均支持单个模型链接导入和官网批量导入。Windows 下使用默认浏览器当前配置的官网登录状态（支持 Chrome、Edge、Brave、Vivaldi），已登录可直接点击“读取我的收藏”或“读取我的上传”。未登录或登录失效时，点击“在默认浏览器登录”，在普通浏览器窗口完成登录后返回客户端重新读取；中文站和国际站需分别登录。不会创建无痕窗口，也不会关闭用户浏览器。客户端只读当前配置的 RH 登录记录，不复制浏览器配置、不读取其他网站凭据；登录状态仅在本次请求内存中使用，不写入本地模型库。暂不支持的默认浏览器会明确提示。

后台通过 HTTP 自动分页获取数据，无需打开官网模型列表或手动翻页。检查读取摘要后点击“确认导入”，才写入对应本地分组。默认导入首个可用版本，也可选择全部可用版本；重复项保留本地设置，支持停止并导入已读取部分，单次最多读取 10000 个模型。私有模型详情链接同样复用默认浏览器的登录状态。使用多个浏览器配置时，请在最近使用的普通配置中登录目标账号后读取。

基础模型类别从官网枚举获取，创建界面支持搜索下拉选择，模型筛选支持多选；保留自定义名称输入，并缓存枚举供离线使用。模型库每组最多展示 30 个模型，使用“下一组”替换当前结果，不随滚动追加；筛选窗口可取消模型类型限制。有关联官网编号的模型可从卡片或详情页打开官网。App 内切换模型后可用 Ctrl+Z 撤销、Ctrl+Y 重做。

API 管理支持直接编辑和保存密钥，也可通过“添加供应商”选择接口模板、指定名称，建立多份独立的地址、模型和密钥配置。翻译模型可选择无需密钥的免费翻译，或复用“大语言模型”区的当前配置；大语言模型翻译提示词支持 `{target_lang}` 目标语言占位符。密钥保存在本地 `apikeys.json`，普通配置与提示词保存在 `settings.json`。

仅自定义接口可手动选择协议。预设供应商及基于预设模板添加的命名账户使用模板协议，不显示协议选择；旧的协议覆盖配置在加载时清理。文本/视觉支持 OpenAI Chat Completions、Responses、Anthropic Messages、Ollama Chat 和 Generate；翻译支持 Google Translation v2 与百度翻译。新建自定义文本/视觉配置默认 Chat Completions，旧配置继续“自动识别”；明确指定的协议优先于 URL 后缀，统一用于实际调用、响应测试和模型列表查询。完整网关路径保持原样，只有根地址或版本基础地址补全协议路径；特殊网关路径无法推断模型目录时提示手填模型。协议选项保存在对应供应商配置中，不影响其他账户；图片类别暂不提供尚未实现的自定义调用协议。

模型设置部分交互参考 [SillyTavern 的连接配置](https://docs.sillytavern.app/usage/core-concepts/connection-profiles/)与模型选择方式，采用本项目独立 PyQt 实现。“复制配置”建立独立命名副本，保留协议、地址、模型与超时，密钥及 Agent 登录授权单独配置。“浏览模型”支持搜索当前候选列表和手填模型 ID，取消不改动当前模型，确认选择后支持输入框撤销；使用模型视图展示大量候选，不为每项创建独立控件。获取目录和测试响应分别显示状态，目录查询失败不阻止手填模型进行响应测试。

文本／视觉模型预设于 2026-09-10 更新，补充 GPT-5.4 与轻量型号、Gemini 3 系列、Grok 4.5、GLM 的文本及视觉型号、千问与阿里云三方模型，以及硅基流动国际站的 Kimi K3、Qwen3.8、DeepSeek 视觉实验模型等。国内与国际目录分别维护，不共用密钥；预设仅提供候选，不代表账户权限或响应测试通过。已保存的模型与手填 ID 保留，Ollama 及订阅 Agent 的文本目录仍按本机安装／登录账户获取。

火山引擎也提供大语言模型与视觉模型设置，使用火山方舟北京 `/api/v3/chat/completions` 接口。两类均预设官方确认完整 ID 的 Seed 2.0 Lite、Pro、Mini，大语言模型另提供 Seed 2.0 Code；支持手填已开通的模型或接入点 ID、独立命名账户和“测试响应”。视觉理解输入图片并返回文字，与 Seedream 图像生成/编辑分开配置。候选目录过滤掉 Seedream、Seedance、SeedEdit 等生成模型；预设不代表当前账户已开通权限。

国内火山方舟与国际 BytePlus ModelArk 分开配置。四类设置（LLM、视觉理解、图像生成、图像编辑）均提供国内北京、国际亚太（柔佛）、国际欧洲（都柏林）独立供应商，密钥、模型与地址分别保存；已有 `volcengine` 配置继续对应国内北京。国际站使用 `ark.ap-southeast.bytepluses.com` 或 `ark.eu-west.bytepluses.com`，模型 ID 按国际文档独立维护，不从国内 `doubao-` 名称推导；欧洲区只预设已确认支持的 Seed 2.0 和 Seedream 5.0 Lite，具体版本权限以账户为准。可手填本区域已开通的模型或接入点 ID。[官方区域及隔离说明](https://docs.byteplus.com/api/docs/modelark/2191806)。

模型设置也支持订阅 Agent：Codex（ChatGPT）、Claude、xAI（Grok）、GitHub Copilot。选择对应 Agent 后点击“浏览器登录”，在默认浏览器中完成账户授权；多账户可通过“添加供应商”创建独立命名的 Agent 配置。大语言模型和视觉模型支持这四类 Agent，模型目录按登录账户读取；Copilot 按目录中的接口及视觉能力选择调用方式。翻译选择“大语言模型翻译”即可复用当前 Agent。

画布节点库与快捷添加窗口统一分为“素材输入、RH App、API 节点、处理与输出”。“API 节点”包含“大语言模型、视觉模型、图像生成、图像编辑”。创建时复制 API 管理当前类别的连接和模型配置，此后各节点的模型、提示词、超时和搜索开关仅在画布节点内生效，不写回设置中心、API 管理或其他节点；切换连接保留本节点的提示词与兼容选项。可在节点的“模型设置”页切换连接或重新读取配置，密钥与 Agent 登录继续由 API 管理维护。大语言模型节点接收提示词并输出文本；视觉节点接收图像与提示词并输出文本；生成节点输出图像；编辑节点接收图像和提示词并输出图像。一组视觉／编辑输入处理一张图片，多张图像可通过连线的“全部匹配结果逐项运行”顺序处理，沿用单项复用与等长配对规则。无连接时使用内部值，已连接却没有可用结果时阻塞，不能回退到手填内容。

“预览／保存”节点提供默认关闭的“保存结果”开关，关闭时仅预览已有结果，即使填写了目录也不会自动复制。开启后使用自定义绝对路径；未填写时保存到当前输出目录的 `canvases/画布名称_画布标识/`，同名画布互不混用。运行时自动创建目录并保存输入文件副本或 UTF-8 文本，下游使用保存后的文件。“重名覆盖”默认关闭，重名时保存为 `文件名(1).后缀`、`文件名(2).后缀`；开启后先完整写入临时文件，再替换目标同名文件。开关与目录随画布 JSON 保存，手动“另存副本”也遵循覆盖选项。

重复过滤要求配置与实际输入未变化，并且结果内容、文件及下游所选的类型／序号仍然有效。只有远程 URL 而没有可用本地文件的媒体结果、部分恢复丢失的结果、被删除或修改的输出均不能复用；内置中间文件清理后也会重新计算。临时路径变化本身不触发云端重跑。

节点“其他设置”提供单一“忽略节点（旁路）”开关。忽略后不执行节点本身，也不创建 App 任务；自动将兼容的连线输入交给下游，保留顺序和文件引用，同类型多输入按端口顺序取第一个。没有兼容连线输入时下游停止，不使用旧结果或手填值。单节点运行只唤起旁路实际需要的上游；强制重跑也不会执行被忽略节点。设置随 JSON 保存、支持撤销，节点标注“已忽略”；节点右键菜单平铺展示忽略、重复过滤及设置入口，并支持批量操作。

图像、视频、音频输入节点可选择文件、选择文件夹、填写路径或拖入素材，所有入口按同一组格式规则过滤并去重。文件夹作为路径保存，运行时在后台读取当前层符合节点类型的文件，按文件名排序，不递归子文件夹；再次运行会重新检查目录变化。拖入空白画布的文件自动按类型分组，文件夹先选择媒体类型。打包导出时将文件夹展开为当前匹配素材，普通画布 JSON 仍保留文件夹引用。

单项图像、视频输入在节点内直接预览，编辑输入后显示当前值；视频只在后台读取首帧，按可见节点加载，使用两个解码工作线程和最多 64 个缩略图缓存，不为节点创建播放器。文本节点可直接编辑，支持补全、剪切／复制／粘贴、撤销／重做、会话文本回退／前进，以及翻译和扩写；与右侧设置共用同一份文本和编辑撤销记录。文本框有焦点时 Ctrl+Z/Y 只操作文本；点击节点标题或画布后操作画布，画布的结构撤销不回退文本内容。编辑器按可见节点创建，缩小时隐藏。文本输入的输出仅写入本轮运行目录，不导入输入素材目录；节点文本设置仍随画布 JSON 保存。API、App 和开启保存的预览／保存节点照常写入正式输出目录，另外保留供画布恢复使用的运行副本；重命名操作不修改源文件。

图像输入使用共用“遮罩 / 绘画”编辑器，画布图像节点、画布 App 节点的未连接图像参数及 App 输入卡片均可打开。窗口分为“遮罩绘制”和“直接绘制”两页，共用同一张原图，两种图层可同时生效。“遮罩绘制”提供遮罩笔、橡皮擦、连通区域油漆桶、颜色选区（HSL／LAB／RGB）；“直接绘制”提供 RGBA 绘画笔、橡皮擦及图层导入。当前页的清空与橡皮擦只影响对应图层。支持圆形／方形笔刷、颜色透明度、硬度、间距、笔触不透明度、图层可见性及黑／白／反相遮罩显示。所有图层同步旋转／镜像，支持撤销／重做，Ctrl + 滚轮缩放，空格／中键平移，Alt + 右键拖动调整笔刷大小或硬度。

多文件时先选择一项；文件夹输入可选其中一张，确认后展开为当前文件列表。确认编辑只保存图层草稿和方向设置，不创建图像文件，也不替换原始输入路径。画布图像节点执行或 App 提交时，遮罩保存到当前输入目录的 `masks/`，格式为独立二值 PNG（1 位黑白、无 Alpha，白色为选区）；直接绘制的透明图层保存到 `paintings/`，格式为 RGBA PNG。文件均按内容命名，未修改不重复写入。本轮临时目录同时保存 RGBA 图层副本、覆盖原图后的合成图和最终上传图；画布最近运行目录保留这些资产，复用到下一轮时一并迁移。输入节点运行后预览覆盖后的图像；快照保留图层路径及编辑设置，重新打开可继续编辑。遮罩、绘画和方向变更参与节点重复过滤，已发起任务使用原来的设置快照。

“直接绘制”支持导入 PNG、WebP、TIFF 等透明图像，也可导入普通 RGB 图像作为不透明 RGBA 图层；导入替换当前绘画层，可撤销。图像按 EXIF 方向读取，分辨率不同时自动缩放到当前编辑尺寸，使用预乘 Alpha 重采样以保留透明边缘；不按遮罩规则取反或二值化。窗口分别显示当前页的图层文件路径，可复制导入或运行后保存的路径。两种图层均不原地改写输入原图。

遮罩可导入 PNG、JPEG、WebP、BMP、TIFF、GIF 等 Pillow 支持的图像，默认透明图取反 Alpha，其余图像取亮度，也可指定 RGB 通道。按 EXIF 方向读取，尺寸不一致时使用最近邻缩放到输入图像尺寸，以 128 为阈值二值化。RH 单图输入采用 [ComfyUI 遮罩约定](https://github.com/Comfy-Org/ComfyUI/blob/master/nodes.py)：执行时额外生成 `alpha = 1 - mask` 的临时上传 PNG，RGB 使用绘画覆盖后的合成内容（无绘画时保持原图），云端工作流需使用加载图像节点的 MASK 输出；该上传图不属于永久保存的遮罩。编辑器支持最多 3200 万像素，撤销记录限制为 30 项／64 MiB。

没有本地路径的素材也可拖入：画布、图像／视频／音频输入列表、App 对应输入卡片支持位图、原始文件数据、HTTP(S) 地址和 HTML 图片／data 地址；输入框及预览也支持粘贴。先按接收节点类型检测实际内容和封装，图像经 Pillow 校验、媒体经 FFmpeg 从内存探测，满足支持格式后才保存到设置指定输入目录的 `imported/`，随后以本地路径回填并调用。原始文件数据保留原格式，位图保存为 PNG；不符合类型的文件不会先转格式后导入，也不会残留在输入目录。下载限制 32 MB、图像限制 3200 万像素，并限制导入并发；登录保护、浏览器内部 blob 地址或来源软件未提供文件内容时，提示复制文件内容或先保存到本地。

“处理与输出”分类提供“读取文件名”和“文件重命名”。文件名读取有三个模式：只读取文件名、只读取后缀、全部读取。例如 `photo.png` 分别输出 `photo`、`png`、`photo.png`，旧版“包含后缀”设置兼容读取。重命名的文件、文件名、后缀分别使用独立输入端口，未连接的名称／后缀使用节点内设置。名称或后缀留空均保持原值，连线输入为空也不修改。重命名在临时目录生成副本（文本写为 UTF-8 文件），输出副本路径供所有后续节点使用；原始输入、其他分支和 App 输出卡片的文件保持不变。同一文件可生成多个名称副本，同名批量项使用独立子目录，互不覆盖。拒绝路径分隔符等非法名称，修改后缀不做格式转换。快照可恢复临时副本引用；临时文件被清理后按缺失结果处理，不修改正式输出文件。

批量素材使用连线的“全部匹配结果逐项运行”；原有“第一项／指定序号”设置仍按选择生效。参照 [ComfyUI 列表逐项处理](https://docs.comfy.org/custom-nodes/backend/lists)，画布记录每项输入与生成结果的来源；有共同来源的多路输入优先按来源配对，无共同来源时等长按序配对、单项复用，缺失、歧义或无法对应的长度差异会阻止执行。不会通过补尾或笛卡尔积猜测配对。API 和 RH App 每组输入生成的多项结果保留所属来源，乱序完成后仍按输入组和返回顺序整理，来源关系随结果快照保存。

“预览／保存”只接收结果，不提供重命名端口或设置。需要沿用输入名称时，将文件夹输入分两路：一路进入 API／App，另一路读取文件名，再分别连接文件重命名节点的文件输入与文件名输入，最后接到预览／保存。来源配对支持 A 生成两张、B 生成三张时对应 A、A、B、B、B；临时副本保留各自名称，保存节点关闭覆盖时可保存为 `A.png`、`A(1).png`、`B.png`、`B(1).png`、`B(2).png`。旧版保存节点的名称连线在打开时迁移为独立重命名节点，不再使用旧配置快照。旧结果如果缺少来源关系，长度不一致时会提示调整或重新执行，不自动重复生成云端任务。

模型节点进入同一工作流队列，按依赖触发；普通 API 不占用 RH 专属应用重试队列，也不借用 RH 密钥。默认每轮重新请求，可开启“过滤重复运行”，支持强制重跑及单节点递归执行。运行快照固定参数，编辑用于下一轮。API 文本和图像结果自动保存到输出目录的 `canvases/画布名称_画布标识/api/节点类型_节点标识/类别/`，可读取文件名、预览、另存并继续传给下游 App；快照只保留文件引用。画布 JSON 不包含密钥与结果数据，导入连接的地址／协议须匹配本机连接才能使用本地凭据。

图像节点支持普通 OpenAI、Gemini、xAI、火山方舟、BytePlus、硅基流动、智谱生成、阿里云接口，以及 Codex／xAI Agent；自定义采用 OpenAI Images 格式。默认省略尺寸参数，使用供应商默认行为，比例可写入提示词。普通图像 API 不注入 Agent 专用提示词。停止会取消未开始节点与后续批次、立即结束本地等待并忽略迟到结果；这些普通模型请求没有统一的服务端取消接口，已送达的请求可能继续计费。模型 HTTP 并发最多 4 个，取消后尚未结束的请求仍占用此上限，不无限创建后台线程。网络失败不自动重新生成，重启也不自动重发模型请求。验证使用系统临时目录的本地模拟与真实 Qt，未进行付费云端出图实测。

Codex、xAI、Claude 的大语言模型／视觉配置默认开启“联网搜索”，可分别关闭，命名配置及副本独立保存。Agent 按问题决定是否检索；扩写、LLM 翻译和图像反推均传递发起时的搜索设置。搜索由供应商服务端执行，不接入本地命令、文件或浏览器工具；当前 Copilot 接口暂不启用搜索。Codex 使用实时 `web_search`，xAI 使用 Responses `web_search`，Claude 使用基础版 `web_search_20250305`（每次请求最多 5 次，避免引入代码执行）。Claude 的暂停响应保留完整工具上下文续接，最多续接 3 次，并共用原请求超时；断流、工具错误、拒绝访问不会自动降级或重新开始请求。返回文字保留官方来源链接，测试响应会请求一次联网检索，分别提示实际搜索完成或模型未调用搜索，并显示可点击来源。公开协议接入不代表订阅账户已获搜索权限，需在实际账户上测试；搜索可能消耗额外额度。实现依据：[Codex 工具定义](https://github.com/openai/codex/blob/main/codex-rs/tools/src/tool_spec.rs)、[xAI 搜索](https://docs.x.ai/developers/tools/web-search)、[Claude 搜索](https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-search-tool)。

图像生成和编辑 Agent **仅提供 Codex、xAI**，设置中只选择负责执行的 **LLM**，候选列表从登录账户获取，无需填写 `gpt-image-*` 或 `grok-imagine-*` 图像模型名称。请求发送到 Agent 的 Responses 接口，由所选 LLM 调用服务端 `image_generation` 工具；生成／编辑分别限制为 `action=generate`／`edit`，从已完成的 `image_generation_call` 中读取并校验图片。旧配置里的图像模型 ID 自动清空，需重新选择账户可用的 LLM；普通 API 图像模型配置不受影响。协议依据：[OpenAI 图像工具](https://developers.openai.com/api/docs/guides/tools-image-generation)、[xAI 图像工具](https://docs.x.ai/developers/tools/image-generation)。公开工具协议不保证订阅账户拥有该工具权限，需使用实际账户验证。

“测试图像生成/编辑”会实际发起一次 Agent 请求，并可能消耗 LLM 与图像工具额度；一次请求可产生多个工具结果。客户端编辑输入上限暂保留为 Codex 5 张、xAI 3 张；无有效输入图片时不会退回文生图。只有文字、工具失败、任务未完成或图片损坏均不会标记为出图成功，也不会切换至普通图像 API 重试。网络超时、断流或服务器错误不会自动重复提交，明确返回 401 时仅尝试刷新授权并重发一次。不运行外部 CLI，也不赋予模型本地命令或文件工具权限。

设置中心 → 提示词提供独立的“Agent 图像生成提示词”和“Agent 图像编辑提示词”，仅用于 Codex／xAI 图像 Agent，支持自动保存、恢复默认及留空使用默认。Agent 请求使用发起时的提示词快照，默认通过 Responses 的 `instructions` 传入；开启兼容模式后合并至 Agent 用户文本，Codex 保留必需的空 `instructions` 字段，xAI 不发送该字段。合并保留内容但没有系统角色优先级，不会先提交失败再自动重发。普通及自定义图像 API 不注入这两项提示词，也不自动拼接到用户输入；加载时清理上一版自动注入普通 API 配置的提示词字段。两类 Agent 提示词互相独立，不修改图像反推、扩写或 RH App 的输入。

图像供应商预设于 2026-09-10 按官方文档校对，独立维护于 `aetherloom_core/image_model_catalog.py`。OpenAI、Gemini、xAI、火山引擎、硅基流动及阿里云提供生成/编辑预设；智谱仅列入图像生成。火山引擎使用火山方舟北京接口，提供 Seedream 5.0 Lite、4.5、4.0，生成和编辑均走 `/api/v3/images/generations`，编辑以 JSON `image` 字段传入参考图；可手填已开通的模型 ID 或接入点 ID，并通过“添加供应商”建立独立账户配置。硅基流动两站分别维护目录，阿里云北京与新加坡保持独立配置；选择旧版万相时，默认地址会跟随模型切换至相应异步接口，手填网关地址保留。Gemini 使用原生 Interactions 图像接口。OpenAI、xAI、Gemini 和硅基流动支持只读“刷新模型”，按当前类别过滤目录；目录查询成功不等于已验证实际出图。被移出预设的旧配置仍保留并提示核对，不自动切换供应商。

Agent 授权由 AetherLoom 独立管理，保存在 `agent_accounts/sessions.json`，Windows 使用当前用户的 DPAPI 加密；不会写入 `settings.json`、`apikeys.json` 或画布文件，该目录也不参与发布和打包。退出账户会移除本客户端对应的授权。图像编辑当前提供配置、调用适配及响应测试，尚未增加独立的图像编辑页面。订阅接口参考实现及许可证见 `THIRD_PARTY_NOTICES.txt`。


This application aims to reduce the computational burden of deploying local models by leveraging APIs to access online models/workflows, while providing a user-friendly local interface that enriches AI painting capabilities and simplifies operations as much as possible.

Currently implemented features:

1. Generate a local UI by entering the Runninghub AI app URL; modify parameters and upload files to run workflows
2. Local view management
3. Local GRC decoding
4. Prompt auto-completion
5. Prompt translation and expansion
6. Image inference (batch supported)
7. RH model library: public model search, version selection, model-name copying, and persistent local favorites shared by App and canvas model pickers. Custom favorites provide shortcuts to existing private model names.


# v0.1.0 alpha版部分功能展示
# Some Features in v0.1.0 Alpha ver.


输入你的Runninghub网站apikey，然后添加Runninghub的任意AI应用网址（支持一键添加所有作者推荐应用，作者会保持更新，具体应用详见我的主页: [https://www.runninghub.cn/user-center/1911823721911500801/webapp?inviteCode=rh-v1380](https://www.runninghub.cn/user-center/1911823721911500801/webapp?inviteCode=rh-v1380)），自动生成对应的应用和节点卡片;

在应用内，自由调整节点卡后点击运行（可设置批次，并行数量取决于你的apikey类型），调用api自动上传文件并创建任务卡片，不断征询任务进度直到返回结果并展示在右侧输出预览里。输入提示词支持提示词自动补全（使用danbooru提示词库并更新到25年11月）。

支持多个应用同时运行，并将所有任务的进度实时展示在应用界面内。

Enter your Runninghub API key, then add any AI app URL from Runninghub (supports one-click addition of all author-recommended apps, which the author keeps updated. For the full app list, see my profile: [https://www.runninghub.ai/user-center/1911823721911500801/webapp?inviteCode=rh-v1380](https://www.runninghub.ai/user-center/1911823721911500801/webapp?inviteCode=rh-v1380)), and it automatically generates the corresponding apps and node cards.

Inside the app, import any required local files or freely adjust the node cards, then click Run (you can set batch size; the number of parallel runs depends on your API key type). It calls the API to upload your files and create task cards, then continuously polls the task progress until results are placed, and displays them in the output preview on the right panel. Support prompt auto-completion (using the danbooru tag library and updated to November 2025).

Running multiple applications simultaneously is possible, with the progress of all tasks displayed in real time within the application interface.

<img width="3154" height="1809" alt="image" src="https://github.com/user-attachments/assets/aa885bae-6b60-4c6f-93f6-29fb3377beb9" />



支持隐私保护，你可以将AI应用在线生成的视图加密后下载到本地再进行解码，防止线上个人隐私泄露；在应用界面右上角勾选本地解码可以在任务完成同时解码返回的文件，并展示解码后预览。

目前仅支持Grid Reversal Codec（GRC）编解码，在线编码工作流详见：[https://www.runninghub.cn/post/1970743440852066305/aiDetail/?inviteCode=rh-v1380](https://www.runninghub.cn/post/1970743440852066305/aiDetail/?inviteCode=rh-v1380).

Support privacy protection: you can encrypt views generated online by AI applications and download them to your local machine for decoding to prevent leakage of personal privacy online; Check the Local Decode option in the upper-right corner of the application interface to enable direct decoding of returned files upon task completion and displaying a decoded preview.

Currently only Grid Reversal Codec (GRC) encoding and decoding is supported. For the online encoding workflow, please refer to: [https://www.runninghub.ai/post/1970743440852066305/aiDetail/?inviteCode=rh-v1380](https://www.runninghub.ai/post/1970743440852066305/aiDetail/?inviteCode=rh-v1380).

<img width="3154" height="1809" alt="image" src="https://github.com/user-attachments/assets/22931105-aedf-44ea-8aed-bc4108797c01" />



支持本地视图管理，拥有丰富的筛选功能，以及XY图表比较功能（支持手动修改排版和添加XY标注，并且支持预览图同步缩放）

Support local view management with rich filtering capabilities and XY chart comparison (supports manual layout editing and XY annotations, plus synchronized preview zoom).

<img width="3154" height="1809" alt="image" src="https://github.com/user-attachments/assets/edc8d7c7-4d07-479b-9298-d206c178134e" />



填写作者Runninghub邀请码rh-v1380以支持作者，并可获得1000RH币。

Enter the author's Runninghub invitation code rh-v1380 to support the author and receive 1000 RH coins.



## 源码目录说明

建议使用 Python 3.10，在项目根目录执行 `python -m pip install -r requirements.txt` 安装依赖。
随后运行根目录 `Start-AetherLoom.cmd` 或 `python AetherLoom.py`。
启动脚本优先使用已激活的虚拟环境，然后依次查找项目内 `.venv`、项目上一级 `.aetherloom-venv`，最后使用 PATH 中的 `python`。也可执行 `.\Start-AetherLoom.cmd "E:\Python310\python.exe"` 指定解释器；脚本会自动定位项目目录，无需先切换工作目录。依赖必须安装在实际使用的解释器内。
切换解释器后需在同一解释器中安装依赖，例如在项目根目录执行 `"E:\Python310\python.exe" -m pip install -r requirements.txt`（PowerShell 中在命令前加 `&`）。启动失败时窗口会显示实际报错及对应安装命令；脚本调用可设置 `AETHERLOOM_NO_PAUSE=1` 禁止暂停。
运行模块位于 `aetherloom_core/`，供应商接口位于 `api_calls/`，打包脚本位于 `packaging_build/`。
任务记录独立保存在 `task_records/runninghub/`。应用任务从进入等候队列起就有自己的 JSON；成功返回 taskId 后才建立云端任务恢复索引和下载校验记录。重启仅恢复已生成结果的下载／本地处理重试，不继续普通等候、生成或尚未执行的工作流。该目录不随源码提交。
API 密钥和个人设置请在客户端内配置；这些文件、测试、文档归档及本地输入输出不随源码提交。

### 本地画布

侧边栏“画布”提供独立的应用工作流编辑页面。可以按分类添加已有 RH 应用、四类 API 节点、图像／视频／音频导入、文本、内容过滤、读取文件名、文件重命名和预览保存节点。双击空白处或按 Tab 搜索节点；从输入或输出端口拖线到空白处，可以添加并连接类型匹配的节点。

空白处左键拖动框选节点，Ctrl 点击切换选中，Shift 点击或框选追加选中，Ctrl+A 全选。节点右键菜单平铺展示操作；右键已选节点保留整个选区，可批量切换忽略、重复过滤、本地解码和结果保存等适用设置。多选时右侧显示批量设置，混合状态以半选表示，一次修改可整体撤销。运行多个选中节点会合并所需上游，共享上游只执行一次；节点内文本的 Ctrl+A/Z/Y 仍操作文本。

“画布 → 删除当前画布”会取消该画布的排队及运行任务，删除 JSON、快照和关联运行临时目录，包括尚未清理的上一轮目录。正在上传或被工作线程读取的临时文件在释放后清理；迟到任务不能重建已删除的运行目录。其他画布、输入素材和正式输出文件保留。

- App 节点随工作流保存官方应用地址。打开画布时会提示缺失的 App，可以一键添加；添加不会覆盖画布节点自己的参数。旧画布可以从站点和 App ID 补齐地址。
- 画布与 RH 主页共用连接设置，支持为国内站、国际站分别保存多把有序 API key，增删或调整顺序会同步到两个页面。`.cn` 与 `.ai` 密钥不通用，任务仅尝试对应站点的列表，缺少时不会借用另一站。密钥仅保存在本地 `apikeys.json`，不进入画布 JSON 或快照。
- 每次排队重试按顺序尝试各 key。官方明确返回容量或队列已满（415／421）时可以换下一把；全部明确拒绝则停止，仍有繁忙 key 则按现有队列规则等待下一次重试。只要返回 taskId，就固定该任务的 key；已排队、运行中或提交响应无法确认时不换 key 重复提交。重启查询也按原 key 的标识恢复，不依赖列表顺序。

- App 节点的参数、本地解码和运行次数独立保存，不会修改 App 页面或其他节点。未连接的输入使用节点内设置，连接后由上游结果覆盖，断开后恢复节点内的值。启用解码后，节点右侧显示“本地解码”标志；结果保存及 App 输出卡片共用现有任务流程。
- 右下角可以设置整图批次数（1～99）、一键运行或全部终止当前画布任务。每批按连线依赖执行完整流程，再推进下一批；上游结果下载及本地处理完成后，下游才进入执行或重试队列。独立分支可并行，同一 App 节点的运行次数与画布批次数独立。
- 流程尚未到达的节点保持普通边框；已激活的等待／重试节点显示黄色边框，运行及结果处理显示绿色，确认失败显示红色。App 节点右上角同步 App 输出卡片的圆形进度。失败节点及其后续分支停止，当前批次的独立分支可以完成，此轮不再推进后续批次。
- 连线默认取第一项类型匹配的结果，也可指定序号或处理全部匹配结果。多个批量输入按顺序配对，单项可复用；其他数量不一致时会提示调整。
- 仅 App 节点可设置“过滤重复运行”，默认关闭；内置节点始终自动复用有效的未变化结果。单节点运行与整图执行共用依赖和复用规则，递归检查上游，单节点操作只执行一批。多批次不会自动强制重跑：所有节点可复用且输入、参数、结果未变时不会新增 RH 任务；只有显式“强制重跑”忽略过滤设置。
- 普通画布保存及导出使用单个 JSON，保存节点设置、App 定义、连线、布局和整图批次数；不包含媒体导入列表、生成结果、执行状态或媒体文件本体。文本参数和 App 手填值属于节点设置，会保留。导出默认使用 `.aetherloom.json` 后缀，不包含 API 密钥和解码密码。
- 每次运行前自动保存画布 JSON 和对应的运行快照，并随任务更新。一张画布只保留一份最新快照，打开哪张画布就读取并展示哪张，不在启动时全量加载所有画布。关闭后仅沿用已有 taskId 恢复已生成结果的下载和处理；普通队列及未提交的下游不自动继续。再次点击运行会按当前设置发起新一轮任务。
- 画布 JSON 是配置依据。快照的版本、标识或配置无法与它对应时，自动清除该快照并从 JSON 初始化；删除画布 JSON 后，对应快照也会清理。外部导入的工作流不继承其他画布的运行状态。
- 快照中的文件结果只保存路径和元数据，不嵌入媒体或文本文件内容。恢复时逐项跳过无法读取的历史结果，不弹出错误；需要这些缺失结果且尚未提交的局部分支跳过本轮，其他分支继续。再次运行画布仍按当前节点设置执行。

工作流 JSON 和自动恢复快照保存在本地 `canvases/`，素材及结果仍引用原文件，不嵌入 JSON。画布使用 AetherLoom 自有文件格式，不直接导入 rhTV 或 ComfyUI 的画布文件。画布、素材、导出文件及临时任务记录均不随源码提交。

画布节点输出额外存入 `.canvas_cache/运行标识摘要/节点标识摘要/outputs/`，包含媒体副本、UTF-8 文本及结果引用清单。每张画布保留最近一次运行目录，无论成功、失败或中断，关闭与重启均保留；`latest.json` 只索引画布与目录，不全量读取画布快照。新一轮先将仍有效的可复用结果迁入新目录，包括单节点运行范围之外的结果，结束后才清理旧目录；上传／模型线程仍在读取时延迟到释放后清理。删除画布或丢弃不匹配的快照时同步清理关联目录。未关联画布的 App 上传临时目录继续在任务结束／关闭后清理，异常残留在启动时清理。正式输出、输入素材和输入目录 `masks/`、`imported/` 不参与删除。

快照的节点结果引用最近运行目录；图像／视频／音频输入仍以原输入路径为依据，遮罩保留独立二值文件及绘画设置。原输入缺失时不使用运行副本冒充原输入。遮罩窗口显示可复制的图像路径和遮罩路径，包括导入路径及运行后保存的输入目录路径；内容未变化时不重复写入遮罩文件。结果文件缺失或改变时取消该结果的复用，局部恢复失败不会阻止打开画布。文本结果在可见区域按需读取预览，不在快照内重复嵌入文件正文。

### 任务与本地解码

本地解码页采用独立素材栏、解码设置和原始／结果对比预览，可直接导入图片、视频并打开素材或结果目录。支持拖动分隔线调整预览空间，窄窗口自动纵向排列；处理日志默认折叠。GRC 网格和 SSTool 密码在开始时固定，处理中禁用重复启动，停止后明确显示取消状态。日志最多保留最近 1500 段，避免长时间使用积累大量显示内容。

App 页面及画布节点都在发起时固定本次任务的解码开关、方式、网格、密码和删除原图选项。之后调整页面或节点设置只影响新任务。图片／视频下载并校验后按任务配置解码，成功后才按本次选项删除原件；解码失败保留原件并提示。文本、音频等不支持解码的结果直接保留。

任务 JSON 位于 `task_records/runninghub/tasks/`：

- `applications/<run_id>.json`：本次输入参数、上传后实际 POST 正文及提交阶段、解码配置、taskId、状态、进度、结果引用和等候组／运行组信息。
- `workflows/<job_id>.json`：每次工作流任务的批次序号、执行范围、状态和实际 App 任务引用；只有节点被依赖流程激发时，才产生对应 App 任务。
- `batches/<group_id>.json`：同组工作流共用的不可变节点及输入定义，避免每个批次重复保存整张图。

JSON 不保存 API 密钥或明文解码密码。API 请求记录通过站点和密钥指纹关联凭据；Windows 下解码密码按任务单独使用系统 DPAPI 加密，放在 `.private/` 内，不进入任务参数查看或画布导出。输出卡右键“查看本次任务参数”可查看脱敏的发起参数、实际 POST 和关联关系；历史任务缺少解码信息时可只为该任务补齐。

任务状态由共享执行服务维护，队列和卡片使用相同任务标识及内存投影，不在绘制或翻页时逐个读写 JSON。正文采用后台合并及原子写入；实际提交前必须确认本任务请求已写入成功。普通任务文档随会话清理，关闭后仅保留下载重试及其关联文档；完成后不再作为重启恢复任务。关闭客户端不会向云端发送取消指令，要终止仍在云端运行的任务请使用取消按钮。
