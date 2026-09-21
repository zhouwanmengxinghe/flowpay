// 展示约定保持在页面组件之外，避免各区域重复定义。
export const time = (timestamp: string) =>
  new Date(timestamp).toLocaleTimeString('zh-CN', { hour12: false })
export type Inspect = (title: string, data: unknown) => void
