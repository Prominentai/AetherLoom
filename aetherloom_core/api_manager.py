"""Catalog of API providers, endpoints, and common model names.

Used by the API 管理 page to populate provider/model dropdowns.
"""
from typing import Dict, List, Optional

# Full catalog remains available for saved settings and older integrations.
API_CATEGORIES = [
	{"key": "translator", "name": "翻译模型", "desc": "多语言机翻"},
	{"key": "llm", "name": "大语言模型", "desc": "通用对话/推理/翻译"},
	{"key": "vision", "name": "视觉模型", "desc": "图像理解/多模态"},
	{"key": "text2img", "name": "文生图模型", "desc": "文本生成图片"},
	{"key": "text2video", "name": "文生视频", "desc": "文本生成视频"},
	{"key": "image2video", "name": "图生视频", "desc": "图像生成视频"},
	{"key": "video_edit", "name": "视频编辑", "desc": "视频剪辑/编辑"},
	{"key": "image_edit", "name": "图像编辑模型", "desc": "修图/重绘/抠图"},
	{"key": "runninghub", "name": "RunningHub", "desc": "RunningHub 站点接口"},
]

# Only these capabilities have active configuration panels. Hidden category
# data is intentionally retained; changing the UI must not delete saved keys.
VISIBLE_API_CATEGORIES = [dict(next(item for item in API_CATEGORIES if item['key'] == key))
                          for key in ('vision', 'llm', 'translator')]
MODEL_CATALOG_UPDATED = '2026-09-07'

# Provider API key portal URLs (if available)
API_KEY_PORTAL_URLS: Dict[str, str] = {
	"baidu_translate": "https://api.fanyi.baidu.com/manage/developer",
	# Direct link to enable Cloud Translation API v2 (project selector required)
	"google_translate": "https://console.cloud.google.com/apis/api/translate.googleapis.com/overview",
	"openai": "https://platform.openai.com/account/api-keys",
	"gemini": "https://aistudio.google.com/app/apikey",
	"grok": "https://console.x.ai/",
	"claude": "https://console.anthropic.com/account/keys",
	"deepseek": "https://platform.deepseek.com/api_keys",
	"glm": "https://open.bigmodel.cn/usercenter/apikeys",
	"siliconflow_cn": "https://cloud.siliconflow.cn/me/account/ak",
	"siliconflow_com": "https://cloud.siliconflow.com/me/account/ak",
	"runninghub_cn": "https://www.runninghub.cn/enterprise-api/sharedApi",
	"runninghub_ai": "https://www.runninghub.ai/enterprise-api/sharedApi",
	"alibaba_bj": "https://bailian.console.alibabacloud.com/?tab=model#/api-key",
	"alibaba_sg": "https://modelstudio.console.alibabacloud.com/?tab=playground#/api-key",
}

# Provider model list or catalog URLs shown in the API 管理 UI
MODEL_LIST_URLS: Dict[str, str] = {
	"baidu_translate": "https://fanyi-api.baidu.com/product/113",
	"google_translate": "https://cloud.google.com/translate/docs/reference/rest/v2/translate#body.QUERY_PARAMETERS.model",
	"openai": "https://developers.openai.com/api/docs/models",
	"gemini": "https://ai.google.dev/gemini-api/docs/models",
	"grok": "https://docs.x.ai/developers/models",
	"claude": "https://platform.claude.com/docs/en/models/overview",
	"deepseek": "https://api-docs.deepseek.com/",
	"ollama": "https://ollama.com/search",
	"glm": "https://docs.bigmodel.cn/cn/guide/start/model-overview",
	"siliconflow_cn": "https://cloud.siliconflow.cn/me/models",
	"siliconflow_com": "https://cloud.siliconflow.com/me/models",
	"alibaba_bj": "https://www.alibabacloud.com/help/zh/model-studio/models",
	"alibaba_sg": "https://www.alibabacloud.com/help/en/model-studio/models",
}

# Provider base registry: shared attributes like endpoint are defined once here.
PROVIDERS: Dict[str, Dict[str, object]] = {
	# Use full Baidu translate path to reduce configuration errors
	"baidu_translate": {"name": "百度翻译", "endpoint": "https://fanyi-api.baidu.com/api/trans/vip/translate"},
	"google_translate": {"name": "谷歌翻译", "endpoint": "https://translation.googleapis.com/language/translate/v2"},
	"openai": {"name": "OpenAI", "endpoint": "https://api.openai.com/v1"},
	"gemini": {"name": "Google Gemini", "endpoint": "https://generativelanguage.googleapis.com/v1beta"},
	"grok": {"name": "xAI Grok", "endpoint": "https://api.x.ai/v1"},
	"claude": {"name": "Anthropic Claude", "endpoint": "https://api.anthropic.com/v1/messages"},
	"deepseek": {"name": "DeepSeek", "endpoint": "https://api.deepseek.com/chat/completions"},
	"ollama": {"name": "Ollama (local)", "endpoint": "http://localhost:11434/api/"},
	"glm": {"name": "智谱 GLM", "endpoint": "https://open.bigmodel.cn/api/paas/v4"},
	"siliconflow_cn": {"name": "硅基流动（中文）", "endpoint": "https://api.siliconflow.cn/v1"},
	"siliconflow_com": {"name": "硅基流动（国际）", "endpoint": "https://api.siliconflow.com/v1"},
	"runninghub_cn": {"name": "RunningHub 中文站", "endpoint": "https://api.runninghub.cn"},
	"runninghub_ai": {"name": "RunningHub 国际站", "endpoint": "https://api.runninghub.ai"},
    "alibaba_bj": {"name": "阿里云（北京）", "endpoint": "https://dashscope.aliyuncs.com/compatible-mode/v1" },
	"alibaba_sg": {"name": "阿里云（新加坡）", "endpoint": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1" },
}

# Providers like openai expose different category-specific base URLs.
PROVIDER_CATEGORY_ENDPOINTS: Dict[str, Dict[str, str]] = {
	"ollama": {
		"llm": "http://localhost:11434/v1/chat/completions",
		"vision": "http://localhost:11434/v1/chat/completions",
	},
	"openai": {
		"llm": "https://api.openai.com/v1/chat/completions",
		"vision": "https://api.openai.com/v1/chat/completions",
		"text2img": "https://api.openai.com/v1/images/generations",
		"image_edit": "https://api.openai.com/v1/images/edits",
		"text2video": "https://api.openai.com/v1/videos",
		"image2video": "https://api.openai.com/v1/videos",
		"video_edit": "https://api.openai.com/v1/videos",
	},
	"grok": {
        "llm": "https://api.x.ai/v1/chat/completions",
        "vision": "https://api.x.ai/v1/chat/completions",
        "text2img": "https://api.x.ai/v1/images/generations",
    },
	"gemini": {
		"llm": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
		"vision": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
		"text2img": "https://generativelanguage.googleapis.com/v1beta/openai/images/generations",
		"image_edit": "https://generativelanguage.googleapis.com/v1beta/openai/images/generations",
	},
	"glm": {
		"llm": "https://open.bigmodel.cn/api/paas/v4/chat/completions",
		"vision": "https://open.bigmodel.cn/api/paas/v4/chat/completions",
		"text2img": "https://open.bigmodel.cn/api/paas/v4/images/generations",
		"image_edit": "https://open.bigmodel.cn/api/paas/v4/images/generations",
	},
	"siliconflow_cn": {
		"llm": "https://api.siliconflow.cn/v1/chat/completions",
		"vision": "https://api.siliconflow.cn/v1/chat/completions",
		"text2img": "https://api.siliconflow.cn/v1/images/generations",
		"image_edit": "https://api.siliconflow.cn/v1/images/generations",
		"text2video": "https://api.siliconflow.cn/v1/video/submit",
		"image2video": "https://api.siliconflow.cn/v1/video/submit",
	},
	"siliconflow_com": {
		"llm": "https://api.siliconflow.com/v1/chat/completions",
		"vision": "https://api.siliconflow.com/v1/chat/completions",
		"text2img": "https://api.siliconflow.com/v1/images/generations",
		"image_edit": "https://api.siliconflow.com/v1/images/generations",
		"text2video": "https://api.siliconflow.com/v1/videos/submit",
		"image2video": "https://api.siliconflow.com/v1/videos/submit",
	},
	"alibaba_bj": {
		"llm": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
		"vision": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
		"text2img": "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation",
		"image_edit": "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation",
		"text2video": "https://dashscope.aliyuncs.com/api/v1/services/aigc/video-generation/video-synthesis",
		"image2video": "https://dashscope.aliyuncs.com/api/v1/services/aigc/video-generation/video-synthesis",
	},
	"alibaba_sg": {
		"llm": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/chat/completions",
		"vision": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/chat/completions",
		"text2img": "https://dashscope-intl.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation",
		"image_edit": "https://dashscope-intl.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation",
		"text2video": "https://dashscope-intl.aliyuncs.com/api/v1/services/aigc/video-generation/video-synthesis",
		"image2video": "https://dashscope-intl.aliyuncs.com/api/v1/services/aigc/video-generation/video-synthesis",
	},
}

# Practical presets verified against official sources, not an entitlement list.
# Saved/custom IDs remain valid inputs even when absent from these suggestions.
# Source and compatibility notes: verification/api-model-catalog-sources.json.
_OPENAI_CHAT_MODELS = (
    'gpt-6-astra', 'gpt-5.6-sol', 'gpt-5.6-terra', 'gpt-5.6-luna',
    'gpt-5.5', 'gpt-4.1', 'gpt-4.1-mini', 'gpt-4o-mini',
)
_GEMINI_CHAT_MODELS = (
    'gemini-3.8-flash', 'gemini-3.5-flash-lite', 'gemini-3.1-pro-preview',
    'gemini-2.5-flash', 'gemini-2.5-flash-lite', 'gemini-2.5-pro',
)
_CLAUDE_CHAT_MODELS = (
    'claude-fable-5-1', 'claude-opus-5', 'claude-sonnet-5', 'claude-haiku-4-5-20251001',
)
_QWEN_CHAT_MODELS = (
    'qwen3.8-max', 'qwen3.8-flash', 'qwen3.7-plus', 'qwen3.7-flash', 'qwen-plus', 'qwen-flash',
)
_QWEN_VISION_MODELS = (
    'qwen3.8-max', 'qwen3.8-flash', 'qwen3.7-plus', 'qwen3-vl-plus', 'qwen3-vl-flash',
)

# Category provider map only lists category-specific models (and optional name overrides).
CATEGORY_PROVIDERS: Dict[str, List[Dict[str, object]]] = {
	"translator": [
		{"key": "baidu_translate", "models": []},
		{"key": "google_translate", "models": ["nmt"]},
	],
    "llm": [
        {"key": "openai", "models": list(_OPENAI_CHAT_MODELS)},
        {"key": "gemini", "models": list(_GEMINI_CHAT_MODELS)},
        {"key": "ollama", "models": []},  # Discover installed models; never assume a model was pulled.
        {"key": "grok", "models": ["grok-4.6", "grok-4.3"]},
        {"key": "claude", "models": list(_CLAUDE_CHAT_MODELS)},
        {"key": "deepseek", "models": ["deepseek-v4-pro", "deepseek-v4-flash"]},
        {"key": "alibaba_bj", "models": list(_QWEN_CHAT_MODELS)},
        {"key": "alibaba_sg", "models": list(_QWEN_CHAT_MODELS)},
        {"key": "glm", "models": ["glm-5.3", "glm-5.3-flash", "glm-5.2", "glm-5.1", "glm-4.7", "glm-4.7-flash"]},
        {"key": "siliconflow_cn", "models": [
            "deepseek-ai/DeepSeek-V4-Pro", "deepseek-ai/DeepSeek-V4-Flash", "Pro/zai-org/GLM-5.2",
            "Pro/zai-org/GLM-5.1", "Pro/moonshotai/Kimi-K2.6", "Qwen/Qwen3.6-35B-A3B", "Qwen/Qwen3.6-27B",
        ]},
        {"key": "siliconflow_com", "models": [
            "deepseek-ai/DeepSeek-V4-Pro", "deepseek-ai/DeepSeek-V4-Flash", "zai-org/GLM-5.3",
            "zai-org/GLM-5.3-Flash", "moonshotai/Kimi-K2.7-Code", "Qwen/Qwen3.6-35B-A3B",
        ]},
    ],
    "vision": [
        {"key": "openai", "models": list(_OPENAI_CHAT_MODELS)},
        {"key": "glm", "models": ["glm-5.3-flash", "glm-5v-turbo", "glm-4.6v", "glm-4.6v-flash"]},
        {"key": "claude", "models": list(_CLAUDE_CHAT_MODELS)},
        {"key": "gemini", "models": list(_GEMINI_CHAT_MODELS)},
        {"key": "siliconflow_cn", "models": [
            "moonshotai/Kimi-K2.7-Code", "Pro/moonshotai/Kimi-K2.6",
            "Qwen/Qwen3.6-35B-A3B", "Qwen/Qwen3-VL-30B-A3B-Instruct",
        ]},
        {"key": "siliconflow_com", "models": [
            "zai-org/GLM-5.3-Flash", "moonshotai/Kimi-K2.7-Code",
            "Qwen/Qwen3.6-35B-A3B", "Qwen/Qwen3-VL-32B-Instruct",
        ]},
        {"key": "grok", "models": ["grok-4.6", "grok-4.3"]},
        {"key": "alibaba_bj", "models": list(_QWEN_VISION_MODELS)},
        {"key": "alibaba_sg", "models": list(_QWEN_VISION_MODELS)},
        {"key": "deepseek", "models": ["deepseek-v4-flash-vision-exp"]},
        {"key": "ollama", "models": []},  # Populate only installed models with vision capability.
    ],
	"text2img": [
		{"key": "siliconflow_cn", "models": ["Qwen/Qwen-Image", "Qwen/Qwen-Image-Edit", "Tongyi-MAI/Z-Image-Turbo", "black-forest-labs/FLUX.2-pro", "black-forest-labs/FLUX.2-flex", "black-forest-labs/FLUX-1.1-pro", "black-forest-labs/FLUX-1.1-pro-Ultra", "black-forest-labs/FLUX.1-dev"]},
		{"key": "siliconflow_com", "models": ["Qwen/Qwen-Image", "Qwen/Qwen-Image-Edit", "Tongyi-MAI/Z-Image-Turbo", "black-forest-labs/FLUX.2-pro", "black-forest-labs/FLUX.2-flex", "black-forest-labs/FLUX-1.1-pro", "black-forest-labs/FLUX-1.1-pro-Ultra", "black-forest-labs/FLUX.1-dev"]},
		{"key": "grok", "models": ["grok-2-image-1212"]},
		{"key": "glm", "models": ["cogview-4-250304", "cogview-3-flash"]},
		{"key": "alibaba_bj", "models": ["wan2.5-t2i-preview", "wan2.2-t2i-plus", "wan2.2-t2i-flash", "qwen-image-plus", "qwen-image"]},
		{"key": "alibaba_sg", "models": ["wan2.5-t2i-preview", "wan2.2-t2i-plus", "wan2.2-t2i-flash", "qwen-image-plus", "qwen-image"]},
	],
	"text2video": [
		{"key": "openai", "models": ["sora-2"]},
		{"key": "siliconflow_cn", "models": ["Wan-AI/Wan2.2-T2V-A14B"]},
		{"key": "siliconflow_com", "models": ["Wan-AI/Wan2.2-T2V-A14B"]},
		{"key": "alibaba_bj", "models": ["wan2.5-t2v-preview", "wan2.2-t2v-plus"]},
		{"key": "alibaba_sg", "models": ["wan2.5-t2v-preview", "wan2.2-t2v-plus"]},
	],
	"image2video": [
		{"key": "openai", "models": ["sora-2-pro", "sora-2"]},
		{"key": "siliconflow_cn", "models": ["Wan-AI/Wan2.2-I2V-A14B"]},
		{"key": "siliconflow_com", "models": ["Wan-AI/Wan2.2-I2V-A14B"]},
		{"key": "alibaba_bj", "models": ["wan2.5-i2v-preview", "wan2.2-i2v-plus", "wan2.2-i2v-flash", "wan2.1-kf2v-plus", "wan2.2-animate-move", "wan2.2-animate-mix"]},
		{"key": "alibaba_sg", "models": ["wan2.5-i2v-preview", "wan2.2-i2v-plus", "wan2.2-i2v-flash", "wan2.1-kf2v-plus", "wan2.2-animate-move", "wan2.2-animate-mix"]},
	],
	"video_edit": [
		{"key": "openai", "models": ["sora-2-pro", "sora-2"]},
		{"key": "alibaba_bj", "models": ["wanx2.1-vace-plus"]},
		{"key": "alibaba_sg", "models": ["wanx2.1-vace-plus"]},
	],
	"image_edit": [
		{"key": "glm", "models": ["cogview-3-plus", "glm-4v"]},
		{"key": "siliconflow_cn", "models": ["Qwen/Qwen-Image-Edit", "black-forest-labs/FLUX.1-Kontext-max", "black-forest-labs/FLUX.1-Kontext-pro", "black-forest-labs/FLUX.1-Kontext-dev"]},
		{"key": "siliconflow_com", "models": ["Qwen/Qwen-Image-Edit", "black-forest-labs/FLUX.1-Kontext-max", "black-forest-labs/FLUX.1-Kontext-pro", "black-forest-labs/FLUX.1-Kontext-dev"]},
        {"key": "alibaba_bj", "models": ["qwen-image-edit-plus", "qwen-image-edit", "wan2.5-i2i-preview", "wanx2.1-imageedit", "aitryon-plus", "aitryon-parsing-v1"]},
		{"key": "alibaba_sg", "models": ["qwen-image-edit-plus", "qwen-image-edit", "wan2.5-i2i-preview"]},
    ],
	"runninghub": [
		{"key": "runninghub_cn", "models": []},
		{"key": "runninghub_ai", "models": []},
	],
}


def get_categories() -> List[Dict[str, object]]:
	return [dict(category) for category in API_CATEGORIES]


def get_visible_api_categories() -> List[Dict[str, object]]:
	return [dict(category) for category in VISIBLE_API_CATEGORIES]


def _merge_provider(base_key: str, cat_entry: Dict[str, object], category: Optional[str] = None) -> Dict[str, object]:
	base = PROVIDERS.get(base_key, {})
	cat_overrides = PROVIDER_CATEGORY_ENDPOINTS.get(base_key, {})
	endpoint = cat_overrides.get(category) if category else None
	if not endpoint:
		endpoint = str(base.get("endpoint", ""))
	merged = {
		"key": base_key,
		"name": cat_entry.get("name", base.get("name", base_key)),
		"endpoint": endpoint,
		"models": list(cat_entry.get("models", [])),
	}
	return merged


def get_providers(category: str) -> List[Dict[str, object]]:
	providers = []
	for entry in CATEGORY_PROVIDERS.get(category, []):
		key = entry.get("key")
		if not key:
			continue
		providers.append(_merge_provider(str(key), entry, category))
	return providers


def find_provider(category: str, provider_key: str) -> Optional[Dict[str, object]]:
	for provider in get_providers(category):
		if provider.get("key") == provider_key:
			return provider
	return None


def get_default_endpoint(category: str, provider_key: str) -> str:
	cat_overrides = PROVIDER_CATEGORY_ENDPOINTS.get(provider_key, {})
	override = cat_overrides.get(category)
	if override:
		return override
	base = PROVIDERS.get(provider_key, {})
	if base:
		return str(base.get("endpoint", ""))
	entry = find_provider(category, provider_key)
	return str(entry.get("endpoint", "")) if entry else ""


def get_models_for_provider(category: str, provider_key: str) -> List[str]:
	entry = find_provider(category, provider_key)
	return list(entry.get("models", [])) if entry else []


def get_api_key_portal(provider_key: str) -> str:
	return API_KEY_PORTAL_URLS.get(provider_key, "")


def get_model_list_url(provider_key: str) -> str:
	return MODEL_LIST_URLS.get(provider_key, "")
