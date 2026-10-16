<template>
  <Teleport to="body" :disabled="!fullscreen">
    <section ref="panel" class="mind-map" :class="{ 'is-fullscreen': fullscreen }" :role="fullscreen ? 'dialog' : 'region'" :aria-modal="(fullscreen && !sourceDialogOpen) || undefined" aria-label="章节思维导图" tabindex="-1">
      <div class="map-toolbar">
        <div class="view-switch" aria-label="导图展示方式">
          <button type="button" :aria-pressed="mode === 'map'" @click="setMode('map')"><Network :size="15" />导图</button>
          <button type="button" :aria-pressed="mode === 'outline'" @click="setMode('outline')"><ListTree :size="15" />大纲</button>
        </div>
        <button ref="fullscreenButton" type="button" class="tool-button fullscreen-button" @click="toggleFullscreen"><Minimize2 v-if="fullscreen" :size="16" /><Maximize2 v-else :size="16" />{{ fullscreen ? '退出全屏' : '全屏查看' }}</button>
        <div class="map-search">
          <Search :size="15" aria-hidden="true" />
          <input v-model="query" type="search" placeholder="搜索知识点" aria-label="搜索导图知识点" @keydown.enter.prevent="navigateMatch(1)" />
          <button v-if="query" type="button" class="icon-button" aria-label="清空导图搜索" @click="query = ''"><X :size="14" /></button>
        </div>
        <div v-if="query" class="search-navigation">
          <span aria-live="polite">{{ matches.length ? `${matchIndex + 1} / ${matches.length}` : '未找到' }}</span>
          <button type="button" class="icon-button" aria-label="上一个匹配节点" :disabled="!matches.length" @click="navigateMatch(-1)"><ChevronUp :size="15" /></button>
          <button type="button" class="icon-button" aria-label="下一个匹配节点" :disabled="!matches.length" @click="navigateMatch(1)"><ChevronDown :size="15" /></button>
        </div>
        <div class="canvas-tools">
          <template v-if="mode === 'map'">
            <button type="button" class="icon-button" aria-label="缩小导图" :disabled="zoom <= MIN_ZOOM" @click="changeZoom(zoom - .15)"><Minus :size="16" /></button>
            <button type="button" class="tool-button zoom-value" aria-label="恢复导图为 100%" @click="changeZoom(1)">{{ Math.round(zoom * 100) }}%</button>
            <button type="button" class="icon-button" aria-label="放大导图" :disabled="zoom >= MAX_ZOOM" @click="changeZoom(zoom + .15)"><Plus :size="16" /></button>
            <button type="button" class="tool-button" @click="fitCanvas"><Scan :size="15" />适应画布</button>
          </template>
          <button type="button" class="tool-button" :disabled="!expandableBranches.length" @click="toggleAll"><ChevronsUpDown :size="15" />{{ allCollapsed ? '全部展开' : '全部收起' }}</button>
        </div>
      </div>
      <div class="map-body" :class="{ 'has-detail': !!$slots.detail }">
        <div class="map-main">
          <div ref="viewport" class="map-viewport" :class="{ 'outline-viewport': mode === 'outline', 'is-dragging': dragging }" :aria-label="mode === 'map' ? '可滚动的思维导图画布，按住空白处拖动' : '章节知识点大纲'" tabindex="0" @pointerdown="startDrag" @pointermove="moveDrag" @pointerup="endDrag" @pointercancel="endDrag" @lostpointercapture="endDrag" @wheel="handleWheel">
            <div class="map-spacer" :style="mode === 'map' ? { width: `${worldSize.width * zoom}px`, height: `${worldSize.height * zoom}px` } : undefined">
              <div ref="world" class="map-world" :class="{ 'outline-world': mode === 'outline' }" :style="mode === 'map' ? { transform: `scale(${zoom})`, marginLeft: `${Math.max(0, (viewportWidth - worldSize.width * zoom) / 2)}px` } : undefined">
                <svg v-if="mode === 'map'" class="map-connections" :width="worldSize.width" :height="worldSize.height" aria-hidden="true"><path v-for="(path, index) in connections" :key="index" :d="path" /></svg>
                <div data-node-path="root" class="node-group root-group"><button type="button" class="map-node root-node" :class="nodeClasses('root')" :aria-pressed="selectedPath === 'root'" @click="selectNode(nodes[0]!)">{{ map.title }}</button></div>
                <ul v-if="map.children.length" class="branch-list">
                  <li v-for="(branch, index) in map.children" :key="`b${index}`" class="branch-row">
                    <div :data-node-path="`b${index}`" class="node-group branch-group">
                      <button type="button" class="map-node branch-node" :class="nodeClasses(`b${index}`)" :aria-pressed="selectedPath === `b${index}`" @click="selectNode({ path: `b${index}`, title: branch.title, branch: index })">
                        <span>{{ branch.title }}</span><span v-if="hasBadge(branch.title)" class="node-badges"><span v-if="examConcepts.includes(branch.title)" class="exam-badge">真题已考</span><span v-if="managementConcepts.includes(branch.title)" class="management-badge">管理学关联</span></span>
                      </button>
                      <button v-if="branch.children.length" type="button" class="branch-toggle" :aria-label="`${collapsed.has(index) ? '展开' : '收起'}分支：${branch.title}`" :aria-expanded="!collapsed.has(index)" @click="toggleBranch(index)"><ChevronRight v-if="collapsed.has(index)" :size="16" /><ChevronDown v-else :size="16" /><span v-if="collapsed.has(index)" class="collapsed-count">{{ branch.children.length }}</span></button>
                    </div>
                    <ul v-if="branch.children.length && !collapsed.has(index)" class="leaf-list">
                      <li v-for="(leaf, leafIndex) in branch.children" :key="`b${index}-l${leafIndex}`" class="node-group leaf-group" :data-node-path="`b${index}-l${leafIndex}`">
                        <button type="button" class="map-node leaf-node" :class="nodeClasses(`b${index}-l${leafIndex}`)" :aria-pressed="selectedPath === `b${index}-l${leafIndex}`" @click="selectNode({ path: `b${index}-l${leafIndex}`, title: leaf.title, branch: index })"><span>{{ leaf.title }}</span><span v-if="hasBadge(leaf.title)" class="node-badges"><span v-if="examConcepts.includes(leaf.title)" class="exam-badge">真题已考</span><span v-if="managementConcepts.includes(leaf.title)" class="management-badge">管理学关联</span></span></button>
                      </li>
                    </ul>
                  </li>
                </ul>
              </div>
            </div>
          </div>
          <p class="map-help">{{ mode === 'map' ? '拖动空白处平移 · Ctrl + 滚轮缩放 · 点击节点查看详情' : '点击节点查看详情，箭头展开或收起分支' }}</p>
        </div>
        <aside v-if="$slots.detail" class="map-detail" aria-label="知识点详情"><slot v-if="selectedPath" name="detail" /><div v-else class="detail-empty"><MousePointer2 :size="25" /><p>点击知识点</p><span>在这里查看概念、背诵要点和原文来源</span></div></aside>
      </div>
    </section>
  </Teleport>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { ChevronDown, ChevronRight, ChevronUp, ChevronsUpDown, ListTree, Maximize2, Minimize2, Minus, MousePointer2, Network, Plus, Scan, Search, X } from 'lucide-vue-next';
import type { LibraryMindMap } from '../types/library';
const props = withDefaults(defineProps<{ map: LibraryMindMap; examConcepts?: string[]; managementConcepts?: string[]; selectedTitle?: string; selectedNodePath?: string; sourceDialogOpen?: boolean }>(), { examConcepts: () => [], managementConcepts: () => [], selectedTitle: '', selectedNodePath: '', sourceDialogOpen: false });
const emit = defineEmits<{ select: [title: string, path: string] }>();
type NodeEntry = { path: string; title: string; branch?: number };
const MIN_ZOOM = .2;
const MAX_ZOOM = 2;
const panel = ref<HTMLElement>();
const viewport = ref<HTMLElement>();
const world = ref<HTMLElement>();
const fullscreenButton = ref<HTMLButtonElement>();
const mode = ref<'map' | 'outline'>(typeof window !== 'undefined' && window.matchMedia('(max-width: 640px)').matches ? 'outline' : 'map');
const fullscreen = ref(false);
const zoom = ref(1);
const collapsed = ref(new Set<number>());
const query = ref('');
const matchIndex = ref(0);
const selectedPath = ref('');
const connections = ref<string[]>([]);
const worldSize = ref({ width: 0, height: 0 });
const viewportWidth = ref(0);
const dragging = ref(false);
let resizeObserver: ResizeObserver | undefined;
let measureFrame = 0;
let previousBodyOverflow = '';
let focusBeforeFullscreen: HTMLElement | null = null;
let drag: { pointer: number; x: number; y: number; left: number; top: number } | undefined;
const nodes = computed<NodeEntry[]>(() => [{ path: 'root', title: props.map.title }, ...props.map.children.flatMap((branch, index) => [{ path: `b${index}`, title: branch.title, branch: index }, ...branch.children.map((leaf, leafIndex) => ({ path: `b${index}-l${leafIndex}`, title: leaf.title, branch: index }))])]);
const matches = computed(() => { const text = query.value.trim().toLocaleLowerCase(); return text ? nodes.value.filter(node => node.title.toLocaleLowerCase().includes(text)) : []; });
const expandableBranches = computed(() => props.map.children.flatMap((branch, index) => branch.children.length ? [index] : []));
const allCollapsed = computed(() => expandableBranches.value.length > 0 && expandableBranches.value.every(index => collapsed.value.has(index)));
const matchPaths = computed(() => new Set(matches.value.map(node => node.path)));
function hasBadge(title: string) { return props.examConcepts.includes(title) || props.managementConcepts.includes(title); }
function nodeClasses(path: string) { return { 'is-selected': selectedPath.value === path, 'is-match': matchPaths.value.has(path), 'is-current-match': matches.value[matchIndex.value]?.path === path }; }
function selectNode(node: NodeEntry) { selectedPath.value = node.path; emit('select', node.title, node.path); }
function toggleBranch(index: number) { const next = new Set(collapsed.value); if (next.has(index)) next.delete(index); else next.add(index); collapsed.value = next; }
function toggleAll() { collapsed.value = allCollapsed.value ? new Set() : new Set(expandableBranches.value); }
function scheduleMeasure() { cancelAnimationFrame(measureFrame); measureFrame = requestAnimationFrame(measureWorld); }
function measureWorld() {
  if (!world.value || mode.value !== 'map') return;
  viewportWidth.value = viewport.value?.clientWidth ?? 0;
  const size = { width: world.value.offsetWidth, height: world.value.offsetHeight };
  if (size.width !== worldSize.value.width || size.height !== worldSize.value.height) worldSize.value = size;
  const origin = world.value.getBoundingClientRect();
  const elements = new Map(Array.from(world.value.querySelectorAll<HTMLElement>('[data-node-path]')).map(element => [element.dataset.nodePath!, element]));
  const link = (from: string, to: string) => {
    const start = elements.get(from)?.getBoundingClientRect();
    const end = elements.get(to)?.getBoundingClientRect();
    if (!start || !end) return '';
    const x1 = (start.right - origin.left) / zoom.value;
    const y1 = (start.top + start.height / 2 - origin.top) / zoom.value;
    const x2 = (end.left - origin.left) / zoom.value;
    const y2 = (end.top + end.height / 2 - origin.top) / zoom.value;
    const middle = (x1 + x2) / 2;
    return `M ${x1} ${y1} C ${middle} ${y1}, ${middle} ${y2}, ${x2} ${y2}`;
  };
  connections.value = props.map.children.flatMap((branch, index) => [link('root', `b${index}`), ...(!collapsed.value.has(index) ? branch.children.map((_, leafIndex) => link(`b${index}`, `b${index}-l${leafIndex}`)) : [])]).filter(Boolean);
}
async function showRoot() {
  await nextTick();
  measureWorld();
  await nextTick();
  if (mode.value === 'map' && viewport.value) viewport.value.scrollTo({ left: 0, top: Math.max(0, (worldSize.value.height * zoom.value - viewport.value.clientHeight) / 2) });
  else viewport.value?.scrollTo({ left: 0, top: 0 });
}
async function setMode(value: 'map' | 'outline') { mode.value = value; await showRoot(); scheduleMeasure(); }
async function changeZoom(value: number, anchor?: { x: number; y: number }) {
  const target = viewport.value;
  const canvas = world.value;
  if (!target || !canvas) return;
  const oldZoom = zoom.value;
  const nextZoom = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, value));
  const viewRect = target.getBoundingClientRect();
  const before = canvas.getBoundingClientRect();
  const x = viewRect.left + target.clientLeft + (anchor?.x ?? target.clientWidth / 2);
  const y = viewRect.top + target.clientTop + (anchor?.y ?? target.clientHeight / 2);
  const contentX = (x - before.left) / oldZoom;
  const contentY = (y - before.top) / oldZoom;
  zoom.value = nextZoom;
  await nextTick();
  const after = canvas.getBoundingClientRect();
  target.scrollLeft += after.left + contentX * nextZoom - x;
  target.scrollTop += after.top + contentY * nextZoom - y;
  scheduleMeasure();
}
async function fitCanvas() {
  if (!viewport.value || !world.value) return;
  measureWorld();
  zoom.value = Math.min(1, Math.max(MIN_ZOOM, Math.min((viewport.value.clientWidth - 16) / worldSize.value.width, (viewport.value.clientHeight - 16) / worldSize.value.height)));
  await nextTick();
  viewport.value.scrollTo({ left: 0, top: 0 });
  scheduleMeasure();
}
function handleWheel(event: WheelEvent) {
  if (mode.value !== 'map' || !event.ctrlKey || !viewport.value) return;
  event.preventDefault();
  const rect = viewport.value.getBoundingClientRect();
  void changeZoom(zoom.value * Math.exp(-event.deltaY * .003), { x: event.clientX - rect.left, y: event.clientY - rect.top });
}
function startDrag(event: PointerEvent) {
  if (mode.value !== 'map' || event.pointerType !== 'mouse' || event.button !== 0 || !viewport.value || (event.target as Element).closest('button, input, a')) return;
  event.preventDefault();
  drag = { pointer: event.pointerId, x: event.clientX, y: event.clientY, left: viewport.value.scrollLeft, top: viewport.value.scrollTop };
  dragging.value = true;
  viewport.value.setPointerCapture(event.pointerId);
  viewport.value.focus({ preventScroll: true });
}
function moveDrag(event: PointerEvent) { if (!drag || drag.pointer !== event.pointerId || !viewport.value) return; viewport.value.scrollLeft = drag.left - (event.clientX - drag.x); viewport.value.scrollTop = drag.top - (event.clientY - drag.y); }
function endDrag() { drag = undefined; dragging.value = false; }
async function locateNode(node: NodeEntry) {
  if (node.branch !== undefined && collapsed.value.has(node.branch)) { const next = new Set(collapsed.value); next.delete(node.branch); collapsed.value = next; }
  selectNode(node);
  await nextTick();
  measureWorld();
  // Apply the expanded world's scroll bounds before positioning the match.
  await nextTick();
  const target = world.value?.querySelector<HTMLElement>(`[data-node-path="${node.path}"]`);
  const view = viewport.value;
  if (!target || !view) return;
  const targetRect = target.getBoundingClientRect();
  const viewRect = view.getBoundingClientRect();
  view.scrollTo({ left: view.scrollLeft + targetRect.left - viewRect.left - (view.clientWidth - targetRect.width) / 2, top: view.scrollTop + targetRect.top - viewRect.top - (view.clientHeight - targetRect.height) / 2 });
}
function navigateMatch(direction: number) { if (!matches.value.length) return; matchIndex.value = (matchIndex.value + direction + matches.value.length) % matches.value.length; void locateNode(matches.value[matchIndex.value]!); }
async function toggleFullscreen() {
  if (!fullscreen.value) {
    focusBeforeFullscreen = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    previousBodyOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    fullscreen.value = true;
    await nextTick();
    fullscreenButton.value?.focus({ preventScroll: true });
  } else {
    fullscreen.value = false;
    document.body.style.overflow = previousBodyOverflow;
    await nextTick();
    if (focusBeforeFullscreen?.isConnected) focusBeforeFullscreen.focus({ preventScroll: true });
  }
  scheduleMeasure();
}
function onDocumentKeydown(event: KeyboardEvent) {
  if (!fullscreen.value || props.sourceDialogOpen) return;
  if (event.key === 'Escape') { event.preventDefault(); void toggleFullscreen(); return; }
  if (event.key !== 'Tab' || !panel.value) return;
  const focusable = Array.from(panel.value.querySelectorAll<HTMLElement>('button:not([disabled]), input:not([disabled]), a[href], [tabindex="0"]')).filter(element => element.getClientRects().length > 0);
  const first = focusable[0]; const last = focusable.at(-1);
  if (!first || !last) return;
  if (event.shiftKey && (document.activeElement === first || !panel.value.contains(document.activeElement))) { event.preventDefault(); last.focus(); }
  else if (!event.shiftKey && (document.activeElement === last || !panel.value.contains(document.activeElement))) { event.preventDefault(); first.focus(); }
}
watch(query, () => { matchIndex.value = 0; const match = matches.value[0]; if (match) void locateNode(match); });
watch([() => props.selectedTitle, () => props.selectedNodePath], ([title, path]) => {
  if (!title) { selectedPath.value = ''; return; }
  const node = path ? nodes.value.find(node => node.path === path) : nodes.value.find(node => node.title === title);
  if (node && node.path !== selectedPath.value) void locateNode(node);
}, { immediate: true });
watch(() => props.map, () => { collapsed.value = new Set(); selectedPath.value = ''; query.value = ''; zoom.value = 1; void showRoot(); });
watch([collapsed, mode, () => props.examConcepts, () => props.managementConcepts], () => { void nextTick(scheduleMeasure); });
watch(world, (element, previous) => { if (previous) resizeObserver?.unobserve(previous); if (element) resizeObserver?.observe(element); });
onMounted(() => { resizeObserver = new ResizeObserver(scheduleMeasure); if (world.value) resizeObserver.observe(world.value); if (viewport.value) resizeObserver.observe(viewport.value); document.addEventListener('keydown', onDocumentKeydown); void showRoot(); });
onBeforeUnmount(() => { cancelAnimationFrame(measureFrame); resizeObserver?.disconnect(); document.removeEventListener('keydown', onDocumentKeydown); if (fullscreen.value) document.body.style.overflow = previousBodyOverflow; });
</script>

<style scoped>
.mind-map { min-width: 0; color: rgb(var(--c-ink)); border: 1px solid rgb(var(--c-line)); border-radius: .85rem; background: rgb(var(--c-surface)); overflow: hidden; container-type: inline-size; }
.is-fullscreen { position: fixed; inset: 0; z-index: 40; border: 0; border-radius: 0; display: flex; flex-direction: column; height: 100dvh; padding: max(.75rem, env(safe-area-inset-top)) max(.75rem, env(safe-area-inset-right)) max(.75rem, env(safe-area-inset-bottom)) max(.75rem, env(safe-area-inset-left)); }
.map-toolbar { display: flex; flex-wrap: wrap; align-items: center; gap: .5rem; padding: .75rem; border-bottom: 1px solid rgb(var(--c-line)); background: rgb(var(--c-raised) / .45); }
.view-switch { display: flex; padding: .15rem; border: 1px solid rgb(var(--c-line)); border-radius: .55rem; background: rgb(var(--c-surface)); }
.view-switch button, .tool-button, .icon-button { display: inline-flex; align-items: center; justify-content: center; gap: .35rem; flex-shrink: 0; min-height: 2rem; padding: .3rem .5rem; border-radius: .4rem; color: rgb(var(--c-sub)); font-size: .75rem; line-height: 1.35; }
.view-switch button[aria-pressed='true'] { background: rgb(var(--c-accent) / .1); color: rgb(var(--c-accent)); font-weight: 600; }
.tool-button:hover, .icon-button:hover, .view-switch button:hover { background: rgb(var(--c-line) / .5); }
button:disabled { opacity: .4; cursor: not-allowed; }
button:focus-visible, input:focus-visible, .map-viewport:focus-visible { outline: 2px solid rgb(var(--c-accent)); outline-offset: -2px; }
.fullscreen-button { margin-left: auto; }
.map-search { display: flex; align-items: center; gap: .4rem; border: 1px solid rgb(var(--c-line-strong)); border-radius: .5rem; padding: 0 .5rem; min-width: 120px; flex: 1 1 160px; background: rgb(var(--c-surface)); color: rgb(var(--c-faint)); }
.map-search input { background: transparent; color: rgb(var(--c-ink)); min-width: 0; width: 100%; height: 2rem; font-size: .8rem; }
.map-search input::-webkit-search-cancel-button { display: none; }
.map-search .icon-button { padding: 0; width: 1.5rem; }
.search-navigation { display: flex; align-items: center; font-size: .7rem; color: rgb(var(--c-sub)); white-space: nowrap; }
.search-navigation .icon-button { padding: .2rem; }
.canvas-tools { display: flex; align-items: center; gap: .1rem; flex-wrap: wrap; }
.zoom-value { min-width: 3rem; font-variant-numeric: tabular-nums; }
.map-body { min-width: 0; min-height: 0; display: grid; }
.map-main { min-width: 0; min-height: 0; display: flex; flex-direction: column; }
.map-viewport { height: 400px; min-height: 200px; overflow: auto; overscroll-behavior: contain; background-color: rgb(var(--c-raised) / .3); background-image: radial-gradient(rgb(var(--c-line-strong) / .7) .8px, transparent .8px); background-size: 18px 18px; cursor: grab; }
.map-viewport.is-dragging { cursor: grabbing; user-select: none; }
.map-spacer { position: relative; min-width: 100%; overflow: hidden; }
.map-world { display: flex; align-items: center; gap: 56px; padding: 36px; width: max-content; position: relative; transform-origin: top left; }
.map-connections { position: absolute; inset: 0; pointer-events: none; overflow: visible; }
.map-connections path { fill: none; stroke: rgb(var(--c-accent) / .35); stroke-width: 1.6; }
.branch-list, .leaf-list { display: flex; flex-direction: column; list-style: none; margin: 0; padding: 0; position: relative; }
.branch-list { gap: 26px; }
.leaf-list { gap: 10px; }
.branch-row { display: flex; align-items: center; gap: 56px; }
.node-group { display: flex; align-items: center; position: relative; flex-shrink: 0; }
.root-group { width: 200px; }
.branch-group, .leaf-group { width: 230px; }
.map-node { display: block; width: 100%; min-width: 0; border: 1px solid rgb(var(--c-line-strong)); border-radius: .7rem; padding: .75rem .85rem; background: rgb(var(--c-surface)); text-align: left; color: rgb(var(--c-sub)); font-size: .85rem; line-height: 1.6; overflow-wrap: anywhere; cursor: pointer; box-shadow: 0 2px 5px rgb(var(--c-sink) / .035); transition: border-color .15s, background-color .15s; }
.map-node:hover { border-color: rgb(var(--c-accent) / .65); color: rgb(var(--c-ink)); }
.root-node { border: 2px solid rgb(var(--c-accent) / .65); color: rgb(var(--c-accent)); font-weight: 700; font-size: .95rem; }
.branch-node { border-left: 3px solid rgb(var(--c-accent) / .65); font-weight: 600; color: rgb(var(--c-ink)); }
.map-node.is-match { border-color: rgb(var(--c-warn) / .8); background: rgb(var(--c-warn) / .08); }
.map-node.is-selected { border-color: rgb(var(--c-accent)); background: rgb(var(--c-accent) / .09); box-shadow: 0 0 0 2px rgb(var(--c-accent) / .2); }
.map-node.is-current-match { outline: 2px solid rgb(var(--c-warn) / .7); outline-offset: 3px; }
.branch-toggle { display: flex; align-items: center; justify-content: center; flex-direction: column; flex-shrink: 0; width: 27px; min-height: 32px; margin-left: 5px; border: 1px solid rgb(var(--c-line-strong)); border-radius: .45rem; background: rgb(var(--c-surface)); color: rgb(var(--c-sub)); }
.branch-toggle:hover { border-color: rgb(var(--c-accent)); color: rgb(var(--c-accent)); }
.collapsed-count { font-size: .6rem; }
.node-badges { display: flex; gap: .3rem; flex-wrap: wrap; margin-top: .35rem; font-weight: 400; font-size: .65rem; line-height: 1.5; }
.node-badges > span { padding: .05rem .3rem; border-radius: .25rem; }
.exam-badge { color: rgb(var(--c-warn)); background: rgb(var(--c-warn) / .1); }
.management-badge { color: rgb(var(--c-accent)); background: rgb(var(--c-accent) / .08); }
.map-help { padding: .6rem .75rem; color: rgb(var(--c-faint)); font-size: .7rem; border-top: 1px solid rgb(var(--c-line)); }
.map-detail { padding: 1rem; border-top: 1px solid rgb(var(--c-line)); min-width: 0; overflow-y: auto; max-height: 300px; overflow-wrap: anywhere; }
.detail-empty { display: flex; align-items: center; justify-content: center; gap: .4rem; flex-wrap: wrap; text-align: center; padding: .5rem; color: rgb(var(--c-faint)); font-size: .8rem; }
.detail-empty p { color: rgb(var(--c-sub)); font-weight: 600; }
.detail-empty span { flex-basis: 100%; font-size: .75rem; }
.outline-viewport { cursor: auto; background: transparent; }
.outline-world { display: block; width: 100%; padding: 1rem; }
.outline-world .root-group, .outline-world .branch-group, .outline-world .leaf-group { width: 100%; }
.outline-world .branch-list { padding: 1rem 0 0 .8rem; margin-left: .4rem; gap: .8rem; border-left: 1px solid rgb(var(--c-line-strong)); }
.outline-world .branch-row { display: block; }
.outline-world .leaf-list { padding: .6rem 0 0 .8rem; margin-left: .5rem; border-left: 1px solid rgb(var(--c-line-strong)); gap: .5rem; }
.outline-world .map-node { padding: .65rem .8rem; }
.is-fullscreen .map-body { flex: 1; overflow: hidden; grid-template-rows: minmax(200px, 1fr) auto; }
.is-fullscreen .map-viewport { height: auto; flex: 1; }
.is-fullscreen .map-detail { max-height: 32dvh; }
@container (min-width: 900px) {
  .map-body.has-detail { grid-template-columns: minmax(0, 1fr) 300px; }
  .is-fullscreen .map-body { grid-template-rows: minmax(0, 1fr); }
  .map-detail { border-top: 0; border-left: 1px solid rgb(var(--c-line)); max-height: 440px; }
  .detail-empty { min-height: 200px; align-content: center; }
  .is-fullscreen .map-detail { max-height: none; }
}
@media (max-width: 640px) {
  .map-toolbar { padding: .55rem; gap: .4rem; }
  .map-search { order: 2; flex-basis: 160px; }
  .search-navigation { order: 2; }
  .canvas-tools { order: 3; width: 100%; }
  .map-viewport { height: 360px; }
  .map-help { font-size: .65rem; }
  .map-detail { padding: .8rem; }
  .is-fullscreen .map-body { grid-template-rows: minmax(150px, 1fr) auto; }
}
@media (prefers-reduced-motion: reduce) { .map-node { transition: none; } }
</style>
