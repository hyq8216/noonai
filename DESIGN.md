---
name: "货品工作台 · noon 沙特"
description: "首版本地单人商品加工工作台的实际视觉与交互记录"
colors:
  ink: "#19242c"
  muted: "#566470"
  line: "#dce2e6"
  paper: "#fff"
  ground: "#f4f6f7"
  nav: "#202c34"
  nav-muted: "#b4c0c8"
  accent: "#ffe266"
  accent-ink: "#292300"
  green: "#186344"
  green-bg: "#e9f5ee"
  red: "#9b3030"
  red-bg: "#fff0ee"
  yellow-bg: "#fff6d8"
  focus: "#4069a4"
  control-border: "#c8d1d8"
  button-border: "#cbd3d9"
  primary-hover: "#f4d351"
  neutral-badge: "#edf1f4"
  neutral-badge-ink: "#465864"
  warning-ink: "#745512"
typography:
  headline:
    fontFamily: "-apple-system, BlinkMacSystemFont, \"PingFang SC\", \"Microsoft YaHei\", sans-serif"
    fontSize: "28px"
    fontWeight: 650
    lineHeight: 1.3
    letterSpacing: "-.035em"
  title:
    fontFamily: "-apple-system, BlinkMacSystemFont, \"PingFang SC\", \"Microsoft YaHei\", sans-serif"
    fontSize: "18px"
    fontWeight: 650
    lineHeight: 1.4
  body:
    fontFamily: "-apple-system, BlinkMacSystemFont, \"PingFang SC\", \"Microsoft YaHei\", sans-serif"
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.6
  label:
    fontFamily: "-apple-system, BlinkMacSystemFont, \"PingFang SC\", \"Microsoft YaHei\", sans-serif"
    fontSize: "13px"
    fontWeight: 550
    lineHeight: 1.6
  arabic:
    fontFamily: "Tahoma, \"Arial\", sans-serif"
    fontSize: "16px"
    fontWeight: 400
    lineHeight: 1.85
rounded:
  compact: "4px"
  badge: "5px"
  field: "6px"
  control: "8px"
  surface: "12px"
spacing:
  inline: "10px"
  content: "16px"
  panel: "20px"
  section: "24px"
  editor: "26px"
components:
  button-primary:
    backgroundColor: "{colors.accent}"
    textColor: "{colors.accent-ink}"
    rounded: "{rounded.control}"
    padding: "9px 16px"
  button-primary-hover:
    backgroundColor: "{colors.primary-hover}"
  button-secondary:
    backgroundColor: "{colors.paper}"
    textColor: "{colors.ink}"
    rounded: "{rounded.control}"
    padding: "9px 16px"
  button-dark:
    backgroundColor: "{colors.nav}"
    textColor: "{colors.paper}"
    rounded: "{rounded.control}"
    padding: "9px 16px"
  button-text:
    backgroundColor: "transparent"
    textColor: "#315775"
    rounded: "{rounded.control}"
    padding: "9px 16px 9px 0"
  button-danger:
    backgroundColor: "{colors.paper}"
    textColor: "{colors.red}"
    rounded: "{rounded.control}"
    padding: "9px 16px"
  button-small:
    backgroundColor: "{colors.paper}"
    textColor: "{colors.ink}"
    rounded: "{rounded.control}"
    padding: "5px 11px"
  input:
    backgroundColor: "{colors.paper}"
    textColor: "{colors.ink}"
    rounded: "{rounded.field}"
    padding: "10px 12px"
  nav-active:
    backgroundColor: "{colors.accent}"
    textColor: "{colors.accent-ink}"
    rounded: "{rounded.control}"
    padding: "12px 15px"
  badge:
    backgroundColor: "{colors.neutral-badge}"
    textColor: "{colors.neutral-badge-ink}"
    rounded: "{rounded.badge}"
    padding: "3px 8px"
  surface:
    backgroundColor: "{colors.paper}"
    rounded: "{rounded.surface}"
---

# Design System: 货品工作台 · noon 沙特

## Overview

**Creative North Star: "商品台账（本次实现描述）"**

“商品台账”是本轮代码实现的描述：冷白内容区、炭灰导航、黄色主要操作，以表格、分隔线和紧凑表单承载商品资料。名称、颜色描述和组件气质均用于记录当前实现，不代表用户确认的品牌命名或长期视觉偏好。

本文覆盖首版本地工作台，以 workbench/static/index.html、style.css、app.js 为实现依据，以 .impeccable/review/ 中 desktop、mobile、desktop-editor、mobile-editor 四张截图为桌面和窄屏视觉证据。截图含明确标记的示例商品，只证明这些界面的本地呈现；不证明真实货源、模型质量、noon 上架或平台可售。无生成的出货栅格设计资产；favicon 是手写几何 SVG，review PNG 是 QA 证据。

**Key Characteristics:**

- 冷白背景、炭灰导航与单一黄色操作强调。
- 紧凑台账与带分隔线的编辑表单，状态始终配有文字。
- 系统中文字体与单独的阿文输入排版。
- 桌面并排编辑与检查事项，窄屏改为纵向排列。

## Colors

颜色以任务可读性为主：黄色标出主要操作与当前导航，冷灰维持层次，绿红黄承担带文字的状态表达。前置 token 的原始值来自 CSS；侧车色阶是文档面板辅助展示，不是新增的出货样式。

### Primary

- **工作黄**（`accent` / `accent-ink`）：导入等主要按钮、当前导航与文字选区；按钮悬停采用 `primary-hover`。

### Neutral

- **炭墨**（`ink`）：正文、商品名与活动步骤下划线。
- **石板灰**（`muted`）：辅助说明、时间与字段提示。
- **冷白纸面**（`paper` / `ground` / `line`）：内容面板、页面底色和结构边线。
- **炭灰导航**（`nav` / `nav-muted`）：侧栏与保存按钮；辅助文字使用明亮灰色。
- **冷蓝焦点**（`focus`）：键盘焦点外框；不是状态成功色。
- **轻灰标记**（`neutral-badge` / `neutral-badge-ink`）：无警告级别的短状态。

### Status

- **核对绿**（`green` / `green-bg`）：本地完成状态和非负贡献测算结果。
- **问题红**（`red` / `red-bg`）：失败任务、错误提示、移除动作与负贡献结果。
- **待办浅黄**（`yellow-bg` / `warning-ink`）：待补充等短状态；大块注意事项使用更深的独立正文色。

**The Text With State Rule.** 状态颜色必须与当前实现中的文字标签同行，不能依赖颜色单独表达任务结果。

## Typography

**Display Font / Body Font:** 系统字体栈，按前置 `typography` 定义；不加载远程字体。中文优先使用设备的系统字体，回退到 PingFang SC、Microsoft YaHei 和 sans-serif。

**Arabic Font:** Tahoma、Arial、sans-serif。阿文输入和文本区显式设置 `dir="rtl"` 与 `lang="ar"`；整个中文界面仍为从左到右布局。

**Character:** 字体接近桌面原生工作软件，利用字重与行距建立层级。实现请求了 550、600、650 等字重；真实字形取决于系统可用字体，未提供自定义字库保证。

### Hierarchy

- **Headline:** 前置 `headline` 用于主标题；窄屏主标题缩为 24px。
- **Title:** 前置 `title` 用于面板标题；三级标题为 15px / 650，检查事项侧栏标题也为 15px。
- **Body:** 前置 `body` 为基础；表格正文与辅助说明使用 13px，辅助说明最大行宽 72ch。
- **Label:** 前置 `label` 用于字段标签。小提示与 SKU 为 11px，徽标同为 11px；示例小标记为 10px。未强行将中文转成大写。
- **Arabic:** 前置 `arabic` 用于阿文输入。文本区允许垂直调节大小。
- **Numeric:** 数量、金额和工作流计数使用等宽数字特性；这不是独立的等宽字体。
- **Empty state:** 空列表引导语使用 34px / 600，窄屏缩为 26px；仅用于这个已实现空态。

## Layout

桌面外壳为 216px 侧栏与可收缩主区的网格。侧栏为 sticky、top 0、高度 100dvh；不是永久固定覆盖层。顶栏高度 69px，主区最大宽度 1550px、居中，内边距 36px 38px 70px。常见动作间距为 10px，表单两列间距 19px，编辑器与检查事项间距 26px。

商品表格使用真实 table、thead、tbody；搜索与批量按钮放在同一工具条。桌面编辑器与 270px 检查事项侧栏并列；检查事项自身不是 sticky。导入区为 1.25fr / 1fr 的两列。

### Responsive behavior

- **1600px 及以上：** 主区上边距增加至 45px，页头下间距为 35px。
- **1150px 及以下：** 侧栏改为 180px，主区内边距 27px 24px 55px；编辑器和检查事项、导入区分别改为单列。检查事项变成有边线的白色面板，条目和元数据可换行。
- **700px 及以下：** 导航移到顶部，非 sticky，变为可横向滚动的一排按钮；隐藏品牌副标题、侧栏脚注和导航计数。顶栏缩为 49px，隐藏额外的店铺连接摘要。主区内边距 26px 18px 40px，页头动作换到标题下方。
- **700px 及以下：** 搜索框独占一行，批量动作可换行；两列表单变为单列。商品列表隐藏供应商、采购成本、内容进度和独立编辑按钮列，保留选择、商品名 / SKU 和审核状态。商品名按钮仍打开编辑页；隐藏资料可在编辑器读取。
- **700px 及以下：** 编辑步骤保持横向滚动，不改为下拉框；图片原图 / 成图仍并排，图高缩为 155px。保存区可换行。表格区域保留 overflow auto 作为兜底。

已检查的截图显示桌面宽 1440px 和窄屏宽 390px 的列表与双语编辑页。它们不是每个设备、每个标签页或浏览器组合的覆盖证明。

## Elevation & Depth

普通容器没有投影。冷灰页面、白色内容面、浅色边线与分隔条提供层次；编辑器不使用装饰性浮起效果。仅底部临时通知使用投影以覆盖当前页面。

### Shadow Vocabulary

- **通知浮层：** `0 8px 24px #19242c26`，只用于 toast。通知距视口底部 27px，最大宽度为 `min(650px, 90vw)`，显示约 6.5 秒。移动截图可见其覆盖部分正文，因此不应将它描述为始终不遮挡内容。

**The Flat Surface Rule.** 现有表格和编辑容器使用边线与底色区分，通知浮层保留唯一的投影层级。

## Shapes

前置 `rounded` 记录实际半径：主要内容容器使用 surface，按钮、导航和通知使用 control，输入使用 field，徽标使用 badge，代码片段和示例小标记使用 compact。结构线与控件边框一般为 1px，工作流和编辑步骤的活动下划线为 2px。待办项圆点直径 5px；输入复选框使用原生形状与 16px 尺寸。

## Components

### Buttons

直接、紧凑的原生 button。一般按钮最小高度 40px；small 最小高度 33px，字号 12px，窄屏一般按钮同样使用 12px 字号。前置组件值记录默认内边距和颜色；small 属于尺寸变体。

主按钮工作黄、保存按钮炭灰、次要按钮白色边线、文字按钮用于返回等低强调动作，危险按钮用红色文字。默认悬停底色为 `#f0f3f5`；主按钮覆盖为自己的悬停黄。dark、text、danger 的基础变体规则在通用 hover 之后，未定义单独悬停视觉，不应在文档预览中添加新效果。背景过渡为 0.15 秒 ease。禁用按钮为 opacity 0.48、not-allowed 光标并使用原生 disabled。

### Chips

状态徽标是非交互 span，不能当筛选按钮使用。基础、警告、完成、错误四种配色，配合明确文字。示例标记另用较小字号和淡黄色背景；当前工作流筛选是带 aria-pressed 的按钮及下划线，不是徽标。

### Cards / Containers

白色细边框容器承载表格、导入、设置与编辑器。普通 panel 内边距 27px，窄屏 20px；编辑表单为 26px，窄屏 20px 16px。表格与编辑器使用 overflow hidden 贴合圆角，内部滚动容器处理宽表格与步骤导航。

### Inputs / Fields

白底、control-border 边线，使用前置 input 组件尺寸。字段有显式 label / for；搜索、列表复选框具有 aria-label。textarea 默认最小高度 106px、行高 1.7，可垂直缩放。未实现单个字段的错误边框 / aria-invalid 系统；请求错误主要进入全局通知。

图片公开地址同样使用具名表单字段，输入会标记未保存，统一保存会收集全部图片地址。离开编辑视图和切换编辑步骤时，未保存内容通过浏览器原生确认框保护；关闭或刷新页面使用 beforeunload 提示。图片移除、上传等动作先保存脏表单。此记录描述代码行为，不构成远端图片可访问性验证。

### Navigation

桌面导航使用炭灰侧栏，默认浅色文字、悬停较亮炭灰、当前项工作黄。主导航有 aria-label，当前按钮有 aria-current="page"。编辑步骤是带 aria-current="step" 的导航按钮；它们并非 ARIA tablist，不宣称具备方向键切换的 tab 模式。

### Readiness and feedback

待完成事项以圆点列表和元信息列出本地阻塞项。信息缺失使用“待确认”等文字，审核状态不会替换“平台可售：未验证”。提示、任务徽标与商品状态均使用相同的语义颜色。

全局 toast 使用 role="status" / aria-live="polite"。页面任务期间设置 body 的 aria-busy，后台状态变化不是每次都完整播报。全局 :focus-visible 是 3px focus 色外框、3px 偏移；页面包含跳到主内容链接。视图导航和打开商品会聚焦主标题，搜索重绘后保留焦点与光标位置，筛选、编辑步骤和列表选择重绘后恢复对应控件焦点。

**Accessibility limits:** 部分保存、异步刷新和后台任务重绘没有统一的焦点恢复机制；禁用动作解释依赖邻近文字，未统一关联 aria-describedby。控件并非全部达到 44px 触摸目标。表格标题没有额外 scope 属性，步骤不实现箭头键 tab 模式。这里记录已有辅助能力及边界，不宣称完成屏幕阅读器实测或 WCAG 认证。

**Motion:** 唯一入场动效为 toast 的 0.18 秒 ease-out 裁切展开；按钮背景过渡为 0.15 秒 ease。prefers-reduced-motion: reduce 会禁用全部 animation 与 transition，不改变主要信息内容。

## Do's and Don'ts

### Do:

- Do 沿用前置 token 和实际组件状态；新增状态需要明确的文字说明。
- Do 保留待确认、示例、本地审核与平台状态的视觉区别。
- Do 在窄屏保留商品名称入口与审核状态，并让编辑步骤可以横向滚动。
- Do 保留表单标签、可见键盘焦点和阿文字段的 RTL / lang 标记。
- Do 将商品原图、加工成图和 QA 截图的用途分开记录。

### Don't:

- Don’t 将本地绿色状态或内容任务完成写成平台上架、真实可售或用户验收。
- Don’t 为普通表格、表单容器额外引入当前实现没有的投影层级。
- Don’t 将本轮实现选择记作已确认的品牌偏好，或将辅助色阶记作出货 CSS。
- Don’t 声称全部控件达到 44px 触摸目标，或仅凭截图宣称通过可访问性认证。

## 0.2 实现延续说明（2026-10-01）

本轮沿用已记录的颜色、字体和控件样式，把应用名称改为Noon Studio，导航扩展为运营总览、商品库、导入货源、订单履约、采购收货、库存台账、业务档案、任务与记录、连接设置。业务页面以表单和库存流水表为主，状态指标只统计本地记录。Mac客户端使用原生标题栏、菜单、文件选择和保存对话框，网页界面嵌在WKWebView中。0.1组件sidecar保留为历史设计资产，尚未为新增运营组件补齐预览。

## 0.44 紧凑业务导航（2026-10-03）

沿用216px炭灰侧栏、黄色当前入口和系统中文字体。运营总览直达，其余39个入口归入商品铺货、订单履约、采购库存、财务经营、自动化、系统设置。用户选择单组展开；商品采集/素材、采购/库存、财务对账、资料备份采用三级入口，日常铺货入口保持二级直达。原40个页面路由及业务保护完整保留。

桌面路由按钮13px、子组标题12px、计数与位置说明11px，延续原辅助字号；组标题使用#d4dde4，子组与说明使用nav-muted，层级细线#465560，窄屏菜单按钮边框#586a76。这些值只用于炭灰导航上的可读层次。桌面目标高38px，窄屏路由目标高44px，展开箭头为同笔画SVG。菜单展开采用aria-expanded与aria-controls，隐藏子项不进入键盘顺序；不伪装为需要专门方向键协议的ARIA tree。

搜索展开匹配层级但不改写原折叠状态，支持1688、模特照、Codex等别名；Escape清空，单结果可Enter进入。成功切换页面清空搜索并打开所在组；取消离开保留草稿和当前页。顶部显示二/三级路径，窄屏默认收起整份导航。折叠菜单只更新导航区域，不重新创建正在编辑的表单；数据刷新保留手动收起状态与侧栏滚动位置。

实际桌面1440px、中宽900px、窄屏390px证据位于本机忽略目录output/playwright/navigation-*；业务空态保持真实的待接入说明，截图和导航回归不证明真实平台可售。
