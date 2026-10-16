<template>
  <div class="answer-text reading-content">
    <template v-for="(block, index) in blocks" :key="index">
      <ol v-if="block.kind === 'ordered'" :start="block.start">
        <li v-for="(item, itemIndex) in block.items" :key="itemIndex"><InlineText :text="item" /></li>
      </ol>
      <ul v-else-if="block.kind === 'unordered'">
        <li v-for="(item, itemIndex) in block.items" :key="itemIndex"><InlineText :text="item" /></li>
      </ul>
      <component v-else-if="'text' in block" :is="block.kind === 'heading' ? `h${Math.min((block.level || 1) + 3, 6)}` : block.kind === 'quote' ? 'blockquote' : 'p'" :class="{ 'answer-subheading': block.kind === 'heading' }">
        <InlineText :text="block.text" />
      </component>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, defineComponent, h } from 'vue';

const props = defineProps<{ content: string }>();
type TextBlock = { kind: 'paragraph' | 'heading' | 'quote'; text: string; level?: number };
type ListBlock = { kind: 'ordered' | 'unordered'; items: string[]; start: number };
type Block = TextBlock | ListBlock;

// Render a small text-only Markdown subset. Model output is never treated as HTML.
const InlineText = defineComponent({
  props: { text: { type: String, required: true } },
  setup: props => () => props.text.split(/(\*\*[^*\n]+\*\*)/g).map(part =>
    /^\*\*[^*\n]+\*\*$/.test(part) ? h('strong', part.slice(2, -2)) : part),
});

function paragraphBlocks(text: string): Block[] {
  // Older answers often put definitions after a colon in one line: “有：(1)…(2)…”.
  // Only split a complete sequence starting at 1, leaving years and other parentheses alone.
  const markers = [...text.matchAll(/[（(](\d{1,2})[)）]\s*/g)];
  const first = markers[0];
  if (!first || markers.length < 2 || !markers.every((match, index) => Number(match[1]) === index + 1)) return [{ kind: 'paragraph', text }];
  const prefix = text.slice(0, first.index).trimEnd();
  if (prefix.trim() && !/[：:]$/.test(prefix)) return [{ kind: 'paragraph', text }];
  const items = markers.map((match, index) => text.slice(match.index! + match[0].length, markers[index + 1]?.index ?? text.length).trim());
  if (items.some(item => !item || /^[+−*/=<>×÷-]/.test(item))) return [{ kind: 'paragraph', text }];
  return [...(prefix ? [{ kind: 'paragraph' as const, text: prefix }] : []), { kind: 'ordered', start: 1, items }];
}

const blocks = computed(() => {
  const result: Block[] = [];
  let paragraph: string[] = [];
  let list: ListBlock | undefined;
  const flush = () => {
    if (paragraph.length) result.push(...paragraphBlocks(paragraph.join('\n')));
    paragraph = [];
  };
  for (const line of props.content.replace(/\r\n?/g, '\n').split('\n')) {
    if (!line.trim()) { flush(); list = undefined; continue; }
    const heading = line.match(/^\s{0,3}(#{1,6})\s+(.+?)\s*#*$/);
    const quote = line.match(/^\s{0,3}>\s?(.*)$/);
    const ordered = line.match(/^\s*(\d{1,3})[.、)]\s+(.+)$/);
    const unordered = line.match(/^\s*[-*+]\s+(.+)$/);
    if (heading || quote) {
      flush(); list = undefined;
      if (heading) result.push({ kind: 'heading', text: heading[2]!, level: heading[1]!.length });
      else result.push({ kind: 'quote', text: quote![1]! });
    } else if (ordered || unordered) {
      flush();
      const kind = ordered ? 'ordered' : 'unordered';
      const start = ordered ? Number(ordered[1]) : 1;
      const text = ordered ? ordered[2]! : unordered![1]!;
      if (!list || list.kind !== kind || (ordered && start !== list.start + list.items.length)) {
        list = { kind, start, items: [] };
        result.push(list);
      }
      list.items.push(text);
    } else {
      list = undefined;
      paragraph.push(line);
    }
  }
  flush();
  return result;
});
</script>

<style scoped>
.answer-text { max-width: 42rem; font-size: 1rem; line-height: 1.9; overflow-wrap: anywhere; }
.answer-text > * + * { margin-top: 1em; }
.answer-text p, .answer-text li, .answer-text blockquote { white-space: pre-wrap; }
.answer-text .answer-subheading { font-size: 1.05em; font-weight: 650; line-height: 1.6; margin-top: 1.5em; }
.answer-text > :first-child { margin-top: 0; }
.answer-text :deep(strong) { font-weight: 650; }
.answer-text ol { list-style-type: decimal; }
.answer-text ul { list-style-type: disc; }
.answer-text ol, .answer-text ul { padding-left: 1.65em; }
.answer-text li { padding-left: .2em; }
.answer-text li + li { margin-top: .65em; }
.answer-text li::marker { color: rgb(var(--c-sub)); font-weight: 600; }
.answer-text blockquote { padding: .65em 1em; border-left: 3px solid rgb(var(--c-line-strong)); background: rgb(var(--c-raised) / .5); color: rgb(var(--c-sub)); border-radius: 0 .5rem .5rem 0; }
</style>
