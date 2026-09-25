"""按任务角色选模型。

agent 只按**角色**要模型（"planner"、"sql_gen"），从不直接指定模型名。
这层间接是成本/准确率消融实验的前提：切换整套路由方案只需改一个配置文件，
同一套评测脚本就能跑出全强模型、全便宜模型、混合路由三种结果，agent 代码不动。
"""

from __future__ import annotations

import os
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from llm.base import LLMProvider, Usage

_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


@dataclass
class Router:
    config: dict[str, Any]
    _cache: dict[str, LLMProvider] = field(default_factory=dict, repr=False)
    _fallback_warned: set[str] = field(default_factory=set, repr=False)
    totals: Usage = field(default_factory=Usage)

    @classmethod
    def from_file(cls, path: str | Path = _CONFIG_DIR / "routing.yaml") -> Router:
        return cls(config=yaml.safe_load(Path(path).read_text(encoding="utf-8")))

    # -- 查找 -------------------------------------------------------------

    def for_role(self, role: str) -> LLMProvider:
        """返回 ``role`` 对应的 provider；该 provider 缺 key 时降级到默认角色。

        缺 key 只降级不中断：否则只有一把 key 的时候整个项目没法开发。
        但会发警告，因为降级后的结果和全 key 运行的结果不可比。
        """
        if role in self._cache:
            return self._cache[role]

        roles = self.config["roles"]
        spec = roles.get(role)
        if spec is None:
            raise KeyError(f"未知角色 {role!r}；已知角色：{sorted(roles)}")

        try:
            provider = self._build(spec)
        except Exception as exc:  # 缺 key、base_url 写错等
            default_role = self.config.get("default")
            if not default_role or role == default_role:
                raise
            if role not in self._fallback_warned:
                self._fallback_warned.add(role)
                warnings.warn(
                    f"角色 {role!r} -> {spec.get('provider')} 不可用（{exc}），"
                    f"降级到 {default_role!r}。本次结果与全 key 运行**不可比**。",
                    stacklevel=2,
                )
            provider = self.for_role(default_role)

        self._cache[role] = provider
        return provider

    def max_tokens_for(self, role: str, default: int = 8192) -> int:
        """该角色的输出预算。

        推理模型把思考也算进输出预算，预算给少了会出现
        "思考把额度烧光、一个字没吐"的空回复——那不是模型不会，是配置错了。
        """
        return int(self.config["roles"].get(role, {}).get("max_tokens", default))

    def escalation_for(self, role: str) -> tuple[int, str] | None:
        """返回 ``(失败几次后升级, 升级到哪个角色)``；没配就返回 None。"""
        spec = self.config["roles"].get(role, {})
        target = spec.get("escalate_to")
        if not target:
            return None
        return int(spec.get("escalate_after", 2)), target

    def record(self, usage: Usage) -> None:
        self.totals = self.totals + usage

    # -- 构造 -------------------------------------------------------------

    def _build(self, spec: dict[str, Any]) -> LLMProvider:
        pconf = self.config["providers"][spec["provider"]]
        kind = pconf["kind"]
        key_env = pconf.get("api_key_env", "")

        if key_env and not os.environ.get(key_env):
            raise RuntimeError(f"环境变量 ${key_env} 未设置")

        if kind == "anthropic":
            from llm.anthropic import AnthropicProvider

            return AnthropicProvider(
                model=spec["model"], api_key_env=key_env, name=spec["provider"]
            )

        if kind == "openai_compat":
            from llm.openai_compat import OpenAICompatProvider

            return OpenAICompatProvider(
                model=spec["model"],
                base_url=pconf.get("base_url"),
                api_key_env=key_env,
                name=spec["provider"],
                extra_params=spec.get("params"),
                timeout=float(spec.get("timeout", 180.0)),
            )

        raise ValueError(f"未知的 provider 类型 {kind!r}")
