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


This application aims to reduce the computational burden of deploying local models by leveraging APIs to access online models/workflows, while providing a user-friendly local interface that enriches AI painting capabilities and simplifies operations as much as possible.

Currently implemented features:

1. Generate a local UI by entering the Runninghub AI app URL; modify parameters and upload files to run workflows
2. Local view management
3. Local GRC decoding
4. Prompt auto-completion
5. Prompt translation and expansion
6. Image inference (batch supported)


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

侧边栏“画布”提供独立的应用工作流编辑页面。可以添加已有 RH 应用、图像／视频／音频导入、文本、结果选择和预览保存节点。双击空白处或按 Tab 搜索节点；从输入或输出端口拖线到空白处，可以添加并连接类型匹配的节点。

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

### 任务与本地解码

本地解码页采用独立素材栏、解码设置和原始／结果对比预览，可直接导入图片、视频并打开素材或结果目录。支持拖动分隔线调整预览空间，窄窗口自动纵向排列；处理日志默认折叠。GRC 网格和 SSTool 密码在开始时固定，处理中禁用重复启动，停止后明确显示取消状态。日志最多保留最近 1500 段，避免长时间使用积累大量显示内容。

App 页面及画布节点都在发起时固定本次任务的解码开关、方式、网格、密码和删除原图选项。之后调整页面或节点设置只影响新任务。图片／视频下载并校验后按任务配置解码，成功后才按本次选项删除原件；解码失败保留原件并提示。文本、音频等不支持解码的结果直接保留。

任务 JSON 位于 `task_records/runninghub/tasks/`：

- `applications/<run_id>.json`：本次输入参数、上传后实际 POST 正文及提交阶段、解码配置、taskId、状态、进度、结果引用和等候组／运行组信息。
- `workflows/<job_id>.json`：每次工作流任务的批次序号、执行范围、状态和实际 App 任务引用；只有节点被依赖流程激发时，才产生对应 App 任务。
- `batches/<group_id>.json`：同组工作流共用的不可变节点及输入定义，避免每个批次重复保存整张图。

JSON 不保存 API 密钥或明文解码密码。API 请求记录通过站点和密钥指纹关联凭据；Windows 下解码密码按任务单独使用系统 DPAPI 加密，放在 `.private/` 内，不进入任务参数查看或画布导出。输出卡右键“查看本次任务参数”可查看脱敏的发起参数、实际 POST 和关联关系；历史任务缺少解码信息时可只为该任务补齐。

任务状态由共享执行服务维护，队列和卡片使用相同任务标识及内存投影，不在绘制或翻页时逐个读写 JSON。正文采用后台合并及原子写入；实际提交前必须确认本任务请求已写入成功。普通任务文档随会话清理，关闭后仅保留下载重试及其关联文档；完成后不再作为重启恢复任务。关闭客户端不会向云端发送取消指令，要终止仍在云端运行的任务请使用取消按钮。
