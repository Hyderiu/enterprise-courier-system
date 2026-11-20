# 部署与配置

## 公开在线演示

公开演示运行完整的 FastAPI 应用和脱敏数据库，因此不能部署在只支持静态文件的 GitHub Pages 上。本仓库提供 `render.yaml`，可在 Render 中直接创建演示服务。

演示服务通过 `scripts/start_public_demo.py` 启动。启动过程会创建虚构数据，并强制关闭钉钉登录、CRM 同步、顺丰、顺丰同城、德邦和京东物流的真实下单能力。

部署完成后，将服务地址写入 README 的“系统演示”部分。免费实例在一段时间无人访问后可能休眠，首次打开通常需要等待服务唤醒。

## 本地演示

1. 创建 Python 3.11+ 虚拟环境。
2. 安装 `requirements.txt`。
3. 复制 `.env.example` 为 `.env`。
4. 执行 `python scripts/create_demo_data.py`。
5. 执行 `python run.py`，访问 `http://127.0.0.1:26666/`。

公开演示必须保持：

```dotenv
PREVIEW_SAFE_MODE=true
SF_LIVE_ORDER_ENABLED=false
SF_CITY_LIVE_ORDER_ENABLED=false
DB_LIVE_ORDER_ENABLED=false
JDL_LIVE_ORDER_ENABLED=false
```

## 私有生产部署

生产部署不应直接使用公开演示的 `.env`。建议流程：

1. 使用独立服务器和非管理员系统账号运行应用。
2. 把真实 `.env` 放在版本库之外，并限制文件权限。
3. 通过 HTTPS 反向代理访问，设置可信 Host、请求体大小和超时。
4. 开启 `SESSION_COOKIE_SECURE=true`，设置高强度随机 `SESSION_SECRET`。
5. 配置企业钉钉应用与 `PUBLIC_BASE_URL`，验证免登和回调域名。
6. 分地区录入承运商账号、店铺/月结配置，并先进行只读报价/查询联调。
7. 只有在完整测试、审批链和回滚方案确认后，才逐家开启实时下单。
8. 定期备份数据库与账单文件，备份应加密并限制访问。

## 必做上线检查

- [ ] `.env` 未被 Git 跟踪。
- [ ] 公开仓库和构建产物通过敏感信息扫描。
- [ ] Debug 与自动重载关闭。
- [ ] 文档接口按需要关闭或限制访问。
- [ ] HTTPS、可信 Host、安全响应头和代理信任范围已配置。
- [ ] Cookie 使用 Secure、HttpOnly、SameSite，并完成 CSRF 风险评估。
- [ ] 上传大小、扩展名、内容类型和账单解析限制已验证。
- [ ] 管理员接口、对象归属和地区隔离已做权限测试。
- [ ] 所有外部请求有超时、错误处理且不接收任意目标 URL。
- [ ] 承运商错误返回能展示可理解原因，不输出密钥或签名原文。

## 环境变量原则

- 密钥只放 `.env` 或企业密钥管理服务。
- 浏览器端 JavaScript 不保存服务端密钥。
- 测试、沙箱和生产凭证分开。
- 月结号、店铺 ID、客户编码也按敏感业务配置处理。
- 日志不得记录请求签名、Access Token、Cookie 或完整个人信息。
