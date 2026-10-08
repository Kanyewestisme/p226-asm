# P226 实际出图程序

本目录是已用于 P226 的 STEP 线稿和原说明书换图程序。默认产物保留原 PDF 的两页 A3 横版结构，只替换第二页的七步安装图并添加审核说明。不是通用 STEP 自动装配系统；型号、安装顺序、实例索引、分组、相机、分离量和箭头都是这份 P226 模型的已人工检查配置。

## 1. 获取与准备输入

在已有仓库中执行 `git pull`，然后进入 `manual-generator`。首次下载可执行：

```sh
git clone https://github.com/Kanyewestisme/p226-asm.git
cd p226-asm/manual-generator
```

把自己已有的两个原文件放到以下位置。仓库不包含原 PDF、STEP、生成图、字体二进制或缓存。

```text
manual-generator/
  input/
    model_archive/p226_asm-20250114.stp
    P226(M)(S) 安装说明书 HBD-RD-P226-006 A0.pdf
```

STEP 也可从仓库已有的 [20250114 Release](https://github.com/Kanyewestisme/p226-asm/releases/tag/20250114) 下载 `p226_asm-20250114.zip` 后解压。PDF 使用自己的原文件。

已核实的输入 SHA256：

- STEP：`bc9fdacd205714dd2589a24a8a0b59b1b5a6972ab9d8e4c80065277962d1c987`
- PDF：`d1f3c368b8e4fede4a61993db05423704889342aa21fafcab6a9a2f396db2669`

## 2. Windows：安装一次，按顺序运行

使用 Python 3.12 64位版；从 [Python 官方下载页](https://www.python.org/downloads/windows/) 安装，不要从陌生软件站安装。下面在 PowerShell 中运行，当前目录为 `manual-generator`。直接调用虚拟环境的 Python，无需改变 PowerShell 执行策略或激活脚本。

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe scripts\import_actual_step.py
.\.venv\Scripts\python.exe scripts\render_steps.py all
.\.venv\Scripts\python.exe scripts\build_original_template.py
```

每条命令成功结束后再执行下一条。没有 `py -3.12` 时，先安装 Python 3.12，或使用已安装 Python 3.12 的完整路径。不要另外安装名为 `fitz` 的包；脚本的 `import fitz` 由 `PyMuPDF` 提供。

## 3. Linux／云端

在已有 Python 3.12 的 Linux 环境中，同样进入 `manual-generator`：

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python scripts/import_actual_step.py
.venv/bin/python scripts/render_steps.py all
.venv/bin/python scripts/build_original_template.py
```

依赖固定为生产使用的 CadQuery 2.7.0、cadquery-ocp 7.8.1.1.post1、PyMuPDF 1.26.6；其它依赖由 pip 安装。无需 FreeCAD、CQ-editor、浏览器或 GPU。复杂总成消隐会消耗 CPU 和内存，`START` 到 `READY` 之间可能较久，不是每一步都有连续进度。

[CadQuery 官方安装说明](https://cadquery.readthedocs.io/en/stable/installation.html) 支持 Windows、Linux、macOS 的 pip 安装。若 pip 提示该平台没有匹配 wheel，请先核对64位 Python 3.12与系统平台，不要自行混换 OCP 版本。Windows 命令已检查语法，当前整理验证环境是 Linux／Python 3.12，未在实体 Windows 电脑上执行。

## 4. 输出与再次出图

- `output/model_inventory/`：逐实例 BREP 缓存、`assembly_parts.json`、导入完成标记。原模型导入记录为139个叶节点、114个实体。同名零件仍按实例保留位置，不按名字合并。
- `output/diagrams/`：每步 SVG 向量线稿、PNG 预览、`manifest.json`。`all` 生成七步和额外的配件总览；也可用 `1` 至 `7` 或 `inventory` 单独出图。
- `output/original_template/P226_original_template_review.pdf`：当前默认两页审核PDF。
- 同目录下的 `installation_page_preview.png`、`qa/`、`vector/`、`build_manifest.json`：预览、逐页检查图、中间向量PDF与换图记录。

模型未改变时保留 BREP 缓存，不必重复导入。只修改图稿或模板排版时，可只运行最后一条命令。源模型改变后必须重新导入并重新核对分组，不能沿用旧缓存。

脚本默认路径以本目录定位；显式传入的相对路径按当前工作目录解析。三个入口均支持 `--help`。例如输入位于其它位置：

```sh
python scripts/import_actual_step.py --step "D:/models/p226_asm-20250114.stp"
python scripts/render_steps.py 1 --inventory "output/model_inventory" --output "output/diagrams"
python scripts/build_original_template.py --template "D:/manuals/P226(M)(S) 安装说明书 HBD-RD-P226-006 A0.pdf" --diagrams "output/diagrams" --output "output/original_template/P226_review.pdf"
```

示例中的 `python` 指所建虚拟环境的 Python；Windows可替换为 `.\.venv\Scripts\python.exe`，Linux为 `.venv/bin/python`。

## 5. 算法与配置

1. `scripts/import_actual_step.py` 使用 OpenCascade XCAF 读取真实 STEP 装配层级、重复名称实例和位置，逐叶节点导出 BREP。导入完成后才写完成标记。
2. `scripts/render_steps.py` 按 P226 特定实例索引组成各阶段，将实际几何平移作分离示意，再把整组几何一起做隐藏线消除，生成 SVG 和箭头。主要步骤与配件总览用精确 BRep HLR；第7步默认使用 polygonal HLR，线性偏差0.20 mm、角偏差0.20 rad，输出仍是向量线稿。原参数保留。
3. `scripts/build_original_template.py` 将七张 SVG 转成向量PDF放回已核实的原模板区域。旧插图用白色叠加层遮住，底层旧向量仍在文件里，这不是删除或脱敏。原文件不被覆盖。
4. `configuration/assembly_mapping.json` 记录已核大总成对应、剔除辅助曲面与待确认项；实际分组和动作仍由 `render_steps.py` 执行。只改 JSON 不会自动改图。
5. `configuration/safe_replacement_regions.json` 保存原模板七个换图遮罩及原PDF摘要。更换PDF前须重新检查坐标与文字碰撞；摘要不符时构建器会停止。

程序不读取 `steps.json`，本目录不包含该原稿审核文件。原说明文字、产品规格、安全和保养内容仅来自用户本地的 PDF 模板，未随程序复制到仓库。

调试时可以设置 `P226_HLR_MODE=exact` 或 `poly` 覆盖消隐方式。正式复现请不设置此变量，以保持原参数。

### 补网布或更换 STEP

新加的网布需要实际建成能参与消隐的面／实体，并加入对应阶段的几何组；只画轮廓、仅改显示透明度或只存在于STEP中但没入组，都不能保证正确遮挡。程序不会自动把新网布纳入 P226 分组。真实带孔网格与用于说明图的完整遮挡面也会得到不同结果。

更换STEP会改变叶节点编号或装配结构，原硬编码索引可能指向别的零件。必须先重新导入、检查实例清单，重新核对 `render_steps.py` 的分组、例外曲面和各步参数，同时更新 `assembly_mapping.json` 的对应关系和摘要，再逐张检查遮挡。源摘要与配置不符时渲染器会停止；不要只修改摘要绕过检查。

## 6. 字体、许可证与审核限制

当前两页流程沿用原PDF内的字体；新增短审核说明使用 PyMuPDF 内建 `china-s` 中文字体。因此无需安装系统中文字体、Noto字体包、字体pip包或手动复制字体。[PyMuPDF字体文档](https://pymupdf.readthedocs.io/en/latest/font.html) 说明了内建CJK字体支持。

此前的十页重新排版程序不是本目录的默认流程，本次最小程序未包含它。它另需 Noto Sans CJK SC Regular／Bold 及对应 OFL 许可，不能把其字体准备步骤套到当前两页流程。依赖及其许可说明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)；本目录未替原项目新增整体开源许可。

这是工程审核稿，不是已获产品方签核的消费者安装说明：

- B/C螺丝没有可靠对应到STEP独立零件，未伪造螺丝、孔位、扭矩、网布或脚托。
- 左右扶手支架的非实体曲面实例75和107按真实模型保留。
- 第5步红箭头表示扶手总成复位，不是螺丝旋入方向。
- 型号／版本、最终孔位、头枕滑块和插入深度、缺失网布及脚托仍须核实。
- 两页原模板保留原安全和保养文字，其中溶剂使用说明存在矛盾，尚未解决；保留原文不表示认可该操作，正式发布前必须由产品方确认并修订。
- 本次整理保留生产算法与画面参数，做入口编译、CLI及已有真实SVG重建核验；未重新执行完整大STEP导入和全部消隐。
