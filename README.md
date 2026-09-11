<p align="center">
  <img src="icons/home_emblem.svg" width="112" alt="AetherLoom">
</p>

<h1 align="center">AetherLoom</h1>
<p align="center">云端 AI 应用 · 本地交互 · 节点画布</p>
<p align="center">
  <a href="https://github.com/Prominentai/AetherLoom/releases">下载发布版本</a> ·
  <a href="https://pan.quark.cn/s/f0a699748a8e">国内下载</a> ·
  <a href="https://www.bilibili.com/video/BV1bfByBNEcv">视频教程</a>
</p>

AetherLoom 是一款 Windows 桌面客户端，通过 API 调用云端模型与 ComfyUI 工作流，在本地编辑参数、组织画布、查看和管理结果，无需部署生成模型。

**当前版本：0.2** · Built with Python / PyQt5

## 主要功能

- **RunningHub 应用**：通过应用网址生成输入卡片，支持标准模型与 LLM、批量运行、任务队列、进度查询和结果下载。
- **节点画布**：连接已添加应用、API 模型与素材节点；支持独立参数、INT / FLOAT 输入、List / Batch、内容过滤、预览保存，以及画布 JSON 与最近运行恢复。
- **API 与 Agent**：配置大语言模型、视觉理解、图像生成和图像编辑，支持多个供应商与自定义连接。
- **提示词工具**：自动补全、翻译、扩写与文本历史。
- **素材与模型库**：本地文件浏览、媒体预览、图像对比；检索 RH 模型并管理本地收藏。
- **图像与解码**：输入图像遮罩、直接绘画，以及 GRC / SSTool 本地解码。

## 界面预览

![AetherLoom 界面预览](https://github.com/user-attachments/assets/806a0280-a4a0-4070-8af5-87e064eb566b)

## 开始使用

1. 下载并启动客户端，在“连接设置”填写对应站点的 RunningHub API Key，或在“API 管理”配置模型连接。
2. 添加 RH 应用、标准模型或 LLM，导入素材并调整参数；也可以在画布中连接节点。
3. 点击运行，在任务队列和输出区查看进度与结果。

RunningHub 中文站 `.cn` 与国际站 `.ai` 使用各自的密钥。配置与素材保存在本地，各画布节点的参数独立设置。

### 从源码运行

使用 **Windows + Python 3.10**，在项目目录执行：

```shell
python -m pip install -r requirements.txt
python AetherLoom.py
```

安装依赖后也可双击 `Start-AetherLoom.cmd`。指定其他 Python 时，将解释器完整路径作为脚本的第一个参数传入。

## 许可

[Apache-2.0](LICENSE) · [第三方组件说明](THIRD_PARTY_NOTICES.txt)
