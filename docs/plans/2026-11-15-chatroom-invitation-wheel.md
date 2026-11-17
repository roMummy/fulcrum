# 邀请图谱滚轮缩放 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 让用户直接通过普通滚轮围绕鼠标位置放大、缩小邀请图谱。

**Architecture:** 修改现有画布 wheel 监听器，复用 `zoom()` 维护唯一视图状态与比例边界。统一滚动单位、控制单次缩放幅度，页面与放大弹窗共用工作区。只更新前端交互及提示，无后端和数据库变更。

**Tech Stack:** 原生 JavaScript、Jinja、Playwright/Chromium、Git、Docker Compose、SSH。

---

### Task 1: 更新画布滚轮与提示

**Files:**
- Modify: `app/static/js/chatroom-invitations.js` 的 wheel 监听器
- Modify: `app/templates/_chatroom_invitations.html` 的图谱操作提示

1. 去除 Ctrl 按键限制；空图谱和 `deltaY === 0` 直接返回。
2. 将行单位按 16 像素、页单位按画布高度折算，像素单位保持原值；单次距离限制在 ±120 像素，缩放因子为 `Math.exp(-delta * 0.002)`。
3. 阻止纵向滚轮默认行为，以鼠标相对画布内边缘的坐标调用已有 `zoom()`；中文注释说明单位、限幅和锚点。
4. 操作提示增加“滚轮缩放”。

### Task 2: 完成轻量验证并提交

**Files:**
- Test: `/tmp/preview_invitation_wheel_ui.py`，复用现有真实路由与临时数据库预览夹具
- Evidence: `.run/chatroom-invitation-wheel-preview/`

1. 执行 `node --check app/static/js/chatroom-invitations.js`、`git diff --check`，预期成功。
2. 在真实 Chromium 验证无 Ctrl 上下滚轮、鼠标锚点、画布内外页面滚动、三种滚动单位、比例上下限、横向输入、弹窗、按钮和拖动；不触发真实邀请同步。
3. 仅暂存本轮文档和两个前端文件，以中文提交并如实记录实际验证。

### Task 3: 部署并验收 201

1. 按 SSH 技能和服务器档案读取凭据，通过密码 SSH 先核对 192.168.2.201 的主机、版本和服务状态。
2. 推送内网 main，备份现有数据库、私有插件与环境，保留 `rollback-9be9133` 镜像。
3. 从精确 Git 提交归档离线构建，仅重建 app，不执行数据库迁移或邀请资料同步。
4. 核对运行版本、JS/模板哈希、HTTP readiness、四容器健康、私有插件完整性，追加服务器维护记录。

## 实际验证

- `node --check app/static/js/chatroom-invitations.js`、`git diff --check` 通过。
- 真实 FastAPI 路由、临时数据库与 Chromium 下，1440 和 390 像素视口共 25 项定向检查通过，浏览器脚本错误 0。覆盖普通滚轮上下方向、鼠标锚点、画布内外页面滚动、像素/行/页单位、细小触控板输入及 Ctrl + 滚轮、纯横向输入、按钮/键盘/拖动、弹窗与关闭后继续缩放、200% 上限和大图最小比例、适应画布以及空图谱。
- 验证脚本初版未等待合成矩阵刷新，并对原生事件使用小数坐标和过严的矩阵精度断言；通过事件与内联样式/合成矩阵对照定位后，修正验证时序及坐标精度，产品代码未因此增加补丁。最终完整定向脚本成功退出，报告保存于 `.run/chatroom-invitation-wheel-preview/report.json`。
- 本轮未运行全量测试，未触发线上邀请同步。
