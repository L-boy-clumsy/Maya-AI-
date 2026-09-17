# -*- coding: utf-8 -*-
"""
Maya AI Assistant 0.3.0 COMPLETE
Maya 2026 / Python 3 / PySide6

Natural language -> controlled JSON plan -> maya.cmds executor.
The plugin never eval/execs Python, MEL, shell, or arbitrary commands returned by the model.
"""

from __future__ import annotations

import base64
import json
import math
import mimetypes
import os
import re
import ssl
import traceback
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import maya.api.OpenMaya as om
import maya.cmds as cmds
import maya.mel as mel
import maya.OpenMayaUI as omui

from PySide6 import QtCore, QtWidgets
from shiboken6 import wrapInstance


PLUGIN_VERSION = "0.3.0"
COMMAND_NAME = "mayaAIAssistant"
WINDOW_OBJECT_NAME = "MayaAIAssistantWindow"

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
TEXT_EXTS = {
    ".txt", ".md", ".json", ".csv", ".xml", ".yaml", ".yml", ".py", ".mel",
    ".obj", ".mtl", ".ma", ".usda", ".ini", ".cfg", ".log", ".toml",
}
LOCAL_ASSET_EXTS = {
    ".fbx", ".abc", ".usd", ".usdc", ".usdz", ".mb", ".hdr", ".exr", ".tx",
}

MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_TEXT_CHARS_PER_FILE = 120_000
MAX_TOTAL_TEXT_CHARS = 350_000
MAX_SCENE_NODES = 120

ALLOWED_OPS = {
    "primitive",
    "soccer_ball",
    "transform",
    "duplicate",
    "rename",
    "delete",
    "group",
    "parent",
    "bevel",
    "smooth",
    "extrude_faces",
    "freeze_transform",
    "center_pivot",
    "material_lambert",
    "material_standard_surface",
    "combine",
    "select",
    "set_attr",
    "poly_boolean",
    "merge_vertices",
    "triangulate",
    "quadrangulate",
    "uv_project",
    "nurbs_primitive",
    "nurbs_curve",
    "loft",
    "revolve",
    "nurbs_extrude",
    "camera",
    "light",
    "skydome",
    "keyframe_transform",
    "keyframe_attr",
    "playback",
    "nparticle",
    "field",
    "connect_dynamic",
    "rigid_body",
    "render_settings",
    "render_current",
    "import_asset",
}

API_PRESET_CUSTOM = "自定义"

# 预设名 -> 填充值。三值语义：
#   键缺失 / None = 不改动该字段；"" = 清空该字段；非空字符串 = 填入该值。
# 读取时必须用 preset.get(key)（默认 None），不能用 preset.get(key, "")。
API_PRESETS = {
    API_PRESET_CUSTOM: {},
    "rightcode": {
        "api_type": "RightCode Responses",
        "base_url": "https://www.rightapi.ai/codex/v1",
        "model": "gpt-5.2",
    },
    "DeepSeek": {
        "api_type": "OpenAI-compatible Chat Completions",
        "base_url": "https://api.deepseek.com",
        "model": "",
    },
    "OpenAI": {
        "api_type": "OpenAI Responses",
        "base_url": "https://api.openai.com/v1",
    },
}

SYSTEM_PROMPT = r"""
You are a planning engine embedded inside Autodesk Maya 2026.
Return ONLY one JSON object. No markdown fences, no commentary before/after JSON.

The JSON must be:
{
  "summary": "short Chinese summary",
  "operations": [ ... ]
}

Never return Python, MEL, shell commands, code, or executable scripts. The host only accepts the controlled operations listed below.

GENERAL RULES
- Use exact Maya node names supplied in scene context when editing existing objects.
- When you create a node and later reference it, explicitly give it a deterministic "name".
- Never assume Maya auto-generated names such as pCube2.
- Prefer a small number of robust operations instead of fragile chains.
- If the requested operation can be expressed with a dedicated tool (for example soccer_ball), use that tool.
- Use numeric values, not expressions.
- Angles are degrees. Maya world Y is up.
- If the user asks to replace existing named nodes, delete them explicitly first. Otherwise do not overwrite or delete existing scene content.
- Do not invent file paths. Only use attachment/local asset paths listed in context.
- For materials/textures, use local attachment paths exactly as supplied.

AVAILABLE OPERATIONS

1) primitive
{"op":"primitive","primitive":"cube|sphere|cylinder|cone|torus|plane","name":"Node","params":{},"translate":[0,0,0],"rotate":[0,0,0],"scale":[1,1,1]}
Common params: cube width/height/depth; sphere radius/subdivisionsAxis/subdivisionsHeight; cylinder radius/height/subdivisionsAxis; cone radius/height/subdivisionsAxis; torus radius/sectionRadius; plane width/height/subdivisionsX/subdivisionsY.

2) soccer_ball
{"op":"soccer_ball","name":"SoccerBall","radius":1.0,"translate":[0,0,0],"surface_texture":true,"bump_strength":0.05}
Creates a truncated-icosahedron style ball with 12 pentagons + 20 hexagons and black/white materials.

3) transform
{"op":"transform","target":"Node","translate":[x,y,z],"rotate":[x,y,z],"scale":[x,y,z],"relative":false}
Any transform field may be omitted.

4) duplicate
{"op":"duplicate","target":"Node","name":"Copy","translate":[0,0,0],"rotate":[0,0,0],"scale":[1,1,1],"relative":false}

5) rename
{"op":"rename","target":"Old","name":"New"}

6) delete
{"op":"delete","targets":["A","B"]}

7) group
{"op":"group","targets":["A","B"],"name":"Group"}

8) parent
{"op":"parent","children":["A","B"],"parent":"Parent"}

9) bevel
{"op":"bevel","targets":["Mesh"],"fraction":0.1,"segments":2}

10) smooth
{"op":"smooth","targets":["Mesh"],"divisions":1}

11) extrude_faces
{"op":"extrude_faces","targets":["Mesh.f[0:3]"],"local_translate_z":0.2,"offset":0.0,"divisions":1}

12) freeze_transform
{"op":"freeze_transform","targets":["Node"]}

13) center_pivot
{"op":"center_pivot","targets":["Node"]}

14) material_lambert
{"op":"material_lambert","targets":["Mesh"],"name":"Mat","color":[0.8,0.8,0.8]}

15) material_standard_surface
{"op":"material_standard_surface","targets":["Mesh"],"name":"Mat","base_color":[0.5,0.5,0.5],"roughness":0.4,"metalness":0.0,"base_color_texture":"C:/.../albedo.jpg","roughness_texture":"...","metalness_texture":"...","normal_texture":"...","bump_strength":0.2}
Texture fields are optional and must exactly match local attachment paths from context.

16) combine
{"op":"combine","targets":["A","B"],"name":"Combined"}

17) select
{"op":"select","targets":["Node"],"replace":true}

18) set_attr
{"op":"set_attr","target":"Node.attribute","value":1.0}
Only numeric/bool/string values are supported.

19) poly_boolean
{"op":"poly_boolean","operation":"union|difference|intersection","targets":["A","B"],"name":"Result"}

20) merge_vertices
{"op":"merge_vertices","targets":["Mesh"],"distance":0.001}

21) triangulate
{"op":"triangulate","targets":["Mesh"]}

22) quadrangulate
{"op":"quadrangulate","targets":["Mesh"]}

23) uv_project
{"op":"uv_project","targets":["Mesh"],"projection":"automatic|planar|cylindrical|spherical"}

24) nurbs_primitive
{"op":"nurbs_primitive","primitive":"sphere|cylinder|cone|plane|circle","name":"NurbsObj","params":{},"translate":[0,0,0],"rotate":[0,0,0],"scale":[1,1,1]}

25) nurbs_curve
{"op":"nurbs_curve","name":"Curve","degree":3,"points":[[x,y,z],...]}

26) loft
{"op":"loft","curves":["Curve1","Curve2"],"name":"LoftSurface","degree":3,"close":false}

27) revolve
{"op":"revolve","curve":"Profile","name":"Revolved","axis":[0,1,0],"start_sweep":0,"end_sweep":360,"degree":3,"sections":16}

28) nurbs_extrude
{"op":"nurbs_extrude","profile":"ProfileCurve","path":"PathCurve","name":"ExtrudedSurface"}

29) camera
{"op":"camera","name":"RenderCam","translate":[0,4,10],"rotate":[-15,0,0],"focal_length":50}

30) light
{"op":"light","light_type":"directional|point|spot|area","name":"Key","translate":[0,5,5],"rotate":[0,0,0],"color":[1,1,1],"intensity":1.0,"exposure":0.0,"cone_angle":40,"penumbra":0}

31) skydome
{"op":"skydome","name":"SkyDome","texture":"C:/.../studio.hdr","intensity":1.0,"exposure":0.0,"rotate":[0,0,0]}

32) keyframe_transform
{"op":"keyframe_transform","target":"Node","frame":1,"translate":[0,0,0],"rotate":[0,0,0],"scale":[1,1,1],"tangent":"auto|linear|step"}

33) keyframe_attr
{"op":"keyframe_attr","target":"Node.attribute","frame":1,"value":0.0,"tangent":"auto|linear|step"}

34) playback
{"op":"playback","start":1,"end":120,"fps":24}

35) nparticle
{"op":"nparticle","name":"Sparks","position":[0,0,0],"emitter_type":"omni|directional","rate":100,"speed":2,"speed_random":0.5,"direction":[0,1,0],"lifespan":2.0,"radius":0.05}

36) field
{"op":"field","field_type":"gravity|turbulence|vortex","name":"Gravity","magnitude":9.8,"direction":[0,-1,0],"attenuation":0.0,"frequency":1.0}

37) connect_dynamic
{"op":"connect_dynamic","targets":["Sparks"],"fields":["Gravity"]}

38) rigid_body
{"op":"rigid_body","targets":["Ball"],"active":true,"mass":1.0,"bounciness":0.5,"friction":0.4}

39) render_settings
{"op":"render_settings","renderer":"arnold","width":1920,"height":1080,"aa_samples":5,"diffuse_samples":2,"specular_samples":2,"transmission_samples":2,"sss_samples":2,"volume_samples":2,"output_prefix":"renders/shot"}

40) render_current
{"op":"render_current","camera":"RenderCam"}

41) import_asset
{"op":"import_asset","path":"C:/exact/path/from/attachments.fbx","namespace":"asset","group_name":"ImportedAsset_GRP"}
Use only for local asset paths listed in context.

Return valid JSON only.
"""


class AssistantError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------


def _maya_main_window():
    ptr = omui.MQtUtil.mainWindow()
    if ptr is None:
        return None
    return wrapInstance(int(ptr), QtWidgets.QWidget)


def _as_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return float(default)


def _vec3(value: Any, default=(0.0, 0.0, 0.0)) -> Tuple[float, float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return tuple(float(x) for x in default)
    return (_as_float(value[0]), _as_float(value[1]), _as_float(value[2]))


def _color3(value: Any, default=(0.5, 0.5, 0.5)) -> Tuple[float, float, float]:
    v = _vec3(value, default)
    return tuple(max(0.0, min(1.0, x)) for x in v)


def _listify(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _unique(seq: Iterable[str]) -> List[str]:
    out = []
    seen = set()
    for item in seq:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _ensure_https_or_local(url: str) -> None:
    p = urllib.parse.urlparse(url)
    if not p.scheme or not p.netloc:
        raise AssistantError("Base URL 无效。")
    host = (p.hostname or "").lower()
    if p.scheme == "https":
        return
    if p.scheme == "http" and host in {"localhost", "127.0.0.1", "::1"}:
        return
    raise AssistantError("远程 API Base URL 必须使用 HTTPS；只有 localhost/127.0.0.1 可使用 HTTP。")


def _join_api_url(base_url: str, suffix: str) -> str:
    return base_url.rstrip("/") + "/" + suffix.lstrip("/")


def _text_from_responses_payload(data: Dict[str, Any]) -> str:
    """从 Responses 响应的完整 JSON 对象里取出正文文本。"""
    text = data.get("output_text")
    if text:
        return str(text)
    texts: List[str] = []
    for item in data.get("output", []) or []:
        for c in item.get("content", []) or []:
            if c.get("type") in {"output_text", "text"} and c.get("text"):
                texts.append(c["text"])
    return "\n".join(texts)


def _responses_text_from_raw(raw: str) -> str:
    """解析 Responses 端点的响应体。

    rightcode / codex 风格的端点按 SSE（text/event-stream）返回，普通端点返回完整 JSON，
    这里两种都支持：以 "{" 开头按 JSON 解析，否则按 SSE 逐行累积 output_text.delta。
    """
    if raw.lstrip().startswith("{"):
        try:
            return _text_from_responses_payload(json.loads(raw))
        except json.JSONDecodeError as exc:
            raise AssistantError("API 返回不是有效 JSON：%s" % raw[:1500]) from exc

    deltas: List[str] = []
    fallback = ""
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        chunk = line[len("data:"):].strip()
        if not chunk or chunk == "[DONE]":
            continue
        try:
            event = json.loads(chunk)
        except json.JSONDecodeError:
            continue  # SSE 流里可能有非 JSON 行，跳过而不中断
        kind = str(event.get("type", ""))
        if kind.endswith("output_text.delta"):
            piece = event.get("delta")
            if piece:
                deltas.append(piece)
        elif kind == "response.completed":
            fallback = _text_from_responses_payload(event.get("response") or {})
        elif kind in {"error", "response.failed"}:
            detail = event.get("error") or event.get("response") or event
            raise AssistantError("API 流式返回错误：%s" % json.dumps(detail, ensure_ascii=False)[:1500])

    text = "".join(deltas) or fallback
    if not text:
        raise AssistantError("无法从 Responses 响应中读取文本：%s" % raw[:1500])
    return text


def _extract_json_object(text: str) -> Dict[str, Any]:
    if not text or not text.strip():
        raise AssistantError("AI 返回内容为空。")
    s = text.strip()
    s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.I)
    s = re.sub(r"\s*```$", "", s)
    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass

    start = s.find("{")
    end = s.rfind("}")
    if start >= 0 and end > start:
        try:
            obj = json.loads(s[start:end + 1])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError as exc:
            raise AssistantError("AI 返回的 JSON 无法解析：%s" % exc) from exc
    raise AssistantError("AI 没有返回有效 JSON 对象。")


def _validate_plan(plan: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(plan, dict):
        raise AssistantError("计划必须是 JSON 对象。")
    ops = plan.get("operations")
    if not isinstance(ops, list):
        raise AssistantError("计划缺少 operations 数组。")
    if len(ops) > 250:
        raise AssistantError("计划操作数量过多（上限 250）。")
    for i, op in enumerate(ops):
        if not isinstance(op, dict):
            raise AssistantError("第 %d 个 operation 不是对象。" % (i + 1))
        name = op.get("op")
        if name not in ALLOWED_OPS:
            raise AssistantError("第 %d 个 operation 类型不允许：%r" % (i + 1, name))
        # Explicitly reject code-like payload keys even inside otherwise valid operations.
        forbidden = {"python", "mel", "shell", "powershell", "command", "script", "exec", "eval"}
        if forbidden.intersection({str(k).lower() for k in op.keys()}):
            raise AssistantError("计划包含不允许的代码/命令字段。")
    if "summary" not in plan:
        plan["summary"] = ""
    return plan


def _scene_context() -> str:
    selection = cmds.ls(selection=True, long=False) or []
    transforms = cmds.ls(type="transform", long=False) or []
    transforms = transforms[:MAX_SCENE_NODES]
    rows = []
    for node in transforms:
        try:
            t = cmds.xform(node, q=True, ws=True, t=True)
            r = cmds.xform(node, q=True, ws=True, ro=True)
            s = cmds.xform(node, q=True, r=True, s=True)
            shapes = cmds.listRelatives(node, shapes=True, noIntermediate=True, fullPath=False) or []
            stypes = [cmds.nodeType(x) for x in shapes]
            rows.append({
                "name": node,
                "shapeTypes": stypes,
                "translate": [round(float(x), 4) for x in t],
                "rotate": [round(float(x), 4) for x in r],
                "scale": [round(float(x), 4) for x in s],
            })
        except Exception:
            rows.append({"name": node})
    payload = {
        "currentFrame": cmds.currentTime(q=True),
        "selection": selection,
        "transforms": rows,
    }
    return json.dumps(payload, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------


@dataclass
class Attachment:
    path: str
    kind: str  # image, text, asset

    @property
    def name(self) -> str:
        return os.path.basename(self.path)


def classify_attachment(path: str) -> str:
    ext = Path(path).suffix.lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext in TEXT_EXTS:
        return "text"
    if ext in LOCAL_ASSET_EXTS:
        return "asset"
    # Unknown files are treated as local assets: name/path context only, never uploaded raw.
    return "asset"


def read_text_attachment(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read(MAX_TEXT_CHARS_PER_FILE)
    except OSError as exc:
        raise AssistantError("读取附件失败 %s：%s" % (path, exc)) from exc


def image_as_data_url(path: str) -> str:
    size = os.path.getsize(path)
    if size > MAX_IMAGE_BYTES:
        raise AssistantError("图片过大（单张上限 12MB）：%s" % os.path.basename(path))
    mime = mimetypes.guess_type(path)[0] or "image/jpeg"
    with open(path, "rb") as f:
        data = base64.b64encode(f.read()).decode("ascii")
    return "data:%s;base64,%s" % (mime, data)


def attachment_context(attachments: Sequence[Attachment]) -> Tuple[str, List[Attachment]]:
    chunks = []
    images = []
    total_chars = 0
    for att in attachments:
        if not os.path.exists(att.path):
            chunks.append("[MISSING] %s" % att.path)
            continue
        if att.kind == "image":
            images.append(att)
            chunks.append("[IMAGE] %s | local_path=%s" % (att.name, att.path))
        elif att.kind == "text":
            text = read_text_attachment(att.path)
            remaining = max(0, MAX_TOTAL_TEXT_CHARS - total_chars)
            if remaining <= 0:
                chunks.append("[TEXT omitted due to total limit] %s" % att.path)
                continue
            text = text[:remaining]
            total_chars += len(text)
            chunks.append("[TEXT FILE] %s\nLOCAL_PATH: %s\n---\n%s\n---" % (att.name, att.path, text))
        else:
            chunks.append("[LOCAL ASSET - contents not uploaded] %s | local_path=%s" % (att.name, att.path))
    return "\n\n".join(chunks), images


# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------


class AIClient:
    def __init__(self, api_type: str, base_url: str, model: str, api_key: str, timeout: int = 120):
        self.api_type = api_type
        self.base_url = base_url.strip()
        self.model = model.strip()
        self.api_key = api_key.strip()
        self.timeout = int(timeout)

    def _post_raw(self, url: str, payload: Dict[str, Any], accept: str = "application/json") -> str:
        _ensure_https_or_local(url)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": accept,
            "User-Agent": "MayaAIAssistant/%s" % PLUGIN_VERSION,
        }
        if self.api_key:
            headers["Authorization"] = "Bearer %s" % self.api_key
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        ctx = ssl.create_default_context()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=ctx) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            raise AssistantError("API HTTP %s：%s" % (exc.code, detail[:1500] or exc.reason)) from exc
        except urllib.error.URLError as exc:
            raise AssistantError("API 网络错误：%s" % exc.reason) from exc
        except Exception as exc:
            raise AssistantError("API 请求失败：%s" % exc) from exc

    def _post_json(self, url: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        raw = self._post_raw(url, payload)
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise AssistantError("API 返回不是有效 JSON：%s" % raw[:1500]) from exc

    def generate_plan(self, user_text: str, attachments: Sequence[Attachment],
                      scene_context: str) -> Dict[str, Any]:
        # scene_context 由调用方在主线程采集后传入。本方法在 QThread 里执行，
        # 而 Maya 命令只允许在主线程调用（见 GenerateWorker 的说明）。
        if not self.model:
            raise AssistantError("Model 不能为空。")
        context_text, images = attachment_context(attachments)
        user_payload = (
            "USER REQUEST:\n%s\n\n"
            "CURRENT MAYA SCENE CONTEXT:\n%s\n\n"
            "ATTACHMENTS / LOCAL ASSETS:\n%s"
        ) % (user_text.strip(), scene_context, context_text or "(none)")

        if self.api_type == "OpenAI Responses":
            return self._generate_responses(user_payload, images)
        if self.api_type == "RightCode Responses":
            return self._generate_rightcode_responses(user_payload, images)
        return self._generate_chat_completions(user_payload, images)

    def _generate_chat_completions(self, user_payload: str, images: Sequence[Attachment]) -> Dict[str, Any]:
        content: Any
        if images:
            parts: List[Dict[str, Any]] = [{"type": "text", "text": user_payload}]
            for att in images:
                parts.append({"type": "image_url", "image_url": {"url": image_as_data_url(att.path)}})
            content = parts
        else:
            content = user_payload

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": content},
            ],
            "temperature": 0.1,
        }
        data = self._post_json(_join_api_url(self.base_url, "chat/completions"), payload)
        try:
            text = data["choices"][0]["message"]["content"]
        except Exception as exc:
            raise AssistantError("无法从 Chat Completions 响应中读取内容：%s" % json.dumps(data, ensure_ascii=False)[:1600]) from exc
        return _validate_plan(_extract_json_object(text))

    def _generate_responses(self, user_payload: str, images: Sequence[Attachment]) -> Dict[str, Any]:
        content: List[Dict[str, Any]] = [{"type": "input_text", "text": user_payload}]
        for att in images:
            content.append({"type": "input_image", "image_url": image_as_data_url(att.path)})
        payload = {
            "model": self.model,
            "instructions": SYSTEM_PROMPT,
            "input": [{"role": "user", "content": content}],
        }
        data = self._post_json(_join_api_url(self.base_url, "responses"), payload)

        text = data.get("output_text")
        if not text:
            texts = []
            for item in data.get("output", []) or []:
                for c in item.get("content", []) or []:
                    if c.get("type") in {"output_text", "text"} and c.get("text"):
                        texts.append(c["text"])
            text = "\n".join(texts)
        if not text:
            raise AssistantError("无法从 Responses API 响应中读取文本：%s" % json.dumps(data, ensure_ascii=False)[:1600])
        return _validate_plan(_extract_json_object(text))

    def _generate_rightcode_responses(self, user_payload: str, images: Sequence[Attachment]) -> Dict[str, Any]:
        # rightcode / codex 端点与 OpenAI 原生格式的差异：input 项要带 type="message"，且按 SSE 流式返回。
        content: List[Dict[str, Any]] = [{"type": "input_text", "text": user_payload}]
        for att in images:
            content.append({"type": "input_image", "image_url": image_as_data_url(att.path)})
        payload = {
            "model": self.model,
            "instructions": SYSTEM_PROMPT,
            "input": [{"type": "message", "role": "user", "content": content}],
            "stream": True,
        }
        raw = self._post_raw(
            _join_api_url(self.base_url, "responses"),
            payload,
            accept="text/event-stream",
        )
        return _validate_plan(_extract_json_object(_responses_text_from_raw(raw)))


class GenerateWorker(QtCore.QObject):
    finished = QtCore.Signal(object)
    failed = QtCore.Signal(str)

    def __init__(self, client: AIClient, user_text: str, attachments: Sequence[Attachment],
                 scene_context: str):
        super().__init__()
        self.client = client
        self.user_text = user_text
        self.attachments = list(attachments)
        self.scene_context = scene_context

    @QtCore.Slot()
    def run(self):
        # 本方法运行在 QThread 里。Maya 只允许在主线程调用 cmds.*，
        # 从工作线程调用会被拒绝，并抛出与本意无关的误导性错误
        # （例如「必须为标志 "selection" 传递一个布尔参数」）。
        # 所以场景上下文已由主线程采集好，这里只做纯网络 / 文件工作。
        try:
            plan = self.client.generate_plan(self.user_text, self.attachments, self.scene_context)
            self.finished.emit(plan)
        except Exception as exc:
            self.failed.emit(str(exc))


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


class PlanExecutor:
    def __init__(self):
        self.name_map: Dict[str, str] = {}
        self.logs: List[str] = []

    def log(self, text: str):
        self.logs.append(str(text))

    def resolve(self, name: str, must_exist: bool = True) -> str:
        if not isinstance(name, str) or not name:
            raise AssistantError("目标节点名称为空。")
        mapped = self.name_map.get(name, name)
        if cmds.objExists(mapped):
            return mapped
        # Component target such as Mesh.f[0]
        if "." in mapped:
            base = mapped.split(".", 1)[0]
            base_mapped = self.name_map.get(base, base)
            candidate = base_mapped + "." + mapped.split(".", 1)[1]
            if cmds.objExists(base_mapped):
                return candidate
        if must_exist:
            raise AssistantError("Scene target does not exist: %s" % name)
        return mapped

    def resolve_targets(self, values: Any, must_exist: bool = True) -> List[str]:
        return [self.resolve(str(x), must_exist=must_exist) for x in _listify(values)]

    def remember(self, planned_name: Optional[str], actual_name: str):
        if planned_name:
            self.name_map[str(planned_name)] = str(actual_name)

    def execute(self, plan: Dict[str, Any]) -> List[str]:
        _validate_plan(plan)
        self.logs = []
        self.name_map = {}
        cmds.undoInfo(openChunk=True, chunkName="Maya AI Assistant")
        try:
            for idx, op in enumerate(plan.get("operations", []), 1):
                opname = op["op"]
                self.log("[%d] %s" % (idx, opname))
                method = getattr(self, "op_" + opname, None)
                if method is None:
                    raise AssistantError("未实现操作：%s" % opname)
                method(op)
            cmds.undoInfo(closeChunk=True)
            return list(self.logs)
        except Exception as exc:
            try:
                cmds.undoInfo(closeChunk=True)
            except Exception:
                pass
            try:
                cmds.undo()
            except Exception:
                pass
            raise AssistantError("%s" % exc) from exc

    # ---- common geometry helpers ----

    def _apply_transform(self, target: str, op: Dict[str, Any], default_relative: bool = False):
        relative = bool(op.get("relative", default_relative))
        if "translate" in op:
            cmds.xform(target, ws=not relative, r=relative, t=_vec3(op["translate"]))
        if "rotate" in op:
            cmds.xform(target, ws=not relative, r=relative, ro=_vec3(op["rotate"]))
        if "scale" in op:
            sc = _vec3(op["scale"], (1, 1, 1))
            if relative:
                cmds.scale(sc[0], sc[1], sc[2], target, relative=True)
            else:
                cmds.xform(target, r=False, s=sc)

    def _transform_of_shape(self, node: str) -> str:
        if cmds.nodeType(node) == "transform":
            return node
        parents = cmds.listRelatives(node, parent=True, fullPath=False) or []
        return parents[0] if parents else node

    # ---- operations ----

    def op_primitive(self, op):
        kind = str(op.get("primitive", "cube")).lower()
        name = str(op.get("name") or "AI_Primitive")
        p = op.get("params") or {}
        if kind == "cube":
            node = cmds.polyCube(
                name=name,
                width=_as_float(p.get("width"), 1),
                height=_as_float(p.get("height"), 1),
                depth=_as_float(p.get("depth"), 1),
            )[0]
        elif kind == "sphere":
            node = cmds.polySphere(
                name=name,
                radius=_as_float(p.get("radius"), 1),
                subdivisionsAxis=int(p.get("subdivisionsAxis", 20)),
                subdivisionsHeight=int(p.get("subdivisionsHeight", 20)),
            )[0]
        elif kind == "cylinder":
            node = cmds.polyCylinder(
                name=name,
                radius=_as_float(p.get("radius"), 1),
                height=_as_float(p.get("height"), 2),
                subdivisionsAxis=int(p.get("subdivisionsAxis", 20)),
            )[0]
        elif kind == "cone":
            node = cmds.polyCone(
                name=name,
                radius=_as_float(p.get("radius"), 1),
                height=_as_float(p.get("height"), 2),
                subdivisionsAxis=int(p.get("subdivisionsAxis", 20)),
            )[0]
        elif kind == "torus":
            node = cmds.polyTorus(
                name=name,
                radius=_as_float(p.get("radius"), 1),
                sectionRadius=_as_float(p.get("sectionRadius"), 0.25),
            )[0]
        elif kind == "plane":
            node = cmds.polyPlane(
                name=name,
                width=_as_float(p.get("width"), 1),
                height=_as_float(p.get("height"), 1),
                subdivisionsX=int(p.get("subdivisionsX", 1)),
                subdivisionsY=int(p.get("subdivisionsY", 1)),
            )[0]
        else:
            raise AssistantError("不支持的 primitive：%s" % kind)
        self.remember(name, node)
        self._apply_transform(node, op)
        self.log("  created %s" % node)

    def op_soccer_ball(self, op):
        name = str(op.get("name") or "SoccerBall")
        radius = max(0.001, _as_float(op.get("radius"), 1.0))
        node, pent_faces, hex_faces = self._create_truncated_icosahedron(name, radius)
        self.remember(name, node)
        self._apply_transform(node, op)

        black = self._create_standard_material(name + "_Black_MAT", (0.015, 0.015, 0.015), 0.45, 0.0)
        white = self._create_standard_material(name + "_White_MAT", (0.8, 0.8, 0.8), 0.5, 0.0)
        if pent_faces:
            cmds.sets(["%s.f[%d]" % (node, i) for i in pent_faces], e=True, forceElement=black[1])
        if hex_faces:
            cmds.sets(["%s.f[%d]" % (node, i) for i in hex_faces], e=True, forceElement=white[1])

        if bool(op.get("surface_texture", False)):
            strength = max(0.0, _as_float(op.get("bump_strength"), 0.05))
            for material, _sg in (black, white):
                try:
                    noise = cmds.shadingNode("noise", asTexture=True, name=material + "_LeatherNoise")
                    place = cmds.shadingNode("place2dTexture", asUtility=True, name=noise + "_place2d")
                    for attr in (
                        "coverage", "translateFrame", "rotateFrame", "mirrorU", "mirrorV", "stagger",
                        "wrapU", "wrapV", "repeatUV", "offset", "rotateUV", "noiseUV", "vertexUvOne",
                        "vertexUvTwo", "vertexUvThree", "vertexCameraOne",
                    ):
                        if cmds.attributeQuery(attr, node=place, exists=True) and cmds.attributeQuery(attr, node=noise, exists=True):
                            try:
                                cmds.connectAttr(place + "." + attr, noise + "." + attr, force=True)
                            except Exception:
                                pass
                    bump = cmds.shadingNode("bump2d", asUtility=True, name=material + "_Bump")
                    cmds.setAttr(bump + ".bumpDepth", strength)
                    cmds.connectAttr(noise + ".outAlpha", bump + ".bumpValue", force=True)
                    if cmds.attributeQuery("normalCamera", node=material, exists=True):
                        cmds.connectAttr(bump + ".outNormal", material + ".normalCamera", force=True)
                except Exception:
                    pass
        self.log("  created soccer ball %s" % node)

    def _create_truncated_icosahedron(self, name: str, radius: float):
        phi = (1.0 + math.sqrt(5.0)) / 2.0
        verts = [
            (-1, phi, 0), (1, phi, 0), (-1, -phi, 0), (1, -phi, 0),
            (0, -1, phi), (0, 1, phi), (0, -1, -phi), (0, 1, -phi),
            (phi, 0, -1), (phi, 0, 1), (-phi, 0, -1), (-phi, 0, 1),
        ]
        faces = [
            (0, 11, 5), (0, 5, 1), (0, 1, 7), (0, 7, 10), (0, 10, 11),
            (1, 5, 9), (5, 11, 4), (11, 10, 2), (10, 7, 6), (7, 1, 8),
            (3, 9, 4), (3, 4, 2), (3, 2, 6), (3, 6, 8), (3, 8, 9),
            (4, 9, 5), (2, 4, 11), (6, 2, 10), (8, 6, 7), (9, 8, 1),
        ]

        # One truncated vertex for each oriented original edge: 12 * 5 = 60.
        neighbors: Dict[int, set] = {i: set() for i in range(len(verts))}
        for a, b, c in faces:
            neighbors[a].update((b, c))
            neighbors[b].update((a, c))
            neighbors[c].update((a, b))

        oriented_index: Dict[Tuple[int, int], int] = {}
        out_verts: List[Tuple[float, float, float]] = []
        for a in range(len(verts)):
            for b in sorted(neighbors[a]):
                va, vb = verts[a], verts[b]
                p = ((2 * va[0] + vb[0]) / 3.0,
                     (2 * va[1] + vb[1]) / 3.0,
                     (2 * va[2] + vb[2]) / 3.0)
                oriented_index[(a, b)] = len(out_verts)
                out_verts.append(p)

        # Scale all new vertices to requested circumscribed radius.
        maxr = max(math.sqrt(x*x + y*y + z*z) for x, y, z in out_verts)
        scale = radius / maxr
        out_verts = [(x*scale, y*scale, z*scale) for x, y, z in out_verts]

        poly_faces: List[List[int]] = []
        pent_indices: List[int] = []
        hex_indices: List[int] = []

        # Pentagons: sort each original vertex's 5 neighbors around a local tangent plane.
        for a, va in enumerate(verts):
            n = om.MVector(*va).normal()
            ref = om.MVector(0, 1, 0)
            if abs(n * ref) > 0.9:
                ref = om.MVector(1, 0, 0)
            u = (ref ^ n).normal()
            v = (n ^ u).normal()
            angled = []
            for b in neighbors[a]:
                vb = om.MVector(*verts[b])
                d = (vb - om.MVector(*va)).normal()
                angle = math.atan2(d * v, d * u)
                angled.append((angle, b))
            ordered = [b for _, b in sorted(angled)]
            poly_faces.append([oriented_index[(a, b)] for b in ordered])
            pent_indices.append(len(poly_faces) - 1)

        # Hexagons from the original triangular faces.
        for a, b, c in faces:
            poly_faces.append([
                oriented_index[(a, b)], oriented_index[(b, a)],
                oriented_index[(b, c)], oriented_index[(c, b)],
                oriented_index[(c, a)], oriented_index[(a, c)],
            ])
            hex_indices.append(len(poly_faces) - 1)

        counts = [len(f) for f in poly_faces]
        connects = [i for f in poly_faces for i in f]
        points = [om.MPoint(*p) for p in out_verts]
        mesh_fn = om.MFnMesh()
        obj = mesh_fn.create(points, counts, connects)
        mesh_fn.setName(name + "Shape")
        shape_path = om.MDagPath.getAPathTo(obj)
        transform = shape_path.transform()
        transform_fn = om.MFnDagNode(transform)
        transform_name = transform_fn.setName(name)
        return transform_name, pent_indices, hex_indices

    def op_transform(self, op):
        target = self.resolve(str(op.get("target")))
        self._apply_transform(target, op)

    def op_duplicate(self, op):
        target = self.resolve(str(op.get("target")))
        planned = str(op.get("name") or (target + "_copy"))
        actual = cmds.duplicate(target, rr=True, name=planned)[0]
        self.remember(planned, actual)
        self._apply_transform(actual, op)
        self.log("  duplicated %s -> %s" % (target, actual))

    def op_rename(self, op):
        old_planned = str(op.get("target"))
        target = self.resolve(old_planned)
        new_planned = str(op.get("name"))
        actual = cmds.rename(target, new_planned)
        self.remember(old_planned, actual)
        self.remember(new_planned, actual)

    def op_delete(self, op):
        targets = self.resolve_targets(op.get("targets"))
        if targets:
            cmds.delete(targets)

    def op_group(self, op):
        targets = self.resolve_targets(op.get("targets"))
        name = str(op.get("name") or "AI_GRP")
        actual = cmds.group(targets, name=name) if targets else cmds.group(empty=True, name=name)
        self.remember(name, actual)

    def op_parent(self, op):
        children = self.resolve_targets(op.get("children"))
        parent = self.resolve(str(op.get("parent")))
        if children:
            cmds.parent(children, parent)

    def op_bevel(self, op):
        targets = self.resolve_targets(op.get("targets"))
        fraction = max(0.0, _as_float(op.get("fraction"), 0.1))
        segments = max(1, int(op.get("segments", 1)))
        for t in targets:
            cmds.polyBevel3(t, fraction=fraction, segments=segments, chamfer=1, autoFit=True)

    def op_smooth(self, op):
        targets = self.resolve_targets(op.get("targets"))
        divisions = max(1, int(op.get("divisions", 1)))
        for t in targets:
            cmds.polySmooth(t, divisions=divisions)

    def op_extrude_faces(self, op):
        targets = self.resolve_targets(op.get("targets"))
        ltz = _as_float(op.get("local_translate_z"), 0.0)
        offset = _as_float(op.get("offset"), 0.0)
        divisions = max(1, int(op.get("divisions", 1)))
        # Process components grouped by mesh to avoid Maya's multi-object component restriction.
        groups: Dict[str, List[str]] = {}
        for comp in targets:
            base = comp.split(".", 1)[0]
            groups.setdefault(base, []).append(comp)
        for _base, comps in groups.items():
            cmds.polyExtrudeFacet(comps, localTranslateZ=ltz, offset=offset, divisions=divisions)

    def op_freeze_transform(self, op):
        for t in self.resolve_targets(op.get("targets")):
            cmds.makeIdentity(t, apply=True, translate=True, rotate=True, scale=True, normal=False)

    def op_center_pivot(self, op):
        for t in self.resolve_targets(op.get("targets")):
            cmds.xform(t, centerPivots=True)

    def _create_lambert_material(self, name: str, color):
        mat = cmds.shadingNode("lambert", asShader=True, name=name)
        cmds.setAttr(mat + ".color", *color, type="double3")
        sg = cmds.sets(renderable=True, noSurfaceShader=True, empty=True, name=name + "SG")
        cmds.connectAttr(mat + ".outColor", sg + ".surfaceShader", force=True)
        return mat, sg

    def _create_standard_material(self, name: str, base_color, roughness: float, metalness: float):
        try:
            mat = cmds.shadingNode("standardSurface", asShader=True, name=name)
            if cmds.attributeQuery("baseColor", node=mat, exists=True):
                cmds.setAttr(mat + ".baseColor", *base_color, type="double3")
            if cmds.attributeQuery("specularRoughness", node=mat, exists=True):
                cmds.setAttr(mat + ".specularRoughness", max(0.0, min(1.0, roughness)))
            if cmds.attributeQuery("metalness", node=mat, exists=True):
                cmds.setAttr(mat + ".metalness", max(0.0, min(1.0, metalness)))
            sg = cmds.sets(renderable=True, noSurfaceShader=True, empty=True, name=name + "SG")
            cmds.connectAttr(mat + ".outColor", sg + ".surfaceShader", force=True)
            return mat, sg
        except Exception:
            return self._create_lambert_material(name, base_color)

    def _assign_sg(self, targets: Sequence[str], sg: str):
        if targets:
            cmds.sets(list(targets), e=True, forceElement=sg)

    def op_material_lambert(self, op):
        targets = self.resolve_targets(op.get("targets"))
        name = str(op.get("name") or "AI_Lambert")
        mat, sg = self._create_lambert_material(name, _color3(op.get("color"), (0.5, 0.5, 0.5)))
        self._assign_sg(targets, sg)
        self.remember(name, mat)

    def _connect_file_texture(self, path: str, material: str, material_attr: str, scalar: bool = False, raw: bool = False):
        if not path:
            return None
        if not os.path.exists(path):
            raise AssistantError("纹理文件不存在：%s" % path)
        file_node = cmds.shadingNode("file", asTexture=True, isColorManaged=True, name=material + "_" + material_attr + "_FILE")
        place = cmds.shadingNode("place2dTexture", asUtility=True, name=file_node + "_place2d")
        cmds.setAttr(file_node + ".fileTextureName", path, type="string")
        if raw:
            try:
                cmds.setAttr(file_node + ".colorSpace", "Raw", type="string")
            except Exception:
                pass
        attrs = [
            "coverage", "translateFrame", "rotateFrame", "mirrorU", "mirrorV", "stagger", "wrapU", "wrapV",
            "repeatUV", "offset", "rotateUV", "noiseUV", "vertexUvOne", "vertexUvTwo", "vertexUvThree", "vertexCameraOne",
        ]
        for attr in attrs:
            if cmds.attributeQuery(attr, node=place, exists=True) and cmds.attributeQuery(attr, node=file_node, exists=True):
                try:
                    cmds.connectAttr(place + "." + attr, file_node + "." + attr, force=True)
                except Exception:
                    pass
        if cmds.attributeQuery("outUV", node=place, exists=True) and cmds.attributeQuery("uvCoord", node=file_node, exists=True):
            try:
                cmds.connectAttr(place + ".outUV", file_node + ".uvCoord", force=True)
            except Exception:
                pass
        if cmds.attributeQuery("outUvFilterSize", node=place, exists=True) and cmds.attributeQuery("uvFilterSize", node=file_node, exists=True):
            try:
                cmds.connectAttr(place + ".outUvFilterSize", file_node + ".uvFilterSize", force=True)
            except Exception:
                pass
        src = file_node + (".outAlpha" if scalar else ".outColor")
        cmds.connectAttr(src, material + "." + material_attr, force=True)
        return file_node

    def op_material_standard_surface(self, op):
        targets = self.resolve_targets(op.get("targets"))
        name = str(op.get("name") or "AI_StandardSurface")
        base = _color3(op.get("base_color"), (0.5, 0.5, 0.5))
        rough = max(0.0, min(1.0, _as_float(op.get("roughness"), 0.4)))
        metal = max(0.0, min(1.0, _as_float(op.get("metalness"), 0.0)))
        mat, sg = self._create_standard_material(name, base, rough, metal)
        self._assign_sg(targets, sg)
        self.remember(name, mat)

        if cmds.nodeType(mat) != "standardSurface":
            return
        if op.get("base_color_texture"):
            self._connect_file_texture(str(op["base_color_texture"]), mat, "baseColor", scalar=False, raw=False)
        if op.get("roughness_texture"):
            self._connect_file_texture(str(op["roughness_texture"]), mat, "specularRoughness", scalar=True, raw=True)
        if op.get("metalness_texture"):
            self._connect_file_texture(str(op["metalness_texture"]), mat, "metalness", scalar=True, raw=True)
        normal = op.get("normal_texture")
        if normal:
            if not os.path.exists(str(normal)):
                raise AssistantError("Normal/Bump 纹理文件不存在：%s" % normal)
            file_node = cmds.shadingNode("file", asTexture=True, isColorManaged=True, name=mat + "_Normal_FILE")
            cmds.setAttr(file_node + ".fileTextureName", str(normal), type="string")
            try:
                cmds.setAttr(file_node + ".colorSpace", "Raw", type="string")
            except Exception:
                pass
            bump = cmds.shadingNode("bump2d", asUtility=True, name=mat + "_Bump")
            strength = _as_float(op.get("bump_strength"), 0.2)
            cmds.setAttr(bump + ".bumpDepth", strength)
            # tangent-space normal by default
            try:
                cmds.setAttr(bump + ".bumpInterp", 1)
            except Exception:
                pass
            cmds.connectAttr(file_node + ".outAlpha", bump + ".bumpValue", force=True)
            cmds.connectAttr(bump + ".outNormal", mat + ".normalCamera", force=True)

    def op_combine(self, op):
        targets = self.resolve_targets(op.get("targets"))
        if len(targets) < 2:
            raise AssistantError("Combine 至少需要两个对象。")
        name = str(op.get("name") or "CombinedMesh")
        actual = cmds.polyUnite(targets, ch=True, mergeUVSets=True, name=name)[0]
        self.remember(name, actual)

    def op_select(self, op):
        targets = self.resolve_targets(op.get("targets"), must_exist=True)
        if bool(op.get("replace", True)):
            cmds.select(targets, replace=True)
        else:
            cmds.select(targets, add=True)

    def op_set_attr(self, op):
        target = str(op.get("target"))
        if "." not in target:
            raise AssistantError("set_attr target 必须是 Node.attribute。")
        node, attr = target.split(".", 1)
        node = self.resolve(node)
        plug = node + "." + attr
        if not cmds.objExists(plug):
            raise AssistantError("属性不存在：%s" % plug)
        value = op.get("value")
        if isinstance(value, bool):
            cmds.setAttr(plug, value)
        elif isinstance(value, (int, float)):
            cmds.setAttr(plug, value)
        elif isinstance(value, str):
            cmds.setAttr(plug, value, type="string")
        elif isinstance(value, (list, tuple)) and len(value) == 3:
            cmds.setAttr(plug, *_vec3(value), type="double3")
        else:
            raise AssistantError("set_attr 只支持 bool/number/string/vec3。")

    def op_poly_boolean(self, op):
        targets = self.resolve_targets(op.get("targets"))
        if len(targets) < 2:
            raise AssistantError("Boolean 至少需要两个对象。")
        mode = str(op.get("operation", "union")).lower()
        code = {"union": 1, "difference": 2, "intersection": 3}.get(mode)
        if code is None:
            raise AssistantError("Boolean operation 无效：%s" % mode)
        name = str(op.get("name") or "BooleanResult")
        result = targets[0]
        for other in targets[1:]:
            result = cmds.polyCBoolOp(result, other, op=code, ch=True, name=name)[0]
        self.remember(name, result)

    def op_merge_vertices(self, op):
        distance = max(0.0, _as_float(op.get("distance"), 0.001))
        for t in self.resolve_targets(op.get("targets")):
            cmds.polyMergeVertex(t, distance=distance, alwaysMergeTwoVertices=False)

    def op_triangulate(self, op):
        for t in self.resolve_targets(op.get("targets")):
            cmds.polyTriangulate(t)

    def op_quadrangulate(self, op):
        for t in self.resolve_targets(op.get("targets")):
            cmds.polyQuad(t)

    def op_uv_project(self, op):
        mode = str(op.get("projection", "automatic")).lower()
        for t in self.resolve_targets(op.get("targets")):
            if mode == "automatic":
                cmds.polyAutoProjection(t, lm=0, pb=0, ibd=True, cm=False, l=2, sc=1, o=1, ps=0.2)
            elif mode == "planar":
                cmds.polyProjection(t, type="Planar", md="y")
            elif mode == "cylindrical":
                cmds.polyProjection(t, type="Cylindrical")
            elif mode == "spherical":
                cmds.polyProjection(t, type="Spherical")
            else:
                raise AssistantError("UV projection 无效：%s" % mode)

    def op_nurbs_primitive(self, op):
        kind = str(op.get("primitive", "sphere")).lower()
        name = str(op.get("name") or "AI_NURBS")
        p = op.get("params") or {}
        if kind == "sphere":
            node = cmds.sphere(name=name, radius=_as_float(p.get("radius"), 1.0), sections=int(p.get("sections", 8)), spans=int(p.get("spans", 4)))[0]
        elif kind == "cylinder":
            node = cmds.cylinder(name=name, radius=_as_float(p.get("radius"), 1), heightRatio=_as_float(p.get("heightRatio"), 2), sections=int(p.get("sections", 8)), spans=int(p.get("spans", 1)))[0]
        elif kind == "cone":
            node = cmds.cone(name=name, radius=_as_float(p.get("radius"), 1), heightRatio=_as_float(p.get("heightRatio"), 2), sections=int(p.get("sections", 8)), spans=int(p.get("spans", 1)))[0]
        elif kind == "plane":
            node = cmds.nurbsPlane(name=name, width=_as_float(p.get("width"), 1), lengthRatio=_as_float(p.get("lengthRatio"), 1), patchesU=int(p.get("patchesU", 1)), patchesV=int(p.get("patchesV", 1)))[0]
        elif kind == "circle":
            node = cmds.circle(name=name, radius=_as_float(p.get("radius"), 1), sections=int(p.get("sections", 8)), normal=_vec3(p.get("normal"), (0, 1, 0)))[0]
        else:
            raise AssistantError("不支持的 NURBS primitive：%s" % kind)
        self.remember(name, node)
        self._apply_transform(node, op)

    def op_nurbs_curve(self, op):
        points = op.get("points") or []
        if len(points) < 2:
            raise AssistantError("NURBS Curve 至少需要两个点。")
        degree = max(1, min(3, int(op.get("degree", 3))))
        degree = min(degree, len(points) - 1)
        name = str(op.get("name") or "AI_Curve")
        node = cmds.curve(name=name, degree=degree, point=[_vec3(p) for p in points])
        self.remember(name, node)

    def op_loft(self, op):
        curves = self.resolve_targets(op.get("curves"))
        if len(curves) < 2:
            raise AssistantError("Loft 至少需要两条曲线。")
        name = str(op.get("name") or "AI_Loft")
        result = cmds.loft(curves, name=name, degree=max(1, min(3, int(op.get("degree", 3)))), close=bool(op.get("close", False)), autoReverse=True, uniform=True, constructionHistory=True)[0]
        self.remember(name, result)

    def op_revolve(self, op):
        curve = self.resolve(str(op.get("curve")))
        name = str(op.get("name") or "AI_Revolve")
        result = cmds.revolve(
            curve,
            name=name,
            axis=_vec3(op.get("axis"), (0, 1, 0)),
            startSweep=_as_float(op.get("start_sweep"), 0),
            endSweep=_as_float(op.get("end_sweep"), 360),
            degree=max(1, min(3, int(op.get("degree", 3)))),
            sections=max(3, int(op.get("sections", 16))),
            constructionHistory=True,
        )[0]
        self.remember(name, result)

    def op_nurbs_extrude(self, op):
        profile = self.resolve(str(op.get("profile")))
        path = self.resolve(str(op.get("path")))
        name = str(op.get("name") or "AI_NurbsExtrude")
        result = cmds.extrude(profile, path, name=name, type=2, fixedPath=True, useComponentPivot=1, useProfileNormal=True, constructionHistory=True)[0]
        self.remember(name, result)

    def op_camera(self, op):
        name = str(op.get("name") or "RenderCam")
        transform, shape = cmds.camera(name=name)
        self.remember(name, transform)
        self._apply_transform(transform, op)
        if "focal_length" in op:
            cmds.setAttr(shape + ".focalLength", _as_float(op.get("focal_length"), 50))

    def op_light(self, op):
        kind = str(op.get("light_type", "area")).lower()
        name = str(op.get("name") or "AI_Light")
        if kind == "directional":
            shape = cmds.directionalLight(name=name + "Shape")
        elif kind == "point":
            shape = cmds.pointLight(name=name + "Shape")
        elif kind == "spot":
            shape = cmds.spotLight(name=name + "Shape")
        elif kind == "area":
            shape = cmds.shadingNode("areaLight", asLight=True, name=name + "Shape")
        else:
            raise AssistantError("不支持的灯光类型：%s" % kind)
        transform = self._transform_of_shape(shape)
        transform = cmds.rename(transform, name)
        self.remember(name, transform)
        self._apply_transform(transform, op)
        color = _color3(op.get("color"), (1, 1, 1))
        if cmds.attributeQuery("color", node=shape, exists=True):
            cmds.setAttr(shape + ".color", *color, type="double3")
        if cmds.attributeQuery("intensity", node=shape, exists=True):
            cmds.setAttr(shape + ".intensity", _as_float(op.get("intensity"), 1.0))
        if cmds.attributeQuery("aiExposure", node=shape, exists=True):
            cmds.setAttr(shape + ".aiExposure", _as_float(op.get("exposure"), 0.0))
        if kind == "spot":
            if "cone_angle" in op:
                cmds.setAttr(shape + ".coneAngle", _as_float(op.get("cone_angle"), 40))
            if "penumbra" in op:
                cmds.setAttr(shape + ".penumbraAngle", _as_float(op.get("penumbra"), 0))

    def _ensure_mtoa(self):
        if cmds.pluginInfo("mtoa", q=True, loaded=True):
            return
        try:
            cmds.loadPlugin("mtoa", quiet=True)
        except Exception as exc:
            raise AssistantError("Arnold/MtoA 未安装或无法加载：%s" % exc) from exc

    def op_skydome(self, op):
        self._ensure_mtoa()
        name = str(op.get("name") or "AI_SkyDome")
        shape = cmds.createNode("aiSkyDomeLight", name=name + "Shape")
        transform = self._transform_of_shape(shape)
        transform = cmds.rename(transform, name)
        self.remember(name, transform)
        self._apply_transform(transform, op)
        if cmds.attributeQuery("intensity", node=shape, exists=True):
            cmds.setAttr(shape + ".intensity", _as_float(op.get("intensity"), 1.0))
        if cmds.attributeQuery("aiExposure", node=shape, exists=True):
            cmds.setAttr(shape + ".aiExposure", _as_float(op.get("exposure"), 0.0))
        tex = op.get("texture")
        if tex:
            tex = str(tex)
            if not os.path.exists(tex):
                raise AssistantError("HDRI 文件不存在：%s" % tex)
            file_node = cmds.shadingNode("file", asTexture=True, isColorManaged=True, name=name + "_HDRI_FILE")
            cmds.setAttr(file_node + ".fileTextureName", tex, type="string")
            try:
                cmds.setAttr(file_node + ".colorSpace", "Raw", type="string")
            except Exception:
                pass
            cmds.connectAttr(file_node + ".outColor", shape + ".color", force=True)

    def _set_tangent(self, target: str, frame: float, tangent: str, attribute: Optional[str] = None):
        tangent = tangent.lower()
        if tangent not in {"auto", "linear", "step"}:
            tangent = "auto"
        kwargs = dict(time=(frame, frame), inTangentType=tangent, outTangentType=tangent)
        if attribute:
            kwargs["attribute"] = attribute
        try:
            cmds.keyTangent(target, **kwargs)
        except Exception:
            pass

    def op_keyframe_transform(self, op):
        target = self.resolve(str(op.get("target")))
        frame = _as_float(op.get("frame"), 1)
        tangent = str(op.get("tangent", "auto"))
        mapping = {
            "translate": ("translateX", "translateY", "translateZ"),
            "rotate": ("rotateX", "rotateY", "rotateZ"),
            "scale": ("scaleX", "scaleY", "scaleZ"),
        }
        for key, attrs in mapping.items():
            if key not in op:
                continue
            vals = _vec3(op[key], (1, 1, 1) if key == "scale" else (0, 0, 0))
            for attr, val in zip(attrs, vals):
                cmds.setKeyframe(target, attribute=attr, time=frame, value=val)
                self._set_tangent(target, frame, tangent, attr)

    def op_keyframe_attr(self, op):
        target = str(op.get("target"))
        if "." not in target:
            raise AssistantError("keyframe_attr target 必须是 Node.attribute。")
        node, attr = target.split(".", 1)
        node = self.resolve(node)
        plug = node + "." + attr
        if not cmds.objExists(plug):
            raise AssistantError("属性不存在：%s" % plug)
        frame = _as_float(op.get("frame"), 1)
        value = _as_float(op.get("value"), 0)
        cmds.setKeyframe(node, attribute=attr, time=frame, value=value)
        self._set_tangent(node, frame, str(op.get("tangent", "auto")), attr)

    def op_playback(self, op):
        start = _as_float(op.get("start"), 1)
        end = _as_float(op.get("end"), 120)
        if end < start:
            start, end = end, start
        cmds.playbackOptions(minTime=start, maxTime=end, animationStartTime=start, animationEndTime=end)
        fps = int(op.get("fps", 24))
        unit = {15: "game", 24: "film", 25: "pal", 30: "ntsc", 48: "show", 50: "palf", 60: "ntscf"}.get(fps)
        if unit:
            cmds.currentUnit(time=unit)

    def op_nparticle(self, op):
        name = str(op.get("name") or "AI_nParticle")
        position = _vec3(op.get("position"), (0, 0, 0))
        created = cmds.nParticle(position=[position], name=name)
        particle = created[0] if isinstance(created, (list, tuple)) else created
        self.remember(name, particle)
        shapes = cmds.listRelatives(particle, shapes=True, fullPath=False) or []
        shape = shapes[0] if shapes else particle
        if cmds.attributeQuery("lifespanMode", node=shape, exists=True):
            cmds.setAttr(shape + ".lifespanMode", 2)
        if cmds.attributeQuery("lifespan", node=shape, exists=True):
            cmds.setAttr(shape + ".lifespan", max(0.01, _as_float(op.get("lifespan"), 2.0)))
        if cmds.attributeQuery("radius", node=shape, exists=True):
            try:
                cmds.setAttr(shape + ".radius", max(0.001, _as_float(op.get("radius"), 0.05)))
            except Exception:
                pass

        emitter_type = str(op.get("emitter_type", "omni")).lower()
        rate = _as_float(op.get("rate"), 100)
        speed = _as_float(op.get("speed"), 2)
        random_speed = _as_float(op.get("speed_random"), 0)
        direction = _vec3(op.get("direction"), (0, 1, 0))
        if emitter_type == "directional":
            em = cmds.emitter(type="directional", rate=rate, speed=speed, speedRandom=random_speed,
                              directionX=direction[0], directionY=direction[1], directionZ=direction[2],
                              name=name + "_Emitter")[0]
        else:
            em = cmds.emitter(type="omni", rate=rate, speed=speed, speedRandom=random_speed, name=name + "_Emitter")[0]
        cmds.xform(em, ws=True, t=position)
        cmds.connectDynamic(particle, emitters=em)
        self.remember(name + "_Emitter", em)

    def op_field(self, op):
        kind = str(op.get("field_type", "gravity")).lower()
        name = str(op.get("name") or ("AI_" + kind.title()))
        mag = _as_float(op.get("magnitude"), 9.8)
        att = max(0.0, _as_float(op.get("attenuation"), 0.0))
        direction = _vec3(op.get("direction"), (0, -1, 0))
        if kind == "gravity":
            node = cmds.gravity(name=name, magnitude=mag, attenuation=att, direction=direction)[0]
        elif kind == "turbulence":
            node = cmds.turbulence(name=name, magnitude=mag, attenuation=att, frequency=_as_float(op.get("frequency"), 1.0))[0]
        elif kind == "vortex":
            node = cmds.vortex(name=name, magnitude=mag, attenuation=att, axis=direction)[0]
        else:
            raise AssistantError("不支持的 field：%s" % kind)
        self.remember(name, node)

    def op_connect_dynamic(self, op):
        targets = self.resolve_targets(op.get("targets"))
        fields = self.resolve_targets(op.get("fields"))
        for t in targets:
            for f in fields:
                cmds.connectDynamic(t, fields=f)

    def op_rigid_body(self, op):
        targets = self.resolve_targets(op.get("targets"))
        active = bool(op.get("active", True))
        mass = max(0.001, _as_float(op.get("mass"), 1.0))
        bounce = max(0.0, min(1.0, _as_float(op.get("bounciness"), 0.5)))
        friction = max(0.0, min(1.0, _as_float(op.get("friction"), 0.4)))
        try:
            cmds.rigidSolver(create=True)
        except Exception:
            pass
        for t in targets:
            result = cmds.rigidBody(t, active=active, mass=mass, bounciness=bounce, friction=friction)
            if result:
                self.log("  rigid body: %s" % result)

    def _safe_set(self, plug: str, value: Any, attr_type: Optional[str] = None):
        if cmds.objExists(plug):
            if attr_type:
                cmds.setAttr(plug, value, type=attr_type)
            else:
                cmds.setAttr(plug, value)

    def op_render_settings(self, op):
        renderer = str(op.get("renderer", "arnold")).lower()
        if renderer == "arnold":
            self._ensure_mtoa()
            self._safe_set("defaultRenderGlobals.currentRenderer", "arnold", "string")
        width = max(1, int(op.get("width", 1920)))
        height = max(1, int(op.get("height", 1080)))
        self._safe_set("defaultResolution.width", width)
        self._safe_set("defaultResolution.height", height)
        self._safe_set("defaultResolution.deviceAspectRatio", float(width) / float(height))
        if renderer == "arnold":
            mapping = {
                "aa_samples": "defaultArnoldRenderOptions.AASamples",
                "diffuse_samples": "defaultArnoldRenderOptions.GIDiffuseSamples",
                "specular_samples": "defaultArnoldRenderOptions.GISpecularSamples",
                "transmission_samples": "defaultArnoldRenderOptions.GITransmissionSamples",
                "sss_samples": "defaultArnoldRenderOptions.GISssSamples",
                "volume_samples": "defaultArnoldRenderOptions.GIVolumeSamples",
            }
            for key, plug in mapping.items():
                if key in op:
                    self._safe_set(plug, max(0, int(op[key])))
        if op.get("output_prefix"):
            self._safe_set("defaultRenderGlobals.imageFilePrefix", str(op["output_prefix"]), "string")

    def op_render_current(self, op):
        camera = self.resolve(str(op.get("camera"))) if op.get("camera") else None
        renderer = ""
        try:
            renderer = cmds.getAttr("defaultRenderGlobals.currentRenderer") or ""
        except Exception:
            pass
        if renderer == "arnold":
            self._ensure_mtoa()
            if hasattr(cmds, "arnoldRender"):
                kwargs = {}
                if camera:
                    shapes = cmds.listRelatives(camera, shapes=True, type="camera") or []
                    kwargs["cam"] = shapes[0] if shapes else camera
                cmds.arnoldRender(**kwargs)
                return
        if camera:
            shapes = cmds.listRelatives(camera, shapes=True, type="camera") or []
            cmds.render(camera=shapes[0] if shapes else camera)
        else:
            cmds.render()

    def op_import_asset(self, op):
        path = os.path.abspath(os.path.expanduser(str(op.get("path") or "")))
        if not path or not os.path.exists(path):
            raise AssistantError("本地资产文件不存在：%s" % path)
        ext = Path(path).suffix.lower()
        namespace = str(op.get("namespace") or "").strip()
        before = set(cmds.ls(assemblies=True) or [])

        if ext == ".abc":
            try:
                if not cmds.pluginInfo("AbcImport", q=True, loaded=True):
                    cmds.loadPlugin("AbcImport", quiet=True)
            except Exception:
                pass
            if hasattr(cmds, "AbcImport"):
                cmds.AbcImport(path, mode="import")
            else:
                mel.eval('AbcImport -mode import "%s"' % path.replace("\\", "/").replace('"', '\\"'))
        elif ext in {".usd", ".usda", ".usdc", ".usdz"}:
            # MayaUSD availability differs by installation. Try file import first.
            try:
                cmds.file(path, i=True, ignoreVersion=True, ra=True, mergeNamespacesOnClash=False,
                          namespace=namespace or ":", options=";", preserveReferences=True)
            except Exception as exc:
                raise AssistantError("USD 导入失败；请确认 MayaUSD 已安装/加载：%s" % exc) from exc
        else:
            kwargs = dict(i=True, ignoreVersion=True, ra=True, mergeNamespacesOnClash=False, options="v=0;", preserveReferences=True)
            if namespace:
                kwargs["namespace"] = namespace
            cmds.file(path, **kwargs)

        after = set(cmds.ls(assemblies=True) or [])
        new_roots = sorted(after - before)
        group_name = str(op.get("group_name") or "").strip()
        if group_name and new_roots:
            actual = cmds.group(new_roots, name=group_name)
            self.remember(group_name, actual)
        self.log("  imported asset: %s" % path)


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------


class MayaAIAssistantWindow(QtWidgets.QDialog):
    def __init__(self, parent=None):
        super().__init__(parent or _maya_main_window())
        self.setObjectName(WINDOW_OBJECT_NAME)
        self.setWindowTitle("Maya AI Assistant  %s" % PLUGIN_VERSION)
        self.resize(640, 860)
        self.setMinimumWidth(540)
        self.setAttribute(QtCore.Qt.WA_DeleteOnClose, True)

        self._settings = QtCore.QSettings("OpenAI", "MayaAIAssistant")
        self._plan: Optional[Dict[str, Any]] = None
        self._attachments: List[Attachment] = []
        self._thread: Optional[QtCore.QThread] = None
        self._worker: Optional[GenerateWorker] = None
        self.executor = PlanExecutor()

        self._build_ui()
        self._load_settings()

    def closeEvent(self, event):
        self._save_settings()
        super().closeEvent(event)

    def _build_ui(self):
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        api_group = QtWidgets.QGroupBox("API")
        form = QtWidgets.QFormLayout(api_group)
        self.api_preset = QtWidgets.QComboBox()
        self.api_preset.addItems(list(API_PRESETS.keys()))
        self.api_preset.setToolTip("选择预设会填充下方接口类型 / Base URL / Model；API Key 永不被改动。")
        self.api_type = QtWidgets.QComboBox()
        self.api_type.addItems(["OpenAI Responses", "OpenAI-compatible Chat Completions", "RightCode Responses"])
        self.base_url = QtWidgets.QLineEdit("https://api.openai.com/v1")
        self.model = QtWidgets.QLineEdit("gpt-5.6-terra")
        self.model.setPlaceholderText("例如 gpt-5.2 / deepseek-chat")
        self.api_key = QtWidgets.QLineEdit()
        self.api_key.setEchoMode(QtWidgets.QLineEdit.Password)
        self.show_key = QtWidgets.QCheckBox("显示 Key")
        self.show_key.toggled.connect(lambda checked: self.api_key.setEchoMode(QtWidgets.QLineEdit.Normal if checked else QtWidgets.QLineEdit.Password))
        form.addRow("配置预设", self.api_preset)
        form.addRow("接口类型", self.api_type)
        form.addRow("Base URL", self.base_url)
        form.addRow("Model", self.model)
        key_row = QtWidgets.QHBoxLayout()
        key_row.addWidget(self.api_key, 1)
        key_row.addWidget(self.show_key)
        form.addRow("API Key", key_row)
        root.addWidget(api_group)

        att_group = QtWidgets.QGroupBox("附件（图片 / 文本 / 本地资产）")
        att_layout = QtWidgets.QVBoxLayout(att_group)
        self.attachment_list = QtWidgets.QListWidget()
        self.attachment_list.setMaximumHeight(120)
        att_layout.addWidget(self.attachment_list)
        att_btns = QtWidgets.QHBoxLayout()
        self.add_image_btn = QtWidgets.QPushButton("添加图片")
        self.add_file_btn = QtWidgets.QPushButton("添加文件")
        self.remove_att_btn = QtWidgets.QPushButton("移除选中")
        self.clear_att_btn = QtWidgets.QPushButton("清空")
        for b in (self.add_image_btn, self.add_file_btn, self.remove_att_btn, self.clear_att_btn):
            att_btns.addWidget(b)
        att_layout.addLayout(att_btns)
        root.addWidget(att_group)

        root.addWidget(QtWidgets.QLabel("自然语言指令"))
        self.prompt = QtWidgets.QPlainTextEdit()
        self.prompt.setPlaceholderText("例如：根据参考图创建产品模型，自动UV，创建PBR材质，建立三点布光和Arnold渲染设置……")
        self.prompt.setMinimumHeight(145)
        root.addWidget(self.prompt)

        btn_row = QtWidgets.QHBoxLayout()
        self.generate_btn = QtWidgets.QPushButton("生成计划")
        self.execute_btn = QtWidgets.QPushButton("执行计划")
        self.undo_btn = QtWidgets.QPushButton("撤销")
        self.execute_btn.setEnabled(False)
        btn_row.addWidget(self.generate_btn)
        btn_row.addWidget(self.execute_btn)
        btn_row.addWidget(self.undo_btn)
        root.addLayout(btn_row)

        self.status = QtWidgets.QLabel("就绪")
        self.status.setWordWrap(True)
        root.addWidget(self.status)

        self.tabs = QtWidgets.QTabWidget()
        self.plan_view = QtWidgets.QPlainTextEdit()
        self.plan_view.setReadOnly(True)
        self.log_view = QtWidgets.QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.tabs.addTab(self.plan_view, "计划预览")
        self.tabs.addTab(self.log_view, "执行日志")
        root.addWidget(self.tabs, 1)

        note = QtWidgets.QLabel("安全模式：模型只生成受控 JSON 计划，不执行模型生成的 Python/MEL/Shell。执行失败时会尝试整批撤销。")
        note.setWordWrap(True)
        root.addWidget(note)

        self.add_image_btn.clicked.connect(self._add_images)
        self.add_file_btn.clicked.connect(self._add_files)
        self.remove_att_btn.clicked.connect(self._remove_selected_attachment)
        self.clear_att_btn.clicked.connect(self._clear_attachments)
        self.generate_btn.clicked.connect(self._generate_plan)
        self.execute_btn.clicked.connect(self._execute_plan)
        self.undo_btn.clicked.connect(self._undo)
        self.api_type.currentTextChanged.connect(self._api_type_changed)
        self.api_preset.currentTextChanged.connect(self._apply_preset)

    def _load_settings(self):
        api_type = self._settings.value("api_type", "OpenAI Responses")
        idx = self.api_type.findText(str(api_type))
        if idx >= 0:
            self.api_type.setCurrentIndex(idx)
        self.base_url.setText(str(self._settings.value("base_url", "https://api.openai.com/v1")))
        self.model.setText(str(self._settings.value("model", "gpt-5.6-terra")))
        env_key = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("OPENAI_API_KEY") or ""
        self.api_key.setText(env_key)

    def _save_settings(self):
        self._settings.setValue("api_type", self.api_type.currentText())
        self._settings.setValue("base_url", self.base_url.text().strip())
        self._settings.setValue("model", self.model.text().strip())
        # Deliberately do not persist the API key.

    def _api_type_changed(self, text: str):
        if text == "OpenAI Responses" and not self.base_url.text().strip():
            self.base_url.setText("https://api.openai.com/v1")

    def _apply_preset(self, name: str):
        preset = API_PRESETS.get(name)
        if not preset:
            return  # 「自定义」是空 dict，未知名字是 None，两者都天然 no-op
        # 阻断 api_type 的信号，使三个字段成为与写入顺序无关的纯赋值。不阻断时写入 api_type
        # 会触发 _api_type_changed 的空 URL 兜底；该兜底值目前恰好与 OpenAI 预设一致，
        # 因此暂无可见差异，阻断是为了不隐式依赖 _api_type_changed 的实现。
        with QtCore.QSignalBlocker(self.api_type):
            if preset.get("api_type") is not None:
                idx = self.api_type.findText(preset["api_type"])
                if idx >= 0:
                    self.api_type.setCurrentIndex(idx)
            if preset.get("base_url") is not None:
                self.base_url.setText(preset["base_url"])
            if preset.get("model") is not None:
                self.model.setText(preset["model"])

    def _add_attachment_paths(self, paths: Sequence[str]):
        existing = {a.path for a in self._attachments}
        for p in paths:
            p = os.path.abspath(p)
            if p in existing:
                continue
            kind = classify_attachment(p)
            att = Attachment(path=p, kind=kind)
            self._attachments.append(att)
            existing.add(p)
            label = {"image": "图片", "text": "文本", "asset": "本地资产"}.get(kind, kind)
            item = QtWidgets.QListWidgetItem("[%s] %s" % (label, p))
            item.setData(QtCore.Qt.UserRole, p)
            self.attachment_list.addItem(item)

    def _add_images(self):
        paths, _ = QtWidgets.QFileDialog.getOpenFileNames(self, "添加参考图片", "", "Images (*.png *.jpg *.jpeg *.gif *.webp)")
        self._add_attachment_paths(paths)

    def _add_files(self):
        paths, _ = QtWidgets.QFileDialog.getOpenFileNames(self, "添加文件/本地资产", "", "All Files (*.*)")
        self._add_attachment_paths(paths)

    def _remove_selected_attachment(self):
        rows = sorted({i.row() for i in self.attachment_list.selectedIndexes()}, reverse=True)
        for row in rows:
            if 0 <= row < len(self._attachments):
                self._attachments.pop(row)
            self.attachment_list.takeItem(row)

    def _clear_attachments(self):
        self._attachments = []
        self.attachment_list.clear()

    def _set_busy(self, busy: bool):
        self.generate_btn.setEnabled(not busy)
        self.execute_btn.setEnabled((not busy) and bool(self._plan))
        self.add_image_btn.setEnabled(not busy)
        self.add_file_btn.setEnabled(not busy)

    def _generate_plan(self):
        text = self.prompt.toPlainText().strip()
        if not text:
            self.status.setText("请输入自然语言指令。")
            return
        api_key = self.api_key.text().strip() or os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("OPENAI_API_KEY") or ""
        base_url = self.base_url.text().strip()
        model = self.model.text().strip()
        if not base_url:
            self.status.setText("Base URL 不能为空。")
            return
        try:
            _ensure_https_or_local(base_url)
        except Exception as exc:
            self.status.setText(str(exc))
            return

        # 场景上下文必须在主线程采集：Maya 只允许在主线程调用 cmds.*，
        # 放到下面的工作线程里会被拒绝并抛出误导性错误（详见 GenerateWorker.run）。
        try:
            scene_context = _scene_context()
        except Exception as exc:
            self.status.setText("读取场景上下文失败：%s" % exc)
            return

        self._save_settings()
        self._set_busy(True)
        self.status.setText("正在请求 AI 生成受控计划……")
        self.plan_view.clear()
        self.log_view.clear()
        self._plan = None

        client = AIClient(self.api_type.currentText(), base_url, model, api_key)
        thread = QtCore.QThread(self)
        worker = GenerateWorker(client, text, list(self._attachments), scene_context)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._on_plan_ready)
        worker.failed.connect(self._on_plan_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        self._thread = thread
        self._worker = worker
        thread.start()

    @QtCore.Slot(object)
    def _on_plan_ready(self, plan):
        self._plan = plan
        self.plan_view.setPlainText(json.dumps(plan, ensure_ascii=False, indent=2))
        self.status.setText("计划已生成，请检查后执行。")
        self._set_busy(False)

    @QtCore.Slot(str)
    def _on_plan_failed(self, error):
        self._plan = None
        self.status.setText("生成计划失败：%s" % error)
        self.log_view.setPlainText(error)
        self.tabs.setCurrentWidget(self.log_view)
        self._set_busy(False)

    def _execute_plan(self):
        if not self._plan:
            return
        try:
            logs = self.executor.execute(self._plan)
            self.log_view.setPlainText("\n".join(logs) or "执行完成。")
            self.tabs.setCurrentWidget(self.log_view)
            self.status.setText("执行完成。可使用 Maya Undo 撤销整批操作。")
        except Exception as exc:
            msg = "执行失败，已尝试回滚：%s" % exc
            self.status.setText(msg)
            self.log_view.setPlainText(msg + "\n\n" + traceback.format_exc())
            self.tabs.setCurrentWidget(self.log_view)

    def _undo(self):
        try:
            cmds.undo()
            self.status.setText("已执行 Maya Undo。")
        except Exception as exc:
            self.status.setText("Undo 失败：%s" % exc)


_WINDOW = None


def show_window():
    global _WINDOW
    try:
        if _WINDOW is not None:
            _WINDOW.close()
            _WINDOW.deleteLater()
    except Exception:
        pass
    _WINDOW = MayaAIAssistantWindow(parent=_maya_main_window())
    _WINDOW.show()
    _WINDOW.raise_()
    _WINDOW.activateWindow()
    return _WINDOW


# ---------------------------------------------------------------------------
# Maya plugin registration
# ---------------------------------------------------------------------------


class MayaAIAssistantCommand(om.MPxCommand):
    @staticmethod
    def creator():
        return MayaAIAssistantCommand()

    def doIt(self, args):
        show_window()


def maya_useNewAPI():
    pass


def initializePlugin(plugin_obj):
    plugin = om.MFnPlugin(plugin_obj, "OpenAI", PLUGIN_VERSION, "Any")
    plugin.registerCommand(COMMAND_NAME, MayaAIAssistantCommand.creator)


def uninitializePlugin(plugin_obj):
    global _WINDOW
    try:
        if _WINDOW is not None:
            _WINDOW.close()
            _WINDOW = None
    except Exception:
        pass
    plugin = om.MFnPlugin(plugin_obj)
    plugin.deregisterCommand(COMMAND_NAME)
