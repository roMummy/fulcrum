"""声明式配置项:字段模型、按小节分组、表单读取。

群功能、(之后的)插件配置、群级覆盖共用同一套字段模型和同一套渲染(templates/_config_card.html),
页面和校验都从字段声明派生,新增能力只需要声明字段,不需要写模板。

字段怎么存(群记录里的 extra_json、插件绑定的 config_json 加密密钥等)由各自的存储层负责,这里只管
"有哪些字段、页面上长什么样、表单怎么读回来"。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

# 表单里配置项的字段名前缀,避免和 enabled 等控制字段重名。
FORM_PREFIX = "cfg_"

# 字段类型:
#   text      单行文本            textarea  多行文本
#   number    整数或小数,可限定范围  bool      开关
#   select    下拉(需要 options)    tags      多个值(逗号分隔),保存为列表
#   url       网址                  time      时间 HH:MM
#   secret    密钥:加密保存,页面不回显,留空表示保持不变
FIELD_KINDS = ("text", "textarea", "number", "bool", "select", "tags", "url", "time", "secret")


@dataclass(frozen=True)
class ConfigField:
    """一个配置项:key 即存入配置的键名。"""

    key: str
    label: str
    # 取值见 FIELD_KINDS
    kind: str = "text"
    default: Any = ""
    hint: str = ""
    # number 类型的取值范围,超出会被钳制;其他类型忽略。
    minimum: int | float | None = None
    maximum: int | float | None = None
    # 页面上的小节标题;同一能力里 group 相同的配置项排在一起,留空归入未命名小节。
    group: str = ""
    # 依赖另一个 bool 配置项的 key:被依赖项关闭时,本项在页面上变淡(仍可编辑、仍会保存)。
    depends_on: str = ""
    # 以下几项供插件清单声明使用,群功能不用:
    # 英文标签(界面语言为英文时优先显示,缺省回退 label)、输入框占位提示、是否必填、
    # 下拉选项 (值, 显示名),select 类型必须提供。
    label_en: str = ""
    placeholder: str = ""
    required: bool = False
    options: tuple[tuple[str, str], ...] = ()


def build_sections(fields: Iterable[ConfigField], config: Mapping[str, Any], *, prefix: str = FORM_PREFIX) -> list[dict[str, Any]]:
    """把字段声明和当前值整理成页面渲染用的小节列表:同一小节的开关一列、输入框两列。

    config 里没有的键按字段默认值处理;密钥不回显,只告诉页面"是否已配置"。
    """

    sections: list[dict[str, Any]] = []
    for item in fields:
        if not sections or sections[-1]["title"] != item.group:
            sections.append({"title": item.group, "switches": [], "inputs": []})
        value = config[item.key] if item.key in config else item.default
        field = {
            "key": item.key,
            "name": prefix + item.key,
            "label": item.label,
            "kind": item.kind,
            "hint": item.hint,
            # 密钥不回显,只告诉页面是否已配置。
            "value": "" if item.kind == "secret" else value,
            "configured": bool(value) if item.kind == "secret" else False,
            "minimum": item.minimum,
            "maximum": item.maximum,
            "depends_on": prefix + item.depends_on if item.depends_on else "",
        }
        # 开关项和输入项分开摆:开关排成一列,输入框排成整齐的两列。
        sections[-1]["switches" if item.kind == "bool" else "inputs"].append(field)
    return sections


def has_inputs(sections: list[dict[str, Any]]) -> bool:
    """有文本/数字/密钥输入的设置才需要"保存"按钮;只有开关的设置点一下就已保存。"""

    return any(section["inputs"] for section in sections)


def read_form(fields: Iterable[ConfigField], form: Mapping[str, Any], *, prefix: str = FORM_PREFIX) -> dict[str, Any]:
    """按字段声明从表单里读出配置。

    复选框没勾选时浏览器不会提交该字段,所以 bool 项按"字段是否存在"判断;其余缺失的字段取默认值。
    """

    config: dict[str, Any] = {}
    for item in fields:
        name = prefix + item.key
        config[item.key] = (name in form) if item.kind == "bool" else form.get(name, item.default)
    return config
