# 原项目上的六项升级

最终 PDF 使用用户提供的两页模板：一页封面、一页安装说明。封面、文字、二维码、配件栏、功能图、字号和位置保持原样，只在校准的步骤图区域替换当前 STP 的图示。模板缺失或对应关系不符时不会另外设计一份 PDF。

原有 `import_actual_step.py`、`render_steps.py`、`cad_pipeline.py`、`build_original_template.py` 和原配置保留兼容。通用应用入口为 `启动.cmd` / `start-app.ps1`，包含 `frontend/`、`backend/`、后台任务和 `scripts/manual.py`；P226 分组和相机是精确绑定原 STP 的示例数据，引擎中没有型号分支。

工作台支持新建/切换项目、上传 STP 及新版本、自动起草与出图、交互三维选角度、每步独立出图、爆炸距离和方向、逐源实例显隐及局部图选择。分离调整即时更新三维模型，线稿任务成功后才替换当前步，失败保留旧图。PDF 模板可在界面上传并逐步框选原图区域，或导入已校准槽位配置；绑定前校验页数、区域、文字遮盖及步骤对应。

| 用户要求 | 当前行为 | 明确边界 |
| --- | --- | --- |
| 1. 没有旧说明书也能起草 | 从 XCAF 实例层级和实际 BREP 表面距离提出分组、候选连接、展示顺序、草案文字和图示配置；可合并组、调序、修改 | 包装总成、连接形式和实际可操作顺序仍是建议；不把纯几何猜测变为事实 |
| 2. STP 是唯一几何依据 | 保留源实例、位置、实体、面、壳，缓存/步骤/图稿绑定来源摘要 | 不补造缺失网布、脚托、孔和螺丝；连续面参与遮挡，真实孔洞按几何出图，边线不能充当完整遮挡面 |
| 3. 固定插图规则 | 统一画布、up、消隐线稿和红色复位箭头；投影评分建议角度及尺寸相关分离量；实际近邻候选点锚定箭头并建议两侧实例局部图 | 包围盒代理评分不是精确连接可见性或运动验证；箭头不自动解释为拧螺丝、插入轴或行程；人工视角/profile不被改写 |
| 4. 换版只依据新 STP | 按几何指纹与位置逐对象/旧步骤报告变化候选、歧义、缺失、需更新的主图和局部图，并建议可人工保留的相机/up | 不按名称或旧索引匹配；换源后旧图/确认不能用于新版本，模板对应也需重核；第一版不自动迁移装配语义 |
| 5. 包装同事确认 | 本地预览、修改图示、保存、逐步核对、确认，旧稿保留在 history | 无部门审批；固定模板模式锁定文字、步骤数、顺序和语义槽位，可改角度、分离、箭头、局部图和逐步显隐 |
| 6. 成品与复用结果 | 原模板 PDF、每步 SVG/PNG、steps.json、instructions.json、图稿清单、模板绑定及 PDF 导出摘要 | 无模板仍可起草/出图，最终 PDF 等待绑定用户模板；不能静默回退到自创排版 |

这些是规则驱动的可修改提议，没有调用生成式 AI 捏造模型或图片。实际安装动作、源中未表达的紧固件和可能不唯一的装配顺序保持待核。P226 人工 profile 不能作为任意 STP 包装总成已经准确自动识别的证明。

## Windows 应用

完整解压后双击 `启动.cmd`。首次安装依赖后会打开 `http://127.0.0.1:8765/`；后续使用本地缓存。详见 [README_APP.md](README_APP.md)。界面的“生成插图”“确认”与“导出原模板 PDF”为独立操作，只有明确导出才写 PDF。

## 命令行兼容入口

首次起草与预览不要求旧说明书：

```powershell
powershell -ExecutionPolicy Bypass -File .\run.ps1 -Step "D:/models/product.stp" -Project "projects/product-v1" -Title "安装说明草案"
```

脚本建立 Python 3.12 虚拟环境、安装固定 CAD/PDF 依赖，首次导入/出图后启动本地预览。再次打开同一项目：

```powershell
powershell -ExecutionPolicy Bypass -File .\run.ps1 -Project "projects/product-v1"
```

初稿可以修改分组、文字、顺序、角度、分离和局部图。“总成与连接候选”还提供实体近邻成对/聚类建议和重复几何批次建议，可以一次勾选候选组再核对合并；不会自动把 CAD 层级认定为包装总成，也不会因为指纹相同就认定是螺丝。候选计算有预算和逐对超时，区分完成、失败、超时、未测试；未找到候选不等于不存在连接。

## 固定原模板，只替换图

当前组/步骤与模板的语义对应需明确校准。P226 原项目参数和七步槽位为独立数据：

```powershell
.\.venv\Scripts\python.exe scripts/manual.py profile --project projects/p226-v1 --profile examples/p226-reviewed-profile.json
.\.venv\Scripts\python.exe scripts/manual.py template --project projects/p226-v1 --template "D:/manuals/用户原模板.pdf" --regions examples/p226-template-regions.json
```

示例只接受原 STP 与原模板的精确摘要，其他型号使用自己的可复用模板配置，不自动继承 P226 文字。`template` 只校验和绑定，不生成图或 PDF。它检查模板摘要、页数、换图区域不压原文字，按步骤 ID 对应槽位，不按数组位置猜测。来源/图稿通过校验且全部图示参数相同时可以保留已有 SVG 字节，迁移另存记录。旧输出、配置和确认可在 history 找回。

绑定后界面有两种操作：

- **用已有图套入原模板**：只复用当前 SVG 导出 PDF，不重新计算三维消隐。
- **更新步骤图并套入原模板**：角度、分离、显隐等改动后计算新步骤图，再套入相同模板。

文字、顺序和槽位在界面及服务器双重固定，详细 JSON 也不能绕过。核对 warnings 仍可改。封面和配件/功能图不换成 CAD 图；默认不新增标题、页脚、审核块或自动扩页。改变步骤粒度需显式重新校准模板。配置可以显式指定注记，默认无新增注记。

导出校验源模型/缓存、配置、SVG 和模板摘要，`pdf_export.json` 记录当前 PDF 的字节摘要。无当前导出记录的旧 PDF 不被当成当前结果提供。

```powershell
.\.venv\Scripts\python.exe scripts/manual.py render --project projects/p226-v1
.\.venv\Scripts\python.exe scripts/manual.py export --project projects/p226-v1
.\.venv\Scripts\python.exe scripts/manual.py review --project projects/p226-v1 --port 8765
```

## 网布遮挡和逐步显示

原 STP 保留。每步 `hidden_part_ids`、`visibility_reason`、`offsets`、`camera`、`up`、`focus_part_ids` 控制显隐、分离、视角和局部图。整图保留网布，观察内部连接时可临时隐藏独立实例；最终总览恢复临时隐藏，已明确的辅助几何排除另记理由，不自动过滤面/壳。

同一不可分实例中的网布与框架目前只能整项隐藏。真实带孔网格可能透出后方线条，不能自动填孔/补面。分离方向和距离是图示参数，不证明实际运动或行程。

## 换版

```powershell
.\.venv\Scripts\python.exe scripts/manual.py draft --step "D:/models/product-new.stp" --project projects/product-v2
.\.venv\Scripts\python.exe scripts/manual.py compare --old-project projects/product-v1 --new-project projects/product-v2
```

新项目 `revision_report.json` 包含 `object_impacts`、`step_impacts`、`diagram_updates`、`view_configuration_suggestions`。相同形状/位置仍是候选，重复件和粗指纹歧义需人工复核。新图只从新项目库存生成，报告不导入旧模型、不重绘、不覆盖旧人工稿。

## 可复用文件和验证

`source.json`/`inventory/` 保存源及逐实例 BREP；`proposed_steps.json` 保留初稿；`steps.json` 保存当前配置和模板绑定；`diagrams/manifest.json` 绑定逐步 SVG/PNG；`instructions.json` 是可复用文字；`manual.pdf`/`pdf_export.json` 是原模板成品及摘要；`template_binding.json` 记录复用图稿的迁移；`confirmation.json` 只绑定该源及配置；旧稿在 `history/`。

改图示参数后旧图与确认失效，只勾选核对不需要重新消隐。源文件原路径仍存在时检查换版；移动后仍能使用通过摘要校验的库存。用户模型和模板不包含在代码分发包中。

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

模板验证为 mock，规则与换版测试为纯数据，不生成图/PDF。全套端到端 CAD 测试会产生临时模型和图，仅需验证实际渲染时运行。本次按用户要求不重复生成 P226 图或 PDF，保留现有七张 SVG。

`book_pdf.py` 和 `two_page_pdf.py` 保留直接调用兼容，已从默认最终导出移除；最终入口只调用 `template_pdf.py`。
