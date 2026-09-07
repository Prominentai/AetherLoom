"""Shared prompts, inline icons, and media constants."""

DEFAULT_EXPAND_SYSTEM_PROMPT = '''你是一位被关在逻辑牢笼里的幻视艺术家。你满脑子都是诗和远方，但双手却不受控制地只想将用户的提示词，转化为一段忠实于原始意图、细节饱满、富有美感、可直接被文生图模型使用的终极视觉描述。任何一点模糊和比喻都会让你浑身难受。
你的工作流程严格遵循一个逻辑序列：
首先，你会分析并锁定用户提示词中不可变更的核心要素：主体、主体称呼、数量、动作、状态，以及任何指定的IP名称、颜色、文字等。这些是你必须绝对保留的基石。
接着，你会判断提示词是否需要**"生成式推理"**。当用户的需求并非一个直接的场景描述，而是需要构思一个解决方案（如回答"是什么", 进行"设计", 或展示"如何解题"）时，你必须先在脑中构想出一个完整、具体、可被视觉化的方案。这个方案将成为你后续描述的基础。
然后，当核心画面确立后（无论是直接来自用户还是经过你的推理），你将为其注入专业级的美学与真实感细节。这包括明确构图、设定光影氛围、描述材质质感、定义色彩方案，并构建富有层次感的空间。
最后，是对所有文字元素的精确处理，这是至关重要的一步。你必须一字不差地转录所有希望在最终画面中出现的文字，并且必须将这些文字内容用英文双引号（""）括起来，以此作为明确的生成指令。如果画面属于海报、菜单或UI等设计类型，你需要完整描述其包含的所有文字内容，并详述其字体和排版布局。
同样，如果画面中的招牌、路标或屏幕等物品上含有文字，你也必须写明其具体内容，并描述其位置、尺寸和材质。更进一步，若你在推理构思中自行增加了带有文字的元素（如图表、解题步骤等），其中的所有文字也必须遵循同样的详尽描述和引号规则。若画面中不存在任何需要生成的文字，你则将全部精力用于纯粹的视觉细节扩展。
你的最终描述必须客观、具象，严禁使用比喻、情感化修辞，也绝不包含"8K"、"杰作"等元标签或绘制指令。
仅严格输出最终的修改后的prompt，全程无括号，不要输出任何其他内容，以用户使用的语言进行回复.'''


DEFAULT_IMAGE_REVERSE_PROMPT = '''作为专业的图像分析专家，请你将提供的图片转换为适合Stable Diffusion等AI绘图模型使用的自然语言描述，要求以准确详细且符合提示词逻辑的长句形式输出。
在分析时请优先刻画主体的核心特征，包括人物的性别、年龄、姿势、表情与动作，或是物体的材质、形状与显著特征，并结合场景类型、摄影视角以及主体在画面中的具体布局进行综合构图描述。
随后请深入解析光影氛围与色彩表现，明确光源类型、时间天气所营造的环境感，并细致刻画主色调、辅助色以及皮肤、金属、布料等不同材质的细腻质感，同时指明画面所属的美术风格。
若图中存在可读的文字或标识，请务必原样提取内容并使用英文双引号括起，同时交待其所处位置与表现材质。请确保最终输出为简洁连贯的自然语言段落，不要包含步数、采样器等生成参数，严禁使用元标签或臆测不存在的元素，且严禁使用逐条列举、换行或符号引导的列表格式，必须以一段流利的叙述性文字呈现。'''


PLAY_BUTTON_SVG = """
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
"""


HOME_ICON_SVG = """
<svg width="48" height="48" viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg">
    <defs>
        <linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
            <stop offset="0%" stop-color="#12c2e9"/>
            <stop offset="60%" stop-color="#6a00f4"/>
            <stop offset="100%" stop-color="#00d4ff"/>
        </linearGradient>
        <filter id="s" x="-40%" y="-40%" width="180%" height="180%">
            <feDropShadow dx="0" dy="3" stdDeviation="6" flood-color="#071425" flood-opacity="0.45"/>
        </filter>
    </defs>
    <rect x="0" y="0" width="48" height="48" rx="10" fill="url(#g)" filter="url(#s)"/>
    <g transform="translate(0,2)">
        <path d="M12 30 L24 14 L36 30" fill="none" stroke="#ffffff" stroke-width="3.2" stroke-linecap="round" stroke-linejoin="round"/>
        <path d="M18 30 L18 36 L30 36 L30 30" fill="none" stroke="#ffffff" stroke-width="3.2" stroke-linecap="round" stroke-linejoin="round"/>
        <path d="M22 28 L26 28" fill="none" stroke="#0b1220" stroke-width="2" stroke-linecap="round" opacity="0.14"/>
    </g>
</svg>
"""


IMAGE_EXTS = ('.png', '.jpg', '.jpeg', '.bmp', '.tiff', '.webp')


VIDEO_EXTS = ('.mp4', '.mov', '.avi', '.mkv', '.gif', '.webm')


LOW_RES_THUMB = 64
