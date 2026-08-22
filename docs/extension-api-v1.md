# BiliBot Extension API v1

Extension API v1 让独立 AstrBot 插件以**受限扩展**形式接入 BiliBot WebUI，而不复制、覆盖或导入主插件私有实现。Creator 扩展是首个适配者。

## 设计目标

- 扩展存在时：WebUI 显示模式入口，并由主插件渲染扩展返回的安全 Page Schema。
- 扩展不存在、关闭或异常时：入口自动消失，BiliBot 原有页面、配置和运行逻辑不受影响。
- 扩展只能获得 Host 明确授权的能力；声明权限不等于获得权限。
- 扩展不能读取 Cookie、数据库连接、插件实例或其他内部对象。
- Extension API v1 默认拒绝视频上传与正式发布。

## 扩展发现

Host 在访问扩展接口时，通过 `context.get_all_stars()` 懒发现同时启用的插件。兼容插件使用 duck typing 暴露：

```python
def get_bilibot_extension_manifest() -> dict: ...
def bind_bilibot_host(host_api) -> None: ...
def unbind_bilibot_host() -> None: ...
async def handle_bilibot_extension_request(request: dict) -> dict: ...
```

发现过程会隔离单个扩展异常，并跳过禁用扩展、无效 manifest、未知 API 版本和重复扩展 ID。

## Manifest 与协议

双端协议文件为 `core/extensions/extension_api_v1.json`。扩展 manifest 至少声明：

- `type`、`id`、`name`、`version`；
- `extension_api` 与 `host_requires`；
- `navigation`、`pages`、`actions`；
- 请求权限 `permissions`。

Host 到扩展的操作名：

```text
page:{page_id}
action:{action_id}
health
```

请求和响应均使用带 `request_id` 的数据字典信封。扩展不直接注册主插件 HTTP 路由。

## WebUI Bridge 路由

以下 endpoint 挂载在 BiliBot WebUI Bridge 的插件前缀下：

```text
GET  extensions
GET  extensions/page?extension_id={extension_id}&page_id={page_id}
POST extensions/action
POST extensions/refresh
```

浏览器提交的扩展 ID、页面 ID、动作 ID和 payload 会先经过 Host。`actor` 与 `request_id` 由 Host 生成，不能由页面伪造。

## Page Schema 安全边界

扩展页面必须使用 `bilibot-schema-v1`。Host 后端先验证组件白名单，前端再按内置 renderer 渲染并转义文本。

Extension API v1 不允许扩展提供：

- 任意 HTML、JavaScript 或 CSS；
- 任意文件系统路径；
- 主插件 Cookie、配置对象、数据库连接或运行时实例；
- 绕过 Host 的 Bilibili 写操作。

Creator 视觉样式由主仓库内置的 `pages/bilibot/creator.css` 提供，并限定在 `.creator-mode` 作用域；离开 Creator 模式后恢复原 WebUI。

## 权限

安全默认授权：

```text
account.identity.read
memory.creator.read
activity.write
storage.extension.read
storage.extension.write
```

默认拒绝：

```text
actions.video.upload
actions.video.publish
```

后续接入真实上传/发布时，应增加管理员显式授权、投稿预览、人工确认、速率限制和审计记录，而不是直接扩大默认权限。

## 兼容与维护

Creator 仓库维护同名 `contracts/extension_api_v1.json`。修改协议时必须：

1. 同步更新两端文件；
2. 保持相同 SHA-256；
3. 更新两端测试和文档；
4. 对破坏性变更升级 `extension_api` 主版本。
