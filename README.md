# p226-asm

安装说明工作台：上传 STP / STEP，起草和编排安装动作，在三维模型中调整每一步的视角、显隐和分离距离，生成 SVG，并沿用自己的两页 PDF 模板输出说明书。

完整前后端位于 [manual-generator](manual-generator)。Windows 下载仓库并解压后，进入该目录双击 `启动.cmd`；首次准备 Python 依赖，随后打开本机工作台。详见 [启动与使用说明](manual-generator/README_APP.md) 和 [六项升级及适用边界](manual-generator/UPGRADES.md)。

工作台提供安装单元拆分与合并、草案步骤增删与重排、单步出图、XYZ 视角辅助，以及原文和译文对应的多语言编辑。已绑定固定模板时保留原文、顺序和版式，只调整步骤图；语言 PDF 使用同一模板中明确校准的文字区域。通用文字翻译服务可以按 [配置指南](manual-generator/README.md#可选文字翻译服务) 接入。

实际安装顺序和连接方式需要包装同事核对。示例配置与指定源文件摘要绑定，不作为其他型号已确认的安装事实。模型、说明书原件及运行数据不包含在源码中。

原 `p226_asm-20250114.zip` 下载归档继续保留在 [20250114 Release](https://github.com/Kanyewestisme/p226-asm/releases/tag/20250114)。
