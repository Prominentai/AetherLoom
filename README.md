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

![home_emblem](https://github.com/user-attachments/assets/a95ff860-8e2c-4439-9151-75b7b3903343)


# AetherLoom - Cloud AI Apps / ComfyUI Workflows Local Interactive Interface and View Management
# 爱思络 —— 云端AI应用/comfyui工作流本地交互界面与视图管理


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

   

