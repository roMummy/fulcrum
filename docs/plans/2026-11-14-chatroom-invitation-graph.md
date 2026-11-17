# 群成员邀请关系图谱 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 将群成员邀请树替换为可交互的层级关系图谱，保留邀请证据、未知关系和成员表格。

**Architecture:** 服务端按 ID 生成无环展示森林并区分有关联及未关联节点；模板输出唯一一组可访问成员卡片。原生 JS 迭代计算层级坐标，SVG 绘制方向连线，页面和原生 dialog 通过移动同一工作区复用状态。

**Tech Stack:** FastAPI、Jinja2、SQLAlchemy、原生 JavaScript、SVG、现有 Soft UI CSS、pytest、Playwright。

---

本轮由当前会话直接执行用户已确认的方案。工作区已有无关未跟踪群功能文件，所有提交明确列出文件，避免混入其他工作；测试只覆盖本次直接相关能力。

### Task 1：明确图谱节点数据

**Files:**
- Modify: `app/services/chatroom_invitations.py`
- Modify: `app/main.py`（群详情邀请视图组装入口）
- Test: `tests/test_chatroom_invitations.py`

1. 将 `build_invitation_tree` 的当前调用统一改为 `build_invitation_graph`，不保留别名。
2. 在当前迭代森林中加入 `connected = bool(parent_key or child_count)` 和 `unlinked_count`；继续返回 ID、父键、角色、状态、邀请证据和循环统计。
3. 验证 `a → b → c` 与孤立 `d`、缺少成员资料的邀请人、循环以及 1500 层链，保证不猜测关系、不修改保存的证据。
4. Run: `.venv/bin/python -m pytest -q tests/test_chatroom_invitations.py`，预期当前邀请逻辑及新增节点分组通过。

### Task 2：替换图谱模板与样式

**Files:**
- Modify: `app/templates/_chatroom_invitations.html`
- Modify: `app/static/css/style.css`
- Modify: `tests/test_chatroom_profile_pages.py`

1. 将旧 disclosure 缩进树替换为单一图谱区域，包含已知计数、搜索、缩放/分支控制、画布、详情、未关联成员及放大弹窗。
2. 卡片通过 Jinja 宏复用，所有动态身份及证据字段保持自动转义；无脚本时仍能阅读卡片与邀请关系说明。
3. 保留当前鉴权/CSRF 的补查历史邀请表单。只有查看图谱的交互使用前端状态，不能写入成员关系。
4. 用当前 CSS token 设置卡片、SVG 连线、选中态、手机触控尺寸、固定画布高度与弹窗尺寸，并删除旧树相关规则。
5. 更新路由断言，覆盖已知关系、未关联成员、空状态和只读用户。

### Task 3：实现迭代布局与统一交互

**Files:**
- Modify: `app/static/js/chatroom-invitations.js`

1. 从服务端卡片读取父键、成员 ID 和证据；使用 Map 管理节点，所有查找均以唯一键完成。
2. 用逆序子树宽度与正序坐标计算布局，按每层真实卡片高度安排纵向间距。SVG 仅绘制可见关系边。
3. 维护展开集合、选中键与单一缩放/平移状态；实现缩放按钮、适应画布、分支切换、拖动和键盘操作。
4. 搜索姓名或 ID，列表显示姓名及 ID 以区分同名；定位时展开祖先并居中，被选中卡片可显示完整证据。
5. 弹窗通过移动同一工作区放大；关闭后移回并恢复入口焦点。ResizeObserver 与字体加载完成后更新尺寸。
6. Run: `node --check app/static/js/chatroom-invitations.js`，预期语法通过。

### Task 4：定向验证、浏览器验收与提交

**Files:**
- Modify: `docs/ui/soft-ui-page-checklist.md`
- Preview: `.run/chatroom-invitation-graph-preview/`（忽略，不提交）

1. Run: `.venv/bin/python -m pytest -q tests/test_chatroom_invitations.py tests/test_chatroom_profile_pages.py tests/test_chatroom_detail_avatar.py`；若虚拟环境实际路径不同，使用仓库已安装 pytest 的解释器。
2. 在真实群详情路由与临时数据库上运行浏览器检查：1440/1024/390、分支、缩放、拖动、姓名/ID/同名定位、深层定位、成员详情、未知/空状态、缺失邀请人、循环及弹窗焦点。
3. 仅执行 Python AST、Jinja 解析、JS 语法及 `git diff --check` 等直接相关检查。
4. 在 UI 清单如实记录构造数据、检查数量和范围，不宣称全量或微桥真机验证。
5. 先提交确认后的设计与计划；功能完成后使用中文提交说明列出变更和实际轻量验证，不包含部署凭据或无关未跟踪文件。

### Task 5：按追加授权发布到 201

1. 读取部署环境文件，先经 SSH 核对 201 当前提交、容器、数据库版本与插件；备份完整数据库、六个私有插件与旧镜像。
2. 推送提交到内网 Git 服务，201 使用一次性认证环境拉取；仅以目标提交的已跟踪文件构建镜像并重建 app。
3. 只读验证健康检查、静态资源和真实群详情；复查已修复的 Hindsight 就绪状态及重启次数，追加服务器维护记录。
