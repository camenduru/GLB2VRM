import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { VRMLoaderPlugin, VRMUtils } from '@pixiv/three-vrm';
import {
  createVRMAnimationClip,
  VRMAnimationLoaderPlugin,
} from '@pixiv/three-vrm-animation';

const $ = (id) => document.getElementById(id);

const SLOTS = [
  ['hips', 'Hips', true],
  ['spine', 'Spine', true],
  ['chest', 'Chest', false],
  ['upperChest', 'Upper chest', false],
  ['neck', 'Neck', false],
  ['head', 'Head', true],
  ['leftShoulder', 'Left shoulder', false],
  ['leftUpperArm', 'Left upper arm', true],
  ['leftLowerArm', 'Left lower arm', true],
  ['leftHand', 'Left hand', true],
  ['rightShoulder', 'Right shoulder', false],
  ['rightUpperArm', 'Right upper arm', true],
  ['rightLowerArm', 'Right lower arm', true],
  ['rightHand', 'Right hand', true],
  ['leftUpperLeg', 'Left upper leg', true],
  ['leftLowerLeg', 'Left lower leg', true],
  ['leftFoot', 'Left foot', true],
  ['leftToes', 'Left toes', false],
  ['rightUpperLeg', 'Right upper leg', true],
  ['rightLowerLeg', 'Right lower leg', true],
  ['rightFoot', 'Right foot', true],
  ['rightToes', 'Right toes', false],
];
const REQUIRED = SLOTS.filter((slot) => slot[2]).map((slot) => slot[0]);
const PHASE_PROGRESS = { queued: 4, blender: 16, pmx: 45, vrm: 76, check: 91 };
const LAST_JOB = 'glb2vrm.lastJob';

const state = {
  models: [],
  model: '',
  info: null,
  mapping: {},
  statusOk: false,
  busy: false,
  job: '',
  runId: 0,
  view: 'glb',
  bones: true,
  glbRoot: null,
  vrmRoot: null,
  glbHelper: null,
  vrmHelper: null,
  glbMarkers: [],
  vrmMarkers: [],
  vrm: null,
  titleAuto: '',
  result: null,
  animMixer: null,
  animAction: null,
  animClip: null,
  animName: '',
  animPlaying: false,
  animUiAt: 0,
};

/* ----------------------------------------------------------------- scene */
const canvas = $('scene');
const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
renderer.setPixelRatio(Math.min(2, window.devicePixelRatio || 1));
renderer.outputColorSpace = THREE.SRGBColorSpace;

const scene = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(32, 1, 0.05, 120);
camera.position.set(0, 1.5, 3.2);
camera.lookAt(0, 1.0, 0);

const controls = new OrbitControls(camera, canvas);
controls.target.set(0, 1.0, 0);
controls.enableDamping = true;
controls.dampingFactor = 0.08;
controls.minDistance = 0.4;
controls.maxDistance = 40;
controls.maxPolarAngle = Math.PI * 0.55;
controls.update();

scene.add(new THREE.HemisphereLight(0xbfe8ff, 0x10141c, 2.2));
const keyLight = new THREE.DirectionalLight(0xffffff, 2.4);
keyLight.position.set(1.8, 3.4, 2.6);
scene.add(keyLight);
const fillLight = new THREE.DirectionalLight(0x88aaff, 0.7);
fillLight.position.set(-2.4, 1.2, -1.8);
scene.add(fillLight);

const ground = new THREE.Mesh(
  new THREE.CircleGeometry(2.4, 72),
  new THREE.MeshStandardMaterial({ color: 0x11161f, roughness: 0.96, metalness: 0 }),
);
ground.rotation.x = -Math.PI / 2;
ground.position.y = -0.002;
scene.add(ground);

const glbHolder = new THREE.Group();
const vrmHolder = new THREE.Group();
scene.add(glbHolder, vrmHolder);

function resize() {
  const rect = canvas.getBoundingClientRect();
  const width = Math.max(320, rect.width);
  const height = Math.max(260, rect.height);
  renderer.setSize(width, height, false);
  camera.aspect = width / height;
  camera.updateProjectionMatrix();
}
window.addEventListener('resize', resize);
if (typeof ResizeObserver !== 'undefined') {
  new ResizeObserver(() => resize()).observe(canvas.parentElement);
}

function draw() {
  requestAnimationFrame(draw);
  const dt = Math.min(0.05, clock.getDelta());
  controls.update();
  if (state.vrm && state.vrm.scene.visible) {
    if (state.animMixer) {
      state.animMixer.update(dt);
      if (state.animPlaying && performance.now() - state.animUiAt > 100) {
        state.animUiAt = performance.now();
        const seek = $('anim-seek');
        if (seek) {
          seek.value = String(state.animAction?.time || 0);
          $('anim-time').textContent =
            `${formatTime(state.animAction?.time || 0)} / ${formatTime(state.animClip?.duration || 0)}`;
        }
      }
    }
    try { state.vrm.update(dt); } catch { /* spring bones can complain on no pose */ }
  }
  renderer.render(scene, camera);
}
const clock = new THREE.Clock();

function frame(root) {
  if (!root) return;
  const box = new THREE.Box3().setFromObject(root);
  if (box.isEmpty() || !Number.isFinite(box.max.x)) return;
  const size = box.getSize(new THREE.Vector3());
  const center = box.getCenter(new THREE.Vector3());
  const fov = THREE.MathUtils.degToRad(camera.fov);
  const heightDist = (size.y * 1.14) / (2 * Math.tan(fov / 2));
  const widthDist = (size.x * 1.14) / (2 * Math.tan(fov / 2) * Math.max(camera.aspect, 0.2));
  const distance = Math.max(heightDist, widthDist, 0.9) + size.z * 0.5;
  controls.target.set(center.x, center.y, center.z);
  camera.position.set(center.x, center.y, center.z + distance);
  camera.lookAt(center);
  controls.update();
  ground.position.y = box.min.y - 0.002;
  ground.scale.setScalar(Math.max(1, Math.max(size.x, size.z) / 2.4));
}

/* ------------------------------------------------------------------- ui */
function toast(message, error = false) {
  const phase = $('phase');
  phase.textContent = message;
  phase.classList.toggle('error', error);
}

function setStatus(text, ok) {
  $('status-text').textContent = text;
  const pill = $('status-pill');
  pill.classList.toggle('ok', ok === true);
  pill.classList.toggle('bad', ok === false);
}

async function api(url, options) {
  const response = await fetch(url, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  return data;
}

function prettyBytes(size) {
  if (!size) return '0 B';
  if (size >= 1 << 30) return `${(size / (1 << 30)).toFixed(2)} GB`;
  if (size >= 1 << 20) return `${(size / (1 << 20)).toFixed(1)} MB`;
  if (size >= 1 << 10) return `${(size / (1 << 10)).toFixed(1)} KB`;
  return `${size} B`;
}

function number(value) {
  return Number(value || 0).toLocaleString('en-US');
}

/* -------------------------------------------------------------- helpers */
function disposeObject(root) {
  if (!root) return;
  root.traverse((object) => {
    if (!object.isMesh) return;
    object.geometry?.dispose?.();
    const materials = Array.isArray(object.material) ? object.material : [object.material];
    for (const material of materials) {
      if (!material) continue;
      material.map?.dispose?.();
      material.dispose?.();
    }
  });
}

function disposeMarkers(list) {
  for (const dot of list) {
    dot.parent?.remove(dot);
    dot.geometry?.dispose?.();
    dot.material?.dispose?.();
  }
  list.length = 0;
}

function clearVRM() {
  clearAnimation();
  $('btn-vrma').disabled = true;
  disposeMarkers(state.vrmMarkers);
  if (state.vrmHelper) {
    state.vrmHelper.parent?.remove(state.vrmHelper);
    state.vrmHelper.geometry?.dispose?.();
    state.vrmHelper.material?.dispose?.();
    state.vrmHelper = null;
  }
  if (state.vrmRoot) {
    disposeObject(state.vrmRoot);
    state.vrmRoot.parent?.remove(state.vrmRoot);
    state.vrmRoot = null;
  }
  state.vrm = null;
  $('view-seg').querySelector('[data-v="vrm"]').disabled = true;
}

function setView(view) {
  state.view = view;
  if (view === 'vrm' && !state.vrmRoot) view = 'glb';
  glbHolder.visible = view === 'glb';
  vrmHolder.visible = view === 'vrm';
  for (const button of $('view-seg').querySelectorAll('button')) {
    button.classList.toggle('on', button.dataset.v === view);
  }
  frame(view === 'vrm' ? state.vrmRoot : state.glbRoot);
}

function setBones(enabled) {
  state.bones = enabled;
  $('btn-bones').classList.toggle('active', enabled);
  if (state.glbHelper) state.glbHelper.visible = enabled;
  if (state.vrmHelper) state.vrmHelper.visible = enabled;
  for (const dot of state.glbMarkers) dot.visible = enabled;
  for (const dot of state.vrmMarkers) dot.visible = enabled;
}

function addMarkers(helper, list, mapped = null, colorMapped = 0x4bf0b6) {
  disposeMarkers(list);
  if (!helper || !helper.bones) return;
  mapped = mapped || new Set(Object.values(state.mapping));
  const box = new THREE.Box3().setFromObject(helper.parent || helper);
  const size = box.isEmpty() ? 1 : Math.max(box.getSize(new THREE.Vector3()).y, 0.2);
  const radius = Math.max(0.0035, size * 0.0045);
  for (const bone of helper.bones) {
    if (!bone?.isBone) continue;
    const isMapped = mapped.has(bone.name);
    const dot = new THREE.Mesh(
      new THREE.SphereGeometry(isMapped ? radius * 1.6 : radius, 10, 8),
      new THREE.MeshBasicMaterial({
        color: isMapped ? colorMapped : 0x6b7a90,
        depthTest: false,
        transparent: true,
        opacity: isMapped ? 0.95 : 0.5,
      }),
    );
    dot.renderOrder = 999;
    dot.visible = state.bones;
    bone.add(dot);
    list.push(dot);
  }
}

function buildHelper(root) {
  const helper = new THREE.SkeletonHelper(root);
  // SkeletonHelper stores bone positions relative to `root` but takes
  // `root.matrixWorld` as its own matrix, which is only correct when the helper
  // is added to the scene. As a child of `root` that applies the root transform
  // twice, so a 180° turn (applyFacing / rotateVRM0) becomes 360° and the rig
  // stays facing the old way while the mesh turns around. Give the helper its
  // own identity local matrix instead; then it renders exactly at the bones.
  helper.matrixAutoUpdate = true;
  helper.matrix = new THREE.Matrix4();
  helper.material.depthTest = false;
  helper.material.transparent = true;
  helper.material.opacity = 0.85;
  helper.material.color.set(0x4bf0b6);
  helper.renderOrder = 998;
  helper.visible = state.bones;
  root.add(helper);
  return helper;
}

/* --------------------------------------------------------------- loading */
function parseGLB(buffer, plugin) {
  return new Promise((resolve, reject) => {
    const loader = new GLTFLoader();
    if (plugin) loader.register(plugin);
    loader.parse(buffer, '', resolve, reject);
  });
}

async function fetchModel(name) {
  // The server serves models with a one-hour cache; a re-upload gets a new
  // name, so a cached preview body is always the same file.
  const response = await fetch(`/api/models/${encodeURIComponent(name)}`);
  if (!response.ok) throw new Error(`could not download ${name}`);
  return response.arrayBuffer();
}

async function installGLB(buffer) {
  const gltf = await parseGLB(buffer);
  if (state.glbRoot) {
    disposeMarkers(state.glbMarkers);
    if (state.glbHelper) {
      state.glbHelper.geometry?.dispose?.();
      state.glbHelper.material?.dispose?.();
      state.glbHelper = null;
    }
    disposeObject(state.glbRoot);
    glbHolder.remove(state.glbRoot);
  }
  const root = gltf.scene;
  glbHolder.rotation.y = 0;
  root.traverse((object) => {
    if (object.isMesh) {
      object.castShadow = true;
      object.receiveShadow = true;
      object.frustumCulled = false;
    }
  });
  state.glbRoot = root;
  glbHolder.add(root);
  state.glbHelper = buildHelper(root);
  if (state.view === 'glb') {
    glbHolder.visible = true;
    vrmHolder.visible = false;
    frame(root);
  }
}

function vrmMappedBones(vrm) {
  const names = new Set();
  if (!vrm?.humanoid) return names;
  for (const slot of Object.keys(state.mapping)) {
    const node = vrm.humanoid.getRawBoneNode(slot) || vrm.humanoid.getNormalizedBoneNode(slot);
    if (node?.name) names.add(node.name);
  }
  return names;
}

async function loadVRM(url) {
  const response = await fetch(url, { cache: 'no-store' });
  if (!response.ok) throw new Error('could not download the VRM');
  const buffer = await response.arrayBuffer();
  const gltf = await parseGLB(buffer, (parser) => new VRMLoaderPlugin(parser));
  const vrm = gltf.userData.vrm;
  if (!vrm) throw new Error('the exported file is not a VRM');
  try { VRMUtils.removeUnnecessaryVertices(gltf.scene); } catch { /* older three-vrm */ }
  try { VRMUtils.combineSkeletons(gltf.scene); } catch { /* optional */ }
  try { VRMUtils.rotateVRM0(vrm); } catch { /* VRM 1.0 needs none */ }
  vrm.scene.traverse((object) => {
    if (object.isMesh) {
      object.castShadow = true;
      object.receiveShadow = true;
      object.frustumCulled = false;
    }
  });

  clearVRM();
  state.vrm = vrm;
  const root = vrm.scene;
  state.vrmRoot = root;
  vrmHolder.add(root);
  state.vrmHelper = buildHelper(root);
  addMarkers(state.vrmHelper, state.vrmMarkers, vrmMappedBones(vrm));
  $('view-seg').querySelector('[data-v="vrm"]').disabled = false;
  $('btn-vrma').disabled = false;
  updateAnimationUI();
  setView('vrm');
}

/* ------------------------------------------------------------ VRM Animation */
function formatTime(seconds) {
  const value = Math.max(0, Number(seconds) || 0);
  const minutes = Math.floor(value / 60);
  const rest = Math.floor(value % 60);
  return `${minutes}:${String(rest).padStart(2, '0')}`;
}

function updateAnimationUI() {
  const bar = $('anim');
  if (!bar) return;
  if (!state.animAction) {
    bar.hidden = true;
    return;
  }
  const duration = state.animClip?.duration || 0;
  bar.hidden = false;
  const seek = $('anim-seek');
  seek.max = String(Math.max(0.01, duration));
  seek.value = String(state.animAction.time || 0);
  $('anim-name').textContent = state.animName || 'animation';
  $('anim-name').title = state.animName || '';
  $('anim-time').textContent =
    `${formatTime(state.animAction.time)} / ${formatTime(duration)}`;
  $('btn-anim-play').textContent = state.animPlaying ? 'Pause' : 'Play';
}

function clearAnimation() {
  if (state.animMixer) {
    state.animMixer.stopAllAction();
    const root = state.vrm?.scene;
    if (root) state.animMixer.uncacheRoot(root);
  }
  state.animMixer = null;
  state.animAction = null;
  state.animClip = null;
  state.animName = '';
  state.animPlaying = false;
  try {
    state.vrm?.humanoid?.resetNormalizedPose?.();
    state.vrm?.update(0);
  } catch { /* the pose reset is best effort */ }
  updateAnimationUI();
}

async function loadVRMAFile(file) {
  if (!file) return;
  if (!/\.vrma$/i.test(file.name)) return toast('choose a .vrma file', true);
  if (!state.vrm) return toast('convert a VRM first', true);
  try {
    const buffer = await file.arrayBuffer();
    const gltf = await parseGLB(buffer, (parser) => new VRMAnimationLoaderPlugin(parser));
    const animation = gltf.userData.vrmAnimations?.[0];
    if (!animation) throw new Error('not a VRM Animation file');
    clearAnimation();
    const clip = createVRMAnimationClip(animation, state.vrm);
    const mixer = new THREE.AnimationMixer(state.vrm.scene);
    const action = mixer.clipAction(clip);
    action.setLoop(THREE.LoopRepeat, Infinity);
    action.clampWhenFinished = false;
    action.play();
    state.animMixer = mixer;
    state.animAction = action;
    state.animClip = clip;
    state.animName = file.name;
    state.animPlaying = true;
    state.animUiAt = 0;
    setView('vrm');
    updateAnimationUI();
    toast(`playing ${file.name}`);
  } catch (err) {
    toast(`could not play the animation: ${err.message}`, true);
  }
}

function toggleAnimation() {
  if (!state.animAction) return;
  state.animPlaying = !state.animPlaying;
  state.animAction.paused = !state.animPlaying;
  updateAnimationUI();
}

function seekAnimation(value) {
  if (!state.animAction) return;
  const time = Number(value) || 0;
  state.animAction.time = Math.min(time, state.animClip?.duration || time);
  state.animMixer?.update(0);
  updateAnimationUI();
}

/* ---------------------------------------------------------------- models */
function highlightModel() {
  for (const button of $('models').querySelectorAll('.clip')) {
    button.classList.toggle('active', button.dataset.name === state.model);
  }
}

function renderModels() {
  const list = $('models');
  list.innerHTML = '';
  for (const model of state.models) {
    const item = document.createElement('li');
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'clip';
    button.dataset.name = model.name;
    button.textContent = model.name;
    button.title = `${model.name} · ${model.bytes_human}`;
    button.classList.toggle('active', model.name === state.model);
    button.addEventListener('click', () => {
      if (model.name !== state.model) selectModel(model.name);
    });
    item.append(button);
    list.append(item);
  }
}

async function refreshModels() {
  const data = await api('/api/models');
  state.models = data.models || [];
  renderModels();
}

function renderInfo(info) {
  $('info').hidden = false;
  $('info-name').textContent = info.name;
  $('info-size').textContent = info.bytes_human || prettyBytes(info.stats.bytes);
  const stats = info.stats;
  const cells = [
    ['Vertices', number(stats.vertices)],
    ['Triangles', number(stats.triangles)],
    ['Bones', number(stats.joints)],
    ['Materials', number(stats.materials)],
    ['Textures', number(stats.images)],
    ['Morph targets', number(stats.morph_targets)],
    ['Animations', number(stats.animations)],
    ['Height', info.bounds ? `${Number(info.bounds.height).toFixed(2)} m` : '—'],
  ];
  const grid = $('info-grid');
  grid.innerHTML = '';
  for (const [key, value] of cells) {
    const row = document.createElement('div');
    const k = document.createElement('span');
    k.className = 'k';
    k.textContent = key;
    const v = document.createElement('span');
    v.className = 'v';
    v.textContent = value;
    row.append(k, v);
    grid.append(row);
  }

  const warn = $('info-warn');
  const messages = [];
  if (info.is_vrm) messages.push('This file is already a VRM — converting it again is usually unnecessary.');
  if (!info.skeleton) messages.push('No skin/bones found: a VRM avatar needs a rigged skeleton.');
  else if (!stats.skinned) messages.push('The mesh is not skinned to the skeleton; the VRM may not deform.');
  warn.hidden = messages.length === 0;
  warn.textContent = messages.join(' ');
}

function updateMapSummary() {
  const mapped = SLOTS.filter(([slot]) => state.mapping[slot]).length;
  const missing = REQUIRED.filter((slot) => !state.mapping[slot]);
  const counts = {};
  for (const [slot] of SLOTS) {
    const bone = state.mapping[slot];
    if (bone) counts[bone] = (counts[bone] || 0) + 1;
  }
  const duplicates = Object.entries(counts).filter(([, n]) => n > 1).map(([bone]) => bone);
  const count = $('map-count');
  count.textContent = `${mapped}/${SLOTS.length}`;
  count.classList.toggle('ok', missing.length === 0 && duplicates.length === 0);
  count.classList.toggle('bad', missing.length > 0 || duplicates.length > 0);

  const note = $('map-note');
  const suggested = state.info?.suggested;
  if (suggested?.warnings?.length) {
    note.textContent = suggested.warnings[suggested.warnings.length - 1];
    note.className = 'note warn';
  } else if (missing.length) {
    note.textContent = `Required bones not mapped: ${missing.map((slot) => {
      const row = SLOTS.find(([name]) => name === slot);
      return row ? row[1] : slot;
    }).join(', ')}`;
    note.className = 'note warn';
  } else if (duplicates.length) {
    note.textContent = `The same bone is used twice: ${duplicates.join(', ')}`;
    note.className = 'note warn';
  } else {
    note.textContent = suggested?.method === 'geometry'
      ? 'Matched from the skeleton shape — check the sides before converting.'
      : 'Matched from the bone names.';
    note.className = 'note';
  }

  updateRunButton();
}

function renderMapping() {
  const list = $('map-list');
  const scroll = list.scrollTop;
  list.innerHTML = '';
  const bones = state.info?.skeleton?.joints?.map((joint) => joint.name) || [];
  const counts = {};
  for (const [slot] of SLOTS) {
    const bone = state.mapping[slot];
    if (bone) counts[bone] = (counts[bone] || 0) + 1;
  }
  for (const [slot, label, required] of SLOTS) {
    const row = document.createElement('div');
    row.className = 'maprow';
    row.dataset.slot = slot;
    const labelEl = document.createElement('label');
    labelEl.textContent = label;
    if (required) {
      const mark = document.createElement('span');
      mark.className = 'req';
      mark.textContent = '*';
      mark.title = 'Required by VRM 1.0';
      labelEl.append(mark);
    }
    const select = document.createElement('select');
    const none = document.createElement('option');
    none.value = '';
    none.textContent = '—';
    select.append(none);
    for (const bone of bones) {
      const option = document.createElement('option');
      option.value = bone;
      option.textContent = bone;
      select.append(option);
    }
    select.value = state.mapping[slot] || '';
    select.disabled = !bones.length;
    select.addEventListener('change', () => {
      if (select.value) state.mapping[slot] = select.value;
      else delete state.mapping[slot];
      applyFacing();
      addMarkers(state.glbHelper, state.glbMarkers);
      renderMapping();
      updateMapSummary();
      if (state.view === 'glb') frame(state.glbRoot);
    });
    row.append(labelEl, select);
    if (required && !state.mapping[slot]) row.classList.add('missing');
    if (state.mapping[slot] && counts[state.mapping[slot]] > 1) row.classList.add('dupe');
    list.append(row);
  }
  list.scrollTop = scroll;
}

/* Raw glTF models often face -Z (glTF's front is +Z, VRM's too). Turn the
   preview around so the first look at the avatar is its face; the conversion
   itself is untouched and lets the converter decide the orientation. */
function applyFacing() {
  const joints = state.info?.skeleton?.joints;
  if (!joints) return;
  const byName = new Map(joints.map((joint) => [joint.name, joint]));
  let fx = 0;
  let fz = 0;
  for (const side of ['left', 'right']) {
    const toe = byName.get(state.mapping[`${side}Toes`]);
    const foot = byName.get(state.mapping[`${side}Foot`]);
    if (!toe || !foot) continue;
    fx += toe.pos[0] - foot.pos[0];
    fz += toe.pos[2] - foot.pos[2];
  }
  const turn = Math.hypot(fx, fz) > 1e-6 && fz < 0 ? Math.PI : 0;
  glbHolder.rotation.y = turn;
}

function applyMapping(mapping) {
  state.mapping = {};
  for (const [slot] of SLOTS) {
    const bone = mapping?.[slot];
    if (bone) state.mapping[slot] = bone;
  }
  renderMapping();
  updateMapSummary();
  applyFacing();
  addMarkers(state.glbHelper, state.glbMarkers);
  if (state.view === 'glb') frame(state.glbRoot);
}

async function selectModel(name, preloaded = null) {
  if (!name) return;
  if (state.busy) {
    toast('wait for the running conversion to finish', true);
    return;
  }
  state.model = name;
  highlightModel();
  try {
    const saved = JSON.parse(localStorage.getItem(LAST_JOB) || 'null');
    if (saved && saved.model !== name) localStorage.removeItem(LAST_JOB);
  } catch { /* ignore a broken entry */ }
  resetResult();
  clearVRM();
  state.view = 'glb';
  glbHolder.visible = true;
  vrmHolder.visible = false;
  toast('reading the model…');

  const titleInput = $('opt-title');
  const stem = name.replace(/\.[^.]+$/, '');
  if (!titleInput.value || titleInput.value === state.titleAuto) titleInput.value = stem;
  state.titleAuto = stem;

  try {
    const [info, buffer] = await Promise.all([
      preloaded || api(`/api/models/${encodeURIComponent(name)}/info`),
      fetchModel(name),
    ]);
    state.info = info;
    renderInfo(info);
    await installGLB(buffer);
    applyMapping(info.suggested?.mapping);
    state.statusOk ? toast('ready to convert') : toast('Blender was not found', true);
  } catch (err) {
    toast(`could not open the model: ${err.message}`, true);
  }
  updateRunButton();
}

async function upload(file) {
  if (!file) return;
  if (state.busy) return toast('wait for the running conversion to finish', true);
  if (!/\.(glb|vrm)$/i.test(file.name)) {
    return toast('choose a .glb file', true);
  }
  toast(`uploading ${file.name}…`);
  try {
    const data = await api(`/api/models?name=${encodeURIComponent(file.name)}`, {
      method: 'POST',
      body: file,
    });
    await refreshModels();
    await selectModel(data.info.name, data.info);
  } catch (err) {
    toast(`upload failed: ${err.message}`, true);
  }
}

/* ---------------------------------------------------------------- convert */
function updateRunButton() {
  const missing = REQUIRED.filter((slot) => !state.mapping[slot]);
  const counts = {};
  for (const [slot] of SLOTS) {
    const bone = state.mapping[slot];
    if (bone) counts[bone] = (counts[bone] || 0) + 1;
  }
  const dupes = Object.values(counts).some((n) => n > 1);
  const ready = Boolean(state.model && state.info?.skeleton && state.statusOk
    && !state.busy && missing.length === 0 && !dupes);
  $('btn-run').disabled = !ready;
  $('btn-run').textContent = state.busy ? 'Converting…' : 'Convert to VRM';
  const vrmaButton = $('btn-vrma');
  if (vrmaButton) vrmaButton.disabled = state.busy || !state.vrm;
}

function resetResult() {
  state.result = null;
  $('result').hidden = true;
  $('log-box').hidden = true;
  $('log').textContent = '';
  $('bar').style.width = '0%';
}

function showResult(result) {
  state.result = result;
  const summary = result.summary || {};
  $('result').hidden = false;
  $('result-title').textContent = 'VRM ready';
  const parts = [summary.spec ? `VRM ${summary.spec}` : 'VRM'];
  if (summary.bones) parts.push(`${summary.bones} bones`);
  if (result.bytes_human) parts.push(result.bytes_human);
  if (summary.morphs) parts.push(`${summary.morphs} morphs`);
  if (summary.springs) parts.push(`${summary.springs} spring chains`);
  if (result.valid === true) parts.push('validated');
  if (result.valid === false) parts.push('validation warning');
  $('result-meta').textContent = parts.join(' · ');
  const link = $('download');
  link.href = `${result.url}?download=1`;
  link.setAttribute('download', result.name || 'avatar.vrm');
}

function setProgress(phase, done) {
  const value = done ? 100 : (PHASE_PROGRESS[phase] || 6);
  $('bar').style.width = `${value}%`;
}

async function poll(runId) {
  if (runId !== state.runId) return;
  let job;
  try {
    job = await api(`/api/jobs/${encodeURIComponent(state.job)}`);
  } catch (err) {
    state.busy = false;
    updateRunButton();
    return toast(`lost the conversion: ${err.message}`, true);
  }
  setProgress(job.phase, job.state === 'done');
  if (job.message) toast(job.message, job.state === 'error');
  if (job.log?.length) {
    $('log-box').hidden = false;
    $('log').textContent = job.log.join('\n');
  }
  if (job.state === 'done') {
    state.busy = false;
    updateRunButton();
    toast('VRM ready — download it on the right');
    showResult(job.result);
    try {
      localStorage.setItem(LAST_JOB, JSON.stringify({ id: state.job, model: state.model }));
    } catch { /* private mode */ }
    try {
      await loadVRM(job.result.url);
      toast('VRM ready — drag to orbit');
    } catch (err) {
      toast(`VRM preview failed: ${err.message}`, true);
    }
    return;
  }
  if (job.state === 'error') {
    state.busy = false;
    updateRunButton();
    $('log-box').open = true;
    toast(job.error || 'conversion failed', true);
    return;
  }
  setTimeout(() => poll(runId), 700);
}

/* A conversion outlives a page reload (the job keeps running on the server).
   Reconnect to the last job for this model and show its VRM when it is done. */
async function restoreJob() {
  let saved = null;
  try {
    saved = JSON.parse(localStorage.getItem(LAST_JOB) || 'null');
  } catch { return; }
  if (!saved?.id || saved.model !== state.model) return;
  let job;
  try {
    job = await api(`/api/jobs/${encodeURIComponent(saved.id)}`);
  } catch {
    try { localStorage.removeItem(LAST_JOB); } catch { /* ignore */ }
    return;
  }
  if (job.state === 'done') {
    showResult(job.result);
    toast('restored the last VRM');
    try {
      await loadVRM(job.result.url);
      toast('VRM ready — drag to orbit');
    } catch (err) {
      toast(`VRM preview failed: ${err.message}`, true);
    }
  } else if (job.state === 'running' || job.state === 'queued') {
    state.job = saved.id;
    state.busy = true;
    state.runId += 1;
    setProgress(job.phase);
    updateRunButton();
    toast(job.message || 'reconnecting to the conversion');
    poll(state.runId);
  }
}

async function convert() {
  if (state.busy) return;
  if (!state.model) return toast('pick a model first', true);
  if (!state.statusOk) return toast('Blender is required for the conversion', true);
  state.runId += 1;
  const runId = state.runId;
  try { localStorage.removeItem(LAST_JOB); } catch { /* ignore */ }
  resetResult();
  state.busy = true;
  updateRunButton();
  setProgress('queued');
  toast('starting the conversion');
  const options = {
    spec: $('spec-seg').querySelector('.on')?.dataset.v || 'vrm1',
    tpose: $('opt-tpose').checked,
    spring: $('opt-spring').checked,
    tex_max: Number($('opt-tex').value || 0),
    tex_jpeg: $('opt-jpeg').checked,
    face180: $('opt-face180').checked,
    title: $('opt-title').value.trim(),
    author: $('opt-author').value.trim(),
  };
  try {
    const data = await api('/api/convert', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model: state.model, mapping: state.mapping, options }),
    });
    state.job = data.id;
    poll(runId);
  } catch (err) {
    state.busy = false;
    updateRunButton();
    setProgress('queued');
    toast(`conversion failed: ${err.message}`, true);
  }
}

/* ----------------------------------------------------------------- wiring */
$('btn-add').addEventListener('click', () => $('file').click());
$('btn-remove').addEventListener('click', async () => {
  if (!state.model) return;
  if (state.busy) return toast('wait for the running conversion to finish', true);
  const name = state.model;
  try {
    await api('/api/clear', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model: name }),
    });
  } catch { /* the file may already be gone */ }
  state.model = '';
  state.info = null;
  state.mapping = {};
  try { localStorage.removeItem(LAST_JOB); } catch { /* ignore */ }
  $('info').hidden = true;
  $('map-list').innerHTML = '';
  $('map-count').textContent = '0/22';
  clearVRM();
  if (state.glbRoot) {
    disposeMarkers(state.glbMarkers);
    if (state.glbHelper) {
      state.glbHelper.geometry?.dispose?.();
      state.glbHelper.material?.dispose?.();
      state.glbHelper = null;
    }
    disposeObject(state.glbRoot);
    glbHolder.remove(state.glbRoot);
    state.glbRoot = null;
  }
  resetResult();
  await refreshModels();
  updateRunButton();
  toast('model removed');
});

$('file').addEventListener('change', () => {
  const file = $('file').files?.[0];
  $('file').value = '';
  upload(file);
});

const drop = $('drop');
drop.addEventListener('click', () => $('file').click());
for (const event of ['dragenter', 'dragover']) {
  drop.addEventListener(event, (e) => {
    e.preventDefault();
    drop.classList.add('over');
  });
}
for (const event of ['dragleave', 'drop']) {
  drop.addEventListener(event, (e) => {
    e.preventDefault();
    drop.classList.remove('over');
  });
}
drop.addEventListener('drop', (e) => {
  const file = [...(e.dataTransfer?.files || [])].find((f) => /\.(glb|vrm)$/i.test(f.name));
  upload(file);
});
window.addEventListener('dragover', (e) => e.preventDefault());
window.addEventListener('drop', (e) => e.preventDefault());

$('btn-vrma').addEventListener('click', () => $('vrma-file').click());
$('vrma-file').addEventListener('change', () => {
  const file = $('vrma-file').files?.[0];
  $('vrma-file').value = '';
  loadVRMAFile(file);
});
$('btn-anim-play').addEventListener('click', () => {
  toggleAnimation();
  toast(state.animPlaying ? 'animation playing' : 'animation paused');
});
$('btn-anim-clear').addEventListener('click', () => {
  clearAnimation();
  toast('animation cleared');
});
$('anim-seek').addEventListener('input', (event) => seekAnimation(event.target.value));

const stage = $('stage');
const stageHint = $('stage-hint');
for (const event of ['dragenter', 'dragover']) {
  stage.addEventListener(event, (e) => {
    e.preventDefault();
    stageHint.hidden = false;
  });
}
stage.addEventListener('dragleave', (e) => {
  if (!e.relatedTarget || !stage.contains(e.relatedTarget)) stageHint.hidden = true;
});
stage.addEventListener('drop', (e) => {
  e.preventDefault();
  stageHint.hidden = true;
  const files = [...(e.dataTransfer?.files || [])];
  const animation = files.find((file) => /\.vrma$/i.test(file.name));
  if (animation) return loadVRMAFile(animation);
  const model = files.find((file) => /\.(glb|vrm)$/i.test(file.name));
  if (model) return upload(model);
});

$('btn-bones').addEventListener('click', () => setBones(!state.bones));
for (const button of $('view-seg').querySelectorAll('button')) {
  button.addEventListener('click', () => {
    if (!button.disabled) setView(button.dataset.v);
  });
}
for (const button of $('spec-seg').querySelectorAll('button')) {
  button.addEventListener('click', () => {
    for (const other of $('spec-seg').querySelectorAll('button')) other.classList.remove('on');
    button.classList.add('on');
  });
}
$('btn-automap').addEventListener('click', (e) => {
  e.preventDefault();
  e.stopPropagation();
  if (state.info?.suggested) applyMapping(state.info.suggested.mapping);
  toast('mapping reset to the automatic result');
});
$('btn-run').addEventListener('click', convert);

/* ------------------------------------------------------------------- boot */
state.camera = camera;
state.controls = controls;
window.__glb2vrm = state;

async function boot() {
  resize();
  draw();
  try {
    const data = await api('/api/status');
    state.statusOk = Boolean(data.status.ok);
    if (data.status.ok) {
      const version = data.status.blender?.version || '';
      setStatus(`Blender ${version} ready`.trim(), true);
    } else {
      setStatus(data.status.reason, false);
    }
  } catch {
    state.statusOk = false;
    setStatus('server unreachable', false);
  }
  try {
    await refreshModels();
    if (state.models.length) {
      await selectModel(state.models[0].name);
      await restoreJob();
    } else {
      toast('drop a rigged GLB to begin');
    }
  } catch (err) {
    toast(`could not list models: ${err.message}`, true);
  }
  updateRunButton();
}

boot();
