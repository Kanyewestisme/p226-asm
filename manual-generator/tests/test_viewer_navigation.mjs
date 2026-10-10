/** Run with `node --test tests/test_viewer_navigation.mjs`; no DOM or WebGL. */
import assert from 'node:assert/strict';
import test from 'node:test';
import * as THREE from '../frontend/vendor/three.module.js';
import { StepViewer } from '../frontend/viewer.js';

const EPSILON = 1e-10;
const vector = values => new THREE.Vector3(...values);
const near = (actual, expected, label) => {
  assert.ok(Math.abs(actual - expected) <= EPSILON, `${label}: ${actual} != ${expected}`);
};
const nearVector = (actual, expected, label) => {
  const left = Array.isArray(actual) ? actual : actual.toArray();
  const right = Array.isArray(expected) ? expected : expected.toArray();
  left.forEach((value, index) => near(value, right[index], `${label}[${index}]`));
};
const assertFrame = view => {
  const camera = vector(view.camera), up = vector(view.up);
  assert.ok([...view.camera, ...view.up].every(Number.isFinite));
  near(camera.length(), 1, 'camera length');
  near(up.length(), 1, 'up length');
  near(camera.dot(up), 0, 'camera/up orthogonality');
  near(up.clone().cross(camera).length(), 1, 'page right length');
};

function makeViewer() {
  const viewer = Object.create(StepViewer.prototype);
  viewer.camera = new THREE.OrthographicCamera(-2, 2, 1, -1, 0.01, 10000);
  viewer.camera.up.set(0, 0, 1);
  viewer.span = 20;
  viewer.aspect = 2;
  viewer.origin = vector([100, 200, 300]);
  viewer.objects = new Map();
  viewer.arrowGroup = new THREE.Group();
  viewer.sceneData = { source_sha256: 'current-source' };
  viewer.explodeRatio = 1;
  viewer._visibleBounds = () => new THREE.Box3(vector([-4, -2, -1]), vector([8, 6, 5]));
  viewer._resize = () => {};
  viewer._makeControls = function () {
    this.controls = {
      target: new THREE.Vector3(),
      update: () => this.camera.lookAt(this.controls.target),
    };
    this.frameDirty = true;
  };
  viewer._makeControls();
  viewer._fitView([3, -4, 5], [0, 0, 1]);
  return viewer;
}

function makeDisplayFixture() {
  const viewer = makeViewer();
  for (const [id, center] of [
    ['base-part', [101, 202, 303]],
    ['moving-part', [110, 205, 308]],
    ['hidden-part', [109, 204, 307]],
    ['excluded-part', [102, 203, 304]],
  ]) {
    const object = new THREE.Group();
    object.userData.center = vector(center);
    viewer.objects.set(id, object);
  }
  const plan = {
    source: { sha256: 'current-source' },
    groups: [
      { id: 'base', part_ids: ['base-part', 'excluded-part'] },
      { id: 'moving', part_ids: ['moving-part', 'hidden-part'] },
    ],
    excluded_part_ids: ['excluded-part'],
    style: { camera: [1, 0, 0], up: [0, 0, 1] },
  };
  const step = {
    camera: [2, -3, 4], up: [3, 2, 0],
    assembled_groups: ['base'], moving_groups: ['moving'],
    offsets: { moving: [5, 2, -1] }, hidden_part_ids: ['hidden-part'], arrows: [],
  };
  viewer.setStep(plan, step);
  viewer.explodeRatio = 2;
  viewer._applyPositions();
  return { viewer, plan, step };
}

const displayState = viewer => [...viewer.objects].map(([id, object]) => ({
  id, visible: object.visible, position: object.position.toArray(),
}));

for (const [axis, camera, up] of [
  ['x', [1, 0, 0], [0, 0, 1]],
  ['y', [0, 1, 0], [0, 0, 1]],
  ['z', [0, 0, 1], [0, 1, 0]],
]) for (const sign of [1, -1]) {
  test(`axis view ${sign > 0 ? '+' : '-'}${axis.toUpperCase()} has an orthonormal camera frame`, () => {
    const viewer = makeViewer();
    viewer.camera.zoom = 3;
    assert.equal(viewer.setAxisView(axis.toUpperCase(), sign), viewer);
    const view = viewer.getView();
    nearVector(view.camera, camera.map(value => value * sign), 'axis camera');
    nearVector(view.up, up, 'axis up');
    assertFrame(view);
    nearVector(viewer.controls.target, [2, 2, 2], 'visible bounds center');
    assert.equal(viewer.camera.zoom, 1);
    assert.ok(Number.isFinite(viewer.fitHeight) && viewer.fitHeight > 0);
  });
}

test('axis navigation and repeated views preserve visible instances and exploded positions', () => {
  const { viewer, plan, step } = makeDisplayFixture();
  const display = displayState(viewer), recipe = JSON.stringify({ plan, step });
  for (const axis of ['x', 'y', 'z']) for (const sign of [1, -1]) {
    viewer.setAxisView(axis, sign);
    const initial = viewer.getView();
    viewer.setAxisView(axis, sign);
    nearVector(viewer.getView().camera, initial.camera, 'repeated axis camera');
    nearVector(viewer.getView().up, initial.up, 'repeated axis up');
    assert.deepEqual(displayState(viewer), display);
    assert.equal(viewer.explodeRatio, 2);
  }
  assert.equal(JSON.stringify({ plan, step }), recipe);
});

test('preserveView step updates retain the dragged, panned and zoomed display camera', () => {
  const { viewer, plan, step } = makeDisplayFixture();
  viewer.roll(Math.PI / 5);
  const pan = vector([6, -2, 4]);
  viewer.camera.position.add(pan);
  viewer.controls.target.add(pan);
  viewer.camera.zoom = 2.5;
  const view = viewer.getView(), position = viewer.camera.position.clone();
  const target = viewer.controls.target.clone(), controls = viewer.controls;
  const changedStep = { ...step, offsets: { moving: [9, 1, 0] }, hidden_part_ids: [] };
  viewer.applyStep(plan, changedStep);
  nearVector(viewer.getView().camera, view.camera, 'preserved camera');
  nearVector(viewer.getView().up, view.up, 'preserved up');
  nearVector(viewer.camera.position, position, 'preserved position');
  nearVector(viewer.controls.target, target, 'preserved pan');
  assert.equal(viewer.camera.zoom, 2.5);
  assert.equal(viewer.controls, controls);
  assert.equal(viewer.objects.get('hidden-part').visible, true);
  nearVector(viewer.objects.get('moving-part').position, [19, 6, 8], 'updated offset');
});

test('roll changes page up around the observation direction without moving the camera', () => {
  const { viewer } = makeDisplayFixture();
  viewer.camera.zoom = 2.25;
  const original = viewer.getView(), position = viewer.camera.position.clone();
  const target = viewer.controls.target.clone(), display = displayState(viewer);
  for (const angle of [Math.PI / 2, -Math.PI / 3, Math.PI * 2]) {
    viewer._fitView(original.camera, original.up);
    viewer.camera.zoom = 2.25;
    viewer.roll(angle);
    const view = viewer.getView();
    // Rodrigues' formula supplies an independent expected rotation.
    const direction = vector(original.camera), initialUp = vector(original.up);
    const expectedUp = initialUp.clone().multiplyScalar(Math.cos(angle))
      .addScaledVector(direction.clone().cross(initialUp), Math.sin(angle))
      .addScaledVector(direction, direction.dot(initialUp) * (1 - Math.cos(angle)));
    nearVector(view.camera, original.camera, 'roll camera');
    nearVector(view.up, expectedUp, 'rolled page up');
    nearVector(viewer.camera.position, position, 'roll position');
    nearVector(viewer.controls.target, target, 'roll target');
    assert.equal(viewer.camera.zoom, 2.25);
    assert.deepEqual(displayState(viewer), display);
    assertFrame(view);
  }
});

test('reset restores the current step camera/up and fit keeps the chosen direction', () => {
  const { viewer, plan, step } = makeDisplayFixture();
  const recipe = JSON.stringify({ plan, step }), display = displayState(viewer);
  viewer.setAxisView('z', -1).roll(Math.PI / 4);
  const navigated = viewer.getView();
  viewer.camera.zoom = 4;
  assert.equal(viewer.fit(), viewer);
  nearVector(viewer.getView().camera, navigated.camera, 'fit camera');
  nearVector(viewer.getView().up, navigated.up, 'fit up');
  assert.equal(viewer.camera.zoom, 1);
  assert.equal(viewer.resetView(), viewer);
  const reset = viewer.getView();
  nearVector(reset.camera, vector(step.camera).normalize(), 'step camera');
  nearVector(reset.up, vector(step.up).normalize(), 'step up');
  assertFrame(reset);
  assert.deepEqual(displayState(viewer), display);
  assert.equal(viewer.explodeRatio, 2);
  assert.equal(JSON.stringify({ plan, step }), recipe);
});

test('getView survives JSON saving and supplies the same right/up frame to HLR', () => {
  const { viewer, step } = makeDisplayFixture();
  viewer.roll(0.73);
  const saved = JSON.parse(JSON.stringify(viewer.getView()));
  assert.deepEqual(Object.keys(saved).sort(), ['camera', 'up']);
  assertFrame(saved);
  // cad_pipeline.hidden_line_svg uses right=up cross camera, then pageUp=camera cross right.
  const camera = vector(saved.camera), right = vector(saved.up).cross(camera).normalize();
  const pageUp = camera.clone().cross(right).normalize();
  const screenRight = vector([1, 0, 0]).applyQuaternion(viewer.camera.quaternion);
  const screenUp = vector([0, 1, 0]).applyQuaternion(viewer.camera.quaternion);
  nearVector(right, screenRight, 'HLR page right');
  nearVector(pageUp, screenUp, 'HLR page up');
  for (const sample of [[4, -7, 2], [-6, 1, 9], [0, 0, 0]]) {
    const point = vector(sample);
    near(point.dot(right), point.dot(screenRight), 'HLR horizontal projection');
    near(point.dot(pageUp), point.dot(screenUp), 'HLR vertical projection');
  }
  Object.assign(step, saved);
  viewer.setAxisView('y', -1);
  viewer.resetView();
  nearVector(viewer.getView().camera, saved.camera, 'saved camera round trip');
  nearVector(viewer.getView().up, saved.up, 'saved up round trip');
});
