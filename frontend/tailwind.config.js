/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{vue,js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        // 主色：靛青（书卷气），作为交互与品牌统一色
        brand: {
          50: '#eef2ff',
          100: '#e0e7ff',
          200: '#c7d2fe',
          300: '#a5b4fc',
          400: '#818cf8',
          500: '#6366f1',
          600: '#4f46e5',
          700: '#4338ca',
          800: '#3730a3',
          900: '#312e81',
          950: '#1e1b4b',
        }
      },
      fontFamily: {
        // 页面标题：宋体系衬线，营造书卷感；正文仍用系统黑体保证可读性
        display: ['"Songti SC"', 'STSong', '"Noto Serif SC"', '"Source Han Serif SC"', 'serif'],
      },
      boxShadow: {
        // shadow-xs 在 Tailwind v3 中默认不存在，补全并统一卡片层级
        xs: '0 1px 2px 0 rgb(28 25 23 / 0.04)',
        card: '0 1px 2px rgb(28 25 23 / 0.04), 0 6px 20px -6px rgb(28 25 23 / 0.07)',
        lift: '0 4px 12px -2px rgb(28 25 23 / 0.08), 0 16px 36px -10px rgb(28 25 23 / 0.14)',
        pop: '0 12px 40px -8px rgb(28 25 23 / 0.18)',
      },
    },
  },
  plugins: [],
}
