/** Current STEP display mesh preview. Orthographic view directions match HLR. */
import * as THREE from './vendor/three.module.js';
import { TrackballControls } from './vendor/TrackballControls.js';

const vector = (value, field) => {
  if (!Array.isArray(value) || value.length !== 3 || value.some(number => typeof number !== 'number' || !Number.isFinite(number))) throw new Error(`${field} 必须为三个有限坐标`);
  return new THREE.Vector3(...value);
};

const AXES = {
  x: { vector: [1, 0, 0], up: [0, 0, 1], color: '#d34141' },
  y: { vector: [0, 1, 0], up: [0, 0, 1], color: '#258149' },
  z: { vector: [0, 0, 1], up: [0, 1, 0], color: '#2878c2' },
};
const svgElement = (name, attributes = {}) => {
  const node = document.createElementNS('http://www.w3.org/2000/svg', name);
  for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, value);
  return node;
};

export class StepViewer {
  constructor(rootElement) {
    if (!rootElement) throw new Error('缺少三维预览容器');
    this.root = rootElement;
    this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    this.renderer.setClearColor(0xf0f2f5);
    this.renderer.domElement.style.cssText = 'display:block;width:100%;height:100%;touch-action:none;';
    this.renderer.domElement.setAttribute('aria-label', '当前 STP 三维预览，左键自由旋转，右键平移，滚轮缩放');
    this.root.appendChild(this.renderer.domElement);
    this.world = new THREE.Scene();
    this.world.add(new THREE.HemisphereLight(0xffffff, 0x717987, 2.3));
    const light = new THREE.DirectionalLight(0xffffff, 2.0);
    light.position.set(2, -3, 5); this.world.add(light);
    this.camera = new THREE.OrthographicCamera(-1, 1, 1, -1, 0.01, 100000);
    this.camera.up.set(0, 0, 1);
    this.camera.position.set(2, -3, 2);
    this.model = new THREE.Group(); this.world.add(this.model);
    this.arrowGroup = new THREE.Group(); this.world.add(this.arrowGroup);
    this.objects = new Map(); this.origin = new THREE.Vector3();
    this.plan = null; this.step = null; this.explodeRatio = 1; this.frameDirty = true;
    this._makeControls();
    this._makeAxes();
    // Trackball stores the canvas rectangle. Scrolling a long page also moves it.
    this.refreshControlBounds = () => this.controls.handleResize();
    this.renderer.domElement.addEventListener('pointerdown', this.refreshControlBounds, true);
    this.resizeObserver = new ResizeObserver(() => this._resize());
    this.resizeObserver.observe(rootElement);
    this._resize(); this.disposed = false;
    this.frame = requestAnimationFrame(() => this._animate());
  }

  _makeControls() {
    this.controls?.dispose();
    this.controls = new TrackballControls(this.camera, this.renderer.domElement);
    this.controls.rotateSpeed = 1.7; this.controls.zoomSpeed = 1.25; this.controls.panSpeed = 0.65;
    this.controls.staticMoving = true;
    // Do not let global A/S/D shortcuts interfere with instruction text fields.
    this.controls.keys = [];
    this.controls.minZoom = 0.03; this.controls.maxZoom = 60;
    this.controls.addEventListener('change', () => { this.frameDirty = true; });
    this.frameDirty = true;
  }

  _makeAxes() {
    this.axesWidget = document.createElement('div');
    this.axesWidget.className = 'viewer-axis-widget';
    this.axesWidget.style.cssText = 'position:absolute;right:10px;top:10px;width:142px;z-index:2;border:1px solid #d6e0e8;border-radius:9px;background:#ffffffed;box-shadow:0 2px 7px #20304012;user-select:none;touch-action:none;';
    this.axesWidget.setAttribute('aria-label', 'XYZ 方向辅助');
    this.axisSvg = svgElement('svg', { viewBox: '0 0 140 116', width: 140, height: 116, role: 'group', 'aria-label': '随当前视角旋转的 XYZ 坐标轴' });
    this.axisSvg.style.cssText = 'display:block;';
    this.axisSvg.appendChild(svgElement('circle', { cx: 70, cy: 58, r: 3, fill: '#718394' }));
    this.axisNodes = [];
    for (const [axis, details] of Object.entries(AXES)) for (const sign of [-1, 1]) {
      const label = `${sign < 0 ? '−' : '+'}${axis.toUpperCase()}`;
      const group = svgElement('g', { tabindex: 0, role: 'button', 'aria-label': `沿 ${label} 轴观察`, 'data-axis': axis, 'data-sign': sign });
      group.style.cursor = 'pointer';
      const title = svgElement('title'); title.textContent = `沿 ${label} 轴观察`; group.appendChild(title);
      const line = svgElement('line', { x1: 70, y1: 58, stroke: details.color, 'stroke-width': sign > 0 ? 2.5 : 1.4, 'stroke-dasharray': sign > 0 ? '' : '3 3' });
      const circle = svgElement('circle', { r: sign > 0 ? 12 : 10, fill: sign > 0 ? details.color : '#ffffff', stroke: details.color, 'stroke-width': 1.4 });
      const text = svgElement('text', { 'text-anchor': 'middle', 'dominant-baseline': 'central', 'font-size': 11, 'font-weight': 700, fill: sign > 0 ? '#fff' : details.color });
      text.textContent = label;
      group.append(line, circle, text);
      group.addEventListener('click', () => this.setAxisView(axis, sign));
      group.addEventListener('keydown', event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); this.setAxisView(axis, sign); } });
      this.axisNodes.push({ axis, sign, group, line, circle, text });
      this.axisSvg.appendChild(group);
    }
    this.axesWidget.appendChild(this.axisSvg);
    // All six directions remain reachable even when projected axis ends overlap.
    const buttons = document.createElement('div');
    buttons.style.cssText = 'display:grid;grid-template-columns:repeat(6,1fr);gap:2px;padding:0 4px 5px;';
    for (const [axis, details] of Object.entries(AXES)) for (const sign of [1, -1]) {
      const button = document.createElement('button');
      button.type = 'button'; button.textContent = `${sign > 0 ? '+' : '−'}${axis.toUpperCase()}`;
      button.setAttribute('aria-label', `沿 ${button.textContent} 轴观察`);
      button.style.cssText = `padding:3px 0;font:600 10px/1.2 "Segoe UI",sans-serif;border:1px solid #e0e7ed;border-radius:4px;min-width:0;background:#fff;color:${details.color};`;
      button.addEventListener('click', () => this.setAxisView(axis, sign));
      buttons.appendChild(button);
    }
    this.axesWidget.appendChild(buttons);
    this.root.appendChild(this.axesWidget);
    this._updateAxes();
  }

  _updateAxes() {
    if (!this.axesWidget || this.axesWidget.hidden) return;
    const inverse = this.camera.quaternion.clone().invert();
    const nodes = this.axisNodes.map(node => {
      const direction = new THREE.Vector3(...AXES[node.axis].vector).multiplyScalar(node.sign).applyQuaternion(inverse);
      return { ...node, direction };
    }).sort((a, b) => a.direction.z - b.direction.z);
    for (const node of nodes) {
      const x = 70 + node.direction.x * 40, y = 58 - node.direction.y * 40;
      node.line.setAttribute('x2', x); node.line.setAttribute('y2', y);
      node.circle.setAttribute('cx', x); node.circle.setAttribute('cy', y);
      node.text.setAttribute('x', x); node.text.setAttribute('y', y);
      node.group.style.opacity = node.direction.z < -0.05 ? '0.58' : '1';
      this.axisSvg.appendChild(node.group);
    }
  }

  _resize() {
    const width = Math.max(1, this.root.clientWidth), height = Math.max(1, this.root.clientHeight || 400);
    this.aspect = width / height;
    this.renderer.setSize(width, height, false);
    this.controls?.handleResize();
    if (this.fitHeight) {
      this.camera.left = -this.fitHeight * this.aspect / 2; this.camera.right = -this.camera.left;
      this.camera.top = this.fitHeight / 2; this.camera.bottom = -this.camera.top;
      this.camera.updateProjectionMatrix();
    }
    this.frameDirty = true;
  }

  _animate() {
    if (this.disposed) return;
    this.controls.update();
    if (this.frameDirty) {
      this.renderer.render(this.world, this.camera); this._updateAxes(); this.frameDirty = false;
    }
    this.frame = requestAnimationFrame(() => this._animate());
  }

  _clear(group) {
    for (const object of [...group.children]) {
      object.traverse(child => {
        child.geometry?.dispose();
        if (Array.isArray(child.material)) child.material.forEach(material => material.dispose());
        else child.material?.dispose();
      });
      group.remove(object);
    }
  }

  async loadScene(scene) {
    if (!scene || scene.units !== 'mm' || !Array.isArray(scene.parts) || !scene.source_sha256) throw new Error('三维场景缺少当前 STP 来源或毫米单位');
    const generation = (this.sceneGeneration || 0) + 1; this.sceneGeneration = generation;
    this._clear(this.model); this._clear(this.arrowGroup); this.objects.clear();
    this.sceneData = scene;
    const minimum = vector(scene.bounds?.[0], '场景包围盒'), maximum = vector(scene.bounds?.[1], '场景包围盒');
    this.origin.copy(minimum).add(maximum).multiplyScalar(0.5);
    this.span = Math.max(1, maximum.clone().sub(minimum).length());
    for (let index = 0; index < scene.parts.length; index++) {
      if (this.disposed || this.sceneGeneration !== generation) return this;
      const part = scene.parts[index];
      if (this.objects.has(part.id)) throw new Error('三维场景实例 ID 重复');
      const low = vector(part.bbox?.[0], '实例包围盒'), high = vector(part.bbox?.[1], '实例包围盒');
      const center = low.clone().add(high).multiplyScalar(0.5);
      const object = new THREE.Group();
      object.userData = { id: part.id, name: part.name, center, bounds: new THREE.Box3(low, high) };
      if (part.positions?.length) {
        const positions = new Float32Array(part.positions.length);
        for (let k = 0; k < positions.length; k++) positions[k] = part.positions[k] - center.getComponent(k % 3);
        const geometry = new THREE.BufferGeometry(); geometry.setAttribute('position', new THREE.BufferAttribute(positions, 3));
        if (part.indices?.length) {
          geometry.setIndex(part.indices); geometry.computeVertexNormals();
          object.add(new THREE.Mesh(geometry, new THREE.MeshStandardMaterial({ color: 0xc6cdd6, roughness: 0.85, metalness: 0.03, side: THREE.DoubleSide, polygonOffset: true, polygonOffsetFactor: 1, polygonOffsetUnits: 1 })));
          // Sharp preview edges avoid rendering every tessellation diagonal.
          const edges = new THREE.EdgesGeometry(geometry, 24);
          object.add(new THREE.LineSegments(edges, new THREE.LineBasicMaterial({ color: 0x404a57, transparent: true, opacity: 0.65 })));
        } else {
          object.add(new THREE.Points(geometry, new THREE.PointsMaterial({ color: 0x475569, size: Math.max(1, this.span / 500), sizeAttenuation: false })));
        }
      }
      if (part.lines?.length) {
        const values = new Float32Array(part.lines.length);
        for (let k = 0; k < values.length; k++) values[k] = part.lines[k] - center.getComponent(k % 3);
        const geometry = new THREE.BufferGeometry(); geometry.setAttribute('position', new THREE.BufferAttribute(values, 3));
        object.add(new THREE.LineSegments(geometry, new THREE.LineBasicMaterial({ color: 0x475569 })));
      }
      object.position.copy(center).sub(this.origin); this.objects.set(part.id, object); this.model.add(object);
      if (index % 8 === 7) await new Promise(resolve => requestAnimationFrame(resolve));
    }
    if (this.disposed || this.sceneGeneration !== generation) return this;
    this.camera.near = 0.01; this.camera.far = this.span * 100 + 100;
    this._fitView([1.5, -2, 1.4], [0, 0, 1]);
    return this;
  }

  _visibleBounds() {
    const bounds = new THREE.Box3();
    for (const object of this.objects.values()) if (object.visible) {
      const shift = object.position.clone().sub(object.userData.center);
      bounds.union(object.userData.bounds.clone().translate(shift));
    }
    if (bounds.isEmpty()) bounds.set(new THREE.Vector3(-1, -1, -1), new THREE.Vector3(1, 1, 1));
    return bounds;
  }

  _fitView(cameraDirection, upDirection) {
    const direction = vector(cameraDirection, '视角').normalize(), up = vector(upDirection, '上方向');
    if (direction.lengthSq() < 0.5) throw new Error('视角方向不能为零');
    const right = up.clone().cross(direction).normalize();
    if (right.lengthSq() < 0.5) throw new Error('视角与上方向不能平行');
    const pageUp = direction.clone().cross(right).normalize();
    const bounds = this._visibleBounds(), center = bounds.getCenter(new THREE.Vector3()), size = bounds.getSize(new THREE.Vector3());
    let horizontal = 0, vertical = 0;
    for (const x of [bounds.min.x, bounds.max.x]) for (const y of [bounds.min.y, bounds.max.y]) for (const z of [bounds.min.z, bounds.max.z]) {
      const point = new THREE.Vector3(x, y, z).sub(center);
      horizontal = Math.max(horizontal, Math.abs(point.dot(right))); vertical = Math.max(vertical, Math.abs(point.dot(pageUp)));
    }
    this.fitHeight = Math.max(1, vertical * 2, horizontal * 2 / (this.aspect || 1)) * 1.22;
    this.camera.up.copy(pageUp); this.camera.position.copy(center).addScaledVector(direction, Math.max(this.span, size.length()) * 3 + 1);
    this.camera.zoom = 1; this.camera.lookAt(center);
    this._makeControls(); this.controls.target.copy(center); this.controls.update(); this._resize();
  }

  _applyPositions() {
    if (!this.plan || !this.step) return;
    const groups = new Map(this.plan.groups.map(group => [group.id, group]));
    const visible = new Map();
    for (const id of [...(this.step.assembled_groups || []), ...(this.step.moving_groups || [])]) {
      const group = groups.get(id); if (!group) throw new Error(`未知总成 ${id}`);
      const offset = vector(this.step.offsets?.[id] || [0, 0, 0], '分离偏移').multiplyScalar(this.explodeRatio);
      for (const partId of group.part_ids) visible.set(partId, offset);
    }
    const hidden = new Set([...(this.plan.excluded_part_ids || []), ...(this.step.hidden_part_ids || [])]);
    for (const [id, object] of this.objects) {
      object.visible = visible.has(id) && !hidden.has(id);
      object.position.copy(object.userData.center).sub(this.origin).add(visible.get(id) || new THREE.Vector3());
    }
    this._clear(this.arrowGroup);
    for (const arrow of this.step.arrows || []) {
      const to = vector(arrow.to, '箭头终点').sub(this.origin);
      const from = vector(arrow.from, '箭头起点').sub(this.origin).sub(to).multiplyScalar(this.explodeRatio).add(to);
      const delta = to.clone().sub(from), length = delta.length();
      if (length > 1e-8) this.arrowGroup.add(new THREE.ArrowHelper(delta.normalize(), from, length, 0xc62828, Math.min(length * 0.22, this.span * 0.045), Math.min(length * 0.08, this.span * 0.02)));
    }
    this.frameDirty = true;
  }

  setStep(plan, step, options = {}) {
    if (!this.sceneData) throw new Error('请先加载当前 STP 场景');
    if (plan?.source?.sha256 !== this.sceneData.source_sha256) throw new Error('步骤与三维场景不是同一个 STP 来源');
    this.plan = plan; this.step = step; this.explodeRatio = 1;
    this._applyPositions();
    if (!options.preserveView) this._fitView(step.camera || plan.style?.camera || [1.5, -2, 1.4], step.up || plan.style?.up || [0, 0, 1]);
    return this;
  }

  applyStep(plan, step, options = { preserveView: true }) { return this.setStep(plan, step, options); }

  getView() {
    this.controls.update();
    const camera = this.camera.position.clone().sub(this.controls.target).normalize();
    const up = new THREE.Vector3(0, 1, 0).applyQuaternion(this.camera.quaternion).normalize();
    return { camera: camera.toArray(), up: up.toArray() };
  }

  setExplodeRatio(ratio) {
    if (typeof ratio !== 'number' || !Number.isFinite(ratio) || ratio < 0 || ratio > 8) throw new Error('分离倍率必须在 0 到 8 之间');
    const view = this.getView(); this.explodeRatio = ratio; this._applyPositions(); this._fitView(view.camera, view.up);
    return this;
  }

  fit() { const view = this.getView(); this._fitView(view.camera, view.up); return this; }

  setAxisView(axis, sign = 1) {
    const key = typeof axis === 'string' ? axis.toLowerCase() : '';
    if (!AXES[key] || (sign !== 1 && sign !== -1)) throw new Error('坐标轴须为 X/Y/Z，方向须为 +1 或 -1');
    const direction = AXES[key].vector.map(value => value * sign);
    this._fitView(direction, AXES[key].up);
    return this;
  }

  resetView() {
    this._fitView(this.step?.camera || this.plan?.style?.camera || [1.5, -2, 1.4], this.step?.up || this.plan?.style?.up || [0, 0, 1]);
    return this;
  }

  roll(angleRadians) {
    if (typeof angleRadians !== 'number' || !Number.isFinite(angleRadians)) throw new Error('滚转角度必须为有限数值');
    const view = this.getView(), target = this.controls.target.clone();
    this.camera.up.copy(new THREE.Vector3(...view.up).applyAxisAngle(new THREE.Vector3(...view.camera), angleRadians));
    this.camera.lookAt(target); this._makeControls(); this.controls.target.copy(target); this.controls.update();
    this._updateAxes();
    return this;
  }

  setAxesVisible(visible) {
    if (this.axesWidget) this.axesWidget.hidden = !visible;
    if (visible) this._updateAxes();
    return this;
  }

  dispose() {
    this.disposed = true; cancelAnimationFrame(this.frame); this.resizeObserver.disconnect(); this.controls.dispose();
    this.renderer.domElement.removeEventListener('pointerdown', this.refreshControlBounds, true);
    this.axesWidget?.remove();
    this._clear(this.model); this._clear(this.arrowGroup); this.renderer.dispose(); this.renderer.domElement.remove();
  }
}
