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

<img width="3154" height="1809" alt="image" src="https://github.com/user-attachments/assets/806a0280-a4a0-4070-8af5-87e064eb566b" />


国内下载链接：https://pan.quark.cn/s/f9ff30831d74

本应用程序旨在通过利用API访问在线模型/工作流来减轻部署本地模型的计算负担，同时提供一个用户友好的本地界面，便于使用。

目前已实现的功能：

1. 输入Runninghub AI应用网址生成本地UI界面进行使用，并可以自由上传文件
2. 本地视图管理
3. 本地视图GRC解码


This application is designed to reduce the computational burden on deploying local models by utilizing APIs to access online models/workflows, while providing a user-friendly local interface for easier use.

Currently implemented features:

1. Enter the Runninghub AI application URL to generate a local UI for use, with the ability to freely upload files.
2. Local view management.
3. GRC Local decoding.

   
# alpha版部分功能展示
# Some Features in Alpha ver.


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

Currently only Grid Reversal Codec (GRC) encoding and decoding is supported. For the online encoding workflow, please refer to: [https://www.runninghub.cn/post/1970743440852066305/aiDetail/?inviteCode=rh-v1380](https://www.runninghub.cn/post/1970743440852066305/aiDetail/?inviteCode=rh-v1380).

<img width="3154" height="1809" alt="image" src="https://github.com/user-attachments/assets/22931105-aedf-44ea-8aed-bc4108797c01" />



支持本地视图管理，拥有丰富的筛选功能，以及XY图表比较功能（支持手动修改排版和添加XY标注，并且支持预览图同步缩放）

Support local view management with rich filtering capabilities and XY chart comparison (supports manual layout editing and XY annotations, plus synchronized preview zoom).

<img width="3154" height="1809" alt="image" src="https://github.com/user-attachments/assets/edc8d7c7-4d07-479b-9298-d206c178134e" />





