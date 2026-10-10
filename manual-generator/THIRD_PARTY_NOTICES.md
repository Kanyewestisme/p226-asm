# 第三方依赖说明

本目录包含用于浏览器三维预览的 Three.js 源码，许可见下。requirements.txt 引用的 Python 依赖由安装者从官方 Python 包源另行安装，其自带许可与版权声明继续适用。本目录不包含原始 CAD、原 PDF 或独立字体文件；这里的说明不替代任何上游许可，也不为 P226 原项目或用户资产新增整体许可。

- Three.js 0.180.0：MIT License。本地文件为 `frontend/vendor/three.module.js`、`three.core.js`、`OrbitControls.js` 和 `TrackballControls.js`，来自官方 [three@0.180.0 npm 包](https://registry.npmjs.org/three/-/three-0.180.0.tgz)。两个 Controls 文件仅将 `three` 导入指向同目录 `three.module.js`；许可完整保留于 `frontend/vendor/LICENSE.three.txt`，文件哈希与来源保留于 `frontend/vendor/three.provenance.json`。当前三维交互使用 TrackballControls。

- CadQuery 2.7.0：Apache License 2.0。项目与许可：[CadQuery](https://github.com/CadQuery/cadquery)。
- cadquery-ocp 7.8.1.1.post1：OCP 的许可为 Apache License 2.0；其 OpenCascade 内核及其它随包组件各自的许可仍适用。[OCP LICENSE](https://github.com/CadQuery/OCP/blob/master/LICENSE)。
- PyMuPDF 1.26.6／MuPDF：上游提供 AGPL 与商业授权，安装、再分发或部署时须遵守适用授权；公开一个代码目录本身不替代合规要求。[PyMuPDF License and Copyright](https://pymupdf.readthedocs.io/en/latest/about.html#license-and-copyright)。
- Python 与 pip 安装的间接依赖遵循各自随包许可。本目录未拷贝上游源文件；cad_pipeline.py 调用上游的 SVG／HLR API。

原PDF中的字体随原文件保留；新文字通过PyMuPDF提供的内建CJK字体写入。当前流程没有捆绑或修改独立字体文件。
