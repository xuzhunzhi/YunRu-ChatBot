"""她自己的群角色查询（**前置插件**）。

它只回答一件事：**她在某个群里是 owner / admin / member**。群主动作（改名片、设管理员、
头衔、公告）与入群审批都以它为前置——判不出角色就一条都不该做。

## 为什么它是插件而不是"共享库"

2026-10-01 用户纠正："一个东西搬进插件另一个插件失效不代表前者不能作为插件，
只需要把前者作为前置插件就行。"

我一开始把它放在 `plugins/_shared/`（靠下划线躲过发现机制），那是错的：
它既不能单独启用，也没法被别的插件声明。现在它是一个**普通插件**，
依赖它的那两个用 `REQUIRES = ("roles",)` 声明——由发现机制保证先装它。

## 它怎么拿到"外面"

**不持有 `transport`**。它走核心注入的 `call_action` 接缝（已过 `capabilities`
闸门、回执已解包），这也是 `SelfRoleCache` 支持的三种 client 形态之一。
"""
from __future__ import annotations

from ... import dev_config
from .roles import SelfRoleCache

#: 别的插件要拿它就用 `REQUIRES = ("roles",)`。它们通过 `registry.shared_roles()` 取。
PREREQUISITE = True


def register(registry) -> None:
    """把角色查询**共享出去**，而不是自己独占。

    核心的 `call_action` 缺失时不装（`roles` 就是查不了）——依赖它的插件会因此
    一起跳过，这正是我们要的：**前置不在，依赖它的也不该半死不活地挂着**。
    """

    if registry.call_action is None:
        raise RuntimeError("roles 需要核心注入 call_action 才能查角色")
    cache = SelfRoleCache(registry.call_action, ttl=dev_config.SELF_ROLE_TTL_SECONDS)
    registry.provide_roles(cache)
