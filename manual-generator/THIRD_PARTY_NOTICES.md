# 第三方依赖说明

本目录不分发第三方库、字体、原始CAD、原PDF或其二进制副本。requirements.txt 引用的依赖由安装者从官方 Python 包源另行安装，其自带许可与版权声明继续适用。这里的说明不替代任何上游许可，也不为 P226 原项目或用户资产新增整体许可。

- CadQuery 2.7.0：Apache License 2.0。项目与许可：[CadQuery](https://github.com/CadQuery/cadquery)。
- cadquery-ocp 7.8.1.1.post1：OCP 的许可为 Apache License 2.0；其 OpenCascade 内核及其它随包组件各自的许可仍适用。[OCP LICENSE](https://github.com/CadQuery/OCP/blob/master/LICENSE)。
- PyMuPDF 1.26.6／MuPDF：上游提供 AGPL 与商业授权，安装、再分发或部署时须遵守适用授权；公开一个代码目录本身不替代合规要求。[PyMuPDF License and Copyright](https://pymupdf.readthedocs.io/en/latest/about.html#license-and-copyright)。
- Python 与 pip 安装的间接依赖遵循各自随包许可。本目录未拷贝上游源文件；cad_pipeline.py 调用上游的 SVG／HLR API。

原PDF中的字体随原文件保留；新文字通过PyMuPDF提供的内建CJK字体写入。当前流程没有捆绑或修改独立字体文件。
