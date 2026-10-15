<script setup lang="ts">
import { computed, ref } from 'vue'
import type { CVView, PreparedImage } from '../experience'
const props = defineProps<{ source: PreparedImage; result: CVView; minimum: number }>()
const showBoxes = ref(true), threshold = ref(props.minimum), selected = ref<number | null>(null)
const visible = computed(() => props.result.boxes.filter(box => box.score >= threshold.value))
const fontSize = computed(() => Math.max(12, props.source.width / 45))
const colors = ['#00775a', '#285ecc', '#9b3b96', '#ad5800', '#007e9a']
</script>
<template>
  <div class="detection-preview">
    <div class="detection-tools"><label><input v-model="showBoxes" type="checkbox" />显示检测框</label><label>显示阈值 <input v-model.number="threshold" type="range" :min="minimum" max="1" step="0.01" aria-label="检测结果显示阈值" />{{ threshold.toFixed(2) }}</label><span>{{ visible.length }} 个目标</span></div>
    <p class="muted">仅筛选本次接口已返回的结果。降低请求阈值以找回未返回目标，需要重新发送请求。</p>
    <div class="detection-layout">
      <div class="image-stage" :style="{ aspectRatio: `${source.width} / ${source.height}` }">
        <img :src="source.url" alt="本次检测图片" />
        <svg v-if="showBoxes && result.hasGeometry" :viewBox="`0 0 ${source.width} ${source.height}`" aria-label="模型返回的检测框叠加图" role="img">
          <g v-for="box in visible" :key="box.id" :data-detection-id="box.id" :class="{ active: selected === box.id }" :style="{ color: colors[(box.id - 1) % colors.length] }" @mouseenter="selected = box.id" @mouseleave="selected = null">
            <title>{{ box.id }} · {{ box.label }} · {{ (box.score * 100).toFixed(1) }}%</title>
            <rect :x="box.box[0]" :y="box.box[1]" :width="box.box[2]! - box.box[0]!" :height="box.box[3]! - box.box[1]!" vector-effect="non-scaling-stroke" />
            <text :x="Math.min(box.box[0]! + 3, Math.max(0, source.width - fontSize * 6))" :y="Math.max(fontSize, box.box[1]! - 4)" :font-size="fontSize">#{{ box.id }} {{ (box.score * 100).toFixed(0) }}%</text>
            <circle v-for="(point, i) in box.landmarks" :key="i" :cx="point[0]" :cy="point[1]" :r="source.width / 180" />
          </g>
        </svg>
      </div>
      <div class="detection-list" aria-label="检测结果列表">
        <button v-for="box in visible" :key="box.id" :class="{ active: selected === box.id }" :data-result-id="box.id" @mouseenter="selected = box.id" @mouseleave="selected = null" @focus="selected = box.id" @blur="selected = null" @click="selected = box.id">
          <strong><i :style="{ background: colors[(box.id - 1) % colors.length] }"></i>#{{ box.id }} {{ box.label }}</strong><span>{{ (box.score * 100).toFixed(1) }}%</span><small>{{ box.box.map(value => value.toFixed(1)).join(', ') }}</small><small v-if="box.angle !== undefined">角度 {{ box.angle.toFixed(3) }} rad</small>
        </button>
        <p v-if="result.hasGeometry && !visible.length" class="muted">本次阈值下未检出目标，不代表图片中一定没有目标。</p>
        <p v-if="result.dimension" class="muted">返回 {{ result.dimension }} 维特征向量，此模型不返回检测框。</p>
        <div v-for="row in result.classifications" :key="row.class_id" class="class-row">类别 #{{ row.class_id }} <strong>{{ row.score.toFixed(4) }}</strong></div>
        <p v-if="result.classifications.length" class="muted">原始分类分数，不强行换算成概率。类别编号以模型标签表为准。</p>
      </div>
    </div>
    <p v-for="warning in result.warnings" :key="warning" class="visual-warning" role="status">{{ warning }}</p>
  </div>
</template>
<style scoped>
.detection-tools { display:flex; gap:16px; flex-wrap:wrap; align-items:center; font-size:12px; }.detection-tools label { display:flex; gap:7px; align-items:center; }.detection-tools input[type=range] { width:100px; }.detection-layout { display:grid; grid-template-columns:minmax(0,1fr) 210px; gap:16px; align-items:start; }.image-stage { position:relative; width:100%; background:#e9efed; border:1px solid #d7e3de; overflow:hidden; }.image-stage img,.image-stage svg { position:absolute; inset:0; width:100%; height:100%; }.image-stage img { object-fit:contain; }.image-stage rect { stroke:currentColor; stroke-width:2; fill:transparent; }.image-stage text { fill:white; stroke:#10242b; stroke-width:3px; paint-order:stroke; font-weight:700; }.image-stage circle { fill:currentColor; }.image-stage g.active rect { fill:currentColor; fill-opacity:.12; stroke-width:4; }.detection-list { max-height:440px; overflow:auto; }.detection-list button { display:grid; grid-template-columns:1fr auto; width:100%; gap:7px; padding:12px; background:white; text-align:left; border:1px solid #dce5e0; margin-bottom:8px; border-radius:8px; font-size:12px; }.detection-list button.active { background:#ecf7f1; border-color:#00775a; }.detection-list small { grid-column:1/-1; color:#657972; overflow-wrap:anywhere; }.detection-list i { display:inline-block; width:8px; height:8px; border-radius:50%; margin-right:6px; }.muted { font-size:12px; line-height:1.6; color:#687b75; }.visual-warning { padding:10px; border-radius:6px; background:#fff5e5; color:#925610; font-size:12px; }.class-row { display:flex; justify-content:space-between; padding:10px; border-bottom:1px solid #e3e9e6; font-size:13px; }@media(max-width:1050px) { .detection-layout { grid-template-columns:1fr; } .detection-list { max-height:240px; } }
</style>
